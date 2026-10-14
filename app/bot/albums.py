"""Collecting Telegram albums into one input.

Telegram delivers every photo / video of an album as a separate message sharing a
`media_group_id`. Updates of one user are processed one at a time (SimpleEventIsolation), so a
handler must never wait for the rest of the album itself — the next item could not even start.
Instead the handler only buffers the message (`AlbumCollector.add`) and returns at once; a
background task per group fires `delay` seconds after the last item arrived and hands the whole
album, ordered by message id, to the callback. The callback runs under the same per-user
isolation lock as handlers, so it never interleaves with another update of that user.

A straggler that arrives after its group was already handed over starts a new group flagged
`late=True` (the composer then appends it instead of replacing the album).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram.types import Message

log = logging.getLogger(__name__)

ALBUM_DELAY = 1.2  # seconds of silence after the last album item before the album is handled
RECENT_TTL = 120.0  # how long a handed-over group is remembered (stragglers, dedupe)
SHUTDOWN_TIMEOUT = 10.0

AlbumCallback = Callable[[list[Message], bool], Awaitable[Any]]


@dataclass
class _Group:
    callback: AlbumCallback
    lock_key: Any
    late: bool
    messages: list[Message] = field(default_factory=list)
    last: float = 0.0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    flush_now: bool = False


class AlbumCollector:
    """Buffers album items per (chat, media_group_id) and hands each album over once."""

    def __init__(self, delay: float = ALBUM_DELAY, *, isolation: Any = None) -> None:
        self.delay = delay
        self.isolation = isolation  # aiogram BaseEventIsolation (lock(key) context manager) or None
        self._groups: dict[tuple[int, str], _Group] = {}
        self._recent: dict[tuple[int, str], float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    @staticmethod
    def _key(message: Message) -> tuple[int, str]:
        return message.chat.id, str(message.media_group_id)

    def add(self, message: Message, callback: AlbumCallback, *, lock_key: Any = None) -> bool:
        """Buffer an album item. Returns True when it opened a new group (the callback of the
        first item is the one that will be called)."""
        key = self._key(message)
        group = self._groups.get(key)
        if group is not None:
            group.messages.append(message)
            group.last = time.monotonic()
            if self._closing:
                group.flush_now = True
                group.wake.set()
            return False
        self._forget_old()
        group = _Group(callback=callback, lock_key=lock_key, late=key in self._recent,
                       messages=[message], last=time.monotonic(), flush_now=self._closing)
        self._groups[key] = group
        task = asyncio.create_task(self._run(key, group), name=f"album-{key[0]}-{key[1]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    def seen(self, message: Message) -> bool:
        """For handlers that only answer once per album: True for the 2nd..nth item of a group
        (the first call for a group marks it as seen)."""
        self._forget_old()
        key = self._key(message)
        if key in self._recent or key in self._groups:
            return True
        self._recent[key] = time.monotonic()
        return False

    def _forget_old(self) -> None:
        limit = time.monotonic() - RECENT_TTL
        for key in [k for k, ts in self._recent.items() if ts < limit]:
            del self._recent[key]

    async def _run(self, key: tuple[int, str], group: _Group) -> None:
        try:
            while not group.flush_now:
                remaining = group.last + self.delay - time.monotonic()
                if remaining <= 0:
                    break
                group.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(group.wake.wait(), remaining)
        finally:
            if self._groups.get(key) is group:
                del self._groups[key]
            self._recent[key] = time.monotonic()
        messages = sorted(group.messages, key=lambda m: m.message_id)
        try:
            if self.isolation is not None and group.lock_key is not None:
                async with self.isolation.lock(key=group.lock_key):
                    await group.callback(messages, group.late)
            else:
                await group.callback(messages, group.late)
        except Exception:
            log.exception("Album %s in chat %s could not be handled", key[1], key[0])

    async def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until every buffered album was handed over. False when `timeout` passed first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._tasks:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            await asyncio.wait(set(self._tasks), timeout=left)
        return True

    async def close(self) -> None:
        """Shutdown: hand over buffered albums right away and wait for them (bounded)."""
        self._closing = True
        for group in list(self._groups.values()):
            group.flush_now = True
            group.wake.set()
        if not await self.wait_idle(SHUTDOWN_TIMEOUT):
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

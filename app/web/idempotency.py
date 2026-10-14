"""Retries of the requests that publish never publish twice (``X-Idempotency-Key``).

Publishing runs inside the HTTP request and may take longer than a reverse proxy waits (nginx:
60 s by default), or the connection may drop; the server finishes the work anyway. Without a key
a retry of «Опубликовать» for a post the panel has not saved yet would create and publish a
second post. So the panel sends a random key with

    POST /api/posts                  create (and publish / schedule / queue)
    PUT  /api/posts/{id}             save (and publish / schedule / queue / apply)
    POST /api/posts/{id}/publish     publish now

and sends the same key again when it retries the same action without having got an answer from
the server (network error, 502/503/504 from the proxy). A new action gets a new key.

The server remembers each key — per method and path, in memory, for IDEMPOTENCY_TTL seconds —
with the answer of the first request:

* the first request is still running → the retry waits for it up to IDEMPOTENCY_WAIT seconds
  and replays its answer; if it is still running then → 409 ``{"error", "post", "replayed": true}``
  with the post's current state (null when the post is not created yet);
* it finished → its answer is replayed (same status and body, ``post`` refreshed to the post's
  current state when the post still exists, plus ``"replayed": true``) — nothing runs again;
* it changed nothing (a 4xx answer without ``post``: invalid input, a new draft rolled back; or a
  failure before a new post was created) → the key is forgotten and a retry runs normally.

A key is 8–64 characters ``[A-Za-z0-9_-]`` (e.g. ``crypto.randomUUID()``), anything else → 400.
Requests without the header work as before. The keys are lost on restart: a publish interrupted
by a restart is marked as interrupted and never sent again automatically, and an existing post
is protected anyway (409 while it is being sent; a sent post needs ``apply`` / is refused).
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiohttp import web

from app.media import MediaError
from app.services.folders import FolderError
from app.services.plan import PlanError
from app.services.posting import PostError
from app.web.auth import JsonError, json_error
from app.web.keys import IDEMPOTENCY_KEY

log = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
IDEMPOTENCY_TTL = 15 * 60  # seconds an answer is remembered
IDEMPOTENCY_WAIT = 20.0  # seconds a retry waits for the first request (below a proxy's 60 s)
MAX_ENTRIES = 1000  # finished entries beyond this are forgotten, oldest first

KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
KEY_ERROR = f"Некорректный заголовок {IDEMPOTENCY_HEADER}: ожидается 8–64 символов A-Z, a-z, 0-9, _ или -"
IN_PROGRESS_ERROR = "Предыдущий такой запрос ещё выполняется — подождите и обновите страницу"
SENDING_ERROR = "Пост сейчас публикуется — подождите и обновите страницу"
INTERNAL_ERROR = "Внутренняя ошибка сервера"
INTERRUPTED_ERROR = "Запрос был прерван — проверьте, что стало с постом"
# Service errors become 4xx answers in app.web.api.error_middleware: nothing was changed.
_CLIENT_ERRORS = (PlanError, FolderError, MediaError, PostError)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
# async (request, post_id) -> the serialized post (as in API answers), or None when it is gone.
CurrentPost = Callable[[web.Request, int], Awaitable[dict | None]]


@dataclass
class Entry:
    """One key: the first request's post (when known) and, once it finished, its answer."""

    post_id: int | None = None
    status: int | None = None
    body: dict | None = None
    finished_at: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


# Request storage: the entry of the request being handled (see `created`).
_ENTRY: web.RequestKey[Entry] = web.RequestKey("pcb_idempotency_entry", Entry)


class IdempotencyStore:
    """In-memory map (method, path, key) → Entry. Finished entries expire after `ttl` seconds."""

    def __init__(self, ttl: float = IDEMPOTENCY_TTL, max_entries: int = MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[tuple[str, str, str], Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, scope: tuple[str, str, str]) -> Entry | None:
        entry = self._entries.get(scope)
        if entry is not None and entry.finished_at is not None and self._clock() - entry.finished_at > self.ttl:
            del self._entries[scope]
            return None
        return entry

    def start(self, scope: tuple[str, str, str], post_id: int | None) -> Entry:
        self._prune()
        entry = Entry(post_id=post_id)
        self._entries[scope] = entry
        return entry

    def finish(self, entry: Entry, status: int, body: dict | None) -> None:
        entry.status = status
        entry.body = body
        if entry.post_id is None and isinstance(body, dict):
            post = body.get("post")
            if isinstance(post, dict) and isinstance(post.get("id"), int):
                entry.post_id = post["id"]
        entry.finished_at = self._clock()

    def forget(self, scope: tuple[str, str, str], entry: Entry) -> None:
        if self._entries.get(scope) is entry:
            del self._entries[scope]

    def _prune(self) -> None:
        current = self._clock()
        for scope in [s for s, e in self._entries.items()
                      if e.finished_at is not None and current - e.finished_at > self.ttl]:
            del self._entries[scope]
        if len(self._entries) >= self.max_entries:
            finished = sorted((e.finished_at, s) for s, e in self._entries.items() if e.finished_at is not None)
            for _, scope in finished[: len(self._entries) - self.max_entries + 1]:
                del self._entries[scope]


def parse_key(request: web.Request) -> str | None:
    """The request's idempotency key; None without the header, 400 for a malformed one."""
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        return None
    key = raw.strip()
    if not KEY_RE.fullmatch(key):
        raise json_error(400, KEY_ERROR)
    return key


def created(request: web.Request, post_id: int) -> None:
    """Called by a handler right after it created a post: from now on a retry with the same key
    gets this post (never a second one), whatever happens next."""
    entry = request.get(_ENTRY)
    if isinstance(entry, Entry):
        entry.post_id = post_id


def _json_body(response: web.StreamResponse) -> dict | None:
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _path_post_id(request: web.Request) -> int | None:
    raw = request.match_info.get("id")
    return int(raw) if raw is not None and raw.isdigit() else None


async def _replay(request: web.Request, entry: Entry, current_post: CurrentPost) -> web.Response:
    body = dict(entry.body) if entry.body is not None else {}
    if entry.post_id is not None:
        post = await current_post(request, entry.post_id)
        if post is not None:
            body["post"] = post
    body["replayed"] = True
    log.info("Web: %s %s repeated with the same %s — answer replayed", request.method, request.path,
             IDEMPOTENCY_HEADER)
    return web.json_response(body, status=entry.status or 200)


async def _still_running(request: web.Request, entry: Entry, current_post: CurrentPost) -> web.Response:
    post = await current_post(request, entry.post_id) if entry.post_id is not None else None
    message = SENDING_ERROR if post is not None and post.get("status") == "sending" else IN_PROGRESS_ERROR
    return web.json_response({"error": message, "post": post, "replayed": True}, status=409)


def idempotent(handler: Handler, current_post: CurrentPost) -> Handler:
    """Wrap a route handler with the ``X-Idempotency-Key`` behaviour (module docstring).
    `current_post` serializes a post for replayed answers."""

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        key = parse_key(request)
        if key is None:
            return await handler(request)
        store = request.app[IDEMPOTENCY_KEY]
        scope = (request.method, request.path, key)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + IDEMPOTENCY_WAIT
        while (entry := store.get(scope)) is not None:
            if entry.done.is_set():
                return await _replay(request, entry, current_post)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return await _still_running(request, entry, current_post)
            try:
                await asyncio.wait_for(entry.done.wait(), remaining)
            except asyncio.TimeoutError:
                return await _still_running(request, entry, current_post)
            # Finished: replay it — or, when it left nothing behind, run this request.

        entry = store.start(scope, _path_post_id(request))
        request[_ENTRY] = entry
        keep = False
        try:
            response = await handler(request)
            store.finish(entry, response.status, _json_body(response))
            keep = True
            return response
        except JsonError as exc:
            body = _json_body(exc)
            # A 4xx without the post changed nothing; with the post (kept / refused for its
            # state) or a 5xx after the post was touched, a retry must see this answer.
            keep = bool(body and body.get("post")) or (exc.status >= 500 and entry.post_id is not None)
            if keep:
                store.finish(entry, exc.status, body)
            raise
        except web.HTTPException as exc:
            keep = exc.status >= 500 and entry.post_id is not None
            if keep:
                store.finish(entry, exc.status, {"error": INTERNAL_ERROR})
            raise
        except _CLIENT_ERRORS:
            raise
        except BaseException as exc:
            # Unexpected error / cancelled: the post may have been changed — never run it again.
            keep = entry.post_id is not None
            if keep:
                message = INTERRUPTED_ERROR if isinstance(exc, asyncio.CancelledError) else INTERNAL_ERROR
                store.finish(entry, 500, {"error": message})
            raise
        finally:
            if not keep:
                store.forget(scope, entry)
            entry.done.set()

    return wrapper

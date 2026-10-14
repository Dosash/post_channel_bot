"""Access control: the bot serves only its admins, in private chats.

`AdminMiddleware` is registered as an *outer* middleware for `message` and `callback_query`
updates. Channel updates (`my_chat_member`, `chat_member`, `channel_post`,
`message_reaction_count`) never pass through it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, User

log = logging.getLogger(__name__)

PRIVATE_TEXT = "🔒 Это приватный бот."

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def is_start_command(message: Message) -> bool:
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return False
    command = text.split(maxsplit=1)[0][1:].split("@", 1)[0]
    return command.lower() == "start"


class AdminMiddleware(BaseMiddleware):
    """Lets through admins only; bootstraps the owner on the very first /start.

    Sets `data["became_owner"] = True` for the update that made the user the owner.
    """

    def __init__(self) -> None:
        self._owner_lock = asyncio.Lock()
        # user_id → (username, full_name) last written to the DB, to avoid a write per message.
        self._profiles: dict[int, tuple[str | None, str | None]] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        db = data["db"]
        user: User | None = data.get("event_from_user")
        if isinstance(event, Message):
            return await self._on_message(handler, event, data, db, user)
        if isinstance(event, CallbackQuery):
            return await self._on_callback(handler, event, data, db, user)
        return await handler(event, data)

    async def _on_message(self, handler: Handler, message: Message, data: dict[str, Any],
                          db: Any, user: User | None) -> Any:
        if message.chat.type != "private" or user is None or user.is_bot:
            return None
        if await db.is_admin(user.id):
            await self._touch_profile(db, user)
            return await handler(message, data)
        if not is_start_command(message):
            return None
        if await self._try_become_owner(db, user):
            data["became_owner"] = True
            return await handler(message, data)
        log.info("Rejected /start from non-admin %s (@%s)", user.id, user.username)
        try:
            await message.answer(PRIVATE_TEXT)
        except TelegramAPIError:
            log.debug("Could not answer non-admin %s", user.id, exc_info=True)
        return None

    async def _on_callback(self, handler: Handler, callback: CallbackQuery, data: dict[str, Any],
                           db: Any, user: User | None) -> Any:
        if user is not None and await db.is_admin(user.id):
            return await handler(callback, data)
        try:
            await callback.answer("🔒", show_alert=False)
        except TelegramAPIError:
            log.debug("Could not answer callback of non-admin", exc_info=True)
        return None

    async def _try_become_owner(self, db: Any, user: User) -> bool:
        """The first user to /start a bot without admins becomes its owner (race-free)."""
        async with self._owner_lock:
            if await db.has_admins():
                return False
            await db.add_admin(user.id, user.username, user.full_name, is_owner=True)
        self._profiles[user.id] = (user.username, user.full_name)
        log.warning("User %s (@%s) became the owner of the bot", user.id, user.username)
        return True

    async def _touch_profile(self, db: Any, user: User) -> None:
        profile = (user.username, user.full_name)
        if self._profiles.get(user.id) == profile:
            return
        await db.update_admin_profile(user.id, user.username, user.full_name)
        self._profiles[user.id] = profile

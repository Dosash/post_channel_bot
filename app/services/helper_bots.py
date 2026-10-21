"""The owner's other bots («боты-помощники»): when this bot becomes admin of a channel, the notice
offers one-tap links that add each of them as an admin of a channel too.

A bot cannot add another bot to a channel itself: `promoteChatMember` only promotes existing
members, and bots are never plain members of a channel. So the bot hands out Telegram's
«add bot as channel admin» link (``t.me/<bot>?startchannel&admin=…``): the admin taps it, picks
the channel, and the rights below are already ticked.

The list is the `helper_bots` setting (JSON list of usernames without @), edited in the panel
(«Настройки» → «Мои боты») and in the bot (``/helperbots``).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

HELPER_BOTS_SETTING = "helper_bots"
MAX_HELPERS = 10
# Rights pre-ticked in Telegram's «add to channel» screen: what a publishing bot needs.
ADMIN_RIGHTS = ("post_messages", "edit_messages", "delete_messages")

_BOT_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,29}bot", re.IGNORECASE)
_TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[\s,;]+")

BAD_NAME_ERROR = "«{name}» — не юзернейм бота: он должен заканчиваться на «bot», например @my_news_bot"
TOO_MANY_ERROR = f"Не больше {MAX_HELPERS} ботов"
SELF_ERROR = "@{name} — это сам бот публикаций, его добавлять не нужно"


def normalize(raw: str) -> str:
    """'@Name_bot', 't.me/Name_bot', 'https://t.me/Name_bot?start=x' → 'Name_bot'; ValueError otherwise."""
    name = _TME_RE.sub("", (raw or "").strip()).lstrip("@").split("?")[0].strip("/")
    if not _BOT_USERNAME_RE.fullmatch(name):
        raise ValueError(BAD_NAME_ERROR.format(name=raw.strip()))
    return name


def parse_list(raw: str | list[str], own_username: str | None = None) -> list[str]:
    """Usernames separated by spaces / commas / new lines (or a list) → unique names in the given
    order. ValueError (Russian) on a bad name, on this bot itself or on too many names."""
    items = raw if isinstance(raw, list) else _SPLIT_RE.split(raw or "")
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError(BAD_NAME_ERROR.format(name=item))
        if not item.strip():
            continue
        name = normalize(item)
        if own_username and name.lower() == own_username.lower():
            raise ValueError(SELF_ERROR.format(name=name))
        if name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    if len(names) > MAX_HELPERS:
        raise ValueError(TOO_MANY_ERROR)
    return names


async def get_helpers(db: Any) -> list[str]:
    raw = await db.get_setting(HELPER_BOTS_SETTING)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


async def set_helpers(db: Any, names: list[str]) -> list[str]:
    await db.set_setting(HELPER_BOTS_SETTING, json.dumps(names) if names else None)
    return names


def add_link(username: str) -> str:
    """Telegram's «add this bot as an admin of a channel» link with the publishing rights ticked."""
    return f"https://t.me/{username}?startchannel&admin={'+'.join(ADMIN_RIGHTS)}"


async def admin_usernames(bot: Any, chat_id: int) -> set[str] | None:
    """Lower-cased usernames of the chat's admins; None when Telegram did not tell."""
    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
    except (TelegramAPIError, OSError, TimeoutError):
        log.warning("Could not get the admins of %s", chat_id, exc_info=True)
        return None
    return {m.user.username.lower() for m in admins if getattr(m.user, "username", None)}


async def helpers_status(bot: Any, db: Any, chat_id: int) -> list[dict]:
    """Every helper bot: {username, is_admin (True / False / None — unknown), url}."""
    helpers = await get_helpers(db)
    if not helpers:
        return []
    admins = await admin_usernames(bot, chat_id)
    return [{"username": name, "is_admin": None if admins is None else name.lower() in admins,
             "url": add_link(name)} for name in helpers]


async def missing_helpers(bot: Any, db: Any, chat_id: int) -> list[str]:
    """Helper bots that are not (or not known to be) admins of the chat."""
    return [h["username"] for h in await helpers_status(bot, db, chat_id) if not h["is_admin"]]

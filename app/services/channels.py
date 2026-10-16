"""Channel management: parsing references, checking the bot's rights, adding / refreshing channels,
member-count snapshots and reacting to the bot's own membership changes (`my_chat_member`).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)

from app.db import CH_ACTIVE, CH_IGNORED, CH_LEFT, CH_PENDING, now
from app.services import avatars

log = logging.getLogger(__name__)

NO_RIGHTS = {"is_admin": False, "can_post": False, "can_edit": False, "can_delete": False}
ALL_RIGHTS = {"is_admin": True, "can_post": True, "can_edit": True, "can_delete": True}

ADMIN_STATUSES = ("administrator", "creator")

NOT_FOUND_ERROR = "Канал не найден. Проверьте @username или ссылку."
NOT_ADMIN_ERROR = (
    "Бот не является администратором этого канала. "
    "Добавьте его в администраторы с правом публикации сообщений."
)
NO_POST_RIGHT_ERROR = "У бота нет права публиковать сообщения в этом канале."
INVITE_LINK_ERROR = (
    "Ссылки-приглашения не подходят. Пришлите @username канала, его ID "
    "или перешлите сюда любой пост из канала."
)
BAD_REF_ERROR = "Не понял, что это за канал. Пришлите @username, ссылку t.me/… или ID канала."

_CHAT_TYPES_RU = {"private": "личный чат", "group": "группа", "supergroup": "супергруппа"}
_TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?P<path>.*)$", re.IGNORECASE)
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_CHAT_ID_RE = re.compile(r"-?\d+")

# Errors meaning "Telegram answered: no such chat / no access", as opposed to transport failures.
_ACCESS_ERRORS = (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound)


class ChannelError(Exception):
    """Channel cannot be added / used. The message is Russian and user-presentable."""


# ------------------------------------------------------------------ references

def parse_channel_ref(ref: str | int) -> str | int:
    """'@name' / 'name' / 't.me/name' / 'https://t.me/name/123' → '@name';
    '-1001234' → int; 't.me/c/1234/5' → -1001234; invite links → ChannelError.

    A bare positive number is treated as the internal id from 't.me/c/<id>' links (→ -100<id>).
    """
    if isinstance(ref, int) and not isinstance(ref, bool):
        return ref
    value = str(ref or "").strip()
    if not value:
        raise ChannelError("Укажите @username, ссылку на канал или его ID.")
    if _CHAT_ID_RE.fullmatch(value):
        return int(value) if value.startswith("-") else int(f"-100{value}")
    if value.lower().startswith("tg://"):
        return _username_from_tg_url(value)
    match = _TME_RE.match(value)
    if match:
        return _parse_tme_path(match.group("path"))
    return _as_username(value)


def _parse_tme_path(path: str) -> str | int:
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ChannelError(BAD_REF_ERROR)
    first = parts[0]
    if first.startswith("+") or first.lower() == "joinchat":
        raise ChannelError(INVITE_LINK_ERROR)
    if first.lower() == "c":
        if len(parts) >= 2 and parts[1].isdigit():
            return int(f"-100{parts[1]}")
        raise ChannelError(BAD_REF_ERROR)
    if first.lower() == "s" and len(parts) >= 2:  # web preview links: t.me/s/name
        first = parts[1]
    return _as_username(first)


def _username_from_tg_url(value: str) -> str:
    query = parse_qs(urlsplit(value).query)
    domain = (query.get("domain") or [""])[0]
    return _as_username(domain)


def _as_username(value: str) -> str:
    name = value[1:] if value.startswith("@") else value
    if not _USERNAME_RE.fullmatch(name):
        raise ChannelError(BAD_REF_ERROR)
    return "@" + name


# ------------------------------------------------------------------ rights

def rights_from_member(member: Any) -> dict:
    """ChatMember (of the bot) → {"is_admin", "can_post", "can_edit", "can_delete"}."""
    status = getattr(member, "status", None)
    status = getattr(status, "value", status)
    if status == "creator":
        return dict(ALL_RIGHTS)
    if status == "administrator":
        return {
            "is_admin": True,
            "can_post": bool(getattr(member, "can_post_messages", False)),
            "can_edit": bool(getattr(member, "can_edit_messages", False)),
            "can_delete": bool(getattr(member, "can_delete_messages", False)),
        }
    return dict(NO_RIGHTS)


def normalize_rights(rights: Any, status: str | None = None) -> dict:
    """Accept rights as a ChatMember, a db-style dict (`can_post`…) or a Telegram-style dict
    (`can_post_messages`…). 'creator' always has every right."""
    if status == "creator":
        return dict(ALL_RIGHTS)
    if rights is None:
        return {**NO_RIGHTS, "is_admin": status in ADMIN_STATUSES}
    if not isinstance(rights, dict):
        return rights_from_member(rights)

    def pick(short: str, long: str) -> bool:
        return bool(rights.get(short, rights.get(long, False)))

    return {
        "is_admin": bool(rights.get("is_admin", status in ADMIN_STATUSES)),
        "can_post": pick("can_post", "can_post_messages"),
        "can_edit": pick("can_edit", "can_edit_messages"),
        "can_delete": pick("can_delete", "can_delete_messages"),
    }


async def check_bot_rights(bot: Any, chat_id: int | str) -> dict:
    """The bot's rights in a chat: {"is_admin", "can_post", "can_edit", "can_delete"}.

    Telegram errors propagate.
    """
    member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
    return rights_from_member(member)


async def _member_count(bot: Any, chat_id: int) -> int | None:
    """Subscriber count, or None if Telegram refuses (non-fatal everywhere)."""
    try:
        return int(await bot.get_chat_member_count(chat_id=chat_id))
    except Exception:
        log.warning("Could not get member count of %s", chat_id, exc_info=True)
        return None


def _type_name(chat_type: Any) -> str:
    value = getattr(chat_type, "value", chat_type)
    return _CHAT_TYPES_RU.get(str(value), str(value))


# ------------------------------------------------------------------ add / refresh

async def add_channel(bot: Any, db: Any, ref: str | int, added_by: int | None) -> dict:
    """Verify a channel and the bot's rights there, then store it as active. Returns the row."""
    chat_ref = parse_channel_ref(ref)
    try:
        chat = await bot.get_chat(chat_id=chat_ref)
    except _ACCESS_ERRORS as exc:
        log.info("get_chat(%s) failed: %s", chat_ref, exc)
        raise ChannelError(NOT_FOUND_ERROR) from exc
    except TelegramAPIError as exc:
        raise ChannelError(f"Не удалось получить данные канала: {exc.message}") from exc

    if getattr(chat.type, "value", chat.type) != "channel":
        raise ChannelError(f"Это не канал (тип: {_type_name(chat.type)})")

    try:
        rights = await check_bot_rights(bot, chat.id)
    except _ACCESS_ERRORS as exc:
        raise ChannelError(NOT_ADMIN_ERROR) from exc
    except TelegramAPIError as exc:
        raise ChannelError(f"Не удалось проверить права бота: {exc.message}") from exc
    if not rights["is_admin"]:
        raise ChannelError(NOT_ADMIN_ERROR)
    if not rights["can_post"]:
        raise ChannelError(NO_POST_RIGHT_ERROR)

    members = await _member_count(bot, chat.id)
    row = await db.upsert_channel(
        chat.id, chat.title or "", chat.username, status=CH_ACTIVE, rights=rights,
        members_count=members, added_by=added_by,
    )
    if members is not None:
        await db.add_member_snapshot(chat.id, members)
    log.info("Channel %s (%s) added by %s", chat.id, chat.title, added_by)
    return row


async def refresh_channel(bot: Any, db: Any, chat_id: int, uploads_dir: Any = None) -> dict:
    """Re-read title/username/rights/members from Telegram.

    Marks the channel 'left' when Telegram says the chat is gone or the bot is no longer admin;
    a 'left' channel where the bot can post again becomes active. Network failures → ChannelError.
    With `uploads_dir` the avatar is re-checked too (app.services.avatars).
    """
    existing = await db.get_channel(chat_id)
    if existing is None:
        raise ChannelError("Канал не найден в списке")
    try:
        chat = await bot.get_chat(chat_id=chat_id)
        rights = await check_bot_rights(bot, chat_id)
    except _ACCESS_ERRORS as exc:
        log.info("Channel %s is no longer accessible: %s", chat_id, exc)
        return await _mark_left(db, existing)
    except TelegramAPIError as exc:
        raise ChannelError(f"Не удалось обновить канал: {exc.message}") from exc

    if not rights["is_admin"]:
        return await db.upsert_channel(chat_id, chat.title or existing["title"], chat.username,
                                       status=CH_LEFT, rights=NO_RIGHTS)

    status = CH_ACTIVE if existing["status"] == CH_LEFT and rights["can_post"] else None
    members = await _member_count(bot, chat_id)
    row = await db.upsert_channel(chat_id, chat.title or existing["title"], chat.username,
                                  status=status, rights=rights, members_count=members)
    if members is not None and row["status"] == CH_ACTIVE:
        await db.add_member_snapshot(chat_id, members)
    if uploads_dir is not None:
        try:
            if await avatars.sync_avatar(bot, db, uploads_dir, chat_id, chat):
                row = await db.get_channel(chat_id)
        except Exception as exc:  # the avatar is cosmetic: the refresh itself succeeded
            log.info("Avatar of channel %s not updated: %s", chat_id, type(exc).__name__)
    return row


async def _mark_left(db: Any, channel: dict) -> dict:
    return await db.upsert_channel(channel["id"], channel["title"], channel["username"],
                                   status=CH_LEFT, rights=NO_RIGHTS)


SNAPSHOT_PAUSE = 0.3  # seconds between getChatMemberCount calls: a burst over many channels hits flood control
SNAPSHOT_RETRY_MAX_WAIT = 120  # a channel Telegram asks to wait longer for is skipped until the next pass


async def take_snapshots(bot: Any, db: Any, *, min_age: float = 0, pause: float = SNAPSHOT_PAUSE) -> int:
    """Store a subscriber-count snapshot for every active channel. Returns how many were stored.

    Channels whose latest snapshot is younger than `min_age` seconds are skipped (a restart soon after
    the previous pass does not ask Telegram again). Requests go one by one with a `pause`; channels hit
    by flood control are retried once after the longest wait Telegram asked for (≤ 2 min). Failures are
    logged one line each, not as tracebacks.
    """
    channels = await db.list_channels([CH_ACTIVE])
    if min_age > 0:
        last = await db.last_snapshot_times()
        channels = [c for c in channels if last.get(int(c["id"]), 0) < now() - min_age]

    async def run(batch: list[dict]) -> tuple[int, list[dict], int]:
        """Snapshot `batch` one by one → (stored, channels hit by flood control, longest wait)."""
        stored, throttled, wait = 0, [], 0
        for i, channel in enumerate(batch):
            if i and pause:
                await asyncio.sleep(pause)
            try:
                count = int(await bot.get_chat_member_count(chat_id=channel["id"]))
                await db.add_member_snapshot(channel["id"], count)
                if count != channel["members_count"]:
                    await db.update_channel(channel["id"], members_count=count)
                stored += 1
            except TelegramRetryAfter as exc:
                throttled.append(channel)
                wait = max(wait, int(exc.retry_after))
            except TelegramAPIError as exc:
                log.warning("Снимок подписчиков канала %s не получен: %s", channel["id"], exc.message)
            except Exception:
                log.warning("Snapshot of channel %s failed", channel["id"], exc_info=True)
        return stored, throttled, wait

    stored, throttled, wait = await run(channels)
    if throttled and wait <= SNAPSHOT_RETRY_MAX_WAIT:
        log.info("Снимок подписчиков: Telegram просит подождать %s с, повторю для %s каналов", wait, len(throttled))
        await asyncio.sleep(wait + 1)
        more, throttled, wait = await run(throttled)
        stored += more
    if throttled:
        log.info("Снимок подписчиков: Telegram просит подождать %s с, %s каналов — в следующий раз",
                 wait, len(throttled))
    if stored < len(channels):
        log.info("Снимок подписчиков: %s из %s каналов", stored, len(channels))
    return stored


# ------------------------------------------------------------------ my_chat_member

async def on_bot_status_change(bot: Any, db: Any, chat: Any, new_status: str, rights: Any, by_user: Any,
                               old_status: Any = None) -> str:
    """React to the bot's own membership change in a chat.

    Returns 'discovered' (bot became admin of a new / non-active channel → stored as pending),
    'rights_updated' (admin rights changed in an active channel), 'removed' (an active channel was
    lost → status left) or 'ignored'.

    `old_status` (the bot's previous status in the chat) separates a real promotion from a mere
    edit of the bot's admin rights: a rights edit never re-offers an ignored channel and re-offers
    a pending one only when it just got the right to post (so «🚫 Игнорировать» sticks and toggling
    rights cannot be used to spam the admins).
    """
    if getattr(chat.type, "value", chat.type) != "channel":
        return "ignored"
    status = getattr(new_status, "value", new_status)
    old = getattr(old_status, "value", old_status)
    existing = await db.get_channel(chat.id)
    title = chat.title or (existing or {}).get("title") or ""
    by = getattr(by_user, "id", by_user)

    if status in ADMIN_STATUSES:
        norm = normalize_rights(rights, status)
        if existing is not None and existing["status"] == CH_ACTIVE:
            await db.upsert_channel(chat.id, title, chat.username, rights=norm)
            return "rights_updated"
        if existing is not None and old in ADMIN_STATUSES and existing["status"] in (CH_IGNORED, CH_PENDING):
            # Only the rights of an already known admin position changed.
            gained_post = existing["status"] == CH_PENDING and norm["can_post"] and not existing["can_post"]
            await db.upsert_channel(chat.id, title, chat.username, rights=norm)
            return "discovered" if gained_post else "ignored"
        # New channel, pending, left — or ignored and promoted again: offer it to the admins.
        members = await _member_count(bot, chat.id)
        await db.upsert_channel(chat.id, title, chat.username, status=CH_PENDING, rights=norm,
                                members_count=members)
        log.info("Bot became admin of channel %s (%s), by user %s", chat.id, title, by)
        return "discovered"

    if existing is not None and existing["status"] in (CH_ACTIVE, CH_PENDING):
        await db.upsert_channel(chat.id, title, chat.username, status=CH_LEFT, rights=NO_RIGHTS)
        log.info("Bot lost admin rights in channel %s (%s), new status %s", chat.id, title, status)
        return "removed" if existing["status"] == CH_ACTIVE else "ignored"
    if existing is not None and existing["status"] == CH_IGNORED:
        await db.upsert_channel(chat.id, title, chat.username, rights=NO_RIGHTS)
    return "ignored"

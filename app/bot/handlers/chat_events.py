"""Channel-side updates: the bot's own admin status, subscribers, channel posts and reactions.

These updates do not come from admins and are not filtered by `AdminMiddleware`. Anybody can make
the bot an admin of their channel, so the «new channel» notice treats the channel title and the
promoter's name as untrusted: they are escaped, shortened, and notices about one channel are
rate-limited (`DiscoveryLimiter`).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ContentType
from aiogram.types import (
    ChatMemberUpdated,
    Message,
    MessageReactionCountUpdated,
    ReactionCount,
)

from app.bot.keyboards import discovered_kb
from app.bot.utils import channel_lock, channel_title, clip, esc
from app.db import CH_ACTIVE
from app.services import channels as channel_service
from app.services.posting import notify_admins

log = logging.getLogger(__name__)

_MEMBER_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}

# Channel messages that are posts (everything else — pins, title / photo changes, video chats,
# boosts… — is a service message and is not counted in the statistics).
POST_CONTENT_TYPES = frozenset({
    ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.ANIMATION, ContentType.AUDIO,
    ContentType.DOCUMENT, ContentType.VOICE, ContentType.VIDEO_NOTE, ContentType.STICKER,
    ContentType.POLL, ContentType.LOCATION, ContentType.VENUE, ContentType.CONTACT, ContentType.DICE,
    ContentType.STORY, ContentType.PAID_MEDIA, ContentType.GIVEAWAY, ContentType.GIVEAWAY_WINNERS,
    ContentType.CHECKLIST, ContentType.LIVE_PHOTO, ContentType.GAME, ContentType.INVOICE,
})

TITLE_LIMIT = 64  # untrusted names in notices are shortened


class DiscoveryLimiter:
    """Rate limit of «bot became admin» notices: one per channel (and right to post) per
    `per_chat` seconds, and at most `burst` notices per `window` seconds overall."""

    def __init__(self, *, per_chat: float = 600, burst: int = 5, window: float = 600) -> None:
        self.per_chat = per_chat
        self.burst = burst
        self.window = window
        self._last: dict[tuple[int, bool], float] = {}
        self._recent: list[float] = []

    def allow(self, chat_id: int, can_post: bool) -> bool:
        current = time.monotonic()
        self._recent = [ts for ts in self._recent if current - ts < self.window]
        self._last = {k: ts for k, ts in self._last.items() if current - ts < self.per_chat}
        key = (chat_id, bool(can_post))
        if key in self._last or len(self._recent) >= self.burst:
            return False
        self._last[key] = current
        self._recent.append(current)
        return True


def _is_member(member: Any) -> bool:
    status = member.status
    if status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return status in _MEMBER_STATUSES


def _ts(value: Any) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _chat_label(chat: Any) -> str:
    title = f"«{esc(clip(chat.title or chat.id, TITLE_LIMIT))}»"
    if chat.username:
        title += f" (@{esc(chat.username)})"
    return title


# ------------------------------------------------------------------ my_chat_member

def discovered_text(event: ChatMemberUpdated, rights: dict) -> str:
    lines = [f"🆕 Бот назначен администратором канала {_chat_label(event.chat)}. Добавить его?"]
    by = event.from_user
    if by is not None and not by.is_bot:
        lines.append(f"Назначил: {esc(clip(by.full_name, TITLE_LIMIT))}")
    if not rights.get("can_post"):
        lines.append("\n⚠️ У бота нет права «Публикация сообщений» — выдайте его, иначе канал не добавить.")
    elif not rights.get("can_edit"):
        lines.append("\nℹ️ Без права «Редактирование» бот не сможет изменять опубликованные посты.")
    lines.append("\n<i>Если вы не знаете этот канал — нажмите «🚫 Игнорировать».</i>")
    return "\n".join(lines)


async def on_my_chat_member(event: ChatMemberUpdated, db: Any, discovery: DiscoveryLimiter | None = None) -> None:
    """The bot was promoted / demoted / removed in a chat."""
    chat = event.chat
    if chat.type != "channel":
        return
    member = event.new_chat_member
    async with channel_lock(chat.id):
        before = await db.get_channel(chat.id)
        result = await channel_service.on_bot_status_change(
            event.bot, db, chat, member.status, member, event.from_user,
            old_status=event.old_chat_member.status,
        )
    log.info("my_chat_member in %s: %s → %s → %s", chat.id, event.old_chat_member.status, member.status, result)
    rights = channel_service.normalize_rights(member, member.status)
    if result == "discovered":
        if discovery is not None and not discovery.allow(chat.id, rights.get("can_post", False)):
            log.warning("Discovery notice for channel %s suppressed (rate limit)", chat.id)
            return
        await notify_admins(event.bot, db, discovered_text(event, rights), reply_markup=discovered_kb(chat.id))
    elif result == "removed":
        title = esc(clip(channel_title(before, chat.title or chat.id), TITLE_LIMIT))
        await notify_admins(event.bot, db,
                            f"⚠️ Бот больше не администратор канала «{title}». Публикация в него невозможна.")
    elif result == "rights_updated" and before and before.get("can_post") and not rights.get("can_post"):
        title = esc(clip(channel_title(before, chat.title or chat.id), TITLE_LIMIT))
        await notify_admins(event.bot, db,
                            f"⚠️ В канале «{title}» у бота отобрали право публикации. "
                            "Посты в этот канал не будут опубликованы.")


# ------------------------------------------------------------------ chat_member

async def on_chat_member(event: ChatMemberUpdated, db: Any) -> None:
    """Subscriber joined / left an active channel (admin changes are ignored)."""
    if event.chat.type != "channel" or event.new_chat_member.user.id == event.bot.id:
        return
    channel = await db.get_channel(event.chat.id)
    if channel is None or channel["status"] != CH_ACTIVE:
        return
    was, now = _is_member(event.old_chat_member), _is_member(event.new_chat_member)
    if was == now:
        return
    kind = "join" if now else "leave"
    await db.add_member_event(event.chat.id, event.new_chat_member.user.id, kind, ts=_ts(event.date))


# ------------------------------------------------------------------ channel_post

async def on_channel_post(message: Message, db: Any) -> None:
    """Count every post of an active channel (posts sent by the bot are already recorded).

    Service messages are skipped; the items of an album carry their `media_group_id`, so the
    statistics count the album once.
    """
    if message.content_type not in POST_CONTENT_TYPES:
        return
    channel = await db.get_channel(message.chat.id)
    if channel is None or channel["status"] != CH_ACTIVE:
        return
    await db.add_channel_post(message.chat.id, message.message_id, via_bot=False, ts=_ts(message.date),
                              media_group_id=message.media_group_id)


# ------------------------------------------------------------------ reactions

def reaction_item(reaction: ReactionCount) -> dict:
    """JSON-friendly reaction: {"type", "emoji"|"custom_emoji_id", "count"}."""
    kind = reaction.type
    type_name = getattr(kind, "type", "unknown")
    item: dict[str, Any] = {"type": str(getattr(type_name, "value", type_name)), "count": reaction.total_count}
    emoji = getattr(kind, "emoji", None)
    custom = getattr(kind, "custom_emoji_id", None)
    if emoji:
        item["emoji"] = emoji
    if custom:
        item["custom_emoji_id"] = custom
    return item


async def on_reaction_count(event: MessageReactionCountUpdated, db: Any) -> None:
    items = [reaction_item(r) for r in event.reactions]
    total = sum(item["count"] for item in items)
    updated = await db.set_target_reactions(event.chat.id, event.message_id, total, items)
    if not updated:
        # Reactions on a later message of a bot-published album belong to that post too.
        target = await db.find_target_by_message(event.chat.id, event.message_id)
        if target is not None and target.get("message_id"):
            updated = await db.set_target_reactions(event.chat.id, target["message_id"], total, items)
    log.debug("Reactions of %s/%s: %s (bot post: %s)", event.chat.id, event.message_id, total, updated)


# ------------------------------------------------------------------ router

def create_router() -> Router:
    router = Router(name="chat_events")
    router.my_chat_member.register(on_my_chat_member)
    router.chat_member.register(on_chat_member)
    router.channel_post.register(on_channel_post, F.chat.type == "channel")
    router.message_reaction_count.register(on_reaction_count)
    return router

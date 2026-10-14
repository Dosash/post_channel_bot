"""Small helpers shared by the bot handlers: safe Telegram calls and Russian formatting."""
from __future__ import annotations

import asyncio
import html
import logging
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Telegram rejects messages longer than 4096 characters: long lists are cut to this many (the rest
# of a screen — header, hints, notes — fits into the remaining ~500).
TEXT_BUDGET = 3600
CUT_NOTE = "<i>…список сокращён</i>"

# Per-channel locks: `my_chat_member` (→ pending) and an admin adding the same channel (→ active)
# may arrive together (e.g. via the native channel picker) and must not interleave.
_channel_locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)


def channel_lock(chat_id: int) -> asyncio.Lock:
    # Keyed by the running loop too: an asyncio.Lock must not be shared between event loops.
    return _channel_locks[(id(asyncio.get_running_loop()), chat_id)]


def esc(value: Any) -> str:
    """HTML-escape any user-provided value for parse_mode=HTML."""
    return html.escape(str(value if value is not None else ""), quote=False)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: plural(2, 'канал', 'канала', 'каналов') → 'канала'."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_num(value: int | None) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ")


def tz_name(tz: Any) -> str:
    """'Asia/Tashkent' for a ZoneInfo."""
    return getattr(tz, "key", None) or str(tz)


def fmt_short(ts: int | None, tz: Any) -> str:
    """'14.10 18:30' (the year is omitted for compact lists)."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


def clip(text: Any, limit: int) -> str:
    """One line of at most `limit` characters ('…' when cut) — for untrusted titles and buttons."""
    value = " ".join(str(text if text is not None else "").split())
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def text_units(text: str) -> int:
    """Length the way Telegram counts it (UTF-16 code units; tags count too, so it errs long)."""
    return len(text.encode("utf-16-le")) // 2


def cut_lines(lines: list[str], budget: int = TEXT_BUDGET, *, more: str = CUT_NOTE) -> list[str]:
    """`lines` limited to about `budget` characters: the lines that do not fit are replaced by one
    `more` line (`{n}` in it = how many lines were left out)."""
    total = 0
    for index, line in enumerate(lines):
        total += text_units(line) + 1
        if total > budget:
            return lines[:index] + [more.format(n=len(lines) - index)]
    return lines


def fit_html(text: str, budget: int = TEXT_BUDGET) -> str:
    """A line-based HTML text (e.g. a publish report: every line closes its own tags) cut by whole
    lines to about `budget` characters, so it can be sent as one message."""
    return "\n".join(cut_lines(text.split("\n"), budget))


def names_text(names: list[str], limit: int = 400) -> str:
    """«A», «B», … as escaped HTML, at most about `limit` characters (cut before escaping, so an
    entity is never split)."""
    return esc(clip(", ".join(f"«{name}»" for name in names), limit))


# ------------------------------------------------------------------ one-shot confirmations

class ConfirmGuard:
    """Publish confirmations («✅ Да, опубликовать») that may be used only once (finding #13).

    Updates of one user are handled one at a time, so a second tap on the same button waits for the
    first publish and then runs against the post as it is afterwards — after a failure it would
    publish again without asking. The used button is remembered here: (chat id, message id) → post
    id. Showing a confirmation screen in a message makes its button usable again. One guard per
    dispatcher (``dp["confirms"]``), bounded — only recent taps matter.
    """

    LIMIT = 1000

    def __init__(self) -> None:
        self._used: OrderedDict[tuple[int, int], int] = OrderedDict()

    def shown(self, chat_id: int, message_id: int | None) -> None:
        """A confirmation screen was (re)shown in this message: its button may be used once again."""
        if message_id:
            self._used.pop((chat_id, message_id), None)

    def use(self, chat_id: int, message_id: int | None, post_id: int) -> bool:
        """Mark the confirmation in this message as used. False when it already was (a second tap
        of the same button): the caller must not publish again."""
        if not message_id:
            return True
        key = (chat_id, message_id)
        if self._used.get(key) == post_id:
            return False
        self._used[key] = post_id
        self._used.move_to_end(key)
        while len(self._used) > self.LIMIT:
            self._used.popitem(last=False)
        return True


CONFIRM_USED_TEXT = "Эта кнопка уже нажата — публикация выполнена, результат выше."


def channel_title(channel: dict | None, fallback: Any = "") -> str:
    """Display title of a channel row (or of a target row with channel_title)."""
    if not channel:
        return str(fallback)
    return (
        channel.get("title")
        or channel.get("channel_title")
        or (f"@{channel['username']}" if channel.get("username") else "")
        or str(channel.get("id") or channel.get("channel_id") or fallback)
    )


def is_not_modified(exc: TelegramAPIError) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in exc.message.lower()


CALLBACK_TEXT_LIMIT = 200  # Telegram rejects longer callback answers


async def safe_answer(callback: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Answer a callback query; a too old query must not break the handler."""
    if text and len(text) > CALLBACK_TEXT_LIMIT:
        text = text[: CALLBACK_TEXT_LIMIT - 1] + "…"
    try:
        await callback.answer(text, show_alert=alert)
    except TelegramAPIError:
        log.debug("Callback answer failed", exc_info=True)


async def safe_delete(bot: Any, chat_id: int, *message_ids: int | None) -> None:
    """Delete messages (several at once with one request), ignoring the ones already gone or too old."""
    ids = list(dict.fromkeys(m for m in message_ids if m))
    if len(ids) > 1:
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=ids)
            return
        except TelegramAPIError:
            log.debug("Bulk delete of %s in %s failed, deleting one by one", ids, chat_id, exc_info=True)
    for message_id in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            log.debug("Could not delete message %s in %s", message_id, chat_id, exc_info=True)


async def edit_or_send(
    bot: Any,
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    """Edit a bot message in place; send a new one when editing is impossible.

    "message is not modified" counts as success. Returns the id of the shown message.
    """
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup,
                link_preview_options=NO_PREVIEW,
            )
            return message_id
        except TelegramAPIError as exc:
            if is_not_modified(exc):
                return message_id
            log.debug("Edit of message %s failed, sending a new one: %s", message_id, exc)
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                  link_preview_options=NO_PREVIEW)
    return sent.message_id


async def show(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> int:
    """Replace the content of the message the callback came from (or send a new message)."""
    message = callback.message
    message_id = message.message_id if isinstance(message, Message) else None
    chat_id = message.chat.id if message is not None else callback.from_user.id
    return await edit_or_send(callback.bot, chat_id, message_id, text, reply_markup)


def callback_chat_id(callback: CallbackQuery) -> int:
    return callback.message.chat.id if callback.message is not None else callback.from_user.id


def callback_message_id(callback: CallbackQuery) -> int | None:
    return callback.message.message_id if isinstance(callback.message, Message) else None


async def premium_emoji_enabled(db: Any) -> bool:
    """The bot has a Fragment username: custom (premium) emoji are published as they are (SPEC_V23)."""
    return await db.get_premium_emoji()


async def is_owner(db: Any, user_id: int) -> bool:
    row = await db.fetchone("SELECT is_owner FROM admins WHERE user_id = ?", (user_id,))
    return bool(row and row["is_owner"])

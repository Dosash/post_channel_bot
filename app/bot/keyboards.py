"""Reply and inline keyboards of the bot (labels are part of the UI contract)."""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    ChatAdministratorRights,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
)

from app.bot.callbacks import ChannelCb, MenuCb
from app.bot.utils import channel_title
from app.services.helper_bots import add_link

# Main menu (reply keyboard) labels.
BTN_NEW = "📝 Новый пост"
BTN_PLAN = "📅 Контент-план"
BTN_CHANNELS = "📢 Каналы"
BTN_STATS = "📊 Статистика"
BTN_POSTS = "🗂 Посты"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "❌ Отмена"
BTN_PICK_CHANNEL = "📢 Выбрать канал"
MENU_LABELS = (BTN_NEW, BTN_PLAN, BTN_CHANNELS, BTN_STATS, BTN_POSTS, BTN_SETTINGS, BTN_HELP, BTN_CANCEL)

# request_id of the "pick a channel" reply button (KeyboardButtonRequestChat).
CHANNEL_REQUEST_ID = 1

# Post options toggled in the composer: field → label.
OPTIONS: dict[str, str] = {
    "silent": "🔕 Без звука",
    "protect": "🔒 Запрет пересылки",
    "disable_preview": "🔗 Без превью ссылок",
    "caption_above": "⬆️ Подпись над медиа",
}

# Quick schedule presets: code → (label, parse_when expression).
SCHEDULE_PRESETS: dict[str, tuple[str, str]] = {
    "1h": ("Через 1 час", "+1h"),
    "3h": ("Через 3 часа", "+3h"),
    "t9": ("Завтра 09:00", "завтра 09:00"),
    "t18": ("Завтра 18:00", "завтра 18:00"),
}


def ib(text: str, data: CallbackData | str, style: str | None = None) -> InlineKeyboardButton:
    """Inline callback button; `style` is the Bot API button color (primary/success/danger)."""
    kwargs: dict = {"text": text, "callback_data": data if isinstance(data, str) else data.pack()}
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


PAGE_ROWS = 40  # keyboard rows per page: Telegram refuses keyboards of more than ~100 buttons


def paginate(rows: list[list], page: int, per_page: int = PAGE_ROWS) -> tuple[list[list], int, int]:
    """(rows of `page`, the page clamped to the existing ones, number of pages)."""
    pages = max(1, math.ceil(len(rows) / per_page))
    page = min(max(page, 0), pages - 1)
    return rows[page * per_page:(page + 1) * per_page], page, pages


def page_nav(page: int, pages: int, make: Callable[[int], Any]) -> list[InlineKeyboardButton] | None:
    """◀️ k/n ▶️ row (`make(page)` builds the callback data), None for a single page."""
    if pages <= 1:
        return None
    return [ib("◀️", make((page - 1) % pages)), ib(f"{page + 1}/{pages}", MenuCb(action="noop")),
            ib("▶️", make((page + 1) % pages))]


def url_button(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def kb(*rows: list[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    """Inline keyboard from rows; empty / None rows are skipped."""
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_PLAN)],
            [KeyboardButton(text=BTN_CHANNELS), KeyboardButton(text=BTN_POSTS)],
            [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие или пришлите текст поста",
    )


def _rights(**required: bool) -> ChatAdministratorRights:
    base = dict.fromkeys(
        ("is_anonymous", "can_manage_chat", "can_delete_messages", "can_manage_video_chats",
         "can_restrict_members", "can_promote_members", "can_change_info", "can_invite_users",
         "can_post_stories", "can_edit_stories", "can_delete_stories", "can_send_welcome_messages"),
        False,
    )
    base.update(required)
    return ChatAdministratorRights(**base)


def channel_request_menu() -> ReplyKeyboardMarkup:
    """Reply keyboard of the «add channel» dialog: Telegram's native channel picker + cancel.

    Telegram adds the bot to the chosen channel with the requested rights if needed.
    """
    rights = _rights(can_post_messages=True, can_edit_messages=True, can_delete_messages=True)
    picker = KeyboardButtonRequestChat(
        request_id=CHANNEL_REQUEST_ID,
        chat_is_channel=True,
        user_administrator_rights=rights,
        bot_administrator_rights=rights,
        request_title=True,
        request_username=True,
    )
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_PICK_CHANNEL, request_chat=picker)],
                  [KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="@username, ссылка или пересланный пост",
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return kb([ib(BTN_CANCEL, MenuCb(action="cancel"))])


def helper_rows(usernames: list[str]) -> list[list[InlineKeyboardButton]]:
    """«➕ @bot» link per helper bot: Telegram's «add as channel admin» screen (app.services.helper_bots)."""
    return [[url_button(f"➕ @{name}"[:64], add_link(name))] for name in usernames]


def discovered_kb(chat_id: int, helpers: list[str] = ()) -> InlineKeyboardMarkup:
    """Buttons of the «bot became admin of a channel» notification (+ links to add the helper bots)."""
    return kb([
        ib("✅ Добавить канал", ChannelCb(action="approve", chat_id=chat_id), style="success"),
        ib("🚫 Игнорировать", ChannelCb(action="ignore", chat_id=chat_id)),
    ], *helper_rows(list(helpers)))


def pending_rows(channels: list[dict]) -> list[list[InlineKeyboardButton]]:
    """One row per pending channel: [✅ Title] [🚫]."""
    rows = []
    for channel in channels:
        title = channel_title(channel)
        rows.append([
            ib(f"✅ {title}"[:60], ChannelCb(action="approve", chat_id=channel["id"]), style="success"),
            ib("🚫", ChannelCb(action="ignore", chat_id=channel["id"])),
        ])
    return rows


def no_channels_kb() -> InlineKeyboardMarkup:
    return kb([ib("➕ Добавить канал", ChannelCb(action="add"), style="primary")])

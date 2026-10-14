"""«📊 Статистика»: overview of all channels and per-channel statistics for 7 / 30 / 90 days."""
from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import ChannelCb, MenuCb, StatsCb, StatsPageCb
from app.bot.filters import TextIs
from app.bot.handlers.common import drop_prompt, is_https, leave_state, panel_button, panel_link_html
from app.bot.keyboards import BTN_STATS, ib, kb, page_nav, paginate
from app.bot.utils import channel_title, fmt_num, safe_answer, show
from app.db import CH_ACTIVE
from app.services import stats as stats_service
from app.services.channels import ChannelError

PERIODS = (7, 30, 90)


async def overview_screen(db: Any, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    data = await stats_service.overview(db)
    active = await db.list_channels([CH_ACTIVE])
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"📢 Каналов: {data['channels_active']}"
        + (f" (ждут подтверждения: {data['channels_pending']})" if data["channels_pending"] else ""),
        f"👥 Подписчиков всего: {fmt_num(data['members_total'])}",
        f"✅ Опубликовано постов: {fmt_num(data['posts_sent'])}",
        f"⏰ Запланировано: {fmt_num(data['posts_scheduled'])}",
        f"📝 Черновиков: {fmt_num(data['posts_draft'])}",
        "",
        "Выберите канал для подробной статистики:" if active else "Подключите канал, чтобы видеть статистику.",
    ]
    rows, page, pages = paginate([[ib(f"📢 {channel_title(ch)} · 👥 {fmt_num(ch['members_count'])}"[:64],
                                      StatsCb(action="ch", chat_id=ch["id"], days=30))] for ch in active], page)
    rows.append(page_nav(page, pages, lambda p: StatsPageCb(page=p)))  # many channels: paged
    if not active:
        rows.append([ib("➕ Добавить канал", ChannelCb(action="add"), style="primary")])
    return "\n".join(lines), kb(*rows)


def channel_stats_kb(chat_id: int, days: int, config: Any) -> InlineKeyboardMarkup:
    periods = [ib(f"{'• ' if d == days else ''}{d} дн.", StatsCb(action="ch", chat_id=chat_id, days=d))
               for d in PERIODS]
    # Telegram may reject URL buttons pointing to http:// / private addresses: the panel link is a
    # button only for an https:// address (otherwise it is printed in the text).
    web = panel_button("🌐 Графики в веб-панели", config)
    return kb(periods, [web] if web else None, [ib("↩️ Ко всем каналам", StatsCb(action="overview"))])


async def channel_stats_screen(db: Any, config: Any, chat_id: int, days: int) -> tuple[str, InlineKeyboardMarkup]:
    days = days if days in PERIODS else 30
    data = await stats_service.channel_stats(db, chat_id, days, config.tz)
    text = stats_service.format_stats_html(data, config.tz)
    if not is_https(config.web_url):
        text += f"\n\n🌐 Графики — в веб-панели: {panel_link_html(config)}"
    return text, channel_stats_kb(chat_id, days, config)


async def cmd_stats(message: Message, state: FSMContext, db: Any) -> None:
    await leave_state(message.bot, message.chat.id, state)
    text, markup = await overview_screen(db)
    await message.answer(text, reply_markup=markup)


async def cb_overview(callback: CallbackQuery, state: FSMContext, db: Any) -> None:
    await drop_prompt(state)
    await safe_answer(callback)
    text, markup = await overview_screen(db)
    await show(callback, text, markup)


async def cb_overview_page(callback: CallbackQuery, callback_data: StatsPageCb, state: FSMContext,
                           db: Any) -> None:
    await drop_prompt(state)
    await safe_answer(callback)
    await show(callback, *await overview_screen(db, callback_data.page))


async def cb_channel(callback: CallbackQuery, callback_data: StatsCb, state: FSMContext, db: Any,
                     config: Any) -> None:
    await drop_prompt(state)
    try:
        text, markup = await channel_stats_screen(db, config, callback_data.chat_id, callback_data.days)
    except ChannelError as exc:
        await safe_answer(callback, str(exc), alert=True)
        return
    await safe_answer(callback)
    await show(callback, text, markup)


def create_router() -> Router:
    router = Router(name="stats")
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_stats, TextIs(BTN_STATS))
    router.callback_query.register(cb_overview, MenuCb.filter(F.action == "stats"))
    router.callback_query.register(cb_overview, StatsCb.filter(F.action == "overview"))
    router.callback_query.register(cb_channel, StatsCb.filter(F.action == "ch"))
    router.callback_query.register(cb_overview_page, StatsPageCb.filter())
    return router


"""/start, /help, /cancel, generic navigation callbacks and the fallback for unknown input."""
from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, Message, MessageOriginChannel

from app.bot.callbacks import ChannelCb, MenuCb
from app.bot.filters import TextIs
from app.bot.keyboards import (
    BTN_CANCEL,
    BTN_HELP,
    ib,
    kb,
    main_menu,
    no_channels_kb,
    pending_rows,
    url_button,
)
from app.bot.states import ChannelStates, ComposeStates, PlanStates, PostStates
from app.bot.compose_view import saved_text
from app.bot.utils import NO_PREVIEW, esc, plural, safe_answer, safe_delete, show
from app.buttons import FORMAT_HELP
from app.db import CH_ACTIVE, CH_PENDING
from app.markdown import SYNTAX_HELP_HTML

log = logging.getLogger(__name__)

HELP_TEXT = (
    "❓ <b>Как пользоваться ботом</b>\n"
    "1. Сделайте бота администратором канала с правом «Публикация сообщений» — я сам предложу "
    "подключить канал. Если бот уже администратор: «📢 Каналы» → «➕ Добавить канал». Каналы можно "
    "разложить по папкам.\n"
    "2. «📝 Новый пост» → пришлите текст, фото, видео или GIF с подписью. Несколько фото или видео "
    "одним сообщением станут альбомом (до 10). Готовый пост можно просто переслать боту — из канала "
    "или от кого угодно: текст, эмодзи, медиа и кнопки-ссылки перенесутся как есть (из главного меню "
    "пересланный пост сразу станет черновиком).\n"
    "3. Под превью появится панель: текст, медиа, кнопки, каналы, опции. Затем «🚀 Опубликовать»: "
    "сразу, в очередь по расписанию каналов или ко времени.\n"
    "4. «📅 Контент-план» — очередь публикаций каждого канала: порядок, время, публикация сейчас. "
    "Расписание канала задаётся в его карточке: «📅 Расписание».\n"
    "5. «🗂 Посты» — отложенные, опубликованные и черновики: перенос, редактирование, удаление.\n\n"
    "<b>Расписание канала</b>\n"
    "<code>пн-пт 09:00, 18:00</code>\n"
    "<code>сб, вс 12:00</code>\n"
    "<code>ежедневно 21:00</code>\n\n"
    f"{SYNTAX_HELP_HTML}\n\n"
    "<b>Кнопки под постом</b>\n"
    f"{esc(FORMAT_HELP)}\n"
    "У альбомов кнопок быть не может — это ограничение Telegram.\n\n"
    "<b>Команды</b>\n"
    "/new — новый пост\n"
    "/plan — контент-план\n"
    "/channels — каналы и папки\n"
    "/posts — посты\n"
    "/stats — статистика\n"
    "/settings — настройки и веб-панель\n"
    "/cancel — отменить текущее действие"
)

MENU_HINT = "Выберите действие в меню 👇"
PENDING_LIMIT = 40  # channels offered for approval in one /start message


# ------------------------------------------------------------------ web panel address

def is_https(url: str | None) -> bool:
    return bool(url) and url.strip().lower().startswith("https://")


def panel_link_html(config: Any) -> str:
    """The web panel address for a message: a link for an https:// address, otherwise <code> —
    Telegram may refuse or not open http:// and private-network addresses, so they are only shown
    for copying."""
    url = config.web_url
    if is_https(url):
        return f'<a href="{html.escape(url, quote=True)}">{esc(url)}</a>'
    return f"<code>{esc(url)}</code>"


def panel_button(text: str, config: Any) -> InlineKeyboardButton | None:
    """URL button to the web panel — only for an https:// address (see `panel_link_html`)."""
    url = config.web_url
    return url_button(text, url) if is_https(url) else None


def help_text(config: Any = None) -> str:
    if config is None:
        return HELP_TEXT
    return f"{HELP_TEXT}\n\n🌐 <b>Веб-панель</b>: {panel_link_html(config)}"


# ------------------------------------------------------------------ helpers

async def leave_state(bot: Any, chat_id: int, state: FSMContext) -> None:
    """Clear the FSM before switching sections.

    The «add channel» dialog replaces the main menu with its own reply keyboard, so leaving it
    must bring the main menu back. Unsaved changes of a published post are dropped — said out
    loud (and their preview removed), never silently (finding #7).
    """
    current = await state.get_state()
    data = await state.get_data()
    await state.clear()
    if current == ChannelStates.waiting_ref.state:
        await bot.send_message(chat_id=chat_id, text="↩️ Добавление канала отменено.", reply_markup=main_menu())
    elif current in ComposeStates and data.get("edit") and (data.get("overrides") or data.get("media_changed")):
        await safe_delete(bot, chat_id, *(data.get("preview_ids") or []))
        await bot.send_message(chat_id=chat_id,
                               text=f"↩️ Несохранённые изменения поста #{data.get('post_id')} отменены.")


# One-line text prompts (schedule, folder name, new time…): navigating elsewhere cancels them, so a
# later free text is not taken as their answer.
PROMPT_STATES = (
    ChannelStates.slots.state, ChannelStates.folder_name.state, ChannelStates.folder_slots.state,
    PlanStates.move_time.state, PostStates.reschedule.state,
)


async def drop_prompt(state: FSMContext) -> None:
    if await state.get_state() in PROMPT_STATES:
        await state.clear()


async def send_pending_channels(message: Message, db: Any) -> None:
    """List channels waiting for approval (if any) with ✅ / 🚫 buttons."""
    pending = await db.list_channels([CH_PENDING])
    if not pending:
        return
    text = "⏳ <b>Каналы ждут подтверждения</b>\nБот назначен в них администратором. Добавить их в бота?"
    if len(pending) > PENDING_LIMIT:  # 2 buttons per channel: the keyboard must stay within Telegram's limit
        text += f"\n\nЗдесь первые {PENDING_LIMIT} из {len(pending)} — остальные в «📢 Каналы»."
    await message.answer(text, reply_markup=kb(*pending_rows(pending[:PENDING_LIMIT])))


def cancel_text(current_state: str | None, data: dict, post: dict | None = None, tz: Any = None) -> str:
    """What /cancel tells the user, depending on what was going on (`post`: the composer's post
    as it is in the DB now — a scheduled post is not called a draft, finding #13)."""
    if current_state is None:
        return "Нечего отменять 🙂 " + MENU_HINT
    post_id = data.get("post_id")
    if current_state in ComposeStates and post_id:
        if data.get("edit"):
            return f"↩️ Редактирование поста #{post_id} отменено, изменения не сохранены."
        if post is not None and tz is not None:
            return "↩️ Готово. " + saved_text(post, tz)
        return f"↩️ Готово. Черновик #{post_id} сохранён в «🗂 Посты» → «📝 Черновики»."
    return "↩️ Действие отменено."


async def _cancel_text(state: FSMContext, db: Any, config: Any) -> tuple[str | None, str]:
    current = await state.get_state()
    data = await state.get_data()
    post = None
    if current in ComposeStates and data.get("post_id") and not data.get("edit"):
        post = await db.get_post(data["post_id"])
    return current, cancel_text(current, data, post, getattr(config, "tz", None))


# ------------------------------------------------------------------ handlers

async def cmd_start(message: Message, state: FSMContext, db: Any, config: Any,
                    became_owner: bool = False) -> None:
    await leave_state(message.bot, message.chat.id, state)
    user = message.from_user
    name = esc(user.first_name if user else "")
    active = await db.list_channels([CH_ACTIVE])
    lines = []
    if became_owner:
        lines.append("<b>👑 Вы назначены владельцем бота.</b>\n")
    lines.append(f"👋 Привет, {name}!")
    lines.append(
        "Я помогу готовить и публиковать посты в ваши Telegram-каналы: Markdown-форматирование, "
        "фото, видео, GIF и альбомы, цветные кнопки, контент-план с расписанием каналов, папки "
        "каналов и статистика."
    )
    lines.append("")
    count = len(active)
    lines.append(f"📢 Подключено: {count} {plural(count, 'канал', 'канала', 'каналов')}")
    lines.append(f"🌐 Веб-панель: {panel_link_html(config)}")
    lines.append("")
    lines.append(MENU_HINT)
    await message.answer("\n".join(lines), reply_markup=main_menu(), link_preview_options=NO_PREVIEW)
    if not active:
        await message.answer(
            "Чтобы начать, сделайте бота администратором канала с правом публикации — я сам "
            "предложу его подключить. Если бот уже администратор, нажмите «➕ Добавить канал».",
            reply_markup=no_channels_kb(),
        )
    await send_pending_channels(message, db)


async def cmd_help(message: Message, config: Any = None) -> None:
    await message.answer(help_text(config), reply_markup=main_menu(), link_preview_options=NO_PREVIEW)


async def cmd_cancel(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    data = await state.get_data()
    _, text = await _cancel_text(state, db, config)
    await state.clear()
    if data.get("edit") and (data.get("overrides") or data.get("media_changed")):
        await safe_delete(message.bot, message.chat.id, *(data.get("preview_ids") or []))
    await message.answer(text, reply_markup=main_menu())


async def cb_cancel(callback: CallbackQuery, state: FSMContext, db: Any, config: Any) -> None:
    current, text = await _cancel_text(state, db, config)
    await state.clear()
    await safe_answer(callback)
    if current == ChannelStates.waiting_ref.state:
        await callback.bot.send_message(chat_id=callback.from_user.id, text=text, reply_markup=main_menu())
        return
    await show(callback, text)


async def cb_noop(callback: CallbackQuery) -> None:
    await safe_answer(callback)


# ------------------------------------------------------------------ fallback

async def fallback_message(message: Message, db: Any, albums: Any = None) -> None:
    if message.media_group_id and albums is not None and albums.seen(message):
        return  # answer an album once, not once per photo
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        channel = await db.get_channel(origin.chat.id)
        if channel is None or channel["status"] != CH_ACTIVE:
            title = esc(origin.chat.title or origin.chat.id)
            await message.answer(
                f"Это пост из канала «{title}», который не подключён к боту. Добавить его?",
                reply_markup=kb([ib("✅ Добавить канал", ChannelCb(action="approve", chat_id=origin.chat.id),
                                    style="success")]),
            )
            return
    await message.answer(
        f"Не понял 🤔 {MENU_HINT}\nЧтобы сделать пост, нажмите «📝 Новый пост».",
        reply_markup=main_menu(),
    )


async def fallback_callback(callback: CallbackQuery) -> None:
    await safe_answer(callback, "Эта кнопка устарела — откройте раздел заново из меню.", alert=True)


async def on_error(event: ErrorEvent) -> bool:
    """Last-resort error handler: log and tell the admin something went wrong."""
    log.error("Unhandled error while processing update %s", event.update.update_id,
              exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query is not None:
            await update.callback_query.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
        elif update.message is not None and update.message.chat.type == "private":
            await update.message.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите /cancel.")
    except Exception:  # the error handler itself must never raise
        log.debug("Could not report the error to the user", exc_info=True)
    return True


# ------------------------------------------------------------------ routers

def create_router() -> Router:
    router = Router(name="common")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_help, TextIs(BTN_HELP))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_cancel, TextIs(BTN_CANCEL))
    router.callback_query.register(cb_cancel, MenuCb.filter(F.action == "cancel"))
    router.callback_query.register(cb_noop, MenuCb.filter(F.action == "noop"))
    return router


def create_fallback_router() -> Router:
    """Must be included last: catches whatever no other handler took."""
    router = Router(name="fallback")
    router.message.register(fallback_message)
    router.callback_query.register(fallback_callback)
    return router

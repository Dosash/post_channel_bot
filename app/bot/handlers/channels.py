"""«📢 Каналы»: channel list grouped by folders, folder screens, channel card (folder, publishing
schedule), approving discovered channels, adding channels."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, MessageOriginChannel

from app.bot.callbacks import (
    ChannelCb,
    ChannelFolderCb,
    ChannelPageCb,
    FolderCb,
    FolderPageCb,
    MenuCb,
    PlanCb,
    StatsCb,
)
from app.bot.filters import TextIs, UserInput
from app.bot.handlers.common import drop_prompt, leave_state
from app.bot.keyboards import (
    BTN_CHANNELS,
    CHANNEL_REQUEST_ID,
    channel_request_menu,
    ib,
    kb,
    main_menu,
    page_nav,
    paginate,
    pending_rows,
    url_button,
)
from app.bot.states import ChannelStates
from app.bot.utils import (
    callback_message_id,
    channel_lock,
    channel_title,
    clip,
    cut_lines,
    edit_or_send,
    esc,
    fmt_num,
    plural,
    safe_answer,
    show,
)
from app.db import CH_ACTIVE, CH_IGNORED, CH_LEFT, CH_PENDING
from app.services import channels as channel_service
from app.services import folders as folder_service
from app.services.channels import ChannelError
from app.services.folders import FolderError
from app.services.plan import SLOTS_HELP, PlanError, describe_slots, normalize_slots, parse_slots, slots_to_text

log = logging.getLogger(__name__)

STATUS_LABELS = {
    CH_ACTIVE: "✅ подключён",
    CH_PENDING: "⏳ ждёт подтверждения",
    CH_IGNORED: "🚫 проигнорирован",
    CH_LEFT: "⚠️ нет доступа — бот больше не администратор",
}
STATUS_ICONS = {CH_ACTIVE: "📢", CH_PENDING: "⏳", CH_IGNORED: "🚫", CH_LEFT: "⚠️"}

ADD_PROMPT = (
    "➕ <b>Добавление канала</b>\n\n"
    "1. Добавьте бота в администраторы канала с правом «Публикация сообщений» "
    "(лучше также «Редактирование» и «Удаление» — чтобы править и удалять посты).\n"
    "2. Пришлите сюда @username или ссылку на канал (t.me/…), его ID — "
    "или перешлите любой пост из канала.\n\n"
    "Можно проще: нажмите «📢 Выбрать канал» внизу — Telegram покажет ваши каналы и сам выдаст "
    "боту нужные права."
)
SLOTS_EXAMPLE = "<code>пн-пт 09:00, 18:00</code>\n<code>сб, вс 12:00</code>"
DIALOG_STATES = (ChannelStates.slots.state, ChannelStates.folder_name.state, ChannelStates.folder_slots.state)
# Long channel lists (finding #18): the text lists about this many characters, the buttons are paged.
LIST_BUDGET = 3000
MORE_BUTTONS = "<i>…и ещё {n} — выберите кнопками ниже</i>"
MORE_SECTIONS = "<i>…список сокращён — выберите кнопками ниже</i>"  # sections + headers: no exact count


def _slots(channel: dict) -> dict:
    try:
        return normalize_slots(channel.get("slots_json"))
    except PlanError:
        return {}


async def _leave_dialog(state: FSMContext) -> None:
    """Navigating away from a schedule / folder name / time prompt cancels it."""
    await drop_prompt(state)


# ------------------------------------------------------------------ rendering

async def channels_screen(db: Any, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Folders and channels outside folders. Long lists are cut in the text and the channel
    buttons are paged, so the screen works with any number of channels (finding #18)."""
    groups = await folder_service.grouped_channels(db, "all")
    folders = [g for g in groups if g["folder"] is not None]
    loose = next((g["channels"] for g in groups if g["folder"] is None), [])
    all_channels = [ch for g in groups for ch in g["channels"]]
    by_status: dict[str, list[dict]] = {}
    for row in loose:
        by_status.setdefault(row["status"], []).append(row)
    active = by_status.get(CH_ACTIVE, [])
    pending = by_status.get(CH_PENDING, [])
    left = by_status.get(CH_LEFT, [])
    ignored = by_status.get(CH_IGNORED, [])

    lines = ["📢 <b>Каналы</b>", ""]
    if not all_channels and not folders:
        lines += [
            "Пока нет ни одного канала.",
            "",
            "Чтобы подключить канал:",
            "1. Добавьте бота в администраторы канала с правом «Публикация сообщений».",
            "2. Я пришлю уведомление — нажмите «✅ Добавить канал».",
            "",
            "Если бот уже администратор — нажмите «➕ Добавить канал» ниже.",
        ]
    body: list[str] = []
    if folders:
        body.append(f"📁 <b>Папки</b> ({len(folders)}):")
        for group in folders:
            n = group["folder"]["channels_count"]
            body.append(f"• {esc(group['folder']['name'])} — {n} {plural(n, 'канал', 'канала', 'каналов')}")
        body.append("")
    if active:
        title = "Без папки" if folders else "Подключены"
        body.append(f"✅ <b>{title}</b> ({len(active)}):")
        for ch in active:
            username = f" (@{esc(ch['username'])})" if ch.get("username") else ""
            body.append(f"• {esc(channel_title(ch))}{username} — 👥 {fmt_num(ch['members_count'])}")
    if pending:
        body += ["", f"⏳ <b>Ждут подтверждения</b> ({len(pending)}):"]
        body += [f"• {esc(channel_title(ch))}" for ch in pending]
    if left:
        body += ["", f"⚠️ <b>Нет доступа</b> ({len(left)}):"]
        body += [f"• {esc(channel_title(ch))} — бот больше не администратор" for ch in left]
    lines += cut_lines(body, LIST_BUDGET, more=MORE_SECTIONS)
    if ignored:
        lines += ["", f"🚫 Проигнорировано: {len(ignored)}"]
    if all_channels:
        lines += ["", "Нажмите на папку или канал."]

    items: list[list] = []
    for group in folders:
        folder = group["folder"]
        label = f"📁 {clip(folder['name'], 40)} · {folder['channels_count']}"
        items.append([ib(label[:64], FolderCb(action="open", folder_id=folder["id"]))])
    for ch in active:
        label = f"📢 {clip(channel_title(ch), 45)} · 👥 {fmt_num(ch['members_count'])}"
        items.append([ib(label[:64], ChannelCb(action="card", chat_id=ch["id"]))])
    items += pending_rows(pending)
    for ch in left:
        items.append([ib(f"⚠️ {clip(channel_title(ch), 55)}"[:64], ChannelCb(action="card", chat_id=ch["id"]))])
    for ch in ignored:
        items.append([ib(f"🚫 {clip(channel_title(ch), 55)}"[:64], ChannelCb(action="card", chat_id=ch["id"]))])
    buttons, page, pages = paginate(items, page)
    if pages > 1:
        lines.append(f"Страница {page + 1} из {pages}")
    buttons.append(page_nav(page, pages, lambda p: ChannelPageCb(page=p)))
    buttons.append([ib("➕ Добавить канал", ChannelCb(action="add"), style="primary"),
                    ib("➕ Новая папка", FolderCb(action="new"))])
    return "\n".join(lines), kb(*buttons)


def _flag(value: Any) -> str:
    return "✅" if value else "❌"


async def channel_card(db: Any, channel: dict, tz: Any, *, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    chat_id = channel["id"]
    folder = await db.get_folder(channel["folder_id"]) if channel.get("folder_id") else None
    slots = _slots(channel)
    lines = [f"📢 <b>{esc(channel_title(channel))}</b>"]
    ident = f"ID <code>{chat_id}</code>"
    if channel.get("username"):
        ident = f"@{esc(channel['username'])} · {ident}"
    lines.append(ident)
    lines.append(f"Статус: {STATUS_LABELS.get(channel['status'], esc(channel['status']))}")
    lines.append(f"👥 Подписчиков: {fmt_num(channel.get('members_count'))}")
    lines.append(
        f"Права бота: {_flag(channel.get('can_post'))} публикация · "
        f"{_flag(channel.get('can_edit'))} редактирование · {_flag(channel.get('can_delete'))} удаление"
    )
    lines.append(f"📁 Папка: {esc(folder['name']) if folder else 'без папки'}")
    lines.append(f"📅 Расписание: {esc(describe_slots(slots))}")
    if channel.get("added_at") and channel["status"] == CH_ACTIVE:
        added = datetime.fromtimestamp(channel["added_at"], tz).strftime("%d.%m.%Y %H:%M")
        lines.append(f"Подключён: {added}")
    if channel["status"] == CH_ACTIVE and not channel.get("can_edit"):
        lines.append("\n⚠️ Без права «Редактирование» бот не сможет изменять опубликованные посты.")
    if note:
        lines += ["", note]

    status = channel["status"]
    rows: list[list] = []
    folder_label = f"📁 Папка: {clip(folder['name'], 30) if folder else 'без папки'}"
    if status == CH_ACTIVE:
        rows.append([ib("🔄 Обновить", ChannelCb(action="refresh", chat_id=chat_id)),
                     ib("📊 Статистика", StatsCb(action="ch", chat_id=chat_id, days=30))])
        rows.append([ib(f"📅 Расписание: {clip(describe_slots(slots), 45)}"[:64],
                        ChannelCb(action="slots", chat_id=chat_id))])
        rows.append([ib(folder_label[:64], ChannelCb(action="folder", chat_id=chat_id)),
                     ib("🗓 Контент-план", PlanCb(action="ch", chat_id=chat_id))])
        rows.append([ib("🗑 Удалить из бота", ChannelCb(action="remove", chat_id=chat_id), style="danger")])
    elif status == CH_PENDING:
        rows.append([ib("✅ Добавить", ChannelCb(action="approve", chat_id=chat_id), style="success"),
                     ib("🚫 Игнорировать", ChannelCb(action="ignore", chat_id=chat_id))])
    elif status == CH_LEFT:
        rows.append([ib("🔄 Проверить снова", ChannelCb(action="refresh", chat_id=chat_id)),
                     ib("🗑 Удалить", ChannelCb(action="remove", chat_id=chat_id), style="danger")])
        rows.append([ib(folder_label[:64], ChannelCb(action="folder", chat_id=chat_id))])
    else:
        rows.append([ib("✅ Добавить", ChannelCb(action="approve", chat_id=chat_id), style="success"),
                     ib("🗑 Удалить", ChannelCb(action="remove", chat_id=chat_id), style="danger")])
    if channel.get("username"):
        rows.append([url_button("↗️ Открыть канал", f"https://t.me/{channel['username']}")])
    back = [ib("↩️ К списку", ChannelCb(action="list"))]
    if folder:
        back.insert(0, ib(f"↩️ {clip(folder['name'], 20)}", FolderCb(action="open", folder_id=folder["id"])))
    rows.append(back)
    return "\n".join(lines), kb(*rows)


async def folder_screen(db: Any, folder_id: int, *, note: str | None = None,
                        page: int = 0) -> tuple[str, InlineKeyboardMarkup] | None:
    folder = await db.get_folder(folder_id)
    if folder is None:
        return None
    channels = await db.fetchall("SELECT * FROM channels WHERE folder_id = ? ORDER BY title COLLATE NOCASE",
                                 (folder_id,))
    lines = [f"📁 <b>{esc(folder['name'])}</b>", ""]
    if channels:
        summaries = {describe_slots(_slots(ch)) for ch in channels}
        common = summaries.pop() if len(summaries) == 1 else "у каналов разное"
        lines.append(f"📅 Расписание: {esc(common)}")
        lines.append(f"Каналов: {len(channels)}")
        body = []
        for ch in channels:
            icon = STATUS_ICONS.get(ch["status"], "•")
            body.append(f"{icon} {esc(channel_title(ch))} — 👥 {fmt_num(ch.get('members_count'))} · "
                        f"📅 {esc(describe_slots(_slots(ch)))}")
        lines += cut_lines(body, LIST_BUDGET, more=MORE_BUTTONS)
    else:
        lines.append("В папке пока нет каналов. Откройте карточку канала и нажмите «📁 Папка», "
                     "чтобы переложить его сюда.")
    fid = folder["id"]
    items = [[ib(f"{STATUS_ICONS.get(ch['status'], '•')} {clip(channel_title(ch), 55)}"[:64],
                 ChannelCb(action="card", chat_id=ch["id"]))] for ch in channels]
    rows, page, pages = paginate(items, page)
    if pages > 1:
        lines.append(f"Страница {page + 1} из {pages}")
    if note:
        lines += ["", note]
    rows.append(page_nav(page, pages, lambda p: FolderPageCb(folder_id=fid, page=p)))
    rows.append([ib("📅 Расписание для всей папки", FolderCb(action="slots", folder_id=fid))])
    rows.append([ib("✏️ Переименовать", FolderCb(action="rename", folder_id=fid)),
                 ib("🗓 Контент-план", PlanCb(action="all", folder_id=fid))])
    rows.append([ib("⬆️ Выше", FolderCb(action="up", folder_id=fid)),
                 ib("⬇️ Ниже", FolderCb(action="down", folder_id=fid))])
    rows.append([ib("🗑 Удалить папку", FolderCb(action="del", folder_id=fid), style="danger")])
    rows.append([ib("↩️ Каналы", ChannelCb(action="list"))])
    return "\n".join(lines), kb(*rows)


def added_text(channel: dict) -> str:
    members = channel.get("members_count")
    suffix = ""
    if members is not None:
        suffix = f" · 👥 {fmt_num(members)} {plural(members, 'подписчик', 'подписчика', 'подписчиков')}"
    return f"✅ Канал «{esc(channel_title(channel))}» добавлен{suffix}"


def after_add_kb() -> InlineKeyboardMarkup:
    return kb([ib("📝 Новый пост", MenuCb(action="new"), style="primary"),
               ib("📢 Каналы", ChannelCb(action="list"))])


# ------------------------------------------------------------------ list & card

async def cmd_channels(message: Message, state: FSMContext, db: Any) -> None:
    await leave_state(message.bot, message.chat.id, state)
    text, markup = await channels_screen(db)
    await message.answer(text, reply_markup=markup)


async def cb_list(callback: CallbackQuery, state: FSMContext, db: Any) -> None:
    await safe_answer(callback)
    if await state.get_state() == ChannelStates.waiting_ref.state:
        await leave_state(callback.bot, callback.from_user.id, state)
    await _leave_dialog(state)
    text, markup = await channels_screen(db)
    await show(callback, text, markup)


async def cb_list_page(callback: CallbackQuery, callback_data: ChannelPageCb, state: FSMContext, db: Any) -> None:
    await safe_answer(callback)
    await _leave_dialog(state)
    await show(callback, *await channels_screen(db, callback_data.page))


async def cb_folder_page(callback: CallbackQuery, callback_data: FolderPageCb, state: FSMContext,
                         db: Any) -> None:
    screen = await folder_screen(db, callback_data.folder_id, page=callback_data.page)
    await _leave_dialog(state)
    if screen is None:
        await safe_answer(callback, "Папка не найдена — возможно, её удалили", alert=True)
        await show(callback, *await channels_screen(db))
        return
    await safe_answer(callback)
    await show(callback, *screen)


async def cb_card(callback: CallbackQuery, callback_data: ChannelCb, state: FSMContext, db: Any,
                  config: Any) -> None:
    channel = await db.get_channel(callback_data.chat_id)
    if channel is None:
        await safe_answer(callback, "Канал не найден", alert=True)
        return
    await _leave_dialog(state)
    await safe_answer(callback)
    await show(callback, *await channel_card(db, channel, config.tz))


async def cb_refresh(callback: CallbackQuery, callback_data: ChannelCb, db: Any, config: Any) -> None:
    try:
        channel = await channel_service.refresh_channel(callback.bot, db, callback_data.chat_id)
    except ChannelError as exc:
        await safe_answer(callback, str(exc), alert=True)
        return
    note = "🔄 Обновлено" if channel["status"] == CH_ACTIVE else "⚠️ Бот не администратор канала"
    await safe_answer(callback, note)
    await show(callback, *await channel_card(db, channel, config.tz))


async def cb_remove(callback: CallbackQuery, callback_data: ChannelCb, db: Any) -> None:
    channel = await db.get_channel(callback_data.chat_id)
    if channel is None:
        await safe_answer(callback, "Канал уже удалён", alert=True)
        return
    await safe_answer(callback)
    text = (
        f"🗑 Удалить канал «{esc(channel_title(channel))}» из бота?\n\n"
        "Бот останется администратором в самом канале, опубликованные посты не пострадают. "
        "Запланированные посты в этот канал опубликованы не будут."
    )
    markup = kb([ib("🗑 Да, удалить", ChannelCb(action="remove_ok", chat_id=channel["id"]), style="danger"),
                 ib("↩️ Отмена", ChannelCb(action="card", chat_id=channel["id"]))])
    await show(callback, text, markup)


async def cb_remove_ok(callback: CallbackQuery, callback_data: ChannelCb, db: Any) -> None:
    channel = await db.get_channel(callback_data.chat_id)
    if channel is not None:
        await db.delete_channel(channel["id"])
        log.info("Channel %s removed by %s", channel["id"], callback.from_user.id)
    await safe_answer(callback, "Канал удалён")
    text, markup = await channels_screen(db)
    await show(callback, text, markup)


# ------------------------------------------------------------------ channel folder

async def cb_folder_picker(callback: CallbackQuery, callback_data: ChannelCb, db: Any) -> None:
    channel = await db.get_channel(callback_data.chat_id)
    if channel is None:
        await safe_answer(callback, "Канал не найден", alert=True)
        return
    await safe_answer(callback)
    folders = await db.list_folders()
    current = channel.get("folder_id")
    rows = []
    for folder in folders:
        mark = "✅ " if folder["id"] == current else ""
        rows.append([ib(f"{mark}📁 {clip(folder['name'], 50)}"[:64],
                        ChannelFolderCb(chat_id=channel["id"], folder_id=folder["id"]))])
    rows.append([ib(f"{'✅ ' if not current else ''}▫️ Без папки", ChannelFolderCb(chat_id=channel["id"], folder_id=0))])
    rows.append([ib("➕ Новая папка", ChannelFolderCb(chat_id=channel["id"], folder_id=-1))])
    rows.append([ib("↩️ Назад", ChannelCb(action="card", chat_id=channel["id"]))])
    text = f"📁 <b>Папка канала «{esc(channel_title(channel))}»</b>\nВыберите папку."
    if not folders:
        text += "\n\nПапок пока нет — создайте первую."
    await show(callback, text, kb(*rows))


async def cb_set_folder(callback: CallbackQuery, callback_data: ChannelFolderCb, state: FSMContext,
                        db: Any, config: Any) -> None:
    channel = await db.get_channel(callback_data.chat_id)
    if channel is None:
        await safe_answer(callback, "Канал не найден", alert=True)
        return
    if callback_data.folder_id == -1:
        await safe_answer(callback)
        await _ask_folder_name(callback, state, folder=None, assign=channel["id"])
        return
    try:
        channel = await folder_service.set_channel_folder(db, channel["id"], callback_data.folder_id or None)
    except FolderError as exc:
        await safe_answer(callback, str(exc), alert=True)
        return
    folder = await db.get_folder(channel["folder_id"]) if channel.get("folder_id") else None
    await safe_answer(callback, f"Канал перемещён в «{folder['name']}»" if folder else "Канал без папки")
    await show(callback, *await channel_card(db, channel, config.tz))


# ------------------------------------------------------------------ channel schedule

def _slots_prompt(title: str, slots: dict, back: Any, clear: Any, *, whole_folder: bool = False,
                  mixed: bool = False, has_any: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Schedule prompt of a channel, or of a whole folder (`mixed`: its channels have different
    schedules, `has_any`: at least one has a schedule — findings #12/#16)."""
    lines = [f"📅 <b>Расписание публикаций {'папки' if whole_folder else 'канала'} «{esc(title)}»</b>"]
    if whole_folder:
        lines.append("Расписание будет задано сразу всем каналам папки.")
    text = slots_to_text(slots)
    if text:
        lines += ["", "Сейчас (нажмите, чтобы скопировать):", f"<pre>{esc(text)}</pre>"]
    elif mixed:
        lines += ["", "Сейчас у каналов папки разные расписания — новое заменит их все."]
    else:
        lines += ["", "Сейчас расписания нет."]
    lines += [
        "",
        "Пришлите расписание одним сообщением — по строке на правило, например:",
        SLOTS_EXAMPLE,
        "",
        esc(SLOTS_HELP),
        "",
        "Посты, поставленные «в очередь», выходят в ближайшее свободное время по расписанию. Уже "
        "запланированные посты сохранят своё время — «🧹 Уплотнить очередь» в контент-плане "
        "перераспределит их.",
    ]
    rows = []
    if text or (whole_folder and has_any):
        rows.append([ib("🧹 Очистить", clear, style="danger")])
    rows.append([ib("↩️ Назад", back)])
    return "\n".join(lines), kb(*rows)


async def open_slots_editor(callback: CallbackQuery, state: FSMContext, db: Any, chat_id: int, *,
                            back: str = "card") -> None:
    """Show the schedule prompt of a channel and wait for the slots text (`back`: card | plan)."""
    channel = await db.get_channel(chat_id)
    if channel is None:
        await safe_answer(callback, "Канал не найден", alert=True)
        return
    await safe_answer(callback)
    await leave_state(callback.bot, callback.from_user.id, state)
    await state.set_state(ChannelStates.slots)
    back_cb = PlanCb(action="ch", chat_id=chat_id) if back == "plan" else ChannelCb(action="card", chat_id=chat_id)
    text, markup = _slots_prompt(channel_title(channel), _slots(channel), back_cb,
                                 ChannelCb(action="slots_clear", chat_id=chat_id))
    message_id = await show(callback, text, markup)
    await state.set_data({"chat_id": chat_id, "msg_id": message_id, "back": back})


async def cb_slots(callback: CallbackQuery, callback_data: ChannelCb, state: FSMContext, db: Any) -> None:
    await open_slots_editor(callback, state, db, callback_data.chat_id)


async def _after_slots(bot: Any, db: Any, config: Any, chat_id: int, message_id: int | None, back: str,
                       user_chat: int, note: str) -> None:
    channel = await db.get_channel(chat_id)
    if channel is None:
        return
    if back == "plan":
        from app.bot.handlers.plan import channel_plan_screen

        text, markup = await channel_plan_screen(db, config, chat_id, note=note)
    else:
        text, markup = await channel_card(db, channel, config.tz, note=note)
    await edit_or_send(bot, user_chat, message_id, text, markup)


async def on_slots_text(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    data = await state.get_data()
    chat_id = data.get("chat_id")
    if not chat_id or await db.get_channel(chat_id) is None:
        await state.clear()
        await message.answer("Канал не найден — возможно, его удалили.", reply_markup=main_menu())
        return
    try:
        slots = parse_slots(message.text or "")
    except PlanError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nИсправьте и пришлите расписание ещё раз или нажмите «↩️ Назад».")
        return
    if not slots:
        await message.answer("Не нашёл ни одного времени. Например: <code>пн-пт 09:00, 18:00</code>")
        return
    await db.set_channel_slots(chat_id, slots)
    await state.clear()
    log.info("Slots of channel %s set by %s: %s", chat_id, message.from_user.id, slots)
    await _after_slots(message.bot, db, config, chat_id, data.get("msg_id"), data.get("back", "card"),
                       message.chat.id, f"✅ Расписание сохранено: {esc(describe_slots(slots))}")


async def cb_slots_clear(callback: CallbackQuery, callback_data: ChannelCb, state: FSMContext, db: Any,
                         config: Any) -> None:
    channel = await db.get_channel(callback_data.chat_id)
    if channel is None:
        await safe_answer(callback, "Канал не найден", alert=True)
        return
    data = await state.get_data()
    back = data.get("back", "card") if await state.get_state() == ChannelStates.slots.state else "card"
    await _leave_dialog(state)
    await db.set_channel_slots(channel["id"], None)
    await safe_answer(callback, "Расписание очищено")
    await _after_slots(callback.bot, db, config, channel["id"], callback_message_id(callback), back,
                       callback.from_user.id, "🧹 Расписание очищено — в очередь этого канала посты больше не встанут.")


# ------------------------------------------------------------------ folders

async def _ask_folder_name(callback: CallbackQuery, state: FSMContext, *, folder: dict | None,
                           assign: int | None = None) -> None:
    await leave_state(callback.bot, callback.from_user.id, state)
    await state.set_state(ChannelStates.folder_name)
    if folder is None:
        text = (f"📁 <b>Новая папка</b>\nПришлите название папки (до {folder_service.MAX_NAME_LENGTH} символов).")
        back = ChannelCb(action="card", chat_id=assign) if assign else ChannelCb(action="list")
    else:
        text = (f"✏️ <b>Переименовать папку «{esc(folder['name'])}»</b>\n"
                f"Пришлите новое название (до {folder_service.MAX_NAME_LENGTH} символов).")
        back = FolderCb(action="open", folder_id=folder["id"])
    message_id = await show(callback, text, kb([ib("↩️ Назад", back)]))
    await state.set_data({"folder_id": folder["id"] if folder else None, "assign": assign, "msg_id": message_id})


async def on_folder_name(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    data = await state.get_data()
    try:
        if data.get("folder_id"):
            folder = await folder_service.rename_folder(db, data["folder_id"], message.text or "")
            note = "✅ Папка переименована"
        else:
            folder = await folder_service.create_folder(db, message.text or "")
            note = "✅ Папка создана"
    except FolderError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nПришлите другое название или нажмите «↩️ Назад».")
        return
    await state.clear()
    assign = data.get("assign")
    if assign and await db.get_channel(assign) is not None:
        channel = await folder_service.set_channel_folder(db, assign, folder["id"])
        text, markup = await channel_card(db, channel, config.tz,
                                          note=f"{note}, канал перемещён в «{esc(folder['name'])}».")
    else:
        text, markup = await folder_screen(db, folder["id"], note=note)
    await edit_or_send(message.bot, message.chat.id, data.get("msg_id"), text, markup)


async def cb_folder(callback: CallbackQuery, callback_data: FolderCb, state: FSMContext, db: Any,
                    config: Any) -> None:
    action = callback_data.action
    if action == "new":
        await safe_answer(callback)
        await _ask_folder_name(callback, state, folder=None)
        return
    folder = await db.get_folder(callback_data.folder_id)
    if folder is None:
        await safe_answer(callback, "Папка не найдена — возможно, её удалили", alert=True)
        await _leave_dialog(state)
        await show(callback, *await channels_screen(db))
        return
    fid = folder["id"]
    if action == "rename":
        await safe_answer(callback)
        await _ask_folder_name(callback, state, folder=folder)
        return
    if action == "slots":
        await safe_answer(callback)
        await leave_state(callback.bot, callback.from_user.id, state)
        await state.set_state(ChannelStates.folder_slots)
        channels = await db.fetchall("SELECT * FROM channels WHERE folder_id = ?", (fid,))
        variants = {slots_to_text(_slots(ch)) for ch in channels}
        current = _slots(channels[0]) if channels and len(variants) == 1 else {}
        text, markup = _slots_prompt(folder["name"], current, FolderCb(action="open", folder_id=fid),
                                     FolderCb(action="slots_clear", folder_id=fid), whole_folder=True,
                                     mixed=len(variants) > 1, has_any=any(_slots(ch) for ch in channels))
        message_id = await show(callback, text, markup)
        await state.set_data({"folder_id": fid, "msg_id": message_id})
        return
    await _leave_dialog(state)
    note = None
    if action == "slots_clear":
        # Every channel of the folder loses its own schedule, which cannot be restored: ask first.
        prompt = await _folder_clear_prompt(db, folder)
        if prompt is None:
            await safe_answer(callback, "У каналов папки нет расписания — очищать нечего")
        else:
            await safe_answer(callback)
            await show(callback, *prompt)
            return
    if action == "slots_clear_ok":
        count = await folder_service.apply_slots_to_folder(db, fid, {})
        log.info("Slots of folder %s cleared by %s", fid, callback.from_user.id)
        await safe_answer(callback, "Расписание очищено")
        note = f"🧹 Расписание очищено у {count} {plural(count, 'канала', 'каналов', 'каналов')}."
    elif action in ("up", "down"):
        await folder_service.move_folder(db, fid, -1 if action == "up" else 1)
        await safe_answer(callback, "Порядок папок изменён")
    elif action == "del":
        await safe_answer(callback)
        row = await db.fetchone("SELECT COUNT(*) AS n FROM channels WHERE folder_id = ?", (fid,))
        n = int(row["n"]) if row else 0
        await show(callback,
                   f"🗑 Удалить папку «{esc(folder['name'])}»?\nКаналы останутся в боте — они переместятся "
                   f"в «Без папки»." + (f" Каналов в папке: {n}." if n else ""),
                   kb([ib("🗑 Да, удалить", FolderCb(action="del_ok", folder_id=fid), style="danger"),
                       ib("↩️ Назад", FolderCb(action="open", folder_id=fid))]))
        return
    elif action == "del_ok":
        await folder_service.delete_folder(db, fid)
        log.info("Folder %s deleted by %s", fid, callback.from_user.id)
        await safe_answer(callback, "Папка удалена")
        await show(callback, *await channels_screen(db))
        return
    else:
        await safe_answer(callback)
    screen = await folder_screen(db, fid, note=note)
    if screen is not None:
        await show(callback, *screen)


async def _folder_clear_prompt(db: Any, folder: dict) -> tuple[str, InlineKeyboardMarkup] | None:
    """«🧹 Очистить» of a folder schedule: what will be lost, and «Да» / «Назад» (None: no channel
    of the folder has a schedule)."""
    fid = folder["id"]
    channels = await db.fetchall("SELECT * FROM channels WHERE folder_id = ? ORDER BY title COLLATE NOCASE", (fid,))
    with_slots = [ch for ch in channels if _slots(ch)]
    n = len(with_slots)
    if not n:
        return None
    lines = [f"🧹 <b>Очистить расписание у {n} {plural(n, 'канала', 'каналов', 'каналов')} папки "
             f"«{esc(folder['name'])}»?</b>", ""]
    lines += cut_lines([f"• {esc(channel_title(ch))} — {esc(describe_slots(_slots(ch)))}" for ch in with_slots],
                       2500, more="<i>…и ещё {n}</i>")
    lines += ["", "Уже запланированные посты сохранят своё время, но новые посты «в очередь» в эти каналы "
                  "не встанут, пока расписание не задано снова. Восстановить его нельзя."]
    markup = kb([ib("🧹 Да, очистить", FolderCb(action="slots_clear_ok", folder_id=fid), style="danger"),
                 ib("↩️ Назад", FolderCb(action="slots", folder_id=fid))])
    return "\n".join(lines), markup


async def on_folder_slots_text(message: Message, state: FSMContext, db: Any) -> None:
    data = await state.get_data()
    folder_id = data.get("folder_id")
    try:
        slots = parse_slots(message.text or "")
        if not slots:
            raise FolderError("Не нашёл ни одного времени. Например: пн-пт 09:00, 18:00")
        count = await folder_service.apply_slots_to_folder(db, folder_id, slots)
    except (PlanError, FolderError) as exc:
        if str(exc) == folder_service.NOT_FOUND_ERROR:
            await state.clear()
            await message.answer("Папка не найдена — возможно, её удалили.", reply_markup=main_menu())
            return
        await message.answer(f"❌ {esc(exc)}\n\nИсправьте и пришлите расписание ещё раз или нажмите «↩️ Назад».")
        return
    await state.clear()
    log.info("Slots of folder %s set by %s: %s", folder_id, message.from_user.id, slots)
    screen = await folder_screen(db, folder_id, note=(
        f"✅ Расписание «{esc(describe_slots(slots))}» задано для {count} "
        f"{plural(count, 'канала', 'каналов', 'каналов')}."))
    if screen is not None:
        await edit_or_send(message.bot, message.chat.id, data.get("msg_id"), *screen)


# ------------------------------------------------------------------ approve / ignore

async def add_channel_locked(bot: Any, db: Any, ref: str | int, added_by: int) -> dict:
    """channels.add_channel, serialized with my_chat_member updates of the same channel."""
    if isinstance(ref, int):
        async with channel_lock(ref):
            return await channel_service.add_channel(bot, db, ref, added_by=added_by)
    return await channel_service.add_channel(bot, db, ref, added_by=added_by)


async def cb_approve(callback: CallbackQuery, callback_data: ChannelCb, db: Any) -> None:
    try:
        channel = await add_channel_locked(callback.bot, db, callback_data.chat_id, callback.from_user.id)
    except ChannelError as exc:
        await safe_answer(callback, str(exc), alert=True)
        return
    await safe_answer(callback, "Канал добавлен")
    await show(callback, added_text(channel), after_add_kb())


async def cb_ignore(callback: CallbackQuery, callback_data: ChannelCb, db: Any) -> None:
    channel = await db.get_channel(callback_data.chat_id)
    if channel is None:
        await safe_answer(callback, "Канал не найден", alert=True)
        return
    title = esc(channel_title(channel))
    if channel["status"] == CH_ACTIVE:
        # A stale notification must not hide a channel somebody already approved.
        await safe_answer(callback, "Канал уже подключён")
        await show(callback, f"✅ Канал «{title}» уже подключён.")
        return
    await db.set_channel_status(channel["id"], CH_IGNORED)
    await safe_answer(callback, "Канал проигнорирован")
    await show(callback, f"🚫 Канал «{title}» проигнорирован. Подключить его можно позже в «📢 Каналы».",
               kb([ib("📢 Каналы", ChannelCb(action="list"))]))


# ------------------------------------------------------------------ adding

async def start_add(bot: Any, chat_id: int, state: FSMContext) -> None:
    """Enter the «add channel» dialog (prompt + reply keyboard with the channel picker)."""
    if await state.get_state() == ChannelStates.waiting_ref.state:
        await state.clear()
    else:
        await leave_state(bot, chat_id, state)
    await state.set_state(ChannelStates.waiting_ref)
    try:
        await bot.send_message(chat_id=chat_id, text=ADD_PROMPT, reply_markup=channel_request_menu())
    except TelegramBadRequest:
        # An old client / server rejecting the native picker must not block adding channels.
        log.warning("Channel picker keyboard rejected, falling back to text input", exc_info=True)
        await bot.send_message(chat_id=chat_id, text=ADD_PROMPT.rsplit("\n\n", 1)[0],
                               reply_markup=kb([ib("❌ Отмена", MenuCb(action="cancel"))]))


async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await start_add(callback.bot, callback.from_user.id, state)


async def _add_and_report(message: Message, state: FSMContext, db: Any, ref: str | int) -> None:
    try:
        channel = await add_channel_locked(message.bot, db, ref, message.from_user.id)
    except ChannelError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nПопробуйте ещё раз или нажмите «❌ Отмена».")
        return
    await state.clear()
    await message.answer(added_text(channel) + "\n\nТеперь можно создать пост: «📝 Новый пост».",
                         reply_markup=main_menu())


async def on_channel_ref(message: Message, state: FSMContext, db: Any) -> None:
    """Text in the «add channel» dialog: @username / link / id."""
    await _add_and_report(message, state, db, message.text.strip())


def _album_seen(message: Message, albums: Any) -> bool:
    """True for the 2nd..nth item of an album: a dialog answers an album once (finding #19)."""
    return bool(message.media_group_id) and albums is not None and albums.seen(message)


async def on_forwarded(message: Message, state: FSMContext, db: Any, albums: Any = None) -> None:
    if _album_seen(message, albums):
        return  # a forwarded album post: its first item adds the channel, the rest stay silent
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        await message.answer("Это пересылка не из канала. Перешлите пост именно из канала "
                             "или пришлите его @username.")
        return
    await _add_and_report(message, state, db, origin.chat.id)


async def on_chat_shared(message: Message, state: FSMContext, db: Any) -> None:
    """A channel picked with the native «📢 Выбрать канал» button."""
    shared = message.chat_shared
    if shared is None or shared.request_id != CHANNEL_REQUEST_ID:
        return
    await _add_and_report(message, state, db, shared.chat_id)


async def on_other_input(message: Message, albums: Any = None) -> None:
    if _album_seen(message, albums):
        return
    await message.answer("Пришлите @username или ссылку на канал, его ID или перешлите пост из канала. "
                         "Для отмены — «❌ Отмена».")


async def on_dialog_other(message: Message, albums: Any = None) -> None:
    if _album_seen(message, albums):
        return
    await message.answer("Сейчас я жду текст. Или нажмите «↩️ Назад».")


# ------------------------------------------------------------------ router

def create_router() -> Router:
    router = Router(name="channels")
    router.message.register(cmd_channels, Command("channels"))
    router.message.register(cmd_channels, TextIs(BTN_CHANNELS))
    router.message.register(on_chat_shared, F.chat_shared)

    waiting = StateFilter(ChannelStates.waiting_ref)
    router.message.register(on_forwarded, waiting, F.forward_origin)
    router.message.register(on_channel_ref, waiting, F.text, UserInput())
    router.message.register(on_other_input, waiting, UserInput())
    router.message.register(on_slots_text, StateFilter(ChannelStates.slots), F.text, UserInput())
    router.message.register(on_folder_name, StateFilter(ChannelStates.folder_name), F.text, UserInput())
    router.message.register(on_folder_slots_text, StateFilter(ChannelStates.folder_slots), F.text, UserInput())
    router.message.register(on_dialog_other, StateFilter(*DIALOG_STATES), UserInput())

    cb = router.callback_query
    cb.register(cb_list, MenuCb.filter(F.action == "channels"))
    cb.register(cb_add, MenuCb.filter(F.action == "add_channel"))
    cb.register(cb_list, ChannelCb.filter(F.action == "list"))
    cb.register(cb_list_page, ChannelPageCb.filter())
    cb.register(cb_folder_page, FolderPageCb.filter())
    cb.register(cb_card, ChannelCb.filter(F.action == "card"))
    cb.register(cb_refresh, ChannelCb.filter(F.action == "refresh"))
    cb.register(cb_remove, ChannelCb.filter(F.action == "remove"))
    cb.register(cb_remove_ok, ChannelCb.filter(F.action == "remove_ok"))
    cb.register(cb_approve, ChannelCb.filter(F.action == "approve"))
    cb.register(cb_ignore, ChannelCb.filter(F.action == "ignore"))
    cb.register(cb_add, ChannelCb.filter(F.action == "add"))
    cb.register(cb_folder_picker, ChannelCb.filter(F.action == "folder"))
    cb.register(cb_slots, ChannelCb.filter(F.action == "slots"))
    cb.register(cb_slots_clear, ChannelCb.filter(F.action == "slots_clear"))
    cb.register(cb_set_folder, ChannelFolderCb.filter())
    cb.register(cb_folder, FolderCb.filter())
    return router

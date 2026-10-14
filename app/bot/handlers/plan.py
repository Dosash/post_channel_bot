"""«📅 Контент-план»: per-channel publication queues, the cross-channel timeline and plan items.

Screens: channel picker grouped by folders (+ «Все каналы» timeline) → channel plan (the next
7 days as text, the nearest queued posts as buttons, schedule editor, queue compaction) → plan
item (one post in one channel: move earlier / later, another time, publish now, take out of the
plan, edit). Every change goes through `app.services.plan`, which refuses to touch a post that
is being published.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import ChannelCb, MenuCb, PlanCb, PlanPageCb
from app.bot.filters import TextIs, UserInput
from app.bot.handlers import compose
from app.bot.handlers.common import drop_prompt, leave_state
from app.bot.keyboards import BTN_PLAN, SCHEDULE_PRESETS, ib, kb, page_nav, paginate
from app.bot.states import PlanStates
from app.bot.utils import (
    callback_chat_id,
    channel_title,
    clip,
    cut_lines,
    edit_or_send,
    esc,
    plural,
    safe_answer,
    show,
    tz_name,
)
from app.buttons import count_buttons
from app.db import CH_ACTIVE, POST_DRAFT, TARGET_PENDING, TARGET_SENT, now
from app.markdown import excerpt
from app.media import MEDIA_ICONS, describe_media, post_media
from app.services import folders as folder_service
from app.services import plan as plan_service
from app.services.plan import PlanError, slot_label
from app.services.posting import PostError, publish_post, send_preview
from app.services.scheduler import format_dt, parse_when

log = logging.getLogger(__name__)

PLAN_DAYS = 7
DETAIL_DAYS = 2  # free slots are listed one by one only for today and tomorrow
MAX_ITEM_BUTTONS = 10
INLINE_CHANNELS_LIMIT = 40  # more active channels → folders open their own screen
NO_FOLDER = -1  # PlanCb.folder_id of the «Без папки» group


def _icon(summary: dict) -> str:
    kind = summary.get("media_kind") or "text"
    return MEDIA_ICONS.get(kind, "") + " " if kind != "text" else ""


def _item_label(summary: dict, limit: int = 40) -> str:
    return f"{_icon(summary)}{clip(summary.get('excerpt') or '', limit)}"


def _day_title(day: str, weekday: str, tz: Any) -> str:
    moment = datetime.fromisoformat(day)
    today = datetime.now(tz).date()
    suffix = {0: " · сегодня", 1: " · завтра"}.get((moment.date() - today).days, "")
    return f"<b>{weekday} {moment:%d.%m}</b>{suffix}"


async def _leave_dialog(state: FSMContext) -> None:
    await drop_prompt(state)


# ------------------------------------------------------------------ home

async def _queued_counts(db: Any) -> tuple[dict[int, int], list[dict]]:
    rows = await db.get_scheduled_targets(from_ts=now())
    counts: dict[int, int] = {}
    for row in rows:
        counts[row["channel_id"]] = counts.get(row["channel_id"], 0) + 1
    return counts, rows


def _channel_button(channel: dict, counts: dict[int, int], prefix: str = "") -> list:
    n = counts.get(channel["id"], 0)
    suffix = f" · ⏰ {n}" if n else ("" if channel.get("slots_json") else " · ⚙️ нет расписания")
    label = f"{prefix}📢 {clip(channel_title(channel), 40)}{suffix}"
    return [ib(label[:64], PlanCb(action="ch", chat_id=channel["id"]))]


async def plan_home(db: Any, config: Any, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    groups = [g for g in await folder_service.grouped_channels(db, ("active",)) if g["channels"]]
    channels = [ch for g in groups for ch in g["channels"]]
    lines = ["📅 <b>Контент-план</b>", ""]
    if not channels:
        lines.append("Подключите канал, чтобы планировать публикации.")
        return "\n".join(lines), kb([ib("➕ Добавить канал", ChannelCb(action="add"), style="primary")])
    counts, rows = await _queued_counts(db)
    if rows:
        first = rows[0]
        lines.append(f"Запланировано публикаций: {len(rows)}")
        lines.append(f"Ближайшая: {slot_label(first['scheduled_at'], config.tz)} · "
                     f"{esc(first.get('channel_title') or first['channel_id'])}")
    else:
        lines.append("Пока ничего не запланировано.")
    no_slots = [ch for ch in channels if not ch.get("slots_json")]
    if no_slots:
        lines.append(f"Без расписания: {len(no_slots)} {plural(len(no_slots), 'канал', 'канала', 'каналов')} — "
                     "задайте его в плане канала («⚙️ Расписание»).")
    lines += ["", "Выберите канал — его очередь публикаций, или «Все каналы» — общую ленту на 7 дней."]

    items = []
    with_folders = any(g["folder"] is not None for g in groups)
    inline = len(channels) <= INLINE_CHANNELS_LIMIT
    for group in groups:
        folder = group["folder"]
        if with_folders:
            fid = folder["id"] if folder else NO_FOLDER
            name = f"📁 {clip(folder['name'], 40)}" if folder else "📂 Без папки"
            action = "all" if inline else "folder"
            items.append([ib(f"{name} · {len(group['channels'])}"[:64], PlanCb(action=action, folder_id=fid))])
        if inline or not with_folders:
            items += [_channel_button(ch, counts, "↳ " if with_folders else "") for ch in group["channels"]]
    # Many channels without folders (or very many folders): the buttons are paged (finding #18).
    rows, page, pages = paginate(items, page)
    return "\n".join(lines), kb([ib("🗓 Все каналы — 7 дней", PlanCb(action="all"), style="primary")], *rows,
                                page_nav(page, pages, lambda p: PlanPageCb(folder_id=0, page=p)))


async def folder_plan_screen(db: Any, folder_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    wanted = None if folder_id == NO_FOLDER else folder_id
    group = next((g for g in await folder_service.grouped_channels(db, ("active",))
                  if (g["folder"]["id"] if g["folder"] else None) == wanted), None)
    channels = group["channels"] if group else []
    name = group["folder"]["name"] if group and group["folder"] else "Без папки"
    counts, _ = await _queued_counts(db)
    text = f"📅 <b>Контент-план · {esc(name)}</b>\nВыберите канал."
    rows, page, pages = paginate([_channel_button(ch, counts) for ch in channels], page)
    return text, kb([ib("🗓 Лента папки — 7 дней", PlanCb(action="all", folder_id=folder_id), style="primary")],
                    *rows, page_nav(page, pages, lambda p: PlanPageCb(folder_id=folder_id, page=p)),
                    [ib("◀️ Каналы", PlanCb(action="home"))])


# ------------------------------------------------------------------ channel plan

async def channel_plan_screen(db: Any, config: Any, chat_id: int, *,
                              note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    tz = config.tz
    data = await plan_service.channel_plan(db, chat_id, tz, days=PLAN_DAYS)
    channel = data["channel"]
    current = now()
    queued = await db.get_scheduled_targets(channel_ids=[chat_id])
    upcoming = [row for row in queued if row["scheduled_at"] > current - 3600]

    lines = [f"🗓 <b>Контент-план: {esc(channel_title(channel))}</b>"]
    if data["slots"]:
        lines.append(f"📅 Расписание: {esc(data['slots_summary'])}")
    else:
        lines.append("📅 Расписание: не настроено — посты можно ставить только ко времени. "
                     "Нажмите «⚙️ Расписание».")
    if upcoming:
        lines.append(f"В очереди: {len(upcoming)} · ближайшая — {slot_label(upcoming[0]['scheduled_at'], tz)}")
    else:
        lines.append("В очереди пусто.")
    if channel["status"] != CH_ACTIVE:
        lines.append("⚠️ Канал не подключён — запланированные посты в него не выйдут.")
    lines.append("")

    body: list[str] = []
    empty_run: list[tuple[str, int]] = []  # (label, free slots) of consecutive days without posts
    explicit = False

    def flush_empty() -> None:
        if not empty_run:
            return
        first, last = empty_run[0][0], empty_run[-1][0]
        span = first if first == last else f"{first} – {last}"
        free = sum(n for _, n in empty_run)
        body.append(f"<i>{span}: " + (f"свободных слотов {free}" if free else "ничего") + "</i>")
        empty_run.clear()

    for index, day in enumerate(data["days"]):
        items = day["items"]
        posts = [it for it in items if it["post"]]
        free = [it for it in items if not it["post"]]
        short = f"{day['weekday']} {datetime.fromisoformat(day['date']):%d.%m}"
        if not posts and (index >= DETAIL_DAYS or not free):
            empty_run.append((short, len(free)))
            continue
        flush_empty()
        header = _day_title(day["date"], day["weekday"], tz)
        if index >= DETAIL_DAYS and free:
            header += f" <i>(свободно ещё {len(free)})</i>"
        body.append(header)
        for item in items:
            if item["post"]:
                summary = item["post"]
                mark = ""
                if not summary.get("queued"):
                    mark, explicit = " ⏱", True
                if item["ts"] <= current:
                    body.append(f"{item['time']}{mark} — ⏳ публикуется: {esc(_item_label(summary))}")
                else:
                    body.append(f"{item['time']}{mark} — {esc(_item_label(summary))}")
            elif index < DETAIL_DAYS:
                body.append(f"{item['time']} — свободно")
    flush_empty()
    last_day = datetime.fromisoformat(data["days"][-1]["date"])
    horizon = int((datetime(last_day.year, last_day.month, last_day.day, tzinfo=tz) + timedelta(days=1)).timestamp())
    later = [row for row in upcoming if row["scheduled_at"] >= horizon]
    if later:
        body.append(f"\nДальше: ещё {len(later)} {plural(len(later), 'пост', 'поста', 'постов')} "
                    f"(до {format_dt(later[-1]['scheduled_at'], tz)[:5]})")
    if explicit:
        body.append("<i>⏱ — точное время, не по расписанию</i>")
    lines += cut_lines(body)
    if note:
        lines += ["", note]

    rows = []
    for row in upcoming[:MAX_ITEM_BUTTONS]:
        summary = plan_service.post_summary(row, queued=row["queued"])
        label = f"{slot_label(row['scheduled_at'], tz)} · {_item_label(summary, 30)}"
        rows.append([ib(label[:64], PlanCb(action="item", chat_id=chat_id, post_id=row["post_id"]))])
    rows.append([ib("⚙️ Расписание", PlanCb(action="slots", chat_id=chat_id)),
                 ib("🧹 Уплотнить очередь", PlanCb(action="compact", chat_id=chat_id))])
    rows.append([ib("📝 Новый пост", MenuCb(action="new")), ib("◀️ Каналы", PlanCb(action="home"))])
    return "\n".join(lines), kb(*rows)


# ------------------------------------------------------------------ timeline

async def timeline_screen(db: Any, config: Any, folder_id: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    tz = config.tz
    title = "Все каналы"
    folder_filter = None
    if folder_id == NO_FOLDER:
        title, folder_filter = "Без папки", 0
    elif folder_id:
        folder = await db.get_folder(folder_id)
        title = f"Папка «{folder['name']}»" if folder else "Папка"
        folder_filter = folder_id
    items = await plan_service.timeline(db, tz, days=PLAN_DAYS, folder_id=folder_filter)
    lines = [f"🗓 <b>{esc(title)} — {PLAN_DAYS} дней</b>", ""]
    if not items:
        lines.append(f"На ближайшие {PLAN_DAYS} дней ничего не запланировано.")
    body: list[str] = []
    day = None
    for item in items:
        if item["date"] != day:
            day = item["date"]
            body.append(_day_title(item["date"], item["weekday"], tz))
        name = esc(clip(item["channel"]["title"] or item["channel"]["id"], 30))
        body.append(f"{item['time']} · {name} — {esc(_item_label(item['post']))}")
    lines += cut_lines(body)
    current = now()
    rows = []
    for item in [it for it in items if it["ts"] > current][:MAX_ITEM_BUTTONS]:
        label = (f"{slot_label(item['ts'], tz)} · {clip(item['channel']['title'] or '', 14)} · "
                 f"{_item_label(item['post'], 20)}")
        rows.append([ib(label[:64], PlanCb(action="item", chat_id=item["channel"]["id"], post_id=item["post"]["id"]))])
    rows.append([ib("◀️ Каналы", PlanCb(action="home"))])
    return "\n".join(lines), kb(*rows)


# ------------------------------------------------------------------ item

def _plan_target(post: dict | None, chat_id: int) -> dict | None:
    if post is None:
        return None
    target = next((t for t in post["targets"] if t["channel_id"] == chat_id), None)
    if target is None or target["status"] != TARGET_PENDING or not target.get("scheduled_at"):
        return None
    return target


async def item_screen(db: Any, config: Any, post: dict, target: dict, *,
                      note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    tz = config.tz
    post_id, chat_id = post["id"], target["channel_id"]
    channel = await db.get_channel(chat_id)
    ts = target["scheduled_at"]
    how = "по расписанию" if target.get("queued") else "точное время"
    weekday = plan_service.WEEKDAYS[datetime.fromtimestamp(ts, tz).weekday()]
    lines = [f"🗓 <b>Пост #{post_id}</b> · «{esc(channel_title(channel, chat_id))}»",
             f"⏰ {weekday} {format_dt(ts, tz)} — {how}"]
    details = [describe_media(post_media(post)) or "📝 текст"]
    n_buttons = count_buttons(post.get("buttons"))
    if n_buttons:
        details.append(f"🔘 {n_buttons} {plural(n_buttons, 'кнопка', 'кнопки', 'кнопок')}")
    lines.append(" · ".join(details))
    text = excerpt(post.get("text_md") or "", 300)
    if text:
        lines += ["", f"<blockquote>{esc(text)}</blockquote>"]
    others = []
    for other in post["targets"]:
        if other["channel_id"] == chat_id:
            continue
        name = other.get("channel_title") or str(other["channel_id"])
        if other["status"] == TARGET_SENT:
            others.append(f"{name} — опубликован ✅")
        elif other["status"] == TARGET_PENDING and other.get("scheduled_at"):
            others.append(f"{name} — {slot_label(other['scheduled_at'], tz)}")
        elif other["status"] == TARGET_PENDING:
            others.append(f"{name} — без времени")
    if others:  # cut before escaping (many channels must not overflow the message, finding #17)
        lines += ["", "Другие каналы: " + esc(clip("; ".join(others), 1500))]
    if note:
        lines += ["", note]

    def cb(action: str, **extra: Any) -> PlanCb:
        return PlanCb(action=action, chat_id=chat_id, post_id=post_id, **extra)

    markup = kb(
        [ib("👁 Превью", cb("preview")), ib("✏️ Редактировать", cb("edit"))],
        [ib("⬆️ Раньше", cb("up")), ib("⬇️ Позже", cb("down"))],
        [ib("⏰ Другое время", cb("time")), ib("🚀 Опубликовать сейчас", cb("pub"), style="success")],
        [ib("❌ Убрать из плана", cb("unq"), style="danger")],
        [ib("◀️ К плану канала", PlanCb(action="ch", chat_id=chat_id))],
    )
    return "\n".join(lines), markup


async def _time_prompt(db: Any, config: Any, post_id: int, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    tz = config.tz
    # The post's own time counts as taken: it is not a place to move to (finding #10).
    free = await plan_service.next_free_slots(db, chat_id, tz, 4)
    text = (
        f"⏰ <b>Новое время поста #{post_id}</b>\n"
        f"Сейчас: {datetime.now(tz):%d.%m.%Y %H:%M} ({esc(tz_name(tz))})\n\n"
        "Пришлите время, например <code>18:30</code>, <code>25.12 10:00</code>, <code>завтра 09:00</code>, "
        "<code>+2h</code> или <code>через 30 минут</code>."
    )
    if free:
        text += "\n\nИли выберите свободный слот расписания:"
    rows = [[ib(f"📅 {slot_label(ts, tz)}", PlanCb(action="slot", chat_id=chat_id, post_id=post_id, ts=ts))]
            for ts in free]
    presets = [ib(label, PlanCb(action=f"at_{code}", chat_id=chat_id, post_id=post_id))
               for code, (label, _) in SCHEDULE_PRESETS.items()]
    rows += [presets[:2], presets[2:]]
    rows.append([ib("↩️ Назад", PlanCb(action="item", chat_id=chat_id, post_id=post_id))])
    return text, kb(*rows)


async def _compact_preview(db: Any, chat_id: int, tz: Any) -> tuple[int, int | None]:
    """(how many queued posts «🧹 Уплотнить очередь» would move, the earliest new time) — exactly
    what `plan_service.compact_queue` would do, without changing anything."""
    return await plan_service.compact_preview(db, chat_id, tz)


# ------------------------------------------------------------------ handlers

async def cmd_plan(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    await leave_state(message.bot, message.chat.id, state)
    text, markup = await plan_home(db, config)
    await message.answer(text, reply_markup=markup)


async def cb_menu_plan(callback: CallbackQuery, state: FSMContext, db: Any, config: Any) -> None:
    await safe_answer(callback)
    await _leave_dialog(state)
    await show(callback, *await plan_home(db, config))


async def cb_plan_page(callback: CallbackQuery, callback_data: PlanPageCb, state: FSMContext, db: Any,
                       config: Any) -> None:
    await safe_answer(callback)
    await _leave_dialog(state)
    if callback_data.folder_id:
        await show(callback, *await folder_plan_screen(db, callback_data.folder_id, callback_data.page))
    else:
        await show(callback, *await plan_home(db, config, callback_data.page))


async def _show_channel(callback: CallbackQuery, db: Any, config: Any, chat_id: int, *,
                        note: str | None = None) -> None:
    """The channel plan in the callback's message (the callback is answered by the caller)."""
    try:
        screen = await channel_plan_screen(db, config, chat_id, note=note)
    except PlanError as exc:
        text, markup = await plan_home(db, config)
        screen = (f"⚠️ {esc(exc)}\n\n{text}", markup)
    await show(callback, *screen)


async def _show_item(callback: CallbackQuery, db: Any, config: Any, post_id: int, chat_id: int, *,
                     note: str | None = None) -> None:
    """A plan item, or the channel plan when the post left the plan meanwhile (not answering)."""
    post = await db.get_post(post_id)
    target = _plan_target(post, chat_id)
    if target is None:
        await _show_channel(callback, db, config, chat_id, note=note)
        return
    await show(callback, *await item_screen(db, config, post, target, note=note))


async def cb_plan(callback: CallbackQuery, callback_data: PlanCb, state: FSMContext, db: Any,
                  config: Any) -> None:
    cb = callback_data
    action = cb.action
    if action != "time":
        await _leave_dialog(state)

    if action == "home":
        await safe_answer(callback)
        await show(callback, *await plan_home(db, config))
    elif action == "folder":
        await safe_answer(callback)
        await show(callback, *await folder_plan_screen(db, cb.folder_id))
    elif action == "all":
        await safe_answer(callback)
        await show(callback, *await timeline_screen(db, config, cb.folder_id))
    elif action == "ch":
        await safe_answer(callback)
        await _show_channel(callback, db, config, cb.chat_id)
    elif action == "slots":
        from app.bot.handlers.channels import open_slots_editor

        await open_slots_editor(callback, state, db, cb.chat_id, back="plan")
    elif action == "compact":
        # It rewrites the times of every queued post of the channel and cannot be undone: ask first
        # (finding #20), the way the web panel does.
        channel = await db.get_channel(cb.chat_id)
        if channel is None:
            await safe_answer(callback, plan_service.CHANNEL_NOT_FOUND, alert=True)
            return
        if not await db.get_channel_slots(cb.chat_id):
            await safe_answer(callback, f"У канала «{channel_title(channel)}» не настроено расписание публикаций",
                              alert=True)
            return
        moved, first = await _compact_preview(db, cb.chat_id, config.tz)
        if not moved:
            await safe_answer(callback, "Очередь и так плотная")
            await _show_channel(callback, db, config, cb.chat_id,
                                note="🧹 Очередь и так плотная — переносить нечего.")
            return
        await safe_answer(callback)
        posts_word = plural(moved, "пост", "поста", "постов")
        text = (f"🧹 <b>Уплотнить очередь «{esc(channel_title(channel))}»?</b>\n\n"
                f"{moved} {posts_word} из очереди {plural(moved, 'займёт', 'займут', 'займут')} ближайшие "
                "свободные слоты расписания — порядок сохранится"
                + (f", ближайшая публикация — уже {slot_label(first, config.tz)}" if first else "") + ".\n"
                "Посты с точным временем ⏱ останутся на месте.\n\nОтменить это нельзя.")
        await show(callback, text,
                   kb([ib("✅ Да, уплотнить", PlanCb(action="compact_ok", chat_id=cb.chat_id), style="primary"),
                       ib("↩️ Назад", PlanCb(action="ch", chat_id=cb.chat_id))]))
    elif action == "compact_ok":
        await show(callback, "⏳ Уплотняю очередь…", None)  # no second tap on the confirmation
        try:
            moved = await plan_service.compact_queue(db, cb.chat_id, config.tz)
        except PlanError as exc:
            await safe_answer(callback, str(exc), alert=True)
            await _show_channel(callback, db, config, cb.chat_id)
            return
        await safe_answer(callback, f"Перенесено постов: {moved}" if moved else "Очередь и так плотная")
        moved_text = plural(moved, "пост перенесён", "поста перенесены", "постов перенесено")
        note = (f"🧹 Очередь уплотнена: {moved} {moved_text} на ближайшие свободные слоты." if moved
                else "🧹 Очередь и так плотная — переносить нечего.")
        await _show_channel(callback, db, config, cb.chat_id, note=note)
    elif action == "item":
        if _plan_target(await db.get_post(cb.post_id), cb.chat_id) is None:
            await safe_answer(callback, "Этого поста уже нет в плане канала", alert=True)
            await _show_channel(callback, db, config, cb.chat_id)
            return
        await safe_answer(callback)
        await _show_item(callback, db, config, cb.post_id, cb.chat_id)
    else:
        await _item_action(callback, cb, state, db, config)


async def _item_action(callback: CallbackQuery, cb: PlanCb, state: FSMContext, db: Any, config: Any) -> None:
    action = cb.action
    post = await db.get_post(cb.post_id)
    target = _plan_target(post, cb.chat_id)
    if action == "preview":
        if post is None:
            await safe_answer(callback, "Пост не найден", alert=True)
            return
        try:
            await send_preview(callback.bot, db, config, post["id"], callback_chat_id(callback))
        except PostError as exc:
            await safe_answer(callback, str(exc), alert=True)
            return
        await safe_answer(callback, "Превью отправлено")
        return
    if action == "edit":
        await safe_answer(callback)
        error = await compose.open_editor(callback.bot, state, db, config, callback_chat_id(callback), cb.post_id)
        if error:
            await callback.bot.send_message(chat_id=callback_chat_id(callback), text=esc(error))
        return
    if target is None:
        await safe_answer(callback, "Этого поста уже нет в плане канала (опубликован, убран или удалён)",
                          alert=True)
        await _show_channel(callback, db, config, cb.chat_id)
        return

    if action in ("up", "down"):
        try:
            moved = await plan_service.shift_target(db, cb.post_id, cb.chat_id, -1 if action == "up" else 1)
        except PlanError as exc:
            await safe_answer(callback, str(exc), alert=True)
            return
        if not moved:
            await safe_answer(callback, "Раньше в очереди ничего нет" if action == "up" else
                              "Позже в очереди ничего нет", alert=True)
            return
        await safe_answer(callback, "Поменял местами с соседним постом")
        await _show_item(callback, db, config, cb.post_id, cb.chat_id)
    elif action == "time":
        await safe_answer(callback)
        await leave_state(callback.bot, callback.from_user.id, state)
        await state.set_state(PlanStates.move_time)
        message_id = await show(callback, *await _time_prompt(db, config, cb.post_id, cb.chat_id))
        await state.set_data({"post_id": cb.post_id, "chat_id": cb.chat_id, "msg_id": message_id})
    elif action == "slot" or action.startswith("at_"):
        if action == "slot":
            ts, queued = cb.ts, True
        else:
            preset = SCHEDULE_PRESETS.get(action[3:])
            try:
                ts = int(parse_when(preset[1] if preset else "", config.tz).timestamp())
            except ValueError as exc:
                await safe_answer(callback, str(exc), alert=True)
                return
            queued = False
        if ts == target["scheduled_at"] and bool(target.get("queued")) == queued:
            # An old keyboard / a preset that lands on the same time: nothing would change.
            await safe_answer(callback, "Пост уже стоит на это время")
            await _show_item(callback, db, config, cb.post_id, cb.chat_id)
            return
        try:
            await plan_service.move_target(db, cb.post_id, cb.chat_id, ts, queued=queued)
        except PlanError as exc:
            await safe_answer(callback, str(exc), alert=True)
            return
        await safe_answer(callback, "Время изменено")
        await _show_item(callback, db, config, cb.post_id, cb.chat_id,
                         note=f"⏰ Перенесено на {format_dt(ts, config.tz)}.")
    elif action == "pub":
        await safe_answer(callback)
        channel = await db.get_channel(cb.chat_id)
        await show(callback,
                   f"🚀 Опубликовать пост #{cb.post_id} в «{esc(channel_title(channel, cb.chat_id))}» прямо сейчас?\n"
                   "В остальные каналы он выйдет по своему плану.",
                   kb([ib("✅ Да, опубликовать", PlanCb(action="pub_ok", chat_id=cb.chat_id, post_id=cb.post_id),
                          style="success"),
                       ib("↩️ Назад", PlanCb(action="item", chat_id=cb.chat_id, post_id=cb.post_id))]))
    elif action == "pub_ok":
        await safe_answer(callback, "⏳ Публикую…")
        # The confirm keyboard is removed first: a second tap cannot start another publish.
        await show(callback, f"⏳ Публикую пост #{cb.post_id}…", None)
        back = kb([ib("◀️ К плану канала", PlanCb(action="ch", chat_id=cb.chat_id))])
        try:
            report = await publish_post(callback.bot, db, config, cb.post_id, channel_ids=[cb.chat_id])
        except PostError as exc:
            await show(callback, f"❌ {esc(exc)}", back)
            return
        log.info("Post %s published to %s from the plan by %s", cb.post_id, cb.chat_id, callback.from_user.id)
        await show(callback, report.summary_html(), back)
    elif action == "unq":
        try:
            await plan_service.unqueue_target(db, cb.post_id, cb.chat_id)
        except PlanError as exc:
            await safe_answer(callback, str(exc), alert=True)
            return
        await safe_answer(callback, "Пост убран из плана")
        fresh = await db.get_post(cb.post_id)
        note = f"↩️ Пост #{cb.post_id} убран из плана канала."
        if fresh is not None and fresh["status"] == POST_DRAFT:
            note += " Он снова черновик — «🗂 Посты» → «📝 Черновики»."
        await _show_channel(callback, db, config, cb.chat_id, note=note)
    else:
        await safe_answer(callback, "Неизвестное действие")


async def on_move_time(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    data = await state.get_data()
    post_id, chat_id = data.get("post_id"), data.get("chat_id")
    if not post_id or not chat_id:
        await state.clear()
        return
    try:
        when = parse_when(message.text or "", config.tz)
        await plan_service.move_target(db, post_id, chat_id, int(when.timestamp()), queued=False)
    except (ValueError, PlanError) as exc:
        await message.answer(f"❌ {esc(exc)}\n\nПопробуйте ещё раз или нажмите «↩️ Назад».")
        return
    await state.clear()
    post = await db.get_post(post_id)
    target = _plan_target(post, chat_id)
    note = f"⏰ Перенесено на {format_dt(int(when.timestamp()), config.tz)}."
    if target is None:
        text, markup = await channel_plan_screen(db, config, chat_id, note=note)
    else:
        text, markup = await item_screen(db, config, post, target, note=note)
    await edit_or_send(message.bot, message.chat.id, data.get("msg_id"), text, markup)


def create_router() -> Router:
    router = Router(name="plan")
    router.message.register(cmd_plan, Command("plan"))
    router.message.register(cmd_plan, TextIs(BTN_PLAN))
    router.message.register(on_move_time, StateFilter(PlanStates.move_time), F.text, UserInput())
    router.callback_query.register(cb_menu_plan, MenuCb.filter(F.action == "plan"))
    router.callback_query.register(cb_plan, PlanCb.filter())
    router.callback_query.register(cb_plan_page, PlanPageCb.filter())
    return router

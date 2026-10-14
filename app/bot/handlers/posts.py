"""«🗂 Посты»: scheduled / published / draft lists, post cards and their actions."""
from __future__ import annotations

import logging
import math
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import MenuCb, PostCb, PostListCb
from app.bot.compose_view import is_published, schedule_prompt, target_title, when_label
from app.bot.filters import TextIs, UserInput
from app.bot.handlers import compose
from app.bot.handlers.common import drop_prompt, leave_state
from app.bot.keyboards import BTN_POSTS, SCHEDULE_PRESETS, ib, kb
from app.bot.states import PostStates
from app.bot.utils import (
    CONFIRM_USED_TEXT,
    callback_chat_id,
    callback_message_id,
    cut_lines,
    edit_or_send,
    esc,
    fit_html,
    fmt_num,
    fmt_short,
    names_text,
    plural,
    safe_answer,
    show,
    tz_name,
)
from app.buttons import count_buttons
from app.db import (
    CH_ACTIVE,
    POST_DELETED,
    POST_DRAFT,
    POST_FAILED,
    POST_PARTIAL,
    POST_SCHEDULED,
    POST_SENDING,
    POST_SENT,
    TARGET_FAILED,
    TARGET_PENDING,
    TARGET_SENT,
)
from app.markdown import excerpt
from app.media import MEDIA_ICONS, describe_media, media_kind, post_media
from app.services import plan as plan_service
from app.services.plan import PlanError
from app.services.posting import (
    PostError,
    delete_published_post,
    message_link,
    publish_post,
    send_preview,
)
from app.services.scheduler import format_dt, parse_when

log = logging.getLogger(__name__)

PAGE_SIZE = 8
# tab key → (title, statuses)
TABS: dict[str, tuple[str, tuple[str, ...]]] = {
    "s": ("⏰ Запланированные", (POST_SCHEDULED,)),
    "p": ("✅ Опубликованные", (POST_SENT, POST_PARTIAL, POST_SENDING, POST_DELETED)),
    "d": ("📝 Черновики", (POST_DRAFT, POST_FAILED)),
}
TAB_EMOJI = {"s": "⏰", "p": "✅", "d": "📝"}
STATUS_EMOJI = {
    POST_DRAFT: "📝", POST_SCHEDULED: "⏰", POST_SENDING: "⏳", POST_SENT: "✅",
    POST_PARTIAL: "⚠️", POST_FAILED: "❌", POST_DELETED: "🗑",
}
STATUS_LABEL = {
    POST_DRAFT: "черновик", POST_SCHEDULED: "запланирован", POST_SENDING: "публикуется",
    POST_SENT: "опубликован", POST_PARTIAL: "опубликован частично", POST_FAILED: "не опубликован (ошибка)",
    POST_DELETED: "удалён из каналов",
}
RECORD_DELETABLE = (POST_DRAFT, POST_SCHEDULED, POST_FAILED, POST_DELETED)


# ------------------------------------------------------------------ list

async def default_tab(db: Any) -> str:
    if await db.count_posts(TABS["s"][1]):
        return "s"
    if await db.count_posts(TABS["p"][1]):
        return "p"
    return "d"


def _post_ts(post: dict, tab: str) -> int | None:
    if tab == "s":
        return post.get("scheduled_at")
    if tab == "p":
        return post.get("sent_at") or post.get("updated_at")
    return post.get("updated_at")


def _short_excerpt(post: dict, limit: int) -> str:
    """List line text: the media icon (for media posts) and the beginning of the text."""
    media = post_media(post)
    text = excerpt(post.get("text_md") or "", limit)
    if not media:
        return text or "—"
    if not text:
        return describe_media(media)
    return f"{MEDIA_ICONS.get(media_kind(media), '🖼')} {text}"


async def posts_screen(db: Any, tz: Any, tab: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    tab = tab if tab in TABS else "d"
    counts = {key: await db.count_posts(statuses) for key, (_, statuses) in TABS.items()}
    total = counts[tab]
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    posts = await db.list_posts(TABS[tab][1], limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    lines = [f"🗂 <b>Посты</b> — {TABS[tab][0]}: {total}"]
    if pages > 1:
        lines.append(f"Страница {page + 1} из {pages}")
    lines.append("")
    lines.append("Выберите пост:" if posts else "Здесь пока пусто.")
    lines.append("<i>⏰ запланированные · ✅ опубликованные · 📝 черновики</i>")

    rows = [[ib(f"{'▸ ' if key == tab else ''}{TAB_EMOJI[key]} {counts[key]}", PostListCb(tab=key, page=0))
             for key in TABS]]
    for post in posts:
        label = (f"{STATUS_EMOJI.get(post['status'], '•')} {fmt_short(_post_ts(post, tab), tz)} · "
                 f"{_short_excerpt(post, 32)}")
        rows.append([ib(label[:64], PostCb(action="open", post_id=post["id"], tab=tab, page=page))])
    if pages > 1:
        rows.append([
            ib("◀️", PostListCb(tab=tab, page=(page - 1) % pages)),
            ib(f"{page + 1}/{pages}", MenuCb(action="noop")),
            ib("▶️", PostListCb(tab=tab, page=(page + 1) % pages)),
        ])
    rows.append([ib("📝 Новый пост", MenuCb(action="new"), style="primary")])
    return "\n".join(lines), kb(*rows)


# ------------------------------------------------------------------ card

def _target_line(target: dict, tz: Any = None, *, post_deleted: bool = False) -> str:
    title = esc(target_title(target))
    status = target["status"]
    if post_deleted and status == TARGET_PENDING and not target.get("scheduled_at"):
        return f"• {title} — отменён"  # deleting from the channels cancelled the rest (finding #15)
    if status == TARGET_SENT:
        line = f"• {title} ✅"
        if target.get("message_id"):
            link = esc(message_link(target, target["message_id"]))
            line = f'• <a href="{link}">{title}</a> ✅'
        if target.get("reactions_total"):
            line += f" ❤️ {fmt_num(target['reactions_total'])}"
        return line
    if status == TARGET_PENDING:
        if target.get("scheduled_at") and tz is not None:
            how = " (по расписанию)" if target.get("queued") else ""
            return f"• {title} ⏰ {when_label(target['scheduled_at'], tz)}{how}"
        return f"• {title} ⏳"
    if status == TARGET_FAILED:
        return f"• {title} ❌ {esc(target.get('error') or '')}".rstrip()
    return f"• {title} 🗑"


def post_card(post: dict, tz: Any, tab: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    status = post["status"]
    post_id = post["id"]
    lines = [f"{STATUS_EMOJI.get(status, '•')} <b>Пост #{post_id}</b> — {STATUS_LABEL.get(status, esc(status))}"]
    if status == POST_SCHEDULED and post.get("scheduled_at"):
        lines.append(f"⏰ Публикация: {format_dt(post['scheduled_at'], tz)} ({esc(tz_name(tz))})")
    if post.get("sent_at"):
        lines.append(f"🕒 Опубликован: {format_dt(post['sent_at'], tz)}")
    elif post.get("updated_at"):
        lines.append(f"🕓 Изменён: {format_dt(post['updated_at'], tz)}")
    details = [describe_media(post_media(post)) or "📝 текст"]
    n_buttons = count_buttons(post.get("buttons"))
    if n_buttons:
        details.append(f"🔘 {n_buttons} {plural(n_buttons, 'кнопка', 'кнопки', 'кнопок')}")
    reactions = sum(t.get("reactions_total") or 0 for t in post["targets"])
    if reactions:
        details.append(f"❤️ {fmt_num(reactions)}")
    lines.append(" · ".join(details))

    text = excerpt(post.get("text_md") or "", 300)
    if text:
        lines += ["", f"<blockquote>{esc(text)}</blockquote>"]
    if post["targets"]:
        lines += ["", "📢 <b>Каналы:</b>"]
        lines += cut_lines([_target_line(t, tz, post_deleted=status == POST_DELETED) for t in post["targets"]],
                           2500, more="<i>…и ещё {n}</i>")
    else:
        lines += ["", "📢 Каналы не выбраны"]
    if post.get("error") and status in (POST_FAILED, POST_PARTIAL):
        lines += ["", f"⚠️ {esc(post['error'])}"]
    return "\n".join(lines), post_card_kb(post, tab, page)


def post_card_kb(post: dict, tab: str, page: int) -> InlineKeyboardMarkup:
    status = post["status"]

    def cb(action: str) -> PostCb:
        return PostCb(action=action, post_id=post["id"], tab=tab, page=page)

    back = [ib("↩️ К списку", PostListCb(tab=tab, page=page))]
    if status == POST_SCHEDULED and is_published(post):
        # Queued to several channels, already out in some of them.
        return kb(
            [ib("✏️ Редактировать", cb("edit"), style="primary")],
            [ib("🚀 Остальные сейчас", cb("now"), style="success"), ib("⏰ Перенести", cb("resched"))],
            [ib("❌ Отменить остальные", cb("unschedule")), ib("👁 Превью", cb("preview"))],
            [ib("📄 Дублировать", cb("dup")), ib("🗑 Удалить из каналов", cb("delch"), style="danger")],
            back,
        )
    if status == POST_SCHEDULED:
        return kb(
            [ib("🚀 Сейчас", cb("now"), style="success"), ib("⏰ Перенести", cb("resched"))],
            [ib("❌ Отменить", cb("unschedule")), ib("✏️ Открыть в редакторе", cb("edit"))],
            [ib("👁 Превью", cb("preview")), ib("🗑 Удалить", cb("delete"), style="danger")],
            back,
        )
    if status in (POST_DRAFT, POST_FAILED):
        return kb(
            [ib("✏️ Открыть в редакторе", cb("edit"), style="primary")],
            [ib("🔁 Повторить публикацию", cb("retry"), style="success")] if status == POST_FAILED else None,
            [ib("👁 Превью", cb("preview")), ib("📄 Дублировать", cb("dup"))],
            [ib("🗑 Удалить", cb("delete"), style="danger")],
            back,
        )
    if status in (POST_SENT, POST_PARTIAL):
        return kb(
            [ib("✏️ Редактировать", cb("edit"), style="primary")],
            [ib("🔁 Повторить для неудачных", cb("retry"), style="success")] if status == POST_PARTIAL else None,
            [ib("👁 Превью", cb("preview")), ib("📄 Дублировать", cb("dup"))],
            [ib("🗑 Удалить из каналов", cb("delch"), style="danger")],
            back,
        )
    if status == POST_DELETED:
        if any(t["status"] == TARGET_SENT for t in post["targets"]):
            # Left by an older version: still shown in a channel — remove it there first.
            return kb([ib("📄 Дублировать", cb("dup")), ib("🗑 Удалить из каналов", cb("delch"), style="danger")], back)
        return kb([ib("📄 Дублировать", cb("dup")), ib("🗑 Удалить запись", cb("delete"), style="danger")], back)
    return kb([ib("🔄 Обновить", cb("open"))], back)  # sending


def confirm_kb(post_id: int, tab: str, page: int, action: str, label: str, style: str) -> InlineKeyboardMarkup:
    return kb([ib(label, PostCb(action=action, post_id=post_id, tab=tab, page=page), style=style),
               ib("↩️ Назад", PostCb(action="open", post_id=post_id, tab=tab, page=page))])


def report_kb(post_id: int, tab: str, page: int) -> InlineKeyboardMarkup:
    return kb([ib("↩️ К посту", PostCb(action="open", post_id=post_id, tab=tab, page=page)),
               ib("🗂 К списку", PostListCb(tab=tab, page=page))])


# ------------------------------------------------------------------ entry & list handlers

async def cmd_posts(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    await leave_state(message.bot, message.chat.id, state)
    text, markup = await posts_screen(db, config.tz, await default_tab(db), 0)
    await message.answer(text, reply_markup=markup)


async def cb_menu_posts(callback: CallbackQuery, db: Any, config: Any) -> None:
    await safe_answer(callback)
    text, markup = await posts_screen(db, config.tz, await default_tab(db), 0)
    await show(callback, text, markup)


async def cb_list(callback: CallbackQuery, callback_data: PostListCb, state: FSMContext,
                  db: Any, config: Any) -> None:
    await drop_prompt(state)
    await safe_answer(callback)
    text, markup = await posts_screen(db, config.tz, callback_data.tab, callback_data.page)
    await show(callback, text, markup)


# ------------------------------------------------------------------ card actions

async def _show_card(callback: CallbackQuery, post: dict, cb: PostCb, config: Any) -> None:
    text, markup = post_card(post, config.tz, cb.tab, cb.page)
    await show(callback, text, markup)


async def _publish(callback: CallbackQuery, cb: PostCb, db: Any, config: Any) -> None:
    # The confirm keyboard goes away before the callback is answered: while a callback is pending
    # Telegram clients ignore further taps, so the button is never tapped twice (findings #10/#13).
    await show(callback, f"⏳ Публикую пост #{cb.post_id}…", None)
    await safe_answer(callback, "⏳ Публикую…")
    try:
        report = await publish_post(callback.bot, db, config, cb.post_id)
    except PostError as exc:
        await show(callback, f"❌ {esc(exc)}", report_kb(cb.post_id, cb.tab, cb.page))
        return
    log.info("Post %s published from its card by %s", cb.post_id, callback.from_user.id)
    await show(callback, fit_html(report.summary_html()), report_kb(cb.post_id, cb.tab, cb.page))


async def _delete_from_channels(callback: CallbackQuery, cb: PostCb, db: Any, post: dict) -> None:
    await show(callback, f"⏳ Удаляю пост #{cb.post_id} из каналов…", None)
    await safe_answer(callback, "⏳ Удаляю…")
    waiting = {t["channel_id"]: t for t in _waiting_targets(post)}
    try:
        report = await delete_published_post(callback.bot, db, cb.post_id)
    except PostError as exc:
        await show(callback, f"❌ {esc(exc)}", report_kb(cb.post_id, cb.tab, cb.page))
        return
    log.info("Post %s deleted from channels by %s", cb.post_id, callback.from_user.id)
    text = fit_html(report.summary_html(), 3300)
    fresh = await db.get_post(cb.post_id)
    still_waiting = {t["channel_id"] for t in _waiting_targets(fresh)} if fresh else set()
    cancelled = [t for cid, t in waiting.items() if cid not in still_waiting]
    if cancelled:
        text += f"\n\n⏰ Публикация в {names_text([target_title(t) for t in cancelled])} отменена."
    await show(callback, text, report_kb(cb.post_id, cb.tab, cb.page))


async def duplicate_post(db: Any, post: dict, user_id: int) -> int:
    """New draft with the same content, options and still active channels."""
    new_id = await db.create_post(
        text_md=post.get("text_md") or "", text_html=post.get("text_html") or "",
        media=post_media(post),
        buttons=post.get("buttons") or [], disable_preview=post.get("disable_preview"),
        silent=post.get("silent"), protect=post.get("protect"), caption_above=post.get("caption_above"),
        status=POST_DRAFT, source="bot", created_by=user_id,
    )
    active = {ch["id"] for ch in await db.list_channels([CH_ACTIVE])}
    await db.set_post_targets(new_id, [t["channel_id"] for t in post["targets"] if t["channel_id"] in active])
    return new_id


async def _reschedule_prompt(callback: CallbackQuery, state: FSMContext, cb: PostCb, config: Any) -> None:
    await state.set_state(PostStates.reschedule)
    await state.set_data({"post_id": cb.post_id, "tab": cb.tab, "page": cb.page,
                          "card_id": callback_message_id(callback)})
    text, _ = schedule_prompt({"id": cb.post_id}, config.tz)
    presets = [ib(label, PostCb(action=f"at_{code}", post_id=cb.post_id, tab=cb.tab, page=cb.page))
               for code, (label, _) in SCHEDULE_PRESETS.items()]
    await show(callback, text.replace("Когда опубликовать?", "Новое время публикации"),
               kb(presets[:2], presets[2:], [ib("↩️ Назад", PostCb(action="open", post_id=cb.post_id,
                                                                      tab=cb.tab, page=cb.page))]))


async def reschedule(db: Any, post_id: int, text: str, tz: Any) -> tuple[bool, str]:
    """Move every not yet published channel of a scheduled post to a new time. Returns (ok, message)."""
    try:
        when = parse_when(text, tz)
    except ValueError as exc:
        return False, str(exc)
    post = await db.get_post(post_id)
    if post is None or post["status"] != POST_SCHEDULED:
        return False, "Пост уже не запланирован (возможно, он опубликован или отменён)."
    channel_ids = [t["channel_id"] for t in post["targets"] if t["status"] in (TARGET_PENDING, TARGET_FAILED)]
    ts = int(when.timestamp())
    try:
        await plan_service.schedule_post_at(db, post_id, channel_ids, ts)
    except PlanError as exc:
        return False, str(exc)
    return True, f"⏰ Пост #{post_id} перенесён на <b>{format_dt(ts, tz)}</b> ({esc(tz_name(tz))})."


async def unschedule(db: Any, post: dict) -> tuple[bool, str]:
    """«❌ Отменить» of a card: only a post that is still scheduled is touched (finding #12)."""
    current = await db.get_post(post["id"])
    status = current["status"] if current else None
    if status != POST_SCHEDULED:
        if status == POST_SENDING:
            return False, "Пост уже публикуется — отменить нельзя"
        if status in (POST_SENT, POST_PARTIAL):
            return False, "Пост уже опубликован"
        return False, "Пост уже не запланирован"
    had_sent = is_published(current)
    try:
        await plan_service.unschedule_post(db, current["id"])
    except PlanError as exc:
        return False, str(exc)
    fresh = await db.get_post(current["id"])
    if fresh is None:
        return False, "Пост удалён"
    if fresh["status"] == POST_SENDING:
        return False, "Пост уже публикуется — отменить нельзя"
    if fresh["status"] in (POST_SENT, POST_PARTIAL) and not had_sent:
        return False, "Пост уже опубликован"
    if had_sent:
        return True, "Публикация в остальные каналы отменена"
    return True, "Публикация отменена — пост теперь черновик"


def _record_deletable(post: dict) -> bool:
    """«🗑 Удалить» (the record): a draft / scheduled / failed / deleted post no channel shows."""
    return post["status"] in RECORD_DELETABLE and not any(t["status"] == TARGET_SENT for t in post["targets"])


def _waiting_targets(post: dict) -> list[dict]:
    """Channels the post still waits for (a time or a place in the queue)."""
    return [t for t in post["targets"]
            if t["status"] == TARGET_PENDING and (t.get("scheduled_at") or t.get("queued"))]


async def cb_post(callback: CallbackQuery, callback_data: PostCb, state: FSMContext,
                  db: Any, config: Any, confirms: Any = None) -> None:
    cb = callback_data
    post = await db.get_post(cb.post_id)
    if post is None:
        await safe_answer(callback, "Пост не найден — возможно, его удалили", alert=True)
        text, markup = await posts_screen(db, config.tz, cb.tab, cb.page)
        await show(callback, text, markup)
        return
    if cb.action != "resched":
        await drop_prompt(state)
    action = cb.action
    status = post["status"]
    chat_id = callback_chat_id(callback)

    if action == "open":
        await safe_answer(callback)
        await _show_card(callback, post, cb, config)
    elif action == "preview":
        try:
            await send_preview(callback.bot, db, config, post["id"], chat_id)
        except PostError as exc:
            await safe_answer(callback, str(exc), alert=True)
            return
        await safe_answer(callback, "Превью отправлено")
    elif action == "edit":
        await safe_answer(callback)
        error = await compose.open_editor(callback.bot, state, db, config, chat_id, post["id"])
        if error:
            await callback.bot.send_message(chat_id=chat_id, text=esc(error))
    elif action == "dup":
        new_id = await duplicate_post(db, post, callback.from_user.id)
        await safe_answer(callback, f"Создан черновик #{new_id}")
        await compose.open_editor(callback.bot, state, db, config, chat_id, new_id,
                                  note=f"📄 Это копия поста #{post['id']}.")
    elif action in ("now", "retry"):
        if status not in (POST_SCHEDULED, POST_FAILED, POST_PARTIAL):
            await safe_answer(callback, "Этот пост сейчас нельзя опубликовать", alert=True)
            return
        await safe_answer(callback)
        pending = [t for t in post["targets"] if t["status"] in (TARGET_PENDING, TARGET_FAILED)]
        n = len(pending)
        shown = await show(callback,
                           f"🚀 Опубликовать пост #{post['id']} сейчас в {n} "
                           f"{plural(n, 'канал', 'канала', 'каналов')}?",
                           confirm_kb(post["id"], cb.tab, cb.page, "pub_ok", "✅ Да, опубликовать", "success"))
        if confirms is not None:
            confirms.shown(chat_id, shown)
    elif action == "pub_ok":
        if status not in (POST_SCHEDULED, POST_FAILED, POST_PARTIAL) or not any(
                t["status"] in (TARGET_PENDING, TARGET_FAILED) for t in post["targets"]):
            # A second tap after the publish finished (finding #10): keep the report on screen.
            await safe_answer(callback, "Пост уже опубликован" if status != POST_SENDING else "Пост уже публикуется",
                              alert=True)
            return
        if confirms is not None and not confirms.use(chat_id, callback_message_id(callback), post["id"]):
            # A second tap after a failed / partial publish (finding #13): never re-send without a new
            # confirmation — a timed-out channel may have received the post after all.
            await safe_answer(callback, CONFIRM_USED_TEXT, alert=True)
            return
        await _publish(callback, cb, db, config)
    elif action == "unschedule":
        ok, message = await unschedule(db, post)
        await safe_answer(callback, message, alert=not ok)
        fresh = await db.get_post(post["id"])
        if fresh is None:
            text, markup = await posts_screen(db, config.tz, cb.tab, cb.page)
            await show(callback, text, markup)
            return
        await _show_card(callback, fresh, cb, config)
    elif action == "resched":
        if status != POST_SCHEDULED:
            await safe_answer(callback, "Пост уже не запланирован", alert=True)
            return
        await safe_answer(callback)
        await _reschedule_prompt(callback, state, cb, config)
    elif action.startswith("at_"):
        preset = SCHEDULE_PRESETS.get(action[3:])
        ok, message = await reschedule(db, post["id"], preset[1] if preset else "", config.tz)
        await safe_answer(callback, None if ok else message, alert=not ok)
        if ok:
            await state.clear()
            await show(callback, message, report_kb(post["id"], cb.tab, cb.page))
    elif action == "delch":
        sent = [t for t in post["targets"] if t["status"] == TARGET_SENT]
        if not sent:
            await safe_answer(callback, "Пост не опубликован ни в одном канале", alert=True)
            return
        await safe_answer(callback)
        n = len(sent)
        text = (f"🗑 Удалить пост #{post['id']} из {n} {plural(n, 'канала', 'каналов', 'каналов')}?\n"
                "Сообщения будут удалены в Telegram, это нельзя отменить.")
        waiting = _waiting_targets(post)
        if waiting:
            # The service also cancels the channels still waiting (finding #15): say so.
            names = names_text([target_title(t) for t in waiting])
            text += f"\n⏰ Публикация в {names} по расписанию тоже будет отменена."
        await show(callback, text,
                   confirm_kb(post["id"], cb.tab, cb.page, "delch_ok", "🗑 Да, удалить", "danger"))
    elif action == "delch_ok":
        if not any(t["status"] == TARGET_SENT for t in post["targets"]):
            await safe_answer(callback, "Пост уже удалён из каналов", alert=True)
            return
        await _delete_from_channels(callback, cb, db, post)
    elif action == "delete":
        if not _record_deletable(post):
            await safe_answer(callback, "Опубликованный пост сначала удалите из каналов", alert=True)
            return
        await safe_answer(callback)
        await show(callback, f"🗑 Удалить пост #{post['id']} из бота? Это нельзя отменить.",
                   confirm_kb(post["id"], cb.tab, cb.page, "delete_ok", "🗑 Да, удалить", "danger"))
    elif action == "delete_ok":
        if not _record_deletable(post):
            await safe_answer(callback, "Этот пост нельзя удалить", alert=True)
            return
        if not await db.delete_post_record(post["id"]):
            # Claimed by the scheduler after the check above (review2 #3): the record must stay to
            # manage the message — never «Пост удалён» here.
            fresh = await db.get_post(post["id"])
            if fresh is not None:
                await safe_answer(callback, "Пост сейчас публикуется — удалить его нельзя"
                                  if fresh["status"] == POST_SENDING else "Этот пост нельзя удалить", alert=True)
                await _show_card(callback, fresh, cb, config)
                return
        log.info("Post %s record deleted by %s", post["id"], callback.from_user.id)
        await safe_answer(callback, "Пост удалён")
        text, markup = await posts_screen(db, config.tz, cb.tab, cb.page)
        await show(callback, text, markup)
    else:
        await safe_answer(callback, "Неизвестное действие")


async def on_reschedule_text(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    if not post_id:
        await state.clear()
        return
    ok, text = await reschedule(db, post_id, message.text or "", config.tz)
    if not ok:
        await message.answer(f"❌ {esc(text)}\n\nПопробуйте ещё раз или нажмите /cancel.")
        return
    await state.clear()
    tab, page = data.get("tab", "s"), data.get("page", 0)
    await edit_or_send(message.bot, message.chat.id, data.get("card_id"), text, report_kb(post_id, tab, page))


# ------------------------------------------------------------------ router

def create_router() -> Router:
    router = Router(name="posts")
    router.message.register(cmd_posts, Command("posts"))
    router.message.register(cmd_posts, TextIs(BTN_POSTS))
    router.message.register(on_reschedule_text, StateFilter(PostStates.reschedule), F.text, UserInput())
    router.callback_query.register(cb_menu_posts, MenuCb.filter(F.action == "posts"))
    router.callback_query.register(cb_list, PostListCb.filter())
    router.callback_query.register(cb_post, PostCb.filter())
    return router


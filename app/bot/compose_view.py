"""Rendering of the post composer: control panel text / keyboard, prompts and pickers.

Pure functions of the post dict (as `load_post` returns it) and the FSM session data (a few
async helpers read channels / the plan from the DB); the handlers live in
`app.bot.handlers.compose`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from app.bot.callbacks import ComposeCb, ComposeChannelCb, ComposeFolderCb, ComposeMediaCb, ComposeOptionCb
from app.bot.keyboards import OPTIONS, SCHEDULE_PRESETS, ib, kb
from app.bot.utils import channel_title, clip, cut_lines, esc, names_text, plural, tz_name
from app.buttons import FORMAT_HELP, build_markup, buttons_to_text, count_buttons
from app.db import (
    CH_ACTIVE,
    POST_FAILED,
    POST_PARTIAL,
    POST_SCHEDULED,
    POST_SENT,
    TARGET_FAILED,
    TARGET_PENDING,
    TARGET_SENT,
)
from app.markdown import CAPTION_LIMIT, TEXT_LIMIT, has_custom_emoji, md_to_html, visible_length
from app.media import (
    MAX_ALBUM,
    MEDIA_ICONS,
    TYPE_LABELS,
    MediaError,
    check_buttons,
    describe_media,
    normalize_media,
    post_media,
)
from app.services import folders as folder_service
from app.services import plan as plan_service
from app.services.posting import PostError, render_text
from app.services.scheduler import format_dt

SHOW_SOURCE_LIMIT = 3000  # longer sources are not echoed back in prompts (message limit 4096)

NO_CHANNELS_PROBLEM = "Сначала выберите каналы — кнопка «📢 Каналы»."
BUTTONS_ALBUM_ERROR = (
    "Telegram не поддерживает кнопки у альбомов. Оставьте одно фото или видео, чтобы добавить кнопки."
)
PUBLISHED_STATUSES = (POST_SENT, POST_PARTIAL)


# ------------------------------------------------------------------ post helpers

def media_of(post: dict) -> list[dict]:
    return post_media(post)


def has_media(post: dict) -> bool:
    return bool(post_media(post))


def is_album(post: dict) -> bool:
    return len(post_media(post)) > 1


def is_published(post: dict) -> bool:
    """The post is shown in at least one channel: it is edited in place (edit mode).

    V2: a post queued to several channels stays 'scheduled' while some channels already got it.
    """
    return post["status"] in PUBLISHED_STATUSES or any(t["status"] == TARGET_SENT for t in post["targets"])


def selected_targets(post: dict, *, edit: bool) -> list[dict]:
    """Targets the post goes to (draft) or was published to (edit mode)."""
    if edit:
        return [t for t in post["targets"] if t["status"] == TARGET_SENT]
    return [t for t in post["targets"] if t["status"] in (TARGET_PENDING, TARGET_FAILED)]


def target_title(target: dict) -> str:
    return channel_title({"title": target.get("channel_title"), "username": target.get("channel_username"),
                          "id": target["channel_id"]})


def when_label(ts: int, tz: Any) -> str:
    """'Пн 14.10 09:00' — a plan time."""
    return plan_service.slot_label(ts, tz)


def available_options(post: dict, *, edit: bool) -> tuple[str, ...]:
    """Options the admin may toggle: all for drafts; for a published post only the one Telegram
    can still change (link preview of a text post / caption position of a media post)."""
    if not edit:
        return tuple(OPTIONS)
    return ("caption_above",) if has_media(post) else ("disable_preview",)


def publish_problem(post: dict, *, edit: bool) -> str | None:
    """Why the post cannot be published / scheduled / saved right now (None when it can)."""
    if not edit and not selected_targets(post, edit=False):
        return NO_CHANNELS_PROBLEM
    try:
        media = normalize_media(post_media(post))
        render_text(post.get("text_md") or "", has_media=bool(media))
        check_buttons(media, post.get("buttons"))
    except (MediaError, PostError) as exc:
        return str(exc)
    try:
        build_markup(post.get("buttons") or [])
    except ValueError as exc:
        return f"Ошибка в кнопках: {exc}"
    return None


def saved_text(post: dict | None, tz: Any) -> str:
    """Where a draft / scheduled post can be found once the composer is left (findings #13)."""
    if post is None:
        return "Пост не найден — возможно, его удалили."
    post_id = post["id"]
    if post["status"] == POST_SCHEDULED and post.get("scheduled_at"):
        return (f"Пост #{post_id} остаётся запланированным — публикация "
                f"{format_dt(post['scheduled_at'], tz)} ({esc(tz_name(tz))}). "
                "Он в «🗂 Посты» → «⏰ Запланированные».")
    if is_published(post):
        return f"Пост #{post_id} опубликован — он в «🗂 Посты» → «✅ Опубликованные»."
    return f"Черновик #{post_id} сохранён в «🗂 Посты» → «📝 Черновики»."


def close_text(post: dict, tz: Any) -> str:
    """Result of «✅ Закрыть»: never promises a publication that cannot happen (finding #14)."""
    post_id = post["id"]
    if post["status"] == POST_SCHEDULED and post.get("scheduled_at"):
        when = format_dt(post["scheduled_at"], tz)
        problem = publish_problem(post, edit=False)
        if problem:
            return (f"⚠️ Пост #{post_id} сохранён, но {when} не опубликуется: {esc(problem)}\n"
                    "Исправьте его в «🗂 Посты» → «⏰ Запланированные» или снимите с расписания.")
        text = f"✅ Пост #{post_id} сохранён и будет опубликован {when}."
        untimed = [t for t in selected_targets(post, edit=False) if not t.get("scheduled_at")]
        if untimed:
            names = names_text([target_title(t) for t in untimed], 1500)
            text += (f"\n⚠️ Без времени: {names} — туда пост сам не выйдет. Откройте пост и нажмите "
                     "«🚀 Опубликовать», чтобы поставить его в очередь или задать время.")
        return text
    return f"💾 Черновик #{post_id} сохранён. Он в «🗂 Посты» → «📝 Черновики»."


# ------------------------------------------------------------------ panel

def _length_line(post: dict) -> str:
    limit = CAPTION_LIMIT if has_media(post) else TEXT_LIMIT
    source = (post.get("text_md") or "").strip()
    length = visible_length(md_to_html(source)) if source else 0
    warn = " ⚠️" if length > limit else ""
    return f"📏 Длина: {length} / {limit}{warn}"


def _target_time(target: dict, tz: Any) -> str:
    if not target.get("scheduled_at"):
        return "без времени"
    label = when_label(target["scheduled_at"], tz)
    return f"⏰ {label}" + (" (по расписанию)" if target.get("queued") else "")


TARGETS_BUDGET = 1500  # the panel lists its channels within this many characters (finding #17)
MORE = "<i>…и ещё {n}</i>"


def _plain_list(items: list[str], limit: int = TARGETS_BUDGET) -> str:
    """'A, B, C' (escaped), cut at about `limit` characters."""
    return esc(clip(", ".join(items), limit))


def _targets_lines(post: dict, tz: Any, *, edit: bool) -> list[str]:
    if edit:
        names = _plain_list([target_title(t) for t in selected_targets(post, edit=True)])
        lines = [f"📢 Опубликован в: {names or '—'}"]
        waiting = [t for t in post["targets"] if t["status"] == TARGET_PENDING and t.get("scheduled_at")]
        if waiting:
            lines.append("⏰ Ждёт публикации: " + _plain_list(
                [f"{target_title(t)} — {when_label(t['scheduled_at'], tz)}" for t in waiting], 800))
        return lines
    targets = selected_targets(post, edit=False)
    if not targets:
        return ["📢 Каналы: ⚠️ не выбраны"]
    if not any(t.get("scheduled_at") for t in targets):
        return ["📢 Каналы: " + _plain_list([target_title(t) for t in targets])]
    lines = ["📢 Каналы:"]
    lines += cut_lines([f"• {esc(target_title(target))} — {_target_time(target, tz)}" for target in targets],
                       TARGETS_BUDGET, more=MORE)
    return lines


def _buttons_line(post: dict) -> str:
    n_buttons = count_buttons(post.get("buttons"))
    if is_album(post):
        return f"🔘 Кнопки: {n_buttons} ⚠️ у альбома кнопок быть не может" if n_buttons else \
            "🔘 Кнопки: недоступны для альбома"
    return f"🔘 Кнопки: {n_buttons or 'нет'}"


PREMIUM_EMOJI_WARNING = "⚠️ Премиум-эмодзи станут обычными (нет юзернейма с Fragment)"


def panel_text(post: dict, data: dict, tz: Any, *, error: str | None = None, note: str | None = None,
               premium_emoji: bool = True) -> str:
    """`premium_emoji` — the bot can publish custom emoji (setting, SPEC_V23); when it cannot, a post
    with custom emoji gets a warning line."""
    edit = bool(data.get("edit"))
    post_id = post["id"]
    if edit:
        header = f"✏️ <b>Редактирование поста #{post_id}</b>"
    elif post["status"] == POST_SCHEDULED:
        header = f"⏰ <b>Запланированный пост #{post_id}</b>"
    else:
        header = f"📝 <b>Черновик #{post_id}</b>"
    lines = [header]
    if not error:
        lines.append("☝️ Так пост будет выглядеть в канале.")
    lines.append("")
    lines += _targets_lines(post, tz, edit=edit)
    lines.append(f"🖼 Медиа: {describe_media(post_media(post)) or 'нет'}")
    lines.append(_buttons_line(post))
    options = [OPTIONS[key] for key in available_options(post, edit=edit) if post.get(key)]
    lines.append("⚙️ Опции: " + (", ".join(options) if options else "по умолчанию"))
    if not edit:
        if post["status"] == POST_SCHEDULED and post.get("scheduled_at"):
            several = len({t.get("scheduled_at") for t in selected_targets(post, edit=False)}) > 1
            label = "Ближайшая публикация" if several else "Публикация"
            lines.append(f"⏰ {label}: {format_dt(post['scheduled_at'], tz)} ({esc(tz_name(tz))})")
        else:
            lines.append("⏰ Публикация: сразу, в очередь или ко времени — кнопка «🚀 Опубликовать»")
    lines.append(_length_line(post))
    if not premium_emoji and has_custom_emoji(post.get("text_md") or ""):
        lines.append(PREMIUM_EMOJI_WARNING)

    if post["status"] == POST_FAILED and post.get("error") and not edit:
        lines += ["", f"❌ Прошлая попытка не удалась: {esc(post['error'])}"]
    if error:
        lines += ["", f"❗️ <b>Ошибка:</b> {esc(error)}"]
    if note:
        lines += ["", note]
    if edit and (data.get("overrides") or data.get("media_changed")):
        lines += ["", "💡 Изменения ещё не применены — нажмите «💾 Сохранить изменения»."]
    lines += ["", "<i>Чтобы заменить текст или медиа, просто пришлите новые. Несколько фото или видео "
                  "одним сообщением станут альбомом.</i>"]
    return "\n".join(lines)


def panel_kb(post: dict, data: dict) -> InlineKeyboardMarkup:
    post_id = post["id"]

    def cb(action: str) -> ComposeCb:
        return ComposeCb(action=action, post_id=post_id)

    album = is_album(post)
    has_buttons = bool(count_buttons(post.get("buttons")))
    if data.get("edit"):
        first = [ib("✏️ Текст", cb("text"))]
        if has_media(post):
            first.append(ib("🖼 Медиа", cb("media")))
        if not album or has_buttons:
            first.append(ib("🔘 Кнопки", cb("buttons")))
        return kb(
            first,
            [ib("⚙️ Опции", cb("options"))],
            [ib("💾 Сохранить изменения", cb("save"), style="success")],
            [ib("✖️ Выйти без сохранения", cb("discard"))],
        )

    scheduled = post["status"] == POST_SCHEDULED
    n_targets = len(selected_targets(post, edit=False))
    if album and not has_buttons:
        content_rows = [[ib("✏️ Текст", cb("text")), ib("🖼 Медиа", cb("media"))],
                        [ib("🔘 Кнопки (недоступны для альбома)", cb("buttons"))]]
    else:
        content_rows = [[ib("✏️ Текст", cb("text")), ib("🖼 Медиа", cb("media")), ib("🔘 Кнопки", cb("buttons"))]]
    return kb(
        *content_rows,
        [ib(f"📢 Каналы ({n_targets})", cb("channels")), ib("⚙️ Опции", cb("options"))],
        [ib("🚀 Опубликовать", cb("publish"), style="success")],
        [ib("↩️ Снять с расписания", cb("unschedule"))] if scheduled else None,
        [ib("✅ Закрыть", cb("close")),
         ib("🗑 Удалить пост" if scheduled else "🗑 Удалить черновик", cb("delete"), style="danger")],
    )


def back_row(post_id: int, action: str = "back") -> list:
    return [ib("↩️ Назад", ComposeCb(action=action, post_id=post_id))]


# ------------------------------------------------------------------ prompts

def text_prompt(post: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        "✏️ <b>Пришлите новый текст поста</b>",
        "Markdown: <code>**жирный**</code>, <code>*курсив*</code>, <code>__подчёркнутый__</code>, "
        "<code>~~зачёркнутый~~</code>, <code>||спойлер||</code>, <code>`код`</code>, "
        "<code>[ссылка](https://…)</code>, <code>&gt; цитата</code>. Подробнее — /help.",
    ]
    if has_media(post):
        lines.append(f"У поста с медиа подпись — не больше {CAPTION_LIMIT} символов.")
    source = (post.get("text_md") or "").strip()
    if source and len(source) <= SHOW_SOURCE_LIMIT:
        lines += ["", "Текущий текст (нажмите, чтобы скопировать):", f"<pre>{esc(source)}</pre>"]
    elif source:
        lines += ["", "Текущий текст слишком длинный, чтобы показать его здесь."]
    rows = []
    if has_media(post) and source:
        rows.append([ib("🧹 Убрать подпись", ComposeCb(action="rmtext", post_id=post["id"]))])
    rows.append(back_row(post["id"]))
    return "\n".join(lines), kb(*rows)


def media_prompt(post: dict, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Media menu: what the post has, how new media will be used, add / replace / remove."""
    post_id = post["id"]
    edit = bool(data.get("edit"))
    media = post_media(post)
    mode = data.get("media_mode") or "replace"
    lines = ["🖼 <b>Медиа поста</b>"]
    if media:
        lines.append(f"Сейчас: {describe_media(media)}")
        if len(media) > 1:
            lines += [f"{index}. {MEDIA_ICONS.get(item.get('type'), '🖼')} {TYPE_LABELS.get(item.get('type'), 'фото')}"
                      for index, item in enumerate(media, 1)]
    else:
        lines.append("Сейчас медиа нет.")
    lines.append("")
    if edit:
        if len(media) > 1:
            lines.append(f"Пришлите альбом ровно из {len(media)} фото/видео — он заменит текущий в каналах после "
                         "«💾 Сохранить изменения». Telegram не позволяет менять количество файлов "
                         "опубликованного альбома.")
        else:
            lines.append("Пришлите новое фото, видео или GIF — оно заменит текущее в каналах после "
                         "«💾 Сохранить изменения».")
    elif mode == "add" and media:
        left = max(0, MAX_ALBUM - len(media))
        lines.append(f"➕ <b>Добавление медиа</b>: присылайте фото и видео — по одному или несколько сразу, "
                     f"каждое добавится к текущим (можно ещё {left} из {MAX_ALBUM}). GIF в альбом добавить нельзя.")
        lines.append("Закончили — нажмите «✔️ Готово».")
    else:
        lines.append("Пришлите фото, видео или GIF — они заменят текущие медиа. Несколько фото или видео "
                     f"одним сообщением станут альбомом (до {MAX_ALBUM}).")
    lines.append("<i>Присылайте как фото или видео (со сжатием), а не файлом.</i>")

    rows: list[list] = []
    if not edit:
        if media:
            add_mark = "• " if mode == "add" else ""
            replace_mark = "• " if mode != "add" else ""
            rows.append([ib(f"{add_mark}➕ Добавить медиа", ComposeCb(action="media_add", post_id=post_id)),
                         ib(f"{replace_mark}🔄 Заменить", ComposeCb(action="media_replace", post_id=post_id))])
        if len(media) > 1:
            removal = [ib(f"❌ {index}", ComposeMediaCb(post_id=post_id, index=index - 1))
                       for index in range(1, len(media) + 1)]
            rows += [removal[i:i + 5] for i in range(0, len(removal), 5)]
        if media:
            rows.append([ib("🗑 Убрать медиа", ComposeCb(action="rmmedia", post_id=post_id), style="danger")])
    if not edit and mode == "add" and media:
        rows.append([ib("✔️ Готово", ComposeCb(action="back", post_id=post_id), style="primary")])
    else:
        rows.append(back_row(post_id))
    return "\n".join(lines), kb(*rows)


def buttons_prompt(post: dict) -> tuple[str, InlineKeyboardMarkup]:
    current = buttons_to_text(post.get("buttons") or [])
    if is_album(post):
        lines = ["🔘 <b>Кнопки под постом</b>", "", f"⚠️ {esc(BUTTONS_ALBUM_ERROR)}"]
    else:
        lines = ["🔘 <b>Кнопки под постом</b>", "", esc(FORMAT_HELP)]
    if current and len(current) <= SHOW_SOURCE_LIMIT:
        lines += ["", "Сейчас (нажмите, чтобы скопировать):", f"<pre>{esc(current)}</pre>"]
    if not is_album(post):
        lines += ["", "Пришлите кнопки одним сообщением."]
    rows = []
    if current:
        rows.append([ib("🗑 Убрать кнопки", ComposeCb(action="rmbuttons", post_id=post["id"]), style="danger")])
    rows.append(back_row(post["id"]))
    return "\n".join(lines), kb(*rows)


def schedule_prompt(post: dict, tz: Any, *, back: str = "publish") -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    text = (
        "⏰ <b>Когда опубликовать?</b>\n"
        f"Сейчас: {now} ({esc(tz_name(tz))})\n\n"
        "Пришлите время, например:\n"
        "• <code>18:30</code> — сегодня (или завтра, если время прошло)\n"
        "• <code>25.12 10:00</code> или <code>25.12.2026 10:00</code>\n"
        "• <code>завтра 09:00</code>\n"
        "• <code>+30m</code>, <code>+2h</code>, <code>+1d</code>, <code>+1h30m</code>\n"
        "• <code>через 2 часа</code>, <code>через 15 минут</code>"
    )
    presets = [ib(label, ComposeCb(action=f"at_{code}", post_id=post["id"]))
               for code, (label, _) in SCHEDULE_PRESETS.items()]
    return text, kb(presets[:2], presets[2:], back_row(post["id"], back))


def _channels_list(targets: list[dict], budget: int = 2800) -> str:
    lines = []
    for target in targets:
        warn = "" if target.get("channel_status") == CH_ACTIVE else " ⚠️ недоступен"
        lines.append(f"• {esc(target_title(target))}{warn}")
    return "\n".join(cut_lines(lines, budget, more=MORE))


def publish_choice(post: dict, tz: Any) -> tuple[str, InlineKeyboardMarkup]:
    """«🚀 Опубликовать»: now, into the channels' queues, or at a given time."""
    post_id = post["id"]
    targets = selected_targets(post, edit=False)
    n = len(targets)
    lines = [f"🚀 <b>Публикация поста #{post_id}</b>",
             f"📢 {n} {plural(n, 'канал', 'канала', 'каналов')}:", _channels_list(targets), ""]
    if post["status"] == POST_SCHEDULED and post.get("scheduled_at"):
        lines += [f"Сейчас пост запланирован на {format_dt(post['scheduled_at'], tz)} — выберите новый вариант "
                  "или вернитесь назад.", ""]
    lines += [
        "Как опубликовать?",
        "🚀 <b>Сейчас</b> — сразу во все выбранные каналы.",
        "📅 <b>В очередь</b> — в ближайшее свободное время по расписанию каждого канала.",
        "⏰ <b>Ко времени</b> — в указанное время во все выбранные каналы.",
    ]

    def cb(action: str) -> ComposeCb:
        return ComposeCb(action=action, post_id=post_id)

    return "\n".join(lines), kb(
        [ib("🚀 Сейчас", cb("now"), style="success")],
        [ib("📅 В очередь по расписанию", cb("queue"), style="primary")],
        [ib("⏰ Ко времени…", cb("schedule"))],
        back_row(post_id),
    )


def confirm_now(post: dict, tz: Any) -> tuple[str, InlineKeyboardMarkup]:
    targets = selected_targets(post, edit=False)
    n = len(targets)
    text = f"🚀 <b>Опубликовать в {n} {plural(n, 'канал', 'канала', 'каналов')}?</b>\n{_channels_list(targets)}"
    if post["status"] == POST_SCHEDULED and post.get("scheduled_at"):
        text += (f"\n\nПост запланирован на {format_dt(post['scheduled_at'], tz)} — "
                 "он будет опубликован прямо сейчас.")
    markup = kb([ib("✅ Да, опубликовать", ComposeCb(action="confirm", post_id=post["id"]), style="success"),
                 ib("↩️ Назад", ComposeCb(action="publish", post_id=post["id"]))])
    return text, markup


QUEUE_UNAVAILABLE = "канал недоступен"
QUEUE_NO_FREE_TIME = "нет свободного времени на год вперёд"
QUEUE_NO_SCHEDULE = "нет расписания"
# problem → (title of the group, hint shown once)
QUEUE_PROBLEMS = {
    QUEUE_NO_SCHEDULE: ("Нет расписания", "Задайте его: «📢 Каналы» → канал → «📅 Расписание»."),
    QUEUE_NO_FREE_TIME: ("Нет свободного времени на год вперёд", ""),
    QUEUE_UNAVAILABLE: ("Недоступны", "Бот больше не администратор этих каналов."),
}


async def queue_preview(db: Any, post: dict, tz: Any) -> tuple[list[tuple[dict, int | None, str | None]], list[int]]:
    """[(target, next free slot | None, problem | None)] for the selected channels and the ids of
    the channels the post can be queued in."""
    targets = selected_targets(post, edit=False)
    ids = [t["channel_id"] for t in targets]
    preview = await plan_service.preview_queue(db, ids, tz, post_id=post["id"]) if ids else {}
    rows = []
    queueable = []
    for target in targets:
        cid = target["channel_id"]
        ts = preview.get(cid)
        problem = None
        if target.get("channel_status") != CH_ACTIVE:
            problem, ts = QUEUE_UNAVAILABLE, None
        elif ts is None:
            slots = await db.get_channel_slots(cid)
            problem = QUEUE_NO_FREE_TIME if slots else QUEUE_NO_SCHEDULE
        else:
            queueable.append(cid)
        rows.append((target, ts, problem))
    return rows, queueable


async def queue_screen(db: Any, post: dict, tz: Any) -> tuple[str, InlineKeyboardMarkup]:
    """«📅 В очередь»: one line per channel that gets a time, channels with a problem grouped by the
    problem (each hint once) — the screen stays below Telegram's 4096 characters (finding #17)."""
    rows, queueable = await queue_preview(db, post, tz)
    post_id = post["id"]
    lines = ["📅 <b>В очередь по расписанию</b>",
             f"Пост #{post_id} встанет в ближайшее свободное время каждого канала:", ""]
    problems: list[str] = []
    for problem, (title, hint) in QUEUE_PROBLEMS.items():
        names = [target_title(target) for target, _, p in rows if p == problem]
        if names:
            problems.append(f"⚠️ {title} ({len(names)}): {names_text(names, 500)}" + (f"\n{hint}" if hint else ""))
    timed = [f"• {esc(target_title(target))} — {when_label(ts, tz)}" for target, ts, _ in rows if ts]
    lines += cut_lines(timed, 3000 - sum(len(p) for p in problems), more=MORE)
    if problems:
        if timed:
            lines.append("")
        lines += problems
    skipped = len(rows) - len(queueable)
    if queueable and skipped:
        lines += ["", "⚠️ Каналы без времени будут сняты с поста — опубликуйте в них отдельно."]
    elif not queueable:
        lines += ["", "Ни у одного выбранного канала нет свободного времени по расписанию. "
                      "Задайте расписание в «📢 Каналы» → канал → «📅 Расписание» или выберите «⏰ Ко времени»."]
    if queueable:
        label = "✅ Поставить в очередь" if not skipped else f"✅ В очередь ({len(queueable)} из {len(rows)})"
        first = [ib(label, ComposeCb(action="queue_ok", post_id=post_id), style="success")]
    else:
        first = [ib("🚫 Нет каналов с расписанием", ComposeCb(action="queue_none", post_id=post_id))]
    return "\n".join(lines), kb(first, back_row(post_id, "publish"))


# ------------------------------------------------------------------ pickers

PICKER_ROWS = 40  # channel rows per page of the picker (Telegram allows ~100 buttons per keyboard)


async def channels_picker(db: Any, post: dict, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Channels grouped by folders: a folder row «📁 Name (k/n)» toggles all of its channels.

    Selected targets whose channel is no longer active are listed too, so they can be unticked
    (findings #11/#22). Many channels are paged (`page`, ◀️ ▶️ rows — ComposeCb action
    ``chpg<N>``), so the keyboard stays within Telegram's limits.
    """
    groups = await folder_service.grouped_channels(db, ("active",))
    selected = {t["channel_id"] for t in selected_targets(post, edit=False)}
    post_id = post["id"]
    active_ids: set[int] = set()
    with_folders = any(group["folder"] is not None and group["channels"] for group in groups)
    items = []
    for group in groups:
        channels = group["channels"]
        if not channels:
            continue
        active_ids |= {ch["id"] for ch in channels}
        prefix = ""
        if with_folders:
            picked = sum(1 for ch in channels if ch["id"] in selected)
            folder = group["folder"]
            name = f"📁 {clip(folder['name'], 40)}" if folder else "📂 Без папки"
            items.append([ib(f"{name} ({picked}/{len(channels)})"[:64],
                             ComposeFolderCb(post_id=post_id, folder_id=folder["id"] if folder else 0))])
            prefix = "↳ "
        for channel in channels:
            mark = "✅" if channel["id"] in selected else "▫️"
            items.append([ib(f"{prefix}{mark} {clip(channel_title(channel), 50)}"[:64],
                             ComposeChannelCb(post_id=post_id, channel_id=channel["id"]))])
    stale = [t for t in selected_targets(post, edit=False) if t["channel_id"] not in active_ids]
    for target in stale:
        items.append([ib(f"✅ ⚠️ {clip(target_title(target), 40)} (недоступен)"[:64],
                         ComposeChannelCb(post_id=post_id, channel_id=target["channel_id"]))])
    pages = max(1, -(-len(items) // PICKER_ROWS))
    page = min(max(page, 0), pages - 1)
    rows = items[page * PICKER_ROWS:(page + 1) * PICKER_ROWS]
    if pages > 1:
        rows.append([ib("◀️", ComposeCb(action=f"chpg{(page - 1) % pages}", post_id=post_id)),
                     ib(f"{page + 1}/{pages}", ComposeCb(action=f"chpg{page}", post_id=post_id)),
                     ib("▶️", ComposeCb(action=f"chpg{(page + 1) % pages}", post_id=post_id))])
    if len(active_ids) > 1 or stale or (selected and not active_ids):
        rows.append([ib("☑️ Все", ComposeCb(action="ch_all", post_id=post_id)),
                     ib("▫️ Никого", ComposeCb(action="ch_none", post_id=post_id))])
    rows.append([ib("✔️ Готово", ComposeCb(action="back", post_id=post_id), style="primary")])
    text = f"📢 <b>Куда публиковать пост #{post_id}?</b>\nОтметьте каналы."
    if with_folders:
        text += " Строка папки отмечает или снимает все её каналы."
    if pages > 1:
        text += f"\nВыбрано: {len(selected)} из {len(active_ids)} · страница {page + 1} из {pages}"
    if stale:
        text += "\n\n⚠️ Некоторые выбранные каналы больше недоступны — снимите их, иначе публикация в них не удастся."
    if not active_ids:
        text += "\n\nНет подключённых каналов — добавьте канал в разделе «📢 Каналы»."
    return text, kb(*rows)


def options_picker(post: dict, *, edit: bool) -> tuple[str, InlineKeyboardMarkup]:
    post_id = post["id"]
    rows = []
    for key in available_options(post, edit=edit):
        mark = "✅" if post.get(key) else "▫️"
        rows.append([ib(f"{mark} {OPTIONS[key]}", ComposeOptionCb(post_id=post_id, option=key))])
    rows.append([ib("✔️ Готово", ComposeCb(action="opts_done", post_id=post_id), style="primary")])
    text = (
        "⚙️ <b>Опции поста</b>\n"
        "🔕 Без звука — подписчики получат уведомление без звука.\n"
        "🔒 Запрет пересылки — пост нельзя переслать или сохранить.\n"
        "🔗 Без превью ссылок — для текстовых постов.\n"
        "⬆️ Подпись над медиа — для постов с фото, видео, GIF или альбомом."
    )
    if edit:
        text += "\n\nУ опубликованного поста Telegram позволяет менять только эту опцию."
    return text, kb(*rows)

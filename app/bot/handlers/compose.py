"""Post composer: draft → preview + control panel → publish now / queue / schedule / save edits.

Every change re-sends a fresh preview (the post exactly as it will look in the channel — one
message, or every message of an album) and the control panel below it; the previous preview and
panel are deleted. Session data kept in FSM:

    post_id        the post being composed
    edit           True for an already published post (shown in at least one channel): changes are
                   kept in `overrides` and applied to the channels only on «💾 Сохранить изменения»,
                   so the published post in the DB never diverges from what the channels show
    overrides      edit mode: unsaved field changes (text_md, media, buttons, options)
    media_changed  edit mode: the media was replaced (→ edit_message_media)
    restored       the session was re-created from the DB (FSM lost: restart, other section…)
    preview_ids, panel_id, prompt_id   messages to clean up
    media_mode     'replace' | 'add' — what new media does while the media menu is open ('add'
                   keeps collecting until «✔️ Готово» or a full album)
    pending        an album waiting for «Убрать кнопки» (albums cannot have buttons)
    picker_page    the page of the channel picker (many channels)

Drafts (draft / scheduled / failed, nothing sent yet) are edited directly in the DB, but only
while they still are drafts: every write is conditional and every input re-reads the post, so a
post published meanwhile (scheduler, web panel) is never written into — the composer closes with
an explanation instead (review findings #8/#20). Panel buttons carry the post id, so a panel
keeps working after a restart (the session is re-created from the DB).

Albums: Telegram sends each photo / video of an album as its own message. Album items are only
buffered (`app.bot.albums.AlbumCollector`) and handled together once the album is complete.
Rendering of the panel and prompts lives in `app.bot.compose_view`.
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import (
    ComposeCb,
    ComposeChannelCb,
    ComposeFolderCb,
    ComposeMediaCb,
    ComposeOptionCb,
    MenuCb,
)
from app.bot.compose_view import (
    BUTTONS_ALBUM_ERROR,
    NO_CHANNELS_PROBLEM,
    available_options,
    buttons_prompt,
    channels_picker,
    close_text,
    confirm_now,
    has_media,
    is_album,
    is_published,
    media_prompt,
    options_picker,
    panel_kb,
    panel_text,
    publish_choice,
    publish_problem,
    queue_preview,
    queue_screen,
    saved_text,
    schedule_prompt,
    selected_targets,
    target_title,
    text_prompt,
    when_label,
)
from app.bot.filters import TextIs, UserInput
from app.bot.handlers.common import leave_state
from app.bot.keyboards import (
    BTN_NEW,
    OPTIONS,
    SCHEDULE_PRESETS,
    cancel_kb,
    ib,
    kb,
    main_menu,
    no_channels_kb,
)
from app.bot.states import ComposeStates
from app.bot.utils import (
    CONFIRM_USED_TEXT,
    NO_PREVIEW,
    callback_chat_id,
    callback_message_id,
    cut_lines,
    edit_or_send,
    esc,
    fit_html,
    names_text,
    plural,
    premium_emoji_enabled,
    safe_answer,
    safe_delete,
    tz_name,
)
from app.buttons import ButtonsError, count_buttons, parse_buttons
from app.db import (
    CH_ACTIVE,
    POST_DELETED,
    POST_DRAFT,
    POST_FAILED,
    POST_FIELDS,
    POST_SCHEDULED,
    POST_SENDING,
    TARGET_SENT,
    _encode_aliases,
    now,
)
from app.markdown import html_to_md, md_to_html
from app.media import (
    ALBUM_TYPES,
    MAX_ALBUM,
    MediaError,
    edit_class,
    media_signature,
    normalize_media,
    post_media,
)
from app.services import folders as folder_service
from app.services import plan as plan_service
from app.services.plan import PlanError
from app.services.posting import (
    PostError,
    apply_file_ids,
    bot_id_of,
    describe_telegram_error,
    edit_published_post,
    learned_file_ids,
    normalize_kind,
    publish_post,
    send_post,
)
from app.services.scheduler import format_dt, parse_when

log = logging.getLogger(__name__)

NEW_PROMPT = (
    "📝 <b>Новый пост</b>\n\n"
    "Пришлите текст поста — или фото, видео, GIF с подписью. Несколько фото или видео одним "
    "сообщением станут альбомом.\n\n"
    "Можно использовать Markdown: <code>**жирный**</code>, <code>*курсив*</code>, "
    "<code>__подчёркнутый__</code>, <code>~~зачёркнутый~~</code>, <code>||спойлер||</code>, "
    "<code>[ссылка](https://…)</code>, <code>&gt; цитата</code> — или обычное форматирование "
    "Telegram. Подробнее — /help."
)
NO_CHANNELS_TEXT = (
    "У вас пока нет подключённых каналов 🙁\n\n"
    "Сделайте бота администратором канала с правом публикации — я предложу его подключить. "
    "Если бот уже администратор, нажмите «➕ Добавить канал»."
)
TEXT_TO_MEDIA_ERROR = "К уже опубликованному текстовому посту нельзя добавить фото, видео или GIF."
SINGLE_TO_ALBUM_ERROR = ("К опубликованному посту с одним файлом нельзя добавить альбом — пришлите одно фото, "
                         "видео или GIF.")
CHANGED_TEXT = (
    "ℹ️ Пост #{id} уже опубликован (по расписанию или из веб-панели) — изменения не применены, "
    "редактор закрыт.\nЧтобы изменить опубликованный пост, откройте его: «🗂 Посты» → пост → "
    "«✏️ Редактировать»."
)
STALE_SESSION_TEXT = "Пост #{id} изменился (например, в веб-панели) — откройте его заново в «🗂 Посты»."
LOST_CHANGES_TEXT = (
    "Несохранённых изменений нет — они были сброшены (переход в другой раздел, /cancel или "
    "перезапуск бота). Пришлите изменения заново."
)
STALE_PANEL = "Эта панель устарела: пост уже изменился. Откройте его заново в «🗂 Посты»."
SENDING_NOW_TEXT = "Пост сейчас публикуется — подождите и откройте его заново"
WAITING_TEXT_HINT = "Сейчас я жду текст. Или нажмите «↩️ Назад»."

DRAFT_STATUSES = (POST_DRAFT, POST_SCHEDULED, POST_FAILED)
# A post that is still a draft (nothing sent, not being sent) is edited directly in the DB.
DRAFT_GUARD = (
    "status IN ('draft', 'scheduled', 'failed') AND NOT EXISTS ("
    "SELECT 1 FROM post_targets t WHERE t.post_id = posts.id AND t.status = 'sent')"
)
BOOL_FIELDS = ("disable_preview", "silent", "protect", "caption_above")


class InputError(Exception):
    """User input that cannot become a post (Russian message)."""


class PostChanged(Exception):
    """The composer's post changed status underneath (published / deleted): stop editing it."""


@dataclass
class Content:
    """One input of the composer: a text, or media (one message or a whole album) with a caption."""

    text_md: str | None = None  # None = the input has no text / caption
    media: list[dict] | None = None  # None = the input has no media
    skipped: int = 0  # album items that are not photos / videos / GIFs
    extra_captions: int = 0  # album items whose own caption was dropped
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ input parsing

def message_media(message: Message, bot_id: int | None = None) -> dict | None:
    """Media item of a photo / video / GIF message (largest photo size), else None.

    `bot_id` — the bot that received the message: its file_ids only work for that bot, so the
    item remembers it as `file_bot` (SPEC_V22).
    """
    if message.photo:
        size = message.photo[-1]
        return _clean_item({"type": "photo", "file_id": size.file_id, "file_bot": bot_id, "width": size.width,
                            "height": size.height, "size": size.file_size})
    if message.animation:  # a GIF also fills `document`, so it is checked first
        anim = message.animation
        return _clean_item({"type": "animation", "file_id": anim.file_id, "file_bot": bot_id, "width": anim.width,
                            "height": anim.height, "duration": anim.duration, "size": anim.file_size,
                            "mime": anim.mime_type})
    if message.video:
        video = message.video
        return _clean_item({"type": "video", "file_id": video.file_id, "file_bot": bot_id, "width": video.width,
                            "height": video.height, "duration": video.duration, "size": video.file_size,
                            "mime": video.mime_type})
    return None


def _clean_item(item: dict) -> dict:
    return {key: value for key, value in item.items() if value is not None}


def _unsupported_hint(message: Message) -> str:
    mime = (message.document.mime_type or "") if message.document else ""
    if mime.startswith("image/"):
        return "Пришлите картинку как фото (со сжатием), а не файлом."
    if mime.startswith("video/"):
        return "Пришлите видео как видео, а не файлом."
    return "Поддерживаются текст, фото, видео и GIF. Пришлите текст поста или медиа."


def content_from_messages(messages: list[Message], bot_id: int | None = None) -> Content:
    """Composer input from one message or the messages of an album (ordered); `bot_id` — the bot
    that received them (see `message_media`)."""
    if len(messages) == 1 and messages[0].text:
        return Content(text_md=html_to_md(messages[0].html_text))
    content = Content(media=[])
    for message in messages:
        item = message_media(message, bot_id)
        if item is None:
            content.skipped += 1
            continue
        content.media.append(item)
        if message.caption:
            if content.text_md is None:
                content.text_md = html_to_md(message.html_text)
            else:
                content.extra_captions += 1
    if not content.media:
        raise InputError(_unsupported_hint(messages[0]))
    if content.skipped:
        content.notes.append(f"ℹ️ Пропущено файлов: {content.skipped} — в пост можно добавить только фото, "
                             "видео и GIF.")
    if content.extra_captions:
        content.notes.append("ℹ️ У альбома одна подпись — взята первая, остальные подписи пропущены.")
    return content


# ------------------------------------------------------------------ post helpers

def open_error(post: dict | None) -> str | None:
    """Why the post cannot be opened in the composer (None when it can)."""
    if post is None:
        return "Пост не найден — возможно, его удалили."
    if post["status"] == POST_SENDING:
        return "Пост сейчас публикуется — подождите немного."
    if post["status"] == POST_DELETED:
        return "Пост удалён из каналов — его можно только продублировать."
    return None


def session_problem(post: dict | None, data: dict) -> str | None:
    """Why the composer session can no longer edit its post (None when it can)."""
    error = open_error(post)
    if error:
        return error
    if is_published(post) != bool(data.get("edit")):
        template = CHANGED_TEXT if is_published(post) else STALE_SESSION_TEXT
        return template.format(id=post["id"])
    return None


def session_data(post: dict, *, panel_id: int | None = None, restored: bool = False) -> dict:
    return {
        "post_id": post["id"],
        "edit": is_published(post),
        "overrides": {},
        "media_changed": False,
        "restored": restored,
        "preview_ids": [],
        "panel_id": panel_id,
        "prompt_id": None,
        "media_mode": None,
        "pending": None,
    }


async def load_post(db: Any, data: dict) -> dict | None:
    """The post as the composer sees it (DB row + unsaved edit-mode changes)."""
    post = await db.get_post(data["post_id"])
    if post is not None and data.get("edit"):
        post.update(data.get("overrides") or {})
    return post


async def update_draft(db: Any, post_id: int, **fields: Any) -> bool:
    """Write post fields only while the post is still a draft (nothing sent, not being sent).

    One conditional UPDATE, so a post claimed / published by the scheduler or the web panel at
    the same moment is never changed. Returns False when the post is no longer a draft.
    """
    _encode_aliases(fields)
    unknown = set(fields) - POST_FIELDS
    if unknown:
        raise ValueError(f"Unknown post fields: {unknown}")
    for key in BOOL_FIELDS:
        if key in fields:
            fields[key] = int(bool(fields[key]))
    fields.setdefault("updated_at", now())
    cols = ", ".join(f"{key} = ?" for key in fields)
    cur = await db.execute(f"UPDATE posts SET {cols} WHERE id = ? AND {DRAFT_GUARD}", (*fields.values(), post_id))
    return cur.rowcount == 1


async def save_fields(db: Any, state: FSMContext, data: dict, **fields: Any) -> dict:
    """Change post fields: in the DB for drafts, in FSM overrides in edit mode. Returns new data.

    Raises PostChanged (with the reason) when a draft is no longer a draft.
    """
    if data.get("edit"):
        overrides = {**(data.get("overrides") or {}), **fields}
        await state.update_data(overrides=overrides)
        return {**data, "overrides": overrides}
    if "text_md" in fields:
        source = (fields["text_md"] or "").strip()
        fields["text_html"] = md_to_html(source) if source else ""
    if not await update_draft(db, data["post_id"], **fields):
        post = await db.get_post(data["post_id"])
        raise PostChanged(session_problem(post, data) or STALE_SESSION_TEXT.format(id=data["post_id"]))
    return data


def media_edit_error(post: dict, new_media: list[dict]) -> str | None:
    """Edit mode: why `new_media` cannot replace the published media (None when it can)."""
    published = normalize_kind(post.get("published_kind")) or edit_class(post_media(post))
    new_class = edit_class(new_media)
    if published == new_class:
        return None
    if published == "text":
        return TEXT_TO_MEDIA_ERROR
    if published.startswith("album:"):
        count = published.split(":", 1)[1]
        return (f"В опубликованном альбоме {count} файлов — пришлите альбом ровно из {count} фото/видео. "
                "Telegram не позволяет добавлять или убирать файлы опубликованного альбома.")
    return SINGLE_TO_ALBUM_ERROR


def _needs_retry(post: dict) -> bool:
    """A published post with a channel whose last edit failed (a save without changes retries it)."""
    signature = media_signature(post_media(post))
    for target in post["targets"]:
        if target["status"] != TARGET_SENT:
            continue
        if target.get("error"):
            return True
        if has_media(post) and target.get("media_sig") is not None and target["media_sig"] != signature:
            return True
    return False


# ------------------------------------------------------------------ preview & panel

async def _remember_uploads(db: Any, post: dict, messages: list[Any], bot_id: int | None = None) -> None:
    """A draft's uploaded files (web) were just sent as a preview: keep Telegram's file_ids
    (with `file_bot` = the bot that sent them)."""
    try:
        sent_media = normalize_media(post_media(post))
    except MediaError:
        return
    learned = learned_file_ids(sent_media, messages)
    if not learned:
        return
    current = await db.get_post(post["id"])
    if current is None:
        return
    media = apply_file_ids(post_media(current), learned, bot_id)
    if media is not None:
        await update_draft(db, post["id"], media=media)


async def _send_preview(bot: Any, db: Any, config: Any, post: dict, chat_id: int,
                        *, edit: bool) -> tuple[list[int], str | None]:
    """Send the post to the admin's chat. Returns (message ids, None) or ([], error)."""
    try:
        messages = await send_post(bot, post, chat_id, config)
    except PostError as exc:
        return [], str(exc)
    except TelegramAPIError as exc:
        log.warning("Preview of post %s failed: %s", post["id"], exc)
        return [], f"Не удалось показать превью: {describe_telegram_error(exc)}"
    if not edit:
        await _remember_uploads(db, post, messages, bot_id_of(bot))
    return [m.message_id for m in messages], None


def in_add_mode(current_state: str | None, data: dict) -> bool:
    """The media menu collects media to append («➕ Добавить медиа», finding #14)."""
    return current_state == ComposeStates.media.state and data.get("media_mode") == "add"


async def refresh(bot: Any, state: FSMContext, db: Any, config: Any, chat_id: int, *,
                  note: str | None = None) -> None:
    """Delete the old preview / panel / prompt and send fresh ones.

    Below the preview goes the main panel — or, while «➕ Добавить медиа» collects media, the media
    menu (with «✔️ Готово»), so the next photo is appended too.
    """
    data = await state.get_data()
    await safe_delete(bot, chat_id, *(data.get("preview_ids") or []), data.get("panel_id"), data.get("prompt_id"))
    post = await load_post(db, data)
    if post is None:
        await state.clear()
        await bot.send_message(chat_id=chat_id, text="Пост не найден — возможно, его удалили в веб-панели.",
                               reply_markup=main_menu())
        return
    preview_ids, error = await _send_preview(bot, db, config, post, chat_id, edit=bool(data.get("edit")))
    updates: dict[str, Any] = {}
    if in_add_mode(await state.get_state(), data) and not error:
        text, markup = media_prompt(post, data)
        if note:
            text += f"\n\n{note}"
    else:
        if in_add_mode(await state.get_state(), data):  # the preview failed: show why on the panel
            await state.set_state(ComposeStates.menu)
            updates["media_mode"] = None
        text = panel_text(post, data, config.tz, error=error, note=note,
                          premium_emoji=await premium_emoji_enabled(db))
        markup = panel_kb(post, data)
    panel = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    await state.update_data(preview_ids=preview_ids, panel_id=panel.message_id, prompt_id=None, **updates)


async def close_session(bot: Any, state: FSMContext, chat_id: int, data: dict, text: str) -> None:
    """Close the composer because its post cannot be edited any more (with the reason)."""
    await state.clear()
    await safe_delete(bot, chat_id, data.get("panel_id"), data.get("prompt_id"))
    await bot.send_message(chat_id=chat_id, text=esc(text), reply_markup=main_menu(),
                           link_preview_options=NO_PREVIEW)


async def live_post(bot: Any, state: FSMContext, db: Any, chat_id: int, data: dict) -> dict | None:
    """Re-read the session's post before a change; closes the composer (and returns None) when
    the post was published, deleted or is being sent meanwhile."""
    post = await db.get_post(data["post_id"])
    problem = session_problem(post, data)
    if problem:
        await close_session(bot, state, chat_id, data, problem)
        return None
    if data.get("edit"):
        post.update(data.get("overrides") or {})
    return post


async def open_editor(bot: Any, state: FSMContext, db: Any, config: Any, chat_id: int, post_id: int,
                      *, note: str | None = None) -> str | None:
    """Open an existing post in the composer. Returns an error text when it cannot be opened."""
    post = await db.get_post(post_id)
    error = open_error(post)
    if error:
        return error
    await leave_state(bot, chat_id, state)
    await state.set_state(ComposeStates.menu)
    await state.set_data(session_data(post))
    await refresh(bot, state, db, config, chat_id, note=note)
    return None


# ------------------------------------------------------------------ entry

async def start_new(bot: Any, chat_id: int, state: FSMContext, db: Any, config: Any) -> None:
    data = await state.get_data()
    previous = data.get("post_id") if await state.get_state() in ComposeStates and not data.get("edit") else None
    await leave_state(bot, chat_id, state)
    if not await db.list_channels([CH_ACTIVE]):
        await bot.send_message(chat_id=chat_id, text=NO_CHANNELS_TEXT, reply_markup=no_channels_kb())
        return
    text = NEW_PROMPT
    if previous:
        text = f"💾 {saved_text(await db.get_post(previous), config.tz)}\n\n" + text
    await state.set_state(ComposeStates.start)
    prompt = await bot.send_message(chat_id=chat_id, text=text, reply_markup=cancel_kb(),
                                    link_preview_options=NO_PREVIEW)
    await state.update_data(prompt_id=prompt.message_id)


async def cmd_new(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    await start_new(message.bot, message.chat.id, state, db, config)


async def cb_new(callback: CallbackQuery, state: FSMContext, db: Any, config: Any) -> None:
    await safe_answer(callback)
    await start_new(callback.bot, callback_chat_id(callback), state, db, config)


# ------------------------------------------------------------------ applying input

async def create_draft(bot: Any, state: FSMContext, db: Any, config: Any, chat_id: int, user_id: int,
                       content: Content) -> None:
    """The first text / media after «📝 Новый пост»: create the draft and show it."""
    data = await state.get_data()
    try:
        media = normalize_media(content.media or [])
    except MediaError as exc:
        await bot.send_message(chat_id=chat_id, text=f"❌ {esc(exc)}")
        return
    text_md = content.text_md or ""
    source = text_md.strip()
    post_id = await db.create_post(
        text_md=text_md, text_html=md_to_html(source) if source else "", media=media, status=POST_DRAFT,
        source="bot", created_by=user_id,
    )
    active = await db.list_channels([CH_ACTIVE])
    if len(active) == 1:
        await db.set_post_targets(post_id, [active[0]["id"]])
    post = await db.get_post(post_id)
    new_data = session_data(post)
    new_data["prompt_id"] = data.get("prompt_id")
    await state.set_state(ComposeStates.menu)
    await state.set_data(new_data)
    log.info("Draft %s created by %s", post_id, user_id)
    await refresh(bot, state, db, config, chat_id, note="\n".join(content.notes) or None)


async def apply_content(bot: Any, state: FSMContext, db: Any, config: Any, chat_id: int, content: Content,
                        *, mode: str = "replace") -> None:
    """Text / media input while the panel or the media menu is shown.

    `mode`: 'replace' (new media replace the post's media) or 'add' (appended, drafts only).
    A caption replaces the text.
    """
    data = await state.get_data()
    post = await live_post(bot, state, db, chat_id, data)
    if post is None:
        return
    edit = bool(data.get("edit"))
    fields: dict[str, Any] = {}
    if content.media is not None:
        new_media = content.media if edit or mode != "add" else post_media(post) + content.media
        if edit:
            error = media_edit_error(await db.get_post(post["id"]), new_media)
            if error:
                await bot.send_message(chat_id=chat_id, text=f"❌ {esc(error)}")
                return
        try:
            fields["media"] = normalize_media(new_media)
        except MediaError as exc:
            await bot.send_message(chat_id=chat_id, text=f"❌ {esc(exc)}")
            return
    if content.text_md is not None:
        fields["text_md"] = content.text_md
    if len(fields.get("media") or []) > 1 and count_buttons(post.get("buttons")):
        await _ask_album_without_buttons(bot, state, chat_id, data, post, fields)
        return
    await commit_fields(bot, state, db, config, chat_id, data, fields, note="\n".join(content.notes) or None)


async def commit_fields(bot: Any, state: FSMContext, db: Any, config: Any, chat_id: int, data: dict,
                        fields: dict, *, note: str | None = None) -> None:
    """Save fields of the session's post and show the result (fresh preview + panel).

    «➕ Добавить медиа» stays on after an add (finding #14): every following photo / video is
    appended too, until «✔️ Готово» or a full album — separate photos (forwarded one by one, or
    sent without grouping) must not replace each other.
    """
    adding = in_add_mode(await state.get_state(), data) and "media" in fields and not data.get("edit")
    try:
        data = await save_fields(db, state, data, **fields)
    except PostChanged as exc:
        await close_session(bot, state, chat_id, data, str(exc))
        return
    keep_add = adding and len(fields["media"]) < MAX_ALBUM
    if adding and not keep_add:
        full = f"ℹ️ В альбоме {MAX_ALBUM} файлов — больше Telegram не позволяет."
        note = f"{note}\n{full}" if note else full
    updates: dict[str, Any] = {"media_mode": "add" if keep_add else None, "pending": None}
    if data.get("edit") and "media" in fields:
        updates["media_changed"] = True
    await state.update_data(**updates)
    await state.set_state(ComposeStates.media if keep_add else ComposeStates.menu)
    await refresh(bot, state, db, config, chat_id, note=note)


async def _ask_album_without_buttons(bot: Any, state: FSMContext, chat_id: int, data: dict, post: dict,
                                     fields: dict) -> None:
    """An album for a post with buttons: Telegram allows no buttons under albums — ask first."""
    await safe_delete(bot, chat_id, data.get("prompt_id"))
    n = len(fields["media"])
    prompt = await bot.send_message(
        chat_id=chat_id,
        text=(f"⚠️ <b>Telegram не поддерживает кнопки у альбомов.</b>\n"
              f"У поста #{post['id']} есть кнопки, а новые медиа — альбом из {n} файлов.\n"
              "Убрать кнопки и применить альбом? Или оставьте одно фото/видео, чтобы сохранить кнопки."),
        reply_markup=kb([ib("🗑 Убрать кнопки и применить альбом", ComposeCb(action="album_ok", post_id=post["id"]),
                            style="danger")],
                        [ib("↩️ Не применять", ComposeCb(action="album_no", post_id=post["id"]))]),
    )
    await state.update_data(pending=fields, prompt_id=prompt.message_id)
    if not in_add_mode(await state.get_state(), data):  # «➕ Добавить медиа» keeps collecting
        await state.set_state(ComposeStates.menu)


# ------------------------------------------------------------------ message input

async def on_first_content(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    """The first text / photo / video / GIF after «📝 Новый пост»: create the draft."""
    try:
        content = content_from_messages([message], bot_id_of(message.bot))
    except InputError as exc:
        await message.answer(esc(exc))
        return
    await create_draft(message.bot, state, db, config, message.chat.id, message.from_user.id, content)


async def on_text(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    """New post text (✏️ Текст, or any text while the panel is shown)."""
    await apply_content(message.bot, state, db, config, message.chat.id,
                        Content(text_md=html_to_md(message.html_text)))


async def on_media(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    """A single photo / video / GIF (media menu, or any media while the panel is shown)."""
    content = content_from_messages([message], bot_id_of(message.bot))
    data = await state.get_data()
    in_menu = await state.get_state() == ComposeStates.media.state
    mode = (data.get("media_mode") or "replace") if in_menu else "replace"
    await apply_content(message.bot, state, db, config, message.chat.id, content, mode=mode)


async def on_album_item(message: Message, state: FSMContext, db: Any, config: Any, albums: Any) -> None:
    """One message of an album: only buffered, the album is handled as a whole (`on_album`)."""
    bot = message.bot
    chat_id = message.chat.id
    user_id = message.from_user.id

    async def handle(messages: list[Message], late: bool) -> None:
        try:
            await on_album(bot, state, db, config, chat_id, user_id, messages, late=late)
        except Exception:
            log.exception("Album of %s could not be handled", user_id)
            with contextlib.suppress(Exception):
                await bot.send_message(chat_id=chat_id, text="⚠️ Не удалось обработать альбом — пришлите его ещё раз.")

    albums.add(message, handle, lock_key=state.key)


async def on_album(bot: Any, state: FSMContext, db: Any, config: Any, chat_id: int, user_id: int,
                   messages: list[Message], *, late: bool = False) -> None:
    """A complete album (runs under the user's update lock, after the last item arrived)."""
    current = await state.get_state()
    try:
        content = content_from_messages(messages, bot_id_of(bot))
    except InputError as exc:
        await bot.send_message(chat_id=chat_id, text=esc(exc))
        return
    if current == ComposeStates.start.state:
        await create_draft(bot, state, db, config, chat_id, user_id, content)
    elif current in (ComposeStates.menu.state, ComposeStates.media.state):
        data = await state.get_data()
        mode = "replace"
        if current == ComposeStates.media.state:
            mode = data.get("media_mode") or "replace"
        if late and not data.get("edit"):
            mode = "add"  # the rest of an album whose first part was already taken
        await apply_content(bot, state, db, config, chat_id, content, mode=mode)
    elif current in (ComposeStates.text.state, ComposeStates.buttons.state, ComposeStates.schedule.state):
        await bot.send_message(chat_id=chat_id, text=WAITING_TEXT_HINT)
    else:
        await bot.send_message(chat_id=chat_id, text="Альбом не принят: редактор поста уже закрыт. "
                                                     "Нажмите «📝 Новый пост».", reply_markup=main_menu())


async def on_buttons(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    try:
        rows = parse_buttons(message.text or "")
    except ButtonsError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nИсправьте и пришлите кнопки ещё раз.")
        return
    if not rows:
        await message.answer("Не нашёл ни одной кнопки. Формат: <code>Текст - https://ссылка</code>")
        return
    data = await state.get_data()
    post = await live_post(message.bot, state, db, message.chat.id, data)
    if post is None:
        return
    if is_album(post):
        await message.answer(f"❌ {esc(BUTTONS_ALBUM_ERROR)}")
        return
    await commit_fields(message.bot, state, db, config, message.chat.id, data, {"buttons": rows})


async def on_schedule_text(message: Message, state: FSMContext, db: Any, config: Any) -> None:
    try:
        when = parse_when(message.text or "", config.tz)
    except ValueError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nПопробуйте ещё раз, например <code>18:30</code> "
                             "или <code>завтра 10:00</code>.")
        return
    try:
        problem = await schedule_post(message.bot, state, db, config, message.chat.id, when)
    except PostChanged as exc:
        await close_session(message.bot, state, message.chat.id, await state.get_data(), str(exc))
        return
    if problem:
        await message.answer(f"❌ {esc(problem)}")


async def on_unsupported(message: Message, state: FSMContext) -> None:
    """Content the current composer step does not accept."""
    current = await state.get_state()
    if message.document:
        hint = _unsupported_hint(message)
    elif in_add_mode(current, await state.get_data()):
        hint = "Сейчас я добавляю к посту фото и видео. Закончили — нажмите «✔️ Готово»."
    elif current == ComposeStates.media.state:
        hint = "Сейчас я жду фото, видео или GIF. Или нажмите «↩️ Назад»."
    elif current in (ComposeStates.text.state, ComposeStates.buttons.state, ComposeStates.schedule.state):
        hint = WAITING_TEXT_HINT
    else:
        hint = "Поддерживаются текст, фото, видео и GIF."
    await message.answer(hint)


# ------------------------------------------------------------------ scheduling

async def _close_with(bot: Any, state: FSMContext, chat_id: int, data: dict, text: str) -> None:
    await state.clear()
    await safe_delete(bot, chat_id, data.get("panel_id"), data.get("prompt_id"))
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=main_menu(), link_preview_options=NO_PREVIEW)


async def schedule_post(bot: Any, state: FSMContext, db: Any, config: Any, chat_id: int,
                        when: datetime) -> str | None:
    """Schedule the composer's post at `when` in all its channels and close the composer.

    Returns a problem text on failure; raises PostChanged when the post is no longer a draft."""
    data = await state.get_data()
    post = await db.get_post(data["post_id"])
    problem = session_problem(post, data)
    if problem:
        raise PostChanged(problem)
    if data.get("edit"):
        return "Этот пост уже нельзя запланировать."
    problem = publish_problem(post, edit=False)
    if problem:
        return problem
    ts = int(when.timestamp())
    try:
        channel_ids = [t["channel_id"] for t in selected_targets(post, edit=False)]
        await plan_service.schedule_post_at(db, post["id"], channel_ids, ts)
    except PlanError as exc:
        return str(exc)
    await _close_with(bot, state, chat_id, data, (
        f"⏰ Пост #{post['id']} запланирован на <b>{format_dt(ts, config.tz)}</b> ({esc(tz_name(config.tz))}).\n"
        "Изменить время или отменить — в «🗂 Посты» → «⏰ Запланированные» или в «📅 Контент-план»."))
    log.info("Post %s scheduled at %s", post["id"], ts)
    return None


# ------------------------------------------------------------------ panel callbacks

@dataclass
class Ctx:
    """Everything a panel action needs."""

    callback: CallbackQuery
    state: FSMContext
    db: Any
    config: Any
    data: dict
    answered: bool = False
    confirms: Any = None  # ConfirmGuard of the dispatcher (one-shot «✅ Да, опубликовать»)

    @property
    def bot(self) -> Any:
        return self.callback.bot

    @property
    def chat_id(self) -> int:
        return callback_chat_id(self.callback)

    @property
    def post_id(self) -> int:
        return self.data["post_id"]

    @property
    def edit(self) -> bool:
        return bool(self.data.get("edit"))

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        if not self.answered:
            self.answered = True
            await safe_answer(self.callback, text, alert=alert)

    async def post(self) -> dict | None:
        return await load_post(self.db, self.data)

    async def save(self, **fields: Any) -> None:
        """Raises PostChanged (handled by the dispatcher of panel actions)."""
        self.data = await save_fields(self.db, self.state, self.data, **fields)

    async def update(self, **values: Any) -> None:
        await self.state.update_data(**values)
        self.data.update(values)

    async def show_panel_message(self, text: str, markup: InlineKeyboardMarkup | None) -> None:
        """Show `text` in the panel message (edited in place)."""
        panel_id = await edit_or_send(self.bot, self.chat_id, self.data.get("panel_id"), text, markup)
        await self.update(panel_id=panel_id)

    async def show_panel(self, *, note: str | None = None) -> None:
        """Main panel in place (the preview did not change)."""
        await self.state.set_state(ComposeStates.menu)
        post = await self.post()
        if post is None:
            await self.gone()
            return
        text = panel_text(post, self.data, self.config.tz, note=note,
                          premium_emoji=await premium_emoji_enabled(self.db))
        await self.show_panel_message(text, panel_kb(post, self.data))

    async def refresh(self, *, note: str | None = None) -> None:
        await self.state.set_state(ComposeStates.menu)
        await refresh(self.bot, self.state, self.db, self.config, self.chat_id, note=note)
        self.data = await self.state.get_data()

    async def gone(self) -> None:
        await self.state.clear()
        await self.answer("Пост не найден", alert=True)
        await edit_or_send(self.bot, self.chat_id, self.data.get("panel_id"),
                           "Пост не найден — возможно, его удалили.")

    async def changed(self, text: str) -> None:
        """The post changed status underneath: tell why and close the composer."""
        await self.answer(text, alert=True)
        await close_session(self.bot, self.state, self.chat_id, self.data, text)

    async def finish(self, text: str) -> None:
        """Close the composer: drop the panel, send the result with the main menu."""
        await _close_with(self.bot, self.state, self.chat_id, self.data, text)


async def _post_for_panel(callback: CallbackQuery, db: Any, post_id: int) -> dict | None:
    """The post a panel button refers to, or None (the callback is answered with the reason)."""
    post = await db.get_post(post_id)
    error = open_error(post)
    if error:
        await safe_answer(callback, error, alert=True)
        return None
    return post


async def _session(callback: CallbackQuery, state: FSMContext, post: dict) -> dict:
    """FSM session for the panel the button belongs to (re-created from the DB when needed)."""
    data = await state.get_data()
    edit = is_published(post)
    message_id = callback_message_id(callback)
    current = await state.get_state()
    same = (current in ComposeStates and current != ComposeStates.start.state
            and data.get("post_id") == post["id"] and bool(data.get("edit")) == edit)
    if not same:
        data = session_data(post, panel_id=message_id, restored=True)
        await state.set_state(ComposeStates.menu)
        await state.set_data(data)
    elif message_id and data.get("panel_id") != message_id:
        # A button of an older panel of this post: that message becomes the panel.
        await safe_delete(callback.bot, callback_chat_id(callback), data.get("panel_id"))
        data["panel_id"] = message_id
        await state.update_data(panel_id=message_id)
    return data


async def _sub_state(ctx: Ctx, new_state: Any, prompt: tuple[str, InlineKeyboardMarkup]) -> None:
    await ctx.answer()
    await ctx.state.set_state(new_state)
    await ctx.show_panel_message(*prompt)


# --- content

async def a_text(ctx: Ctx, post: dict) -> None:
    await _sub_state(ctx, ComposeStates.text, text_prompt(post))


async def a_rmtext(ctx: Ctx, post: dict) -> None:
    if not has_media(post):
        await ctx.answer("У поста без медиа текст обязателен", alert=True)
        return
    await ctx.save(text_md="")
    await ctx.answer("Подпись убрана")
    await ctx.refresh()


async def a_media(ctx: Ctx, post: dict) -> None:
    if ctx.edit and not has_media(post):
        await ctx.answer(TEXT_TO_MEDIA_ERROR, alert=True)
        return
    await ctx.update(media_mode="replace")
    await _sub_state(ctx, ComposeStates.media, media_prompt(post, ctx.data))


async def a_media_add(ctx: Ctx, post: dict) -> None:
    media = post_media(post)
    if len(media) >= MAX_ALBUM:
        await ctx.answer(f"В посте уже {MAX_ALBUM} файлов — больше Telegram не позволяет", alert=True)
        return
    if any(item.get("type") not in ALBUM_TYPES for item in media):
        await ctx.answer("GIF нельзя объединить с другими файлами в альбом — замените его или уберите", alert=True)
        return
    await ctx.update(media_mode="add")
    await _sub_state(ctx, ComposeStates.media, media_prompt(post, ctx.data))


async def a_media_replace(ctx: Ctx, post: dict) -> None:
    await ctx.update(media_mode="replace")
    await _sub_state(ctx, ComposeStates.media, media_prompt(post, ctx.data))


async def a_rmmedia(ctx: Ctx, post: dict) -> None:
    if not has_media(post):
        await ctx.answer("Медиа и так нет")
        return
    await ctx.save(media=[])
    await ctx.answer("Медиа убраны")
    await ctx.refresh()


async def a_buttons(ctx: Ctx, post: dict) -> None:
    if is_album(post) and not count_buttons(post.get("buttons")):
        await ctx.answer(BUTTONS_ALBUM_ERROR, alert=True)
        return
    await _sub_state(ctx, ComposeStates.buttons, buttons_prompt(post))


async def a_rmbuttons(ctx: Ctx, post: dict) -> None:
    await ctx.save(buttons=[])
    await ctx.answer("Кнопки убраны")
    await ctx.refresh()


async def a_album_ok(ctx: Ctx, post: dict) -> None:
    pending = ctx.data.get("pending")
    if not pending:
        await ctx.answer("Этот альбом уже не ждёт подтверждения — пришлите его снова", alert=True)
        return
    await ctx.answer("Кнопки убраны, альбом применён")
    await commit_fields(ctx.bot, ctx.state, ctx.db, ctx.config, ctx.chat_id, ctx.data,
                        {**pending, "buttons": []})


async def a_album_no(ctx: Ctx, post: dict) -> None:
    await ctx.answer("Альбом не применён")
    await safe_delete(ctx.bot, ctx.chat_id, ctx.data.get("prompt_id"))
    await ctx.update(pending=None, prompt_id=None, media_mode=None)
    await ctx.show_panel(note="↩️ Альбом не применён — пост остался как был.")


# --- channels & options

async def set_targets(db: Any, post_id: int, channel_ids: list[int]) -> bool:
    """Make the unsent targets of a draft equal to `channel_ids` (an empty list removes them all)
    and recompute its status (a scheduled post whose last timed channel was removed becomes a
    draft again). False — nothing changed — when the scheduler claimed the post meanwhile (review2
    #3: a channel being sent to must keep its target, or the message would have no record)."""
    changed = await db.set_post_targets(post_id, channel_ids)
    await db.refresh_post_schedule(post_id)
    return changed


async def _set_targets(ctx: Ctx, channel_ids: list[int]) -> bool:
    if await set_targets(ctx.db, ctx.post_id, channel_ids):
        return True
    await ctx.answer(SENDING_NOW_TEXT, alert=True)
    return False


async def a_channels(ctx: Ctx, post: dict) -> None:
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    await ctx.update(picker_page=0)
    await ctx.show_panel_message(*await channels_picker(ctx.db, post))


async def a_ch_page(ctx: Ctx, post: dict, page: int) -> None:
    """◀️ / ▶️ of a picker with many channels (the page is kept for the toggles)."""
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    await ctx.update(picker_page=page)
    await ctx.show_panel_message(*await channels_picker(ctx.db, post, page))


async def a_ch_all(ctx: Ctx, post: dict) -> None:
    active = await ctx.db.list_channels([CH_ACTIVE])
    if not await _set_targets(ctx, [ch["id"] for ch in active]):
        return
    await ctx.answer("Выбраны все каналы")
    await ctx.show_panel_message(*await channels_picker(ctx.db, await ctx.post(), ctx.data.get("picker_page") or 0))


async def a_ch_none(ctx: Ctx, post: dict) -> None:
    if not await _set_targets(ctx, []):
        return
    await ctx.answer("Выбор снят")
    await ctx.show_panel_message(*await channels_picker(ctx.db, await ctx.post(), ctx.data.get("picker_page") or 0))


async def a_options(ctx: Ctx, post: dict) -> None:
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    await ctx.show_panel_message(*options_picker(post, edit=ctx.edit))


async def a_opts_done(ctx: Ctx, post: dict) -> None:
    await ctx.answer()
    await ctx.refresh()


async def a_back(ctx: Ctx, post: dict) -> None:
    await ctx.answer()
    await ctx.update(media_mode=None)
    await ctx.show_panel()


# --- publishing

async def _refuse_unpublishable(ctx: Ctx, post: dict) -> bool:
    """Answer with the reason (and show the channel picker when no channel is selected)."""
    problem = publish_problem(post, edit=False)
    if not problem:
        return False
    await ctx.answer(problem, alert=True)
    if problem == NO_CHANNELS_PROBLEM:
        await ctx.show_panel_message(*await channels_picker(ctx.db, post))
    return True


async def a_publish(ctx: Ctx, post: dict) -> None:
    if await _refuse_unpublishable(ctx, post):
        return
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    await ctx.show_panel_message(*publish_choice(post, ctx.config.tz))


async def a_now(ctx: Ctx, post: dict) -> None:
    if await _refuse_unpublishable(ctx, post):
        return
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    await ctx.show_panel_message(*confirm_now(post, ctx.config.tz))
    if ctx.confirms is not None:
        ctx.confirms.shown(ctx.chat_id, ctx.data.get("panel_id"))


async def a_confirm(ctx: Ctx, post: dict) -> None:
    # The keyboard goes away before the callback is answered: Telegram clients ignore taps while a
    # callback is pending, so «✅ Да, опубликовать» cannot be tapped twice (finding #13).
    await ctx.show_panel_message(f"⏳ Публикую пост #{ctx.post_id}…", None)
    await ctx.answer("⏳ Публикую…")
    try:
        report = await publish_post(ctx.bot, ctx.db, ctx.config, ctx.post_id)
    except PostError as exc:
        await ctx.show_panel(note=f"❌ {esc(exc)}")
        return
    log.info("Post %s published by %s: %s ok, %s failed", ctx.post_id, ctx.callback.from_user.id,
             report.ok_count, report.fail_count)
    await ctx.finish(fit_html(report.summary_html()))


async def a_queue(ctx: Ctx, post: dict) -> None:
    if await _refuse_unpublishable(ctx, post):
        return
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    await ctx.show_panel_message(*await queue_screen(ctx.db, post, ctx.config.tz))


async def a_queue_none(ctx: Ctx, post: dict) -> None:
    await ctx.answer("У выбранных каналов нет расписания. Задайте его: «📢 Каналы» → канал → «📅 Расписание», "
                     "или выберите «⏰ Ко времени».", alert=True)


async def a_queue_ok(ctx: Ctx, post: dict) -> None:
    if await _refuse_unpublishable(ctx, post):
        return
    rows, queueable = await queue_preview(ctx.db, post, ctx.config.tz)
    if not queueable:
        await a_queue_none(ctx, post)
        return
    try:
        times = await plan_service.queue_post(ctx.db, ctx.post_id, queueable, ctx.config.tz)
    except PlanError as exc:
        await ctx.answer(str(exc), alert=True)
        return
    dropped = [target for target, _, _ in rows if target["channel_id"] not in times]
    if dropped:
        await _set_targets(ctx, list(times))
    await ctx.answer("Пост поставлен в очередь")
    lines = [f"📅 Пост #{ctx.post_id} поставлен в очередь:"]
    queued = [f"• {esc(target_title(target))} — {when_label(times[target['channel_id']], ctx.config.tz)}"
              for target, _, _ in rows if times.get(target["channel_id"])]
    lines += cut_lines(queued, 2800, more="<i>…и ещё {n}</i>")  # finding #17: ≤ 4096 characters
    if dropped:
        lines.append(f"⚠️ Сняты с поста (нет времени по расписанию, {len(dropped)}): "
                     + names_text([target_title(t) for t in dropped]))
    lines.append("\nПорядок и время — в «📅 Контент-план».")
    log.info("Post %s queued by %s: %s", ctx.post_id, ctx.callback.from_user.id, times)
    await ctx.finish("\n".join(lines))


async def a_schedule(ctx: Ctx, post: dict) -> None:
    if await _refuse_unpublishable(ctx, post):
        return
    await _sub_state(ctx, ComposeStates.schedule, schedule_prompt(post, ctx.config.tz))


async def a_schedule_preset(ctx: Ctx, post: dict, code: str) -> None:
    preset = SCHEDULE_PRESETS.get(code)
    if preset is None:
        await ctx.answer("Неизвестный вариант", alert=True)
        return
    try:
        when = parse_when(preset[1], ctx.config.tz)
    except ValueError as exc:
        await ctx.answer(str(exc), alert=True)
        return
    problem = publish_problem(post, edit=False)
    if problem:
        await ctx.answer(problem, alert=True)
        return
    problem = await schedule_post(ctx.bot, ctx.state, ctx.db, ctx.config, ctx.chat_id, when)
    if problem:
        await ctx.answer(problem, alert=True)
        return
    await ctx.answer("Пост запланирован")


async def a_unschedule(ctx: Ctx, post: dict) -> None:
    try:
        await plan_service.unschedule_post(ctx.db, ctx.post_id)
    except PlanError as exc:
        await ctx.answer(str(exc), alert=True)
        return
    await ctx.answer("Расписание снято")
    await ctx.show_panel(note="↩️ Пост снят с расписания и снова стал черновиком.")


# --- closing / deleting

async def a_close(ctx: Ctx, post: dict) -> None:
    await ctx.answer()
    await ctx.state.clear()
    await safe_delete(ctx.bot, ctx.chat_id, ctx.data.get("prompt_id"))
    await edit_or_send(ctx.bot, ctx.chat_id, ctx.data.get("panel_id"), close_text(post, ctx.config.tz))


async def a_delete(ctx: Ctx, post: dict) -> None:
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    what = "запланированный пост" if post["status"] == POST_SCHEDULED else "черновик"
    markup = kb([ib("🗑 Да, удалить", ComposeCb(action="delete_ok", post_id=ctx.post_id), style="danger"),
                 ib("↩️ Назад", ComposeCb(action="back", post_id=ctx.post_id))])
    await ctx.show_panel_message(f"🗑 Удалить {what} #{ctx.post_id}? Это нельзя отменить.", markup)


async def a_delete_ok(ctx: Ctx, post: dict) -> None:
    current = await ctx.db.get_post(ctx.post_id)
    if current is None or is_published(current) or current["status"] not in DRAFT_STATUSES:
        await ctx.answer("Этот пост уже нельзя удалить как черновик", alert=True)
        return
    if not await ctx.db.delete_post_record(ctx.post_id):
        # Claimed by the scheduler (or sent) after the check above: the record must stay (review2 #3).
        fresh = await ctx.db.get_post(ctx.post_id)
        if fresh is not None:
            await ctx.answer(SENDING_NOW_TEXT if fresh["status"] == POST_SENDING
                             else "Этот пост уже нельзя удалить как черновик", alert=True)
            return
    log.info("Draft %s deleted by %s", ctx.post_id, ctx.callback.from_user.id)
    await ctx.answer("Удалено")
    await ctx.state.clear()
    await safe_delete(ctx.bot, ctx.chat_id, *(ctx.data.get("preview_ids") or []), ctx.data.get("prompt_id"))
    await edit_or_send(ctx.bot, ctx.chat_id, ctx.data.get("panel_id"), f"🗑 Пост #{ctx.post_id} удалён.")


# --- edit mode

def _has_changes(data: dict) -> bool:
    return bool(data.get("overrides")) or bool(data.get("media_changed"))


async def _can_save(ctx: Ctx, post: dict) -> bool:
    """Saving without changes only makes sense to retry a failed edit (finding #7)."""
    if _has_changes(ctx.data) or _needs_retry(post):
        return True
    if ctx.data.get("restored"):
        await ctx.answer(LOST_CHANGES_TEXT, alert=True)
        await ctx.refresh(note="ℹ️ Показан текущий опубликованный вариант поста.")
        await ctx.update(restored=False)  # the owner sees the real state now
    else:
        await ctx.answer("Изменений нет — сначала измените текст, медиа, кнопки или опции.", alert=True)
    return False


async def a_save(ctx: Ctx, post: dict) -> None:
    if not await _can_save(ctx, post):
        return
    problem = publish_problem(post, edit=True)
    if problem:
        await ctx.answer(problem, alert=True)
        return
    await ctx.answer()
    await ctx.state.set_state(ComposeStates.menu)
    targets = selected_targets(post, edit=True)
    n = len(targets)
    text = (f"💾 <b>Применить изменения?</b>\nПост обновится в {n} "
            f"{plural(n, 'канале', 'каналах', 'каналах')}:\n"
            + "\n".join(cut_lines([f"• {esc(target_title(t))}" for t in targets], 3000, more="<i>…и ещё {n}</i>")))
    if any(t["status"] == "pending" and t.get("scheduled_at") for t in post["targets"]):
        text += "\n\nВ остальные каналы пост выйдет по расписанию уже в новом виде."
    markup = kb([ib("✅ Да, сохранить", ComposeCb(action="save_ok", post_id=ctx.post_id), style="success"),
                 ib("↩️ Назад", ComposeCb(action="back", post_id=ctx.post_id))])
    await ctx.show_panel_message(text, markup)


async def a_save_ok(ctx: Ctx, post: dict) -> None:
    if not await _can_save(ctx, post):
        return
    problem = publish_problem(post, edit=True)
    if problem:
        await ctx.answer(problem, alert=True)
        return
    await ctx.answer("⏳ Сохраняю…")
    await ctx.show_panel_message(f"⏳ Обновляю пост #{ctx.post_id} в каналах…", None)
    overrides = dict(ctx.data.get("overrides") or {})
    original = await ctx.db.get_post(ctx.post_id)
    backup = {key: original[key] for key in overrides if key in original}
    if "text_md" in overrides:
        backup["text_html"] = original.get("text_html") or ""
    if overrides:
        await ctx.db.update_post(ctx.post_id, **overrides)
    try:
        report = await edit_published_post(ctx.bot, ctx.db, ctx.config, ctx.post_id,
                                           media_changed=bool(ctx.data.get("media_changed")))
    except PostError as exc:
        if backup:
            await ctx.db.update_post(ctx.post_id, **backup)  # nothing was applied: keep DB == channels
        await ctx.show_panel(note=f"❌ {esc(exc)}")
        return
    if report.ok_count == 0:
        # Every channel refused: the channels still show the old post, so the DB must too (finding
        # #9). The session keeps the changes, so «💾 Сохранить» can simply be pressed again.
        if backup:
            await ctx.db.update_post(ctx.post_id, **backup)
        await ctx.show_panel(note=fit_html(report.summary_html(), 1500) + "\n\nИзменения не применены — "
                                  "попробуйте сохранить ещё раз или выйдите без сохранения.")
        return
    log.info("Post %s edited by %s: %s ok, %s failed", ctx.post_id, ctx.callback.from_user.id,
             report.ok_count, report.fail_count)
    await ctx.finish(fit_html(report.summary_html()))


async def a_discard(ctx: Ctx, post: dict) -> None:
    await ctx.answer("Изменения отменены")
    await ctx.state.clear()
    if _has_changes(ctx.data):
        await safe_delete(ctx.bot, ctx.chat_id, *(ctx.data.get("preview_ids") or []))  # shows unsaved changes
    await safe_delete(ctx.bot, ctx.chat_id, ctx.data.get("prompt_id"))
    await edit_or_send(ctx.bot, ctx.chat_id, ctx.data.get("panel_id"),
                       f"✖️ Редактирование поста #{ctx.post_id} закрыто, изменения не сохранены.")


DRAFT_ACTIONS = {
    "media_add": a_media_add, "media_replace": a_media_replace, "rmmedia": a_rmmedia,
    "album_ok": a_album_ok, "album_no": a_album_no,
    "channels": a_channels, "ch_all": a_ch_all, "ch_none": a_ch_none,
    "publish": a_publish, "now": a_now, "confirm": a_confirm,
    "queue": a_queue, "queue_ok": a_queue_ok, "queue_none": a_queue_none,
    "schedule": a_schedule, "unschedule": a_unschedule,
    "close": a_close, "delete": a_delete, "delete_ok": a_delete_ok,
}
EDIT_ACTIONS = {"save": a_save, "save_ok": a_save_ok, "discard": a_discard}
COMMON_ACTIONS = {
    "text": a_text, "rmtext": a_rmtext, "media": a_media, "buttons": a_buttons,
    "rmbuttons": a_rmbuttons, "options": a_options, "opts_done": a_opts_done, "back": a_back,
}


def _action_handler(action: str, edit: bool) -> Any:
    """Handler of a panel action for the post's mode, or None when the action does not apply."""
    if action in COMMON_ACTIONS:
        return COMMON_ACTIONS[action]
    if edit:
        return EDIT_ACTIONS.get(action)
    if action.startswith("at_"):
        code = action[3:]
        return lambda ctx, post: a_schedule_preset(ctx, post, code)
    if action.startswith("chpg") and action[4:].isdigit():
        page = int(action[4:])
        return lambda ctx, post: a_ch_page(ctx, post, page)
    return DRAFT_ACTIONS.get(action)


async def on_panel(callback: CallbackQuery, callback_data: ComposeCb, state: FSMContext,
                   db: Any, config: Any, confirms: Any = None) -> None:
    post = await _post_for_panel(callback, db, callback_data.post_id)
    if post is None:
        return
    handler = _action_handler(callback_data.action, is_published(post))
    if handler is None:
        # E.g. a second click on «✅ Да, опубликовать»: the post is already published.
        await safe_answer(callback, STALE_PANEL, alert=True)
        return
    if (callback_data.action == "confirm" and confirms is not None
            and not confirms.use(callback_chat_id(callback), callback_message_id(callback), post["id"])):
        # A second tap after a publish that failed everywhere (finding #13): no re-send without a new
        # confirmation, and no composer session is re-opened for the old panel.
        await safe_answer(callback, CONFIRM_USED_TEXT, alert=True)
        return
    data = await _session(callback, state, post)
    ctx = Ctx(callback=callback, state=state, db=db, config=config, data=data, confirms=confirms)
    try:
        await handler(ctx, await ctx.post())
    except PostChanged as exc:
        await ctx.changed(str(exc))
    await ctx.answer()


async def _draft_panel_post(callback: CallbackQuery, db: Any, post_id: int) -> dict | None:
    post = await _post_for_panel(callback, db, post_id)
    if post is not None and is_published(post):
        await safe_answer(callback, "Каналы опубликованного поста изменить нельзя", alert=True)
        return None
    return post


async def _show_picker(callback: CallbackQuery, state: FSMContext, db: Any, data: dict) -> None:
    text, markup = await channels_picker(db, await load_post(db, data), data.get("picker_page") or 0)
    panel_id = await edit_or_send(callback.bot, callback_chat_id(callback), data.get("panel_id"), text, markup)
    await state.update_data(panel_id=panel_id)


async def on_channel_toggle(callback: CallbackQuery, callback_data: ComposeChannelCb, state: FSMContext,
                            db: Any) -> None:
    post = await _draft_panel_post(callback, db, callback_data.post_id)
    if post is None:
        return
    data = await _session(callback, state, post)
    selected = [t["channel_id"] for t in selected_targets(post, edit=False)]
    channel_id = callback_data.channel_id
    if channel_id in selected:
        selected.remove(channel_id)  # also a channel that is no longer active (findings #11/#22)
    else:
        channel = await db.get_channel(channel_id)
        if channel is None or channel["status"] != CH_ACTIVE:
            await safe_answer(callback, "Канал больше не подключён", alert=True)
            return
        selected.append(channel_id)
    if not await set_targets(db, post["id"], selected):
        await safe_answer(callback, SENDING_NOW_TEXT, alert=True)
        return
    await safe_answer(callback)
    await _show_picker(callback, state, db, data)


async def on_folder_toggle(callback: CallbackQuery, callback_data: ComposeFolderCb, state: FSMContext,
                           db: Any) -> None:
    """A folder row of the picker: select every channel of the folder, or clear them if all are."""
    post = await _draft_panel_post(callback, db, callback_data.post_id)
    if post is None:
        return
    data = await _session(callback, state, post)
    wanted = callback_data.folder_id or None
    group = next((g for g in await folder_service.grouped_channels(db, ("active",))
                  if (g["folder"]["id"] if g["folder"] else None) == wanted), None)
    ids = [ch["id"] for ch in group["channels"]] if group else []
    if not ids:
        await safe_answer(callback, "В папке нет подключённых каналов", alert=True)
        return
    selected = [t["channel_id"] for t in selected_targets(post, edit=False)]
    if all(cid in selected for cid in ids):
        selected = [cid for cid in selected if cid not in ids]
        note = "Каналы папки сняты"
    else:
        selected += [cid for cid in ids if cid not in selected]
        note = "Каналы папки выбраны"
    if not await set_targets(db, post["id"], selected):
        await safe_answer(callback, SENDING_NOW_TEXT, alert=True)
        return
    await safe_answer(callback, note)
    await _show_picker(callback, state, db, data)


async def on_media_remove(callback: CallbackQuery, callback_data: ComposeMediaCb, state: FSMContext,
                          db: Any, config: Any) -> None:
    """«❌ N» in the media menu of a draft album."""
    post = await _post_for_panel(callback, db, callback_data.post_id)
    if post is None:
        return
    if is_published(post):
        await safe_answer(callback, "Telegram не позволяет убирать файлы из опубликованного альбома", alert=True)
        return
    data = await _session(callback, state, post)
    ctx = Ctx(callback=callback, state=state, db=db, config=config, data=data)
    media = post_media(post)
    index = callback_data.index
    if not 0 <= index < len(media):
        await ctx.answer("Этого файла уже нет", alert=True)
        await ctx.show_panel_message(*media_prompt(post, ctx.data))
        return
    try:
        await ctx.save(media=media[:index] + media[index + 1:])
    except PostChanged as exc:
        await ctx.changed(str(exc))
        return
    await ctx.answer(f"Файл {index + 1} убран")
    if in_add_mode(await state.get_state(), ctx.data):
        # still adding (finding #14): the media menu stays under the fresh preview
        await refresh(ctx.bot, state, db, config, ctx.chat_id)
        return
    await ctx.update(media_mode=None)
    await ctx.refresh()


async def on_option_toggle(callback: CallbackQuery, callback_data: ComposeOptionCb, state: FSMContext,
                           db: Any, config: Any) -> None:
    post = await _post_for_panel(callback, db, callback_data.post_id)
    if post is None:
        return
    data = await _session(callback, state, post)
    ctx = Ctx(callback=callback, state=state, db=db, config=config, data=data)
    post = await ctx.post()
    option = callback_data.option
    if option not in available_options(post, edit=ctx.edit):
        await ctx.answer("Эту опцию сейчас нельзя изменить", alert=True)
        return
    enabled = not post.get(option)
    try:
        await ctx.save(**{option: enabled})
    except PostChanged as exc:
        await ctx.changed(str(exc))
        return
    await ctx.answer(f"{OPTIONS[option]}: {'вкл' if enabled else 'выкл'}")
    await ctx.show_panel_message(*options_picker(await ctx.post(), edit=ctx.edit))


# ------------------------------------------------------------------ router

def create_router() -> Router:
    router = Router(name="compose")
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_new, TextIs(BTN_NEW))
    router.callback_query.register(cb_new, MenuCb.filter(F.action == "new"))

    msg = router.message
    media_types = F.photo | F.video | F.animation
    msg.register(on_album_item, StateFilter(ComposeStates), F.media_group_id, UserInput())
    msg.register(on_first_content, StateFilter(ComposeStates.start), UserInput())
    msg.register(on_text, StateFilter(ComposeStates.text, ComposeStates.menu), F.text, UserInput())
    msg.register(on_media, StateFilter(ComposeStates.media, ComposeStates.menu), media_types)
    msg.register(on_buttons, StateFilter(ComposeStates.buttons), F.text, UserInput())
    msg.register(on_schedule_text, StateFilter(ComposeStates.schedule), F.text, UserInput())
    msg.register(on_unsupported, StateFilter(ComposeStates), UserInput())

    router.callback_query.register(on_panel, ComposeCb.filter())
    router.callback_query.register(on_channel_toggle, ComposeChannelCb.filter())
    router.callback_query.register(on_folder_toggle, ComposeFolderCb.filter())
    router.callback_query.register(on_media_remove, ComposeMediaCb.filter())
    router.callback_query.register(on_option_toggle, ComposeOptionCb.filter())
    return router

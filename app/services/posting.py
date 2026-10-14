"""Publishing posts to Telegram channels.

Shared by the bot and the web panel: rendering the markdown source into Telegram HTML, sending a
post (text, photo / video / GIF with a caption, or an album) with its buttons and options,
publishing it to the selected channels with per-channel error handling, editing and deleting
already published messages, previews and admin notifications.

Every Telegram error is caught per channel and turned into a Russian, user-presentable message;
a post never stays in the intermediate 'sending' status after `publish_post` returns or raises.
Publishing comes in two flavours: manual («Опубликовать сейчас» — every pending/failed target
now) and scheduled (`only_due=True` — only targets whose own time has come, claimed atomically so
a post cancelled or moved meanwhile never goes out).

Running publish/edit/delete operations are counted, so a graceful shutdown can `wait_idle()`
before closing the bot session and the database.

Media files (SPEC_V22): a Telegram file_id belongs to the bot that received it, recorded in the
item's `file_bot`. Sending with another bot uses the item's uploaded file (`path`) instead, or
fails with FOREIGN_MEDIA_ERROR when there is none. Items stored before V2.2 (no `file_bot`) try
their file_id; when Telegram does not know it, the uploaded files are sent once more. Every
file_id learned from an upload is written back together with the bot's id.

Uploads get a request timeout sized by the file size (aiogram's default is 60 s for the whole
request); a network error while uploading may still have reached the channel, so such a target is
marked INTERRUPTED_ERROR («проверьте, вышел ли пост в канале») instead of an ordinary failure.

Premium emoji (SPEC_V23): unless the `premium_emoji` setting is on (the bot has a Fragment
username), custom emoji are sent and edited as their regular fallback emoji.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
)

from app.buttons import build_markup
from app.db import (
    CH_ACTIVE,
    INTERRUPTED_ERROR,
    POST_DELETED,
    POST_DRAFT,
    POST_FAILED,
    POST_PARTIAL,
    POST_SCHEDULED,
    POST_SENDING,
    POST_SENT,
    TARGET_DELETED,
    TARGET_FAILED,
    TARGET_PENDING,
    TARGET_SENT,
    now,
)
from app.markdown import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    md_to_html,
    strip_custom_emoji_html,
    strip_html,
    validate_telegram_html,
    visible_length,
)
from app.media import (
    MediaError,
    check_buttons,
    edit_class,
    media_signature,
    normalize_media,
    post_media,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

KIND_TEXT = "text"
KIND_MEDIA = "media"

# Statuses from which a post may be (re)published manually.
PUBLISHABLE_STATUSES = (POST_DRAFT, POST_SCHEDULED, POST_FAILED, POST_PARTIAL)

RETRY_AFTER_MAX = 30  # longest flood-control wait we sit out before the single retry (longer → fail at once)
CHANNEL_PAUSE = 0.05  # pause between channels to stay well below Telegram rate limits
# Uploads: aiogram's default request timeout (60 s) covers the whole request, upload included.
UPLOAD_TIMEOUT_MIN = 300  # seconds for any request that uploads a file
UPLOAD_RATE = 128 * 1024  # bytes per second assumed for the upload (≈ 1 Мбит/с)
UPLOAD_TIMEOUT_MAX = 3600

KIND_MISMATCH_ERROR = (
    "Нельзя поменять тип поста (текст/медиа/альбом) после публикации — "
    "удалите его и опубликуйте заново"
)
CHANNEL_INACTIVE_ERROR = "Канал не подключён к боту"
BUSY_ERROR = "Пост уже публикуется или опубликован"
NOT_DUE_ERROR = "Пост больше не ждёт публикации по расписанию"
NO_TARGETS_ERROR = "Не выбраны каналы для публикации"
ALBUM_MISMATCH_ERROR = "Альбом в канале не совпадает с постом — удалите пост и опубликуйте заново"
# INTERRUPTED_ERROR (from app.db): the send was cut — the message may be in the channel or not.
EDIT_INTERRUPTED_ERROR = ("Связь прервалась во время загрузки файла — проверьте сообщение в канале "
                          "и сохраните пост ещё раз")
DELETE_BUSY_ERROR = "Пост сейчас публикуется — удалите его из каналов чуть позже"
UNREACHABLE_NOTE = "Нет доступа к каналу — сообщение не удалено ботом, удалите его вручную"
FOREIGN_MEDIA_ERROR = "Медиа загружено через другого бота — загрузите файл заново"
# Telegram's answers for a file_id it does not know (e.g. one received by another bot).
FILE_ID_ERRORS = ("wrong file identifier", "wrong remote file", "file reference", "file_reference")
STALE_MEDIA_SIG = ""  # media_sig of a legacy target whose media edit failed: forces a retry

_FILE_NOUNS = {"photo": "картинки", "video": "видео", "animation": "GIF"}

# Indirection so tests can replace waiting without patching asyncio globally.
_pause: Callable[[float], Awaitable[Any]] = asyncio.sleep


class PostError(Exception):
    """Post cannot be rendered / published / edited. The message is Russian and user-presentable."""


class PostNotDue(PostError):
    """Scheduler path: the post is no longer due (cancelled, moved, or taken by another publisher)."""


class SentMessages(list):
    """Messages returned for a post's media (by item order) plus `via_path`: the indexes of the
    items sent from their uploaded file — their new Telegram file_ids are worth keeping."""

    def __init__(self, messages: Iterable[Any] = (), via_path: Iterable[int] = ()) -> None:
        super().__init__(messages)
        self.via_path = frozenset(via_path)


def bot_id_of(bot: Any) -> int | None:
    """Telegram id of a bot object (aiogram derives it from the token), None when unknown."""
    value = getattr(bot, "id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class TargetResult:
    channel_id: int
    title: str
    ok: bool
    message_id: int | None = None
    link: str | None = None
    error: str | None = None
    note: str | None = None  # remark on a success (e.g. the message could only be forgotten locally)


_REPORT_VERBS = {"publish": "Опубликовано", "edit": "Обновлено", "delete": "Удалено"}


@dataclass
class PublishReport:
    post_id: int
    results: list[TargetResult] = field(default_factory=list)
    action: str = "publish"  # 'publish' | 'edit' | 'delete' — only changes the summary wording

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def to_dict(self) -> dict:
        return {
            "post_id": self.post_id,
            "action": self.action,
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "results": [asdict(r) for r in self.results],
        }

    def summary_html(self, budget: int | None = None) -> str:
        """Russian report for the bot (parse_mode=HTML). `budget`: at most about that many
        characters (a Telegram message holds 4096) — channel lines beyond it become «…и ещё N»."""
        verb = _REPORT_VERBS.get(self.action, _REPORT_VERBS["publish"])
        total = len(self.results)
        if self.fail_count == 0:
            icon = "✅"
        elif self.ok_count:
            icon = "⚠️"
        else:
            icon = "❌"
        lines = [f"{icon} {verb}: {self.ok_count} из {total}"]
        for r in self.results:
            name = html.escape(r.title or str(r.channel_id))
            if not r.ok:
                lines.append(f"❌ {name} — {html.escape(r.error or 'неизвестная ошибка')}")
            elif r.note:
                lines.append(f"⚠️ {name} — {html.escape(r.note)}")
            elif r.link and self.action != "delete":
                lines.append(f'• <a href="{html.escape(r.link, quote=True)}">{name}</a>')
            else:
                lines.append(f"• {name}")
        return "\n".join(lines if budget is None else _fit_lines(lines, budget))


def _fit_lines(lines: list[str], budget: int) -> list[str]:
    """`lines` (the first one always kept) within about `budget` UTF-16 units: the rest → «…и ещё N»."""
    total = 0
    for index, line in enumerate(lines):
        total += len(line.encode("utf-16-le")) // 2 + 1
        if total > budget and index:
            return lines[:index] + [f"<i>…и ещё {len(lines) - index}</i>"]
    return lines


# ------------------------------------------------------------------ in-flight operations

_active_operations = 0


@asynccontextmanager
async def _tracked() -> AsyncIterator[None]:
    global _active_operations
    _active_operations += 1
    try:
        yield
    finally:
        _active_operations -= 1


def active_operations() -> int:
    """Number of publish / edit / delete operations running right now."""
    return _active_operations


async def wait_idle(timeout: float | None = None, *, poll: float = 0.05) -> bool:
    """Wait until no publish / edit / delete operation is running (graceful shutdown).

    Returns False when `timeout` seconds passed first.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while _active_operations > 0:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)
    return True


# ------------------------------------------------------------------ rendering

def render_text(text_md: str, *, has_media: bool | None = None, has_photo: bool | None = None,
                premium_emoji: bool = True) -> str:
    """Markdown source → validated Telegram HTML within the length limit.

    `has_media` selects the caption limit (1024) instead of the message limit (4096); `has_photo`
    is its deprecated alias. `premium_emoji=False` (the bot has no Fragment username, SPEC_V23)
    turns custom emoji into their regular fallback emoji — what subscribers would see anyway.
    Raises PostError for an empty text-only post, broken markup or a too long text. Returns ''
    for a media post without a caption.
    """
    if has_media is None:
        has_media = bool(has_photo)
    source = (text_md or "").strip()
    if not source:
        if has_media:
            return ""
        raise PostError("Пост пустой — добавьте текст или медиа")
    rendered = md_to_html(source)
    try:
        validate_telegram_html(rendered)
    except ValueError as exc:
        raise PostError(f"Ошибка форматирования: {exc}") from exc
    if not has_media and not strip_html(rendered).strip():
        raise PostError("Пост пустой — добавьте текст или медиа")
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT
    length = visible_length(rendered)
    if length > limit:
        message = f"Текст слишком длинный: {length} из {limit} символов"
        if has_media:
            message += f" (для поста с медиа лимит {CAPTION_LIMIT})"
        raise PostError(message)
    return rendered if premium_emoji else strip_custom_emoji_html(rendered)


async def premium_emoji_enabled(db: Any) -> bool:
    """The `premium_emoji` setting (SPEC_V23): custom emoji are sent as they are only when on."""
    return bool(await db.get_premium_emoji())


def message_link(channel: dict, message_id: int) -> str:
    """Public link to a channel message.

    Accepts a channels row (`id`, `username`) or a target row (`channel_id`, `channel_username`).
    """
    username = channel.get("username") or channel.get("channel_username")
    if username:
        return f"https://t.me/{str(username).lstrip('@')}/{message_id}"
    chat_id = channel.get("id", channel.get("channel_id"))
    raw = str(chat_id)
    internal = raw[4:] if raw.startswith("-100") else raw.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"


def normalize_kind(kind: str | None) -> str | None:
    """Stored published_kind → edit class (V1 stored 'photo' for picture posts)."""
    return KIND_MEDIA if kind == "photo" else kind


def post_kind(post: dict) -> str:
    """Edit class of a post dict (`media` list or legacy picture columns): text | media | album:n."""
    return edit_class(post_media(post))


def _class_error(published: str, current: str) -> str:
    if published.startswith("album:") and current.startswith("album:"):
        count = published.split(":", 1)[1]
        return (f"В опубликованном альбоме должно остаться {count} файлов — Telegram не позволяет "
                "добавлять или убирать файлы альбома. Удалите пост и опубликуйте заново")
    return KIND_MISMATCH_ERROR


def _post_media(post: dict) -> list[dict]:
    try:
        return normalize_media(post_media(post))
    except MediaError as exc:
        raise PostError(str(exc)) from exc


def _check_buttons(media: list[dict], buttons: Any) -> None:
    try:
        check_buttons(media, buttons)
    except MediaError as exc:
        raise PostError(str(exc)) from exc


def _markup(post: dict) -> InlineKeyboardMarkup | None:
    try:
        return build_markup(post.get("buttons") or [])
    except ValueError as exc:  # ButtonsError
        raise PostError(f"Ошибка в кнопках: {exc}") from exc


def uses_file_id(item: dict, bot_id: int | None, *, legacy_path: bool = False) -> bool:
    """Whether `item` is sent by its Telegram file_id (else from its uploaded file).

    A file_id of another bot is never used. A legacy file_id (no `file_bot`) is tried unless
    `legacy_path` asks to send the uploaded file instead (the retry after Telegram rejected it).
    """
    if not item.get("file_id"):
        return False
    owner = item.get("file_bot")
    if owner is None:
        return not (legacy_path and item.get("path"))
    return bot_id is None or owner == bot_id


def _media_source(item: dict, config: Any, bot_id: int | None = None, *,
                  legacy_path: bool = False) -> str | FSInputFile:
    """The item's Telegram file_id when this bot may use it, else the uploaded file from
    `config.uploads_dir` (PostError when there is none)."""
    if uses_file_id(item, bot_id, legacy_path=legacy_path):
        return item["file_id"]
    noun = _FILE_NOUNS.get(item.get("type"), "медиа")
    name = item.get("path")
    if not name:
        if item.get("file_id"):  # a file_id of another bot and no local copy
            raise PostError(FOREIGN_MEDIA_ERROR)
        raise PostError(f"Нет файла {noun} — загрузите его заново")
    base = Path(config.uploads_dir).resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise PostError("Некорректный путь к файлу медиа")
    if not path.is_file():
        raise PostError(f"Файл {noun} не найден — загрузите его заново")
    return FSInputFile(path)


def _message_file_id(message: Any, kind: str) -> str | None:
    """Telegram file_id of the media in a sent message (preferring the expected kind)."""
    if not isinstance(message, Message):
        return None
    photo = message.photo[-1].file_id if message.photo else None
    video = message.video.file_id if message.video else None
    animation = message.animation.file_id if message.animation else None
    preferred = {"photo": photo, "video": video, "animation": animation}.get(kind)
    return preferred or animation or video or photo


def learned_file_ids(media: list[dict], messages: list[Any]) -> dict[tuple[str, str], str]:
    """{(type, path): file_id} for items sent from their uploaded file whose message reports a
    file_id (by order). `messages` from this module carry `via_path`; for a plain list the items
    without a file_id count as uploaded."""
    via_path = getattr(messages, "via_path", None)
    learned: dict[tuple[str, str], str] = {}
    for index, (item, message) in enumerate(zip(media, messages)):
        if not item.get("path"):
            continue
        if via_path is None and item.get("file_id"):
            continue
        if via_path is not None and index not in via_path:
            continue
        file_id = _message_file_id(message, item["type"])
        if file_id:
            learned[(item["type"], item["path"])] = file_id
    return learned


def apply_file_ids(media: list[dict], learned: dict[tuple[str, str], str],
                   bot_id: int | None = None) -> list[dict] | None:
    """Copy of `media` with learned file_ids (and `file_bot` = `bot_id`) filled in, paths kept;
    None if nothing changed."""
    changed = False
    result = []
    for item in media:
        new = dict(item)
        key = (item.get("type"), item.get("path"))
        if item.get("path") and key in learned and (
                item.get("file_id") != learned[key] or item.get("file_bot") != bot_id):
            new["file_id"] = learned[key]
            if bot_id is None:
                new.pop("file_bot", None)
            else:
                new["file_bot"] = bot_id
            changed = True
        result.append(new)
    return result if changed else None


async def _store_file_ids(db: Any, post_id: int, learned: dict[tuple[str, str], str],
                          bot_id: int | None = None) -> None:
    """Write learned file_ids into the stored media (re-read, so a concurrent edit is not undone)."""
    if not learned:
        return
    current = await db.get_post(post_id)
    if current is None:
        return
    updated = apply_file_ids(post_media(current), learned, bot_id)
    if updated is not None:
        await db.update_post(post_id, media=updated)


def _link_preview(post: dict) -> LinkPreviewOptions | None:
    return LinkPreviewOptions(is_disabled=True) if post.get("disable_preview") else None


def _input_media(item: dict, source: str | FSInputFile, *, caption: str | None,
                 caption_above: bool | None) -> InputMediaPhoto | InputMediaVideo | InputMediaAnimation:
    kwargs: dict[str, Any] = {"media": source, "caption": caption or None, "parse_mode": "HTML",
                              "show_caption_above_media": caption_above}
    if item["type"] == "video":
        return InputMediaVideo(supports_streaming=True, **kwargs)
    if item["type"] == "animation":
        return InputMediaAnimation(**kwargs)
    return InputMediaPhoto(**kwargs)


# ---------------------------------------------------------------- telegram errors

def describe_telegram_error(exc: TelegramAPIError) -> str:
    """Human Russian description of a Telegram API error."""
    if isinstance(exc, TelegramRetryAfter):
        return f"Telegram ограничил частоту запросов, повторите через {exc.retry_after} с"
    raw = getattr(exc, "message", None) or str(exc)
    low = raw.lower()
    if "chat not found" in low:
        return "Канал не найден"
    if any(s in low for s in ("not enough rights", "chat_admin_required", "need administrator rights",
                              "have no rights to send")):
        return "Нет прав на публикацию"
    if "bot was kicked" in low or "bot is not a member" in low:
        return "Бот удалён из канала"
    if "can't parse entities" in low:
        return f"Ошибка разметки: {raw}"
    if "message to edit not found" in low or "message_id_invalid" in low:
        return "Сообщение не найдено в канале (возможно, удалено вручную)"
    if "no text in the message to edit" in low or "no caption in the message to edit" in low:
        return KIND_MISMATCH_ERROR
    if "message can't be deleted" in low:
        return "Telegram не разрешает удалить это сообщение"
    if "message can't be edited" in low:
        return "Telegram не разрешает изменить это сообщение"
    if any(marker in low for marker in FILE_ID_ERRORS):
        return "Файл недоступен в Telegram — загрузите его заново"
    if "request entity too large" in low or "file is too big" in low:
        return "Файл слишком большой для Telegram (не больше 50 МБ)"
    if "photo_invalid_dimensions" in low:
        return "Telegram не принял картинку: неподходящие размеры"
    if "caption is too long" in low:
        return "Подпись слишком длинная для Telegram"
    if isinstance(exc, TelegramNetworkError):
        return f"Сетевая ошибка: {raw}"
    return raw


def _is_file_id_error(exc: Exception) -> bool:
    """Telegram does not know a file_id we sent (it belongs to another bot / expired)."""
    if not isinstance(exc, TelegramBadRequest):
        return False
    low = (exc.message or "").lower()
    return any(marker in low for marker in FILE_ID_ERRORS)


def _legacy_uploads(media: list[dict], config: Any) -> bool:
    """Some legacy item (file_id without `file_bot`) also has its uploaded file on disk."""
    base = Path(config.uploads_dir)
    return any(item.get("file_id") and item.get("file_bot") is None and item.get("path")
               and (base / item["path"]).is_file() for item in media)


def _is_not_modified(exc: TelegramAPIError) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in exc.message.lower()


def _is_already_deleted(exc: TelegramAPIError) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message to delete not found" in exc.message.lower()


def _is_chat_unreachable(exc: TelegramAPIError) -> bool:
    """The bot has no access to the channel at all (kicked / not a member / channel gone)."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    return isinstance(exc, TelegramBadRequest) and "chat not found" in exc.message.lower()


async def _with_retry(call: Callable[[], Awaitable[T]]) -> T:
    """Run a Telegram call; on flood control wait the time Telegram asked for and retry once —
    only when it is at most RETRY_AFTER_MAX s. A longer wait is raised at once: a retry inside
    the flood window would be rejected again (the scheduler re-plans such a channel instead)."""
    try:
        return await call()
    except TelegramRetryAfter as exc:
        if exc.retry_after > RETRY_AFTER_MAX:
            raise
        delay = max(exc.retry_after, 0)
        log.warning("Flood control, retrying in %s s", delay)
        await _pause(delay)
        return await call()


# ------------------------------------------------------------------ uploads

def upload_timeout(sources: Iterable[Any]) -> int | None:
    """Request timeout for a call that uploads files among `sources`: UPLOAD_TIMEOUT_MIN plus the
    time to push their bytes at UPLOAD_RATE, at most UPLOAD_TIMEOUT_MAX. None when nothing is
    uploaded (file_ids only) — aiogram's default timeout applies then."""
    files = [source for source in sources if isinstance(source, FSInputFile)]
    if not files:
        return None
    total = 0
    for source in files:
        try:
            total += Path(source.path).stat().st_size
        except OSError:
            pass
    return min(UPLOAD_TIMEOUT_MAX, max(UPLOAD_TIMEOUT_MIN, 60 + total // UPLOAD_RATE))


def _timeout_kwargs(sources: Iterable[Any]) -> dict[str, Any]:
    timeout = upload_timeout(sources)
    return {"request_timeout": timeout} if timeout else {}


@contextmanager
def _upload_errors(uploading: bool) -> Iterator[None]:
    """A network error (timeout included) while files were being uploaded is tagged: the request
    may still have reached Telegram, so the message may be in the chat."""
    try:
        yield
    except TelegramNetworkError as exc:
        if uploading:
            exc.during_upload = True  # type: ignore[attr-defined]
        raise


def _cut_upload(exc: BaseException) -> bool:
    """`exc` is a network error raised while uploading files (see `_upload_errors`)."""
    return isinstance(exc, TelegramNetworkError) and bool(getattr(exc, "during_upload", False))


# ------------------------------------------------------------------ sending

def _sources(bot: Any, media: list[dict], config: Any, *,
             legacy_path: bool) -> tuple[list[str | FSInputFile], set[int]]:
    """What to send for every item (file_id or uploaded file) and the indexes sent from files."""
    bot_id = bot_id_of(bot)
    sources = [_media_source(item, config, bot_id, legacy_path=legacy_path) for item in media]
    return sources, {index for index, source in enumerate(sources) if not isinstance(source, str)}


async def _deliver(
    bot: Any, post: dict, chat_id: int | str, config: Any, text_html: str,
    markup: InlineKeyboardMarkup | None, media: list[dict], *, legacy_path: bool = False,
) -> SentMessages:
    """Send an already rendered post to a chat; returns every sent message (album: all of them)."""
    silent = bool(post.get("silent"))
    protect = bool(post.get("protect"))
    above = bool(post.get("caption_above")) or None
    if not media:
        message = await bot.send_message(
            chat_id=chat_id, text=text_html, parse_mode="HTML", reply_markup=markup,
            disable_notification=silent, protect_content=protect, link_preview_options=_link_preview(post),
        )
        return SentMessages([message])
    sources, via_path = _sources(bot, media, config, legacy_path=legacy_path)
    timeout = _timeout_kwargs(sources)
    if len(media) == 1:
        item = media[0]
        common: dict[str, Any] = {
            "chat_id": chat_id, "caption": text_html or None, "parse_mode": "HTML", "reply_markup": markup,
            "disable_notification": silent, "protect_content": protect, "show_caption_above_media": above,
            **timeout,
        }
        source = sources[0]
        with _upload_errors(bool(via_path)):
            if item["type"] == "video":
                message = await bot.send_video(video=source, supports_streaming=True, **common)
            elif item["type"] == "animation":
                message = await bot.send_animation(animation=source, **common)
            else:
                message = await bot.send_photo(photo=source, **common)
        return SentMessages([message], via_path)
    group = [
        _input_media(item, source, caption=text_html if index == 0 else None,
                     caption_above=above if index == 0 else None)
        for index, (item, source) in enumerate(zip(media, sources))
    ]
    with _upload_errors(bool(via_path)):
        messages = await bot.send_media_group(chat_id=chat_id, media=group, disable_notification=silent,
                                              protect_content=protect, **timeout)
    return SentMessages(messages, via_path)


async def _deliver_with_fallback(
    bot: Any, post: dict, chat_id: int | str, config: Any, text_html: str,
    markup: InlineKeyboardMarkup | None, media: list[dict],
) -> SentMessages:
    """`_deliver`; when Telegram does not know a legacy file_id (one stored before V2.2, maybe of
    the previous bot) and the uploaded file is still there, send once more from the files."""
    try:
        return await _deliver(bot, post, chat_id, config, text_html, markup, media)
    except TelegramBadRequest as exc:
        if not (_is_file_id_error(exc) and _legacy_uploads(media, config)):
            raise
        log.info("Post %s: Telegram does not know a stored file_id (%s) — sending the uploaded files",
                 post.get("id"), exc.message)
        return await _deliver(bot, post, chat_id, config, text_html, markup, media, legacy_path=True)


async def send_post(bot: Any, post: dict, chat_id: int | str, config: Any, *,
                    premium_emoji: bool = True) -> SentMessages:
    """Low level: render and send a post (text, single media with caption, or an album) to any chat.

    Returns the sent messages (one, or every album message; a list with `via_path`, see
    `learned_file_ids`). Raises PostError for render / buttons / media problems (FOREIGN_MEDIA_ERROR
    for a file_id of another bot without an uploaded file); Telegram errors propagate.
    `premium_emoji=False` sends custom emoji as regular ones (see `render_text`).
    """
    media = _post_media(post)
    text_html = render_text(post.get("text_md") or "", has_media=bool(media), premium_emoji=premium_emoji)
    _check_buttons(media, post.get("buttons"))
    return await _deliver_with_fallback(bot, post, chat_id, config, text_html, _markup(post), media)


async def _channel_info(db: Any, target: dict) -> tuple[dict | None, str, dict]:
    """(channel row or None, display title, dict usable by message_link) for a target row."""
    channel = await db.get_channel(target["channel_id"])
    title = (channel or {}).get("title") or target.get("channel_title") or str(target["channel_id"])
    link_info = channel or {"id": target["channel_id"], "username": target.get("channel_username")}
    return channel, title, link_info


# ------------------------------------------------------------------ publishing

def _due_targets(targets: list[dict], ts: int) -> list[dict]:
    return [t for t in targets if t["status"] == TARGET_PENDING and t.get("scheduled_at")
            and t["scheduled_at"] <= ts]


def _manual_targets(targets: list[dict], channel_ids: set[int] | None) -> list[dict]:
    return [t for t in targets if t["status"] in (TARGET_PENDING, TARGET_FAILED)
            and (channel_ids is None or t["channel_id"] in channel_ids)]


async def publish_post(
    bot: Any, db: Any, config: Any, post_id: int, *, only_due: bool = False, now_ts: int | None = None,
    channel_ids: Iterable[int] | None = None,
) -> PublishReport:
    """Publish a post to its channels.

    Manual (`only_due=False`): every pending/failed target (optionally only `channel_ids`) is sent
    now, ignoring and clearing its scheduled time; the post must be draft/scheduled/failed/partial.
    Scheduler (`only_due=True`): only pending targets whose `scheduled_at <= now_ts`; the post must
    still be 'scheduled' and due — otherwise PostNotDue (also after the atomic claim, when the
    targets were cancelled or moved meanwhile).

    Validation (render, buttons, media, targets, edit class) happens before the post is claimed,
    so a PostError raised here leaves the post untouched. Once claimed, the post always leaves
    'sending': 'scheduled' while pending targets with a time remain, else sent / partial / failed.

    Each target is marked `sending_at` right before its Telegram call (cleared by the write that
    settles it), so a restart after a kill knows which channel may have the message. Scheduler
    path: a channel Telegram refuses for flood control longer than RETRY_AFTER_MAX is not failed —
    it stays pending, re-planned for when the wait is over (nothing was posted).
    """
    await db.migrate()
    post = await db.get_post(post_id)
    if post is None:
        raise PostError("Пост не найден")
    current = now_ts if now_ts is not None else now()
    wanted = {int(c) for c in channel_ids} if channel_ids is not None else None
    if only_due:
        due_post = post["status"] == POST_SCHEDULED and post.get("scheduled_at") and post["scheduled_at"] <= current
        if not due_post or not _due_targets(post["targets"], current):
            raise PostNotDue(NOT_DUE_ERROR)
    else:
        if post["status"] not in PUBLISHABLE_STATUSES:
            raise PostError(BUSY_ERROR)
        if not _manual_targets(post["targets"], wanted):
            raise PostError(NO_TARGETS_ERROR)

    premium = await premium_emoji_enabled(db)
    media, text_html, markup = _prepare_publish(post, config, bot_id_of(bot), premium_emoji=premium)

    async with _tracked():
        if only_due:
            claimed = await db.claim_post_for_sending(post_id, (POST_SCHEDULED,), due_before=current)
        else:
            claimed = await db.claim_post_for_sending(post_id, PUBLISHABLE_STATUSES)
        if not claimed:
            raise PostNotDue(NOT_DUE_ERROR) if only_due else PostError(BUSY_ERROR)

        report = PublishReport(post_id=post_id)
        aborted = False  # True once sending started and until it completed
        in_flight: int | None = None  # channel being sent to right now
        try:
            # Re-read under the claim: content and targets may have changed since the check (an
            # edit saved a moment ago, a target cancelled / moved). Errors here leave the targets
            # as they were; the finish step restores the post status.
            fresh = await db.get_post(post_id)
            if fresh is None:
                raise PostError("Пост не найден")
            post = fresh
            media, text_html, markup = _prepare_publish(post, config, bot_id_of(bot), premium_emoji=premium)
            targets = _due_targets(post["targets"], current) if only_due else _manual_targets(post["targets"], wanted)
            if not targets:
                raise PostNotDue(NOT_DUE_ERROR) if only_due else PostError(NO_TARGETS_ERROR)
            if not only_due:
                # Sent now: their plan slot is void, and a crash mid-way must not leave a time that
                # would make the scheduler send them again later.
                for target in targets:
                    if target.get("scheduled_at") or target.get("queued"):
                        await db.set_target_schedule(post_id, target["channel_id"], None)
            aborted = True
            for index, target in enumerate(targets):
                if index:
                    await _pause(CHANNEL_PAUSE)
                in_flight = target["channel_id"]
                await db.update_target(post_id, in_flight, sending_at=now())
                result, media = await _publish_target(bot, db, config, post, media, target, text_html, markup,
                                                      defer_on_flood=only_due)
                in_flight = None
                report.results.append(result)
            aborted = False
        finally:
            if in_flight is not None:
                await _mark_interrupted(db, post_id, in_flight)
            await _finish_publish(db, post_id, report, text_html, media, aborted=aborted)
    return report


def _prepare_publish(post: dict, config: Any, bot_id: int | None = None, *,
                     premium_emoji: bool = True) -> tuple[list[dict], str, InlineKeyboardMarkup | None]:
    """Validate a post for publishing: (media, rendered text, markup) or PostError."""
    media = _post_media(post)
    text_html = render_text(post.get("text_md") or "", has_media=bool(media), premium_emoji=premium_emoji)
    markup = _markup(post)
    _check_buttons(media, post.get("buttons"))
    for item in media:
        _media_source(item, config, bot_id)  # fail early on a missing upload / another bot's file
    published = normalize_kind(post.get("published_kind"))
    if published and any(t["status"] == TARGET_SENT for t in post["targets"]) and published != edit_class(media):
        raise PostError(_class_error(published, edit_class(media)))
    return media, text_html, markup


async def _mark_interrupted(db: Any, post_id: int, channel_id: int) -> None:
    """The send to this channel was cut (cancelled / crashed): Telegram may or may not have the
    message, so the target must not stay due for an automatic re-send — mark it failed (also a
    failed target that was being retried: its old error would hide the possible message)."""
    try:
        target = await db.get_target(post_id, channel_id)
        if target is None:
            return
        if target["status"] in (TARGET_PENDING, TARGET_FAILED):
            await db.update_target(post_id, channel_id, status=TARGET_FAILED, error=INTERRUPTED_ERROR,
                                   scheduled_at=None, queued=0, sending_at=None)
        elif target.get("sending_at") is not None:
            await db.update_target(post_id, channel_id, sending_at=None)
    except Exception:  # the DB itself may be what failed; the finish step must still run
        log.exception("Could not mark the interrupted target %s of post %s", channel_id, post_id)


async def _publish_target(
    bot: Any, db: Any, config: Any, post: dict, media: list[dict], target: dict, text_html: str,
    markup: InlineKeyboardMarkup | None, *, defer_on_flood: bool = False,
) -> tuple[TargetResult, list[dict]]:
    """Send to one channel. Returns the result and the media list (with newly learned file_ids).

    A network error during an upload → INTERRUPTED_ERROR (the message may be in the channel).
    `defer_on_flood` (scheduler): flood control → the target stays pending for a later retry.
    """
    post_id = post["id"]
    channel_id = target["channel_id"]
    channel, title, link_info = await _channel_info(db, target)
    if channel is None or channel["status"] != CH_ACTIVE:
        return await _fail_target(db, post_id, channel_id, title, CHANNEL_INACTIVE_ERROR), media

    try:
        messages = await _with_retry(
            lambda: _deliver_with_fallback(bot, post, channel_id, config, text_html, markup, media))
    except TelegramAPIError as exc:
        if _cut_upload(exc):
            log.warning("Publishing post %s to %s was cut while uploading: %s", post_id, channel_id, exc)
            return await _fail_target(db, post_id, channel_id, title, INTERRUPTED_ERROR), media
        if defer_on_flood and isinstance(exc, TelegramRetryAfter):
            log.warning("Post %s to %s: flood control for %s s — re-planned", post_id, channel_id, exc.retry_after)
            return await _defer_target(db, post_id, channel_id, title, exc), media
        log.warning("Publishing post %s to %s failed: %s", post_id, channel_id, exc)
        return await _fail_target(db, post_id, channel_id, title, describe_telegram_error(exc)), media
    except PostError as exc:
        return await _fail_target(db, post_id, channel_id, title, str(exc)), media
    except Exception as exc:  # unexpected (network layer, validation) — must not abort other channels
        log.exception("Unexpected error publishing post %s to %s", post_id, channel_id)
        return await _fail_target(db, post_id, channel_id, title, f"Неизвестная ошибка: {exc}"), media
    if not messages:
        return await _fail_target(db, post_id, channel_id, title, "Telegram не вернул сообщение"), media

    ts = now()
    ids = [m.message_id for m in messages]
    await db.update_target(
        post_id, channel_id, status=TARGET_SENT, message_id=ids[0],
        message_ids_json=json.dumps(ids) if len(ids) > 1 else None, sent_at=ts, error=None,
        scheduled_at=None, queued=0, media_sig=media_signature(media), sending_at=None,
    )
    group_id = getattr(messages[0], "media_group_id", None) if len(ids) > 1 else None
    await db.add_channel_post(channel_id, ids[0], via_bot=True, ts=ts, media_group_id=group_id)
    learned = learned_file_ids(media, messages)
    if learned:
        bot_id = bot_id_of(bot)
        media = apply_file_ids(media, learned, bot_id) or media  # the remaining channels reuse the upload
        await _store_file_ids(db, post_id, learned, bot_id)
    return TargetResult(channel_id=channel_id, title=title, ok=True, message_id=ids[0],
                        link=message_link(link_info, ids[0])), media


async def _fail_target(db: Any, post_id: int, channel_id: int, title: str, error: str) -> TargetResult:
    await db.update_target(post_id, channel_id, status=TARGET_FAILED, error=error, scheduled_at=None, queued=0,
                           sending_at=None)
    return TargetResult(channel_id=channel_id, title=title, ok=False, error=error)


async def _defer_target(db: Any, post_id: int, channel_id: int, title: str,
                        exc: TelegramRetryAfter) -> TargetResult:
    """Scheduler: Telegram refused the send for flood control — nothing was posted. The target
    stays pending (queued flag kept) with a new time right after the wait, so the scheduler sends
    it then instead of the slot being lost."""
    wait = max(int(exc.retry_after), 1)
    error = f"Telegram ограничил частоту запросов — повторю автоматически через {wait} с"
    await db.update_target(post_id, channel_id, error=error, scheduled_at=now() + wait + 1, sending_at=None)
    return TargetResult(channel_id=channel_id, title=title, ok=False, error=error)


async def _finish_publish(
    db: Any, post_id: int, report: PublishReport, text_html: str, media: list[dict], *, aborted: bool,
) -> None:
    """Move the post out of 'sending' according to the state of all its targets.

    Pending targets with a time left → 'scheduled' (earliest time); otherwise over non-deleted
    targets: all sent → 'sent', some → 'partial', none → 'failed' (when something failed or the run
    was aborted) or back to 'draft' (nothing was attempted, e.g. everything was unscheduled).
    """
    targets = await db.get_targets(post_id)
    times = [t["scheduled_at"] for t in targets if t["status"] == TARGET_PENDING and t.get("scheduled_at")]
    live = [t for t in targets if t["status"] != TARGET_DELETED]
    sent = sum(1 for t in live if t["status"] == TARGET_SENT)
    failed = sum(1 for t in live if t["status"] == TARGET_FAILED)
    pending = sum(1 for t in live if t["status"] == TARGET_PENDING)
    if times:
        status = POST_SCHEDULED
    elif live and sent == len(live):
        status = POST_SENT
    elif sent:
        status = POST_PARTIAL
    elif report.fail_count or aborted or (failed and not pending):
        status = POST_FAILED
    else:
        status = POST_DRAFT
    errors = [f"{r.title}: {r.error}" for r in report.results if not r.ok]
    if not errors and aborted and status in (POST_FAILED, POST_PARTIAL):
        errors = ["Публикация прервана"]
    fields: dict[str, Any] = {
        "status": status,
        "scheduled_at": min(times) if times else None,
        "error": "; ".join(errors) or None,
        "text_html": text_html,
    }
    if report.ok_count:
        fields["sent_at"] = now()
        fields["published_kind"] = edit_class(media)
    await db.update_post(post_id, **fields)


# ------------------------------------------------------------------ editing

def _needs_media_edit(target: dict, signature: str, media_changed: bool) -> bool:
    """A channel needs the media replaced when what it shows differs from the post's media.

    Targets published before V2 have no signature: then the caller's `media_changed` decides.
    """
    shown = target.get("media_sig")
    if shown is None:
        return media_changed
    return shown != signature


async def edit_published_post(
    bot: Any, db: Any, config: Any, post_id: int, *, media_changed: bool = False,
    photo_changed: bool | None = None,
) -> PublishReport:
    """Apply the current post content to every message already sent to channels.

    Text posts → edit_message_text. Single media → edit_message_caption, or edit_message_media
    for channels whose shown media differs from the post's (per-target signature, so a channel
    whose media edit failed gets it again on the next save). Albums → caption of the first
    message, and edit_message_media for every message when the media differs (same count
    required). "message is not modified" counts as success. A post cannot switch between text /
    media / album after publication (PostError). `photo_changed` is a deprecated alias of
    `media_changed` (only consulted for targets published before V2).
    """
    media_changed = bool(media_changed or photo_changed)
    await db.migrate()
    post = await db.get_post(post_id)
    if post is None:
        raise PostError("Пост не найден")
    targets = [t for t in post["targets"] if t["status"] == TARGET_SENT]
    if not targets:
        raise PostError("Пост удалён из каналов" if post["status"] == POST_DELETED else "Пост ещё не опубликован")
    media = _post_media(post)
    kind = edit_class(media)
    published = normalize_kind(post.get("published_kind"))
    if published and published != kind:
        raise PostError(_class_error(published, kind))

    text_html = render_text(post.get("text_md") or "", has_media=bool(media),
                            premium_emoji=await premium_emoji_enabled(db))
    markup = _markup(post)
    _check_buttons(media, post.get("buttons"))
    signature = media_signature(media)
    plan = [(t, bool(media) and _needs_media_edit(t, signature, media_changed)) for t in targets]
    if any(need for _, need in plan):
        for item in media:
            _media_source(item, config, bot_id_of(bot))

    report = PublishReport(post_id=post_id, action="edit")
    async with _tracked():
        for index, (target, need_media) in enumerate(plan):
            if index:
                await _pause(CHANNEL_PAUSE)
            result, media = await _edit_target(bot, db, config, post, media, target, text_html, markup,
                                               need_media=need_media, signature=signature)
            report.results.append(result)

        errors = [f"{r.title}: {r.error}" for r in report.results if not r.ok]
        fields: dict[str, Any] = {"text_html": text_html, "error": "; ".join(errors) or None}
        if report.ok_count and not post.get("published_kind"):
            fields["published_kind"] = kind
        await db.update_post(post_id, **fields)
    return report


async def _edit_target(
    bot: Any, db: Any, config: Any, post: dict, media: list[dict], target: dict, text_html: str,
    markup: InlineKeyboardMarkup | None, *, need_media: bool, signature: str,
) -> tuple[TargetResult, list[dict]]:
    channel_id = target["channel_id"]
    message_id = target["message_id"]
    _, title, link_info = await _channel_info(db, target)
    stale = need_media and target.get("media_sig") is None
    try:
        messages = await _with_retry(
            lambda: _edit_with_fallback(bot, post, media, target, config, text_html, markup, need_media=need_media)
        )
    except TelegramAPIError as exc:
        if not _is_not_modified(exc):
            log.warning("Editing post %s in %s failed: %s", post["id"], channel_id, exc)
            # A cut upload: the channel may show the new media or the old one — the stored
            # signature stays the old one, so the next save replaces the media again.
            error = EDIT_INTERRUPTED_ERROR if _cut_upload(exc) else describe_telegram_error(exc)
            return await _edit_failed(db, post["id"], channel_id, title, error, stale=stale), media
        messages = []
    except PostError as exc:
        return await _edit_failed(db, post["id"], channel_id, title, str(exc), stale=stale), media
    except Exception as exc:
        log.exception("Unexpected error editing post %s in %s", post["id"], channel_id)
        return await _edit_failed(db, post["id"], channel_id, title, f"Неизвестная ошибка: {exc}",
                                  stale=stale), media

    # Success: the channel now shows the post's media (also adopts it for pre-V2 targets).
    await db.update_target(post["id"], channel_id, error=None, media_sig=signature)
    if need_media:
        learned = learned_file_ids(media, messages)
        if learned:
            bot_id = bot_id_of(bot)
            media = apply_file_ids(media, learned, bot_id) or media
            await _store_file_ids(db, post["id"], learned, bot_id)
    return TargetResult(channel_id=channel_id, title=title, ok=True, message_id=message_id,
                        link=message_link(link_info, message_id)), media


async def _edit_failed(db: Any, post_id: int, channel_id: int, title: str, error: str, *,
                       stale: bool = False) -> TargetResult:
    # The message stays published, so the target keeps its 'sent' status; only the error is noted.
    fields: dict[str, Any] = {"error": error}
    if stale:
        fields["media_sig"] = STALE_MEDIA_SIG
    await db.update_target(post_id, channel_id, **fields)
    return TargetResult(channel_id=channel_id, title=title, ok=False, error=error)


async def _edit_with_fallback(
    bot: Any, post: dict, media: list[dict], target: dict, config: Any, text_html: str,
    markup: InlineKeyboardMarkup | None, *, need_media: bool,
) -> list[Any]:
    """`_edit_message`; a legacy file_id Telegram does not know → once more from the uploaded files."""
    try:
        return await _edit_message(bot, post, media, target, config, text_html, markup, need_media=need_media)
    except TelegramBadRequest as exc:
        if not (need_media and _is_file_id_error(exc) and _legacy_uploads(media, config)):
            raise
        log.info("Post %s: Telegram does not know a stored file_id (%s) — sending the uploaded files",
                 post.get("id"), exc.message)
        return await _edit_message(bot, post, media, target, config, text_html, markup, need_media=need_media,
                                   legacy_path=True)


async def _edit_message(
    bot: Any, post: dict, media: list[dict], target: dict, config: Any, text_html: str,
    markup: InlineKeyboardMarkup | None, *, need_media: bool, legacy_path: bool = False,
) -> list[Any]:
    """Edit one channel's message(s). Returns the messages returned by media edits (by item order,
    a SentMessages with `via_path` when media were replaced)."""
    chat_id = target["channel_id"]
    message_id = target["message_id"]
    above = bool(post.get("caption_above"))
    if not media:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text_html, parse_mode="HTML",
            reply_markup=markup, link_preview_options=_link_preview(post),
        )
        return []
    if len(media) == 1:
        if need_media:
            sources, via_path = _sources(bot, media, config, legacy_path=legacy_path)
            with _upload_errors(bool(via_path)):
                result = await bot.edit_message_media(
                    chat_id=chat_id, message_id=message_id, reply_markup=markup,
                    media=_input_media(media[0], sources[0], caption=text_html, caption_above=above),
                    **_timeout_kwargs(sources),
                )
            return SentMessages([result], via_path)
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id, caption=text_html or None, parse_mode="HTML",
            reply_markup=markup, show_caption_above_media=above,
        )
        return []

    ids = target.get("message_ids") or []
    if len(ids) != len(media):
        raise PostError(ALBUM_MISMATCH_ERROR)
    if not need_media:
        await bot.edit_message_caption(chat_id=chat_id, message_id=ids[0], caption=text_html or None,
                                       parse_mode="HTML", show_caption_above_media=above)
        return []
    sources, via_path = _sources(bot, media, config, legacy_path=legacy_path)
    results: list[Any] = []
    for index, (item, source, album_message_id) in enumerate(zip(media, sources, ids)):
        first = index == 0
        try:
            with _upload_errors(index in via_path):
                result = await bot.edit_message_media(
                    chat_id=chat_id, message_id=album_message_id,
                    media=_input_media(item, source, caption=text_html if first else None,
                                       caption_above=above if first else None),
                    **_timeout_kwargs([source]),
                )
        except TelegramBadRequest as exc:
            if not _is_not_modified(exc):
                raise
            result = None
        results.append(result)
    return SentMessages(results, via_path)


# ------------------------------------------------------------------ deleting

# A post shown in some channel may be in any of these statuses while it is deleted from channels
# ('deleted' too: a channel whose deletion failed earlier can be retried).
DELETABLE_STATUSES = (POST_DRAFT, POST_SCHEDULED, POST_SENT, POST_PARTIAL, POST_FAILED, POST_DELETED)


async def delete_published_post(bot: Any, db: Any, post_id: int) -> PublishReport:
    """Delete the post's messages from every channel it was sent to.

    The post is claimed ('sending') for the whole deletion, so the scheduler cannot publish it to
    a queued channel meanwhile (PostError DELETE_BUSY_ERROR when it is being published right
    now). First its pending targets are taken out of the plan — whatever the per-channel results,
    a post the owner deletes never goes out later by itself (a channel can still be published by
    hand).

    Targets deleted successfully (or already missing) get status 'deleted'. A channel the bot has
    no access to any more (kicked / channel gone) cannot be cleaned up by the bot: its target is
    marked deleted locally with a note asking to remove the message by hand. Finally the post
    becomes 'deleted' when no channel shows it any more, else 'sent' / 'partial' with the failures
    in `error` («Удалить из каналов» can be repeated).
    """
    post = await db.get_post(post_id)
    if post is None:
        raise PostError("Пост не найден")
    if post["status"] == POST_SENDING:
        raise PostError(DELETE_BUSY_ERROR)
    if not any(t["status"] == TARGET_SENT for t in post["targets"]):
        raise PostError("Пост не опубликован ни в одном канале")

    report = PublishReport(post_id=post_id, action="delete")
    async with _tracked():
        if not await db.claim_post_for_sending(post_id, DELETABLE_STATUSES):
            raise PostError(DELETE_BUSY_ERROR)
        try:
            await db.clear_target_schedules(post_id)
            # Fresh rows under the claim: a channel published a moment ago is deleted too.
            targets = [t for t in await db.get_targets(post_id) if t["status"] == TARGET_SENT]
            for index, target in enumerate(targets):
                if index:
                    await _pause(CHANNEL_PAUSE)
                report.results.append(await _delete_target(bot, db, post_id, target))
        finally:
            await _finish_delete(db, post_id, report)
    return report


async def _finish_delete(db: Any, post_id: int, report: PublishReport) -> None:
    """Release a post claimed for deletion: 'deleted' when no channel shows it any more, else
    'sent' (every live channel still shows it) / 'partial', with the failures in `error`."""
    targets = await db.get_targets(post_id)
    live = [t for t in targets if t["status"] != TARGET_DELETED]
    sent = sum(1 for t in live if t["status"] == TARGET_SENT)
    if not sent:
        status = POST_DELETED
    elif sent == len(live):
        status = POST_SENT
    else:
        status = POST_PARTIAL
    errors = [f"{r.title}: {r.error}" for r in report.results if not r.ok]
    await db.update_post_if(post_id, POST_SENDING, status=status, scheduled_at=None,
                            error="; ".join(errors) or None)


async def _delete_target(bot: Any, db: Any, post_id: int, target: dict) -> TargetResult:
    channel_id = target["channel_id"]
    message_id = target["message_id"]
    ids = target.get("message_ids") or [message_id]
    _, title, _ = await _channel_info(db, target)
    try:
        await _delete_messages(bot, channel_id, ids)
    except TelegramAPIError as exc:
        if _is_chat_unreachable(exc):
            log.warning("Post %s: no access to %s (%s) — marking deleted locally", post_id, channel_id, exc)
            await db.update_target(post_id, channel_id, status=TARGET_DELETED, error=UNREACHABLE_NOTE)
            return TargetResult(channel_id=channel_id, title=title, ok=True, message_id=message_id,
                                note=UNREACHABLE_NOTE)
        log.warning("Deleting post %s from %s failed: %s", post_id, channel_id, exc)
        error = describe_telegram_error(exc)
        await db.update_target(post_id, channel_id, error=error)
        return TargetResult(channel_id=channel_id, title=title, ok=False, message_id=message_id, error=error)
    await db.update_target(post_id, channel_id, status=TARGET_DELETED, error=None)
    return TargetResult(channel_id=channel_id, title=title, ok=True, message_id=message_id)


async def _delete_messages(bot: Any, chat_id: int, message_ids: list[int]) -> None:
    """Delete a message or a whole album. Messages already gone count as deleted.

    Albums use one delete_messages call; if Telegram rejects it (BadRequest), the messages are
    deleted one by one (the first real error is raised after trying all of them).
    """
    if len(message_ids) > 1:
        try:
            await _with_retry(lambda: bot.delete_messages(chat_id=chat_id, message_ids=list(message_ids)))
            return
        except TelegramBadRequest as exc:
            if _is_chat_unreachable(exc):
                raise
            log.info("Bulk delete in %s failed (%s), deleting one by one", chat_id, exc)
    first_error: TelegramAPIError | None = None
    for message_id in message_ids:
        try:
            await _with_retry(lambda mid=message_id: bot.delete_message(chat_id=chat_id, message_id=mid))
        except TelegramAPIError as exc:
            if _is_already_deleted(exc):
                continue
            if _is_chat_unreachable(exc):
                raise
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


# ------------------------------------------------------------------ preview & notifications

async def send_preview(bot: Any, db: Any, config: Any, post_id: int, chat_id: int) -> list[Message]:
    """Send the post as it will look in the channel to `chat_id` (an admin's DM).

    Returns the sent messages. Raises PostError for render problems and Telegram failures.
    """
    post = await db.get_post(post_id)
    if post is None:
        raise PostError("Пост не найден")
    media = _post_media(post)
    premium = await premium_emoji_enabled(db)
    try:
        messages = await _with_retry(lambda: send_post(bot, post, chat_id, config, premium_emoji=premium))
    except TelegramAPIError as exc:
        raise PostError(f"Не удалось отправить превью: {describe_telegram_error(exc)}") from exc
    await _store_file_ids(db, post_id, learned_file_ids(media, messages), bot_id_of(bot))
    return messages


async def notify_admins(bot: Any, db: Any, text: str, reply_markup: Any = None) -> int:
    """Send an HTML message to every admin; failures are logged and ignored.

    Returns how many admins received it.
    """
    delivered = 0
    for admin_id in sorted(await db.get_admin_ids()):
        try:
            await bot.send_message(
                chat_id=admin_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            delivered += 1
        except Exception:  # an admin who blocked the bot must not break the caller
            log.warning("Could not notify admin %s", admin_id, exc_info=True)
    return delivered

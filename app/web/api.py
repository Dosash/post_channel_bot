"""JSON API of the web panel (``<prefix>/api/*``): posts, channels, overview, admins, settings.

Settings: ``GET /api/settings`` (bot, password, ``premium_emoji``…), ``PUT /api/settings/premium-emoji``
``{enabled: bool}`` → ``{premium_emoji}`` (SPEC_V23: the bot has a Fragment username, custom emoji
are published as they are). ``PUT /api/settings/helper-bots {bots: ["a_bot", …] | "a_bot b_bot"}`` →
``{helper_bots}`` — the owner's other bots offered as «➕ @bot» links in new channels;
``GET /api/channels/{id}/helpers`` → ``{helpers: [{username, is_admin, url}]}``.

Publishing requests (``POST /api/posts``, ``PUT /api/posts/{id}``, ``POST /api/posts/{id}/publish``)
accept ``X-Idempotency-Key``: a retry with the same key never creates or publishes again
(``app.web.idempotency``).

Paths are relative to the panel prefix (``WEB_PREFIX``, see ``app.web.urls``); URLs handed out in
responses (media files, uploads) are built with ``api_url`` and include it.

Media endpoints (render, uploads, post media files) live in ``app.web.api_media``, folders,
publishing slots and the content plan in ``app.web.api_plan``; both reuse the helpers and the
serializers defined here.

Errors are returned as ``{"error": "<Russian message>"}`` with a 4xx status (see
``error_middleware``). Timestamps are unix seconds. Business logic lives in ``app.services``;
handlers here only validate input, call the services and serialize the result.

Posts (SPEC_V2 §7): a post carries a ``media`` list (photos, videos, GIFs, albums). A request may
reference a Telegram ``file_id`` only when it already belongs to the post itself or — for
duplicates — to the post given as ``copy_from``; uploaded files are referenced by their name in
the uploads directory. Saving takes an ``action``: ``draft`` | ``publish`` (now) | ``schedule``
(explicit time for every selected channel) | ``queue`` (each channel's next free slot); without
an action a scheduled post keeps its schedule (``keep``). A post already sent to some channel is
edited in place (``apply: true``), every mutation is refused with 409 while a post is being sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from app.buttons import ButtonsError, validate_buttons
from app.config import BOT_TOKEN_SETTING, Config
from app.db import (
    CH_ACTIVE,
    CH_IGNORED,
    CH_LEFT,
    CH_PENDING,
    POST_DELETED,
    POST_DRAFT,
    POST_FAILED,
    POST_PARTIAL,
    POST_SCHEDULED,
    POST_SENDING,
    POST_SENT,
    TARGET_PENDING,
    TARGET_SENT,
    Database,
    _encode_aliases,
    now,
)
from app.markdown import excerpt
from app.media import (
    EXTRA_KEYS,
    FILE_ID_RE,
    TYPE_EXTENSIONS,
    UPLOAD_NAME_RE,
    MediaError,
    check_buttons,
    describe_media,
    edit_class,
    media_kind,
    media_signature,
    normalize_media,
    post_media,
)
from app.services import channels as channels_service
from app.services import helper_bots
from app.services import plan as plan_service
from app.services import stats as stats_service
from app.services import timezone as timezone_service
from app.services.channels import ChannelError
from app.services.folders import CHANNEL_NOT_FOUND_ERROR, NOT_FOUND_ERROR, FolderError
from app.services.plan import PlanError
from app.services.posting import (
    DELETE_BUSY_ERROR,
    KIND_MISMATCH_ERROR,
    PostError,
    PublishReport,
    delete_published_post,
    edit_published_post,
    message_link,
    normalize_kind,
    publish_post,
    render_text,
    send_preview,
)
from app.web.auth import (
    JsonError,
    fixed_password,
    get_password_hash,
    is_api_path,
    json_error,
    password_source,
    read_json,
)
from app.web import idempotency
from app.web.idempotency import idempotent
from app.web.keys import BOT_INFO_KEY, BOT_KEY, CONFIG_KEY, DB_KEY, PREFIX_KEY, TOKEN_STATE_KEY
from app.web.logmask import mask_secrets
from app.web.urls import api_url, local_path, prefixed_router

log = logging.getLogger(__name__)

MAX_SOURCE_LENGTH = 65536  # raw markdown; the real limits are checked on the rendered text

# The bot's getMe for «Обзор», «Настройки» and /api/health (`cached_bot_info`): a request waits
# at most GETME_TIMEOUT s for Telegram; after a failure Telegram is asked again only after
# GETME_RETRY s (until then the panel shows the bot without its name at once).
GETME_TIMEOUT = 5.0
GETME_RETRY = 30.0
SETTINGS_GETME_TIMEOUT = GETME_TIMEOUT  # compatibility alias

SCHEDULE_MIN_LEAD = 10  # seconds
SCHEDULE_MAX_AHEAD = 5 * 366 * 86400

STATUS_FILTERS: dict[str, list[str] | None] = {
    "draft": [POST_DRAFT],
    "scheduled": [POST_SCHEDULED],
    "sent": [POST_SENT, POST_PARTIAL, POST_FAILED, POST_DELETED, POST_SENDING],
    "all": None,
}
# Statuses a post sent to at least one channel can have (scheduled: queued to other channels).
PUBLISHED_STATUSES = (POST_SENT, POST_PARTIAL, POST_SCHEDULED)
RECORD_DELETABLE_STATUSES = (POST_DRAFT, POST_SCHEDULED, POST_FAILED, POST_DELETED)
ACTIONS = ("draft", "publish", "schedule", "queue", "keep")
OPTION_KEYS = ("disable_preview", "silent", "protect", "caption_above")
CHANNEL_ORDER = {CH_ACTIVE: 0, CH_PENDING: 1}
INT64_MAX = 2**63 - 1
WEEK = range(7)

SENDING_ERROR = "Пост сейчас публикуется — подождите и обновите страницу"
DELETED_EDIT_ERROR = "Пост удалён из каналов — его можно только дублировать"
NO_CHANNELS_ERROR = "Выберите хотя бы один канал"
MEDIA_FILE_ID_ERROR = "Файл медиа не найден — загрузите его заново"
MEDIA_TYPE_LOCKED_ERROR = "Тип файла, полученного через Telegram, изменить нельзя — загрузите файл заново"
COPY_SOURCE_ERROR = "Исходный пост для копии не найден"
_MISSING_UPLOAD = {
    "photo": "Картинка не найдена — загрузите её заново",
    "video": "Видео не найдено — загрузите его заново",
    "animation": "GIF не найден — загрузите его заново",
}

_INT_RE = re.compile(r"-?[0-9]{1,19}")

_STATUS_MESSAGES = {
    400: "Некорректный запрос",
    401: "auth_required",
    403: "Доступ запрещён",
    404: "Не найдено",
    405: "Метод не поддерживается",
    413: "Запрос слишком большой",
    429: "Слишком много запросов",
}
_NOT_FOUND_MESSAGES = frozenset({
    plan_service.POST_NOT_FOUND, plan_service.CHANNEL_NOT_FOUND, NOT_FOUND_ERROR, CHANNEL_NOT_FOUND_ERROR,
})


# ------------------------------------------------------------------ middleware

def service_error(exc: Exception) -> JsonError:
    """PlanError / FolderError / MediaError / PostError → JSON error (404 for a missing object,
    409 while the post is being sent, else 400)."""
    message = str(exc) or "Ошибка запроса"
    if message in _NOT_FOUND_MESSAGES:
        return json_error(404, message)
    if isinstance(exc, PlanError) and message == plan_service.SENDING_ERROR:
        return json_error(409, message)
    return json_error(400, message)


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """For ``/api``: HTTP errors → JSON ``{"error"}``; service errors → 4xx; others → 500 JSON."""
    path = local_path(request)
    if path is None or not is_api_path(path):
        return await handler(request)
    try:
        return await handler(request)
    except JsonError:
        raise
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        headers = {k: v for k, v in exc.headers.items() if k in ("Allow", "Retry-After")}
        message = _STATUS_MESSAGES.get(exc.status) or exc.reason or "Ошибка запроса"
        return web.json_response({"error": message}, status=exc.status, headers=headers)
    except (PlanError, FolderError, MediaError, PostError) as exc:
        raise service_error(exc) from exc
    except Exception:
        log.exception("Unhandled error in %s %s", request.method, request.path)
        return web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)


# ------------------------------------------------------------------ small helpers

def _db(request: web.Request) -> Database:
    return request.app[DB_KEY]


def _config(request: web.Request) -> Config:
    return request.app[CONFIG_KEY]


def _bot(request: web.Request) -> Any:
    return request.app[BOT_KEY]


def _uploads_dir(request: web.Request) -> Path:
    return Path(_config(request).uploads_dir)


def path_id(request: web.Request, name: str = "id") -> int:
    value = as_int(request.match_info[name])
    if value is None:
        raise json_error(404, "Не найдено")
    return value


def as_int(value: Any) -> int | None:
    """int (not bool) or a decimal string within the int64 range, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not _INT_RE.fullmatch(value):
            return None
        value = int(value)
    if not isinstance(value, int) or abs(value) > INT64_MAX:
        return None
    return value


def _report(report: PublishReport | None) -> dict | None:
    return report.to_dict() if report is not None else None


def _has_sent(post: dict) -> bool:
    return any(t.get("status") == TARGET_SENT for t in post.get("targets") or [])


# ------------------------------------------------------------------ serialization

def media_version(item: dict) -> str:
    """Short hash of a media item's identity (type + file): changes when the file is replaced,
    stays when Telegram's file_id is learned for an uploaded file (cache busting)."""
    ident = json.dumps([item.get("type"), item.get("path") or item.get("file_id") or ""])
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:10]


def media_url(post_id: int, index: int, item: dict) -> str:
    """URL of a post's media file, with the panel prefix (see app.web.urls.api_url)."""
    return api_url(f"/api/posts/{post_id}/media/{index}?v={media_version(item)}")


def serialize_media(post_id: int, media: list[dict]) -> list[dict]:
    result = []
    for index, item in enumerate(media):
        data = {
            "index": index,
            "type": item.get("type"),
            "url": media_url(post_id, index, item),
            "path": item.get("path"),
            "file_id": item.get("file_id"),
            # Bot that owns file_id (None: unknown, stored before V2.2). With another bot running,
            # an item without `path` has to be uploaded again (SPEC_V22).
            "file_bot": item.get("file_bot"),
        }
        data.update({key: item[key] for key in EXTRA_KEYS if item.get(key) is not None})
        result.append(data)
    return result


def serialize_target(target: dict) -> dict:
    message_id = target.get("message_id")
    channel_known = target.get("channel_status") is not None
    link = None
    if message_id and channel_known and target.get("status") == TARGET_SENT:
        link = message_link(target, message_id)
    return {
        "channel_id": target["channel_id"],
        "title": target.get("channel_title"),
        "username": target.get("channel_username"),
        "folder_id": target.get("channel_folder_id"),
        "status": target.get("status"),
        "message_id": message_id,
        "message_ids": target.get("message_ids") or ([message_id] if message_id else []),
        "link": link,
        "error": target.get("error"),
        "sent_at": target.get("sent_at"),
        "scheduled_at": target.get("scheduled_at"),
        "queued": bool(target.get("queued")),
        "reactions_total": target.get("reactions_total") or 0,
    }


def stored_media(post: dict) -> list[dict]:
    """The post's media list as stored (legacy picture columns included); junk entries skipped."""
    return [item for item in post_media(post) if isinstance(item, dict)]


def serialize_post(post: dict) -> dict:
    media = stored_media(post)
    first = media[0] if media else None
    photo = first if first is not None and first.get("type") == "photo" else None
    text_md = post.get("text_md") or ""
    return {
        "id": post["id"],
        "status": post["status"],
        "text_md": text_md,
        "text_html": post.get("text_html") or "",
        "excerpt": excerpt(text_md) if text_md.strip() else "",
        "media": serialize_media(post["id"], media),
        "media_kind": media_kind(media),
        "media_label": describe_media(media),
        "has_media": bool(media),
        # V1 compatibility: the first item when it is a picture.
        "has_photo": photo is not None,
        "photo_url": media_url(post["id"], 0, photo) if photo is not None else None,
        "photo_path": photo.get("path") if photo is not None else None,
        "buttons": post.get("buttons") or [],
        "options": {key: bool(post.get(key)) for key in OPTION_KEYS},
        "scheduled_at": post.get("scheduled_at"),
        "sent_at": post.get("sent_at"),
        "created_at": post.get("created_at"),
        "updated_at": post.get("updated_at"),
        "error": post.get("error"),
        "source": post.get("source"),
        "published_kind": normalize_kind(post.get("published_kind")),
        "targets": [serialize_target(t) for t in post.get("targets") or []],
    }


def week_slots(slots: object) -> dict[str, list[str]]:
    """Slots as all seven weekdays with string keys: {"0": ["09:00"], …, "6": []}."""
    try:
        clean = plan_service.normalize_slots(slots)
    except PlanError:
        clean = {}
    return {str(day): list(clean.get(day, [])) for day in WEEK}


def serialize_channel(row: dict) -> dict:
    data = dict(row)
    for key in ("can_post", "can_edit", "can_delete"):
        data[key] = bool(data.get(key))
    username = data.get("username")
    data["link"] = f"https://t.me/{username}" if username else None
    raw_slots = data.pop("slots_json", None)
    try:
        slots = plan_service.normalize_slots(raw_slots)
    except PlanError:
        slots = {}
    data["folder_id"] = data.get("folder_id")
    data["slots"] = week_slots(slots)
    data["slots_text"] = plan_service.slots_to_text(slots)
    data["slots_summary"] = plan_service.describe_slots(slots)
    return data


async def get_post_or_404(db: Database, post_id: int) -> dict:
    post = await db.get_post(post_id)
    if post is None:
        raise json_error(404, "Пост не найден")
    return post


async def post_payload(db: Database, post_id: int, report: PublishReport | None = None,
                       schedule: dict[int, int] | None = None) -> dict:
    return {
        "post": serialize_post(await get_post_or_404(db, post_id)),
        "report": _report(report),
        "schedule": {str(cid): ts for cid, ts in schedule.items()} if schedule is not None else None,
    }


async def _error_with_post(db: Database, post_id: int, status: int, message: str) -> JsonError:
    post = await db.get_post(post_id)
    return json_error(status, message, post=serialize_post(post) if post else None)


# ------------------------------------------------------------------ input parsing

@dataclass
class _PostInput:
    text_md: str
    text_html: str
    media: list[dict]
    media_changed: bool
    buttons: list[list[dict]]
    options: dict[str, bool]

    def fields(self) -> dict[str, Any]:
        """Columns for db.create_post / db.update_post."""
        return {
            "text_md": self.text_md,
            "text_html": self.text_html,
            "media": self.media,
            "buttons": self.buttons,
            **self.options,
        }


def parse_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise json_error(400, "Текст поста должен быть строкой")
    if len(value) > MAX_SOURCE_LENGTH:
        raise json_error(400, "Текст поста слишком длинный")
    return value.replace("\r\n", "\n")


def _render(text_md: str, *, has_media: bool, strict: bool) -> str:
    """Telegram HTML of the text. Drafts (strict=False) may be empty / too long: '' is stored then."""
    try:
        return render_text(text_md, has_media=has_media)
    except PostError as exc:
        if strict:
            raise json_error(400, str(exc)) from exc
        return ""


def _parse_buttons(value: Any) -> list[list[dict]]:
    try:
        return validate_buttons(value)
    except ButtonsError as exc:
        raise json_error(400, str(exc)) from exc


def _parse_options(value: Any, defaults: dict | None = None) -> dict[str, bool]:
    defaults = defaults or {}
    if value is None:
        return {key: bool(defaults.get(key, False)) for key in OPTION_KEYS}
    if not isinstance(value, dict):
        raise json_error(400, "Опции поста должны быть объектом")
    return {key: bool(value.get(key, defaults.get(key, False))) for key in OPTION_KEYS}


def _legacy_media(body: dict, existing: dict | None) -> list[dict]:
    """V1 picture fields → media list: ``keep_photo: true`` keeps the stored media, otherwise
    ``photo_path`` (upload) or ``photo_file_id`` set one picture, their absence removes it."""
    if existing is not None and body.get("keep_photo") is True:
        return [dict(item) for item in post_media(existing)]
    path = body.get("photo_path")
    if path not in (None, ""):
        match = UPLOAD_NAME_RE.fullmatch(path) if isinstance(path, str) else None
        if match is None or match.group(1) not in TYPE_EXTENSIONS["photo"]:
            raise json_error(400, "Некорректное имя файла картинки")
        return [{"type": "photo", "path": path}]
    file_id = body.get("photo_file_id")
    if file_id not in (None, ""):
        if not isinstance(file_id, str) or not FILE_ID_RE.fullmatch(file_id):
            raise json_error(400, "Некорректный идентификатор картинки")
        return [{"type": "photo", "file_id": file_id}]
    return []


def _find_known(item: dict, known: list[dict]) -> dict | None:
    if item.get("file_id"):
        found = next((k for k in known if k.get("file_id") == item["file_id"]), None)
        if found is None or (item.get("path") and found.get("path") and found["path"] != item["path"]):
            raise json_error(400, MEDIA_FILE_ID_ERROR)
        return found
    if item.get("path"):
        return next((k for k in known if k.get("path") == item["path"]), None)
    return None


def _resolve_item(item: dict, known: list[dict], uploads_dir: Path) -> dict:
    """A validated request item → the item to store.

    Items that already belong to the post (or to the copy source) keep their stored file_id,
    its owner bot (`file_bot`) and path — so a Telegram file_id learned after publishing survives
    an echo without it. A type change of an uploaded file drops the old file_id (it is re-uploaded
    as the new type). `file_bot` never comes from the request.
    """
    match = _find_known(item, known)
    if match is None:
        result = dict(item)
        result.pop("file_bot", None)
    else:
        result = {key: match[key] for key in EXTRA_KEYS if match.get(key) is not None}
        result.update({key: item[key] for key in EXTRA_KEYS if item.get(key) is not None})
        result.update(type=item["type"], file_id=match.get("file_id"), path=match.get("path"))
        if match.get("file_id") and match.get("file_bot") is not None:
            result["file_bot"] = match["file_bot"]
        if item["type"] != match.get("type"):
            if not result["path"]:
                raise json_error(400, MEDIA_TYPE_LOCKED_ERROR)
            result["file_id"] = None
            result.pop("file_bot", None)
        try:
            result = normalize_media([result])[0]  # file extension vs. the (new) type
        except MediaError as exc:
            raise json_error(400, str(exc)) from exc
    if not result.get("file_id") and not (uploads_dir / result["path"]).is_file():
        raise json_error(400, _MISSING_UPLOAD.get(result["type"], MEDIA_FILE_ID_ERROR))
    return result


def _parse_media(body: dict, uploads_dir: Path, existing: dict | None, source: dict | None) -> list[dict]:
    raw = body["media"] if body.get("media") is not None else _legacy_media(body, existing)
    try:
        items = normalize_media(raw)
    except MediaError as exc:
        raise json_error(400, str(exc)) from exc
    known = [item for post in (existing, source) if post is not None for item in post_media(post)]
    return [_resolve_item(item, known, uploads_dir) for item in items]


def _parse_post_input(body: dict, uploads_dir: Path, *, existing: dict | None, source: dict | None,
                      strict: bool) -> _PostInput:
    media = _parse_media(body, uploads_dir, existing, source)
    buttons = _parse_buttons(body.get("buttons"))
    try:
        check_buttons(media, buttons)
    except MediaError as exc:
        raise json_error(400, str(exc)) from exc
    text_md = parse_text(body.get("text_md"))
    old_media = post_media(existing) if existing is not None else []
    return _PostInput(
        text_md=text_md,
        text_html=_render(text_md, has_media=bool(media), strict=strict),
        media=media,
        media_changed=media_signature(old_media) != media_signature(media),
        buttons=buttons,
        options=_parse_options(body.get("options"), existing),
    )


async def _copy_source(db: Database, value: Any) -> dict | None:
    if value in (None, ""):
        return None
    post_id = as_int(value)
    post = await db.get_post(post_id) if post_id is not None else None
    if post is None:
        raise json_error(400, COPY_SOURCE_ERROR)
    return post


async def parse_channel_ids(db: Database, value: Any, *, active_only: bool = True) -> list[int]:
    """Unique channel ids in the given order (by default only *active* channels are accepted)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        raise json_error(400, "Список каналов должен быть массивом")
    ids: list[int] = []
    for item in value:
        channel_id = as_int(item)
        if channel_id is None:
            raise json_error(400, "Некорректный ID канала")
        if channel_id not in ids:
            ids.append(channel_id)
    if ids and active_only:
        active = {row["id"] for row in await db.list_channels([CH_ACTIVE])}
        for channel_id in ids:
            if channel_id not in active:
                row = await db.get_channel(channel_id)
                name = f"«{row['title']}»" if row and row.get("title") else str(channel_id)
                raise json_error(400, f"Канал {name} не подключён — выберите другой канал")
    return ids


def _parse_action(value: Any) -> str | None:
    if value is None:
        return None
    if value not in ACTIONS:
        raise json_error(400, "Неизвестное действие: ожидается draft, publish, schedule или queue")
    return value


def parse_timestamp(value: Any, *, empty_message: str = "Укажите время публикации") -> int:
    if value is None or isinstance(value, bool):
        raise json_error(400, empty_message)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise json_error(400, "Некорректное время публикации")
        value = int(value)
    ts = as_int(value)
    if ts is None:
        raise json_error(400, "Некорректное время публикации")
    return ts


def _parse_scheduled_at(value: Any) -> int:
    ts = parse_timestamp(value)
    current = now()
    if ts <= current + SCHEDULE_MIN_LEAD:
        raise json_error(400, "Время публикации уже прошло")
    if ts > current + SCHEDULE_MAX_AHEAD:
        raise json_error(400, "Слишком далёкая дата публикации")
    return ts


def _names(channels: list[dict]) -> str:
    return ", ".join(f"«{ch.get('title') or ch['id']}»" for ch in channels)


async def _check_queue(db: Database, channel_ids: list[int], tz: Any, post_id: int | None = None) -> None:
    """Fail early (nothing saved yet) when a channel cannot take the post into its queue."""
    rows = [await db.get_channel(cid) or {"id": cid} for cid in channel_ids]
    no_slots = [row for row in rows if not await db.get_channel_slots(row["id"])]
    if no_slots:
        word = "канала" if len(no_slots) == 1 else "каналов"
        raise json_error(400, f"У {word} {_names(no_slots)} не настроено расписание публикаций")
    free = await plan_service.preview_queue(db, channel_ids, tz, post_id=post_id)
    full = [row for row in rows if free.get(row["id"]) is None]
    if full:
        word = "канала" if len(full) == 1 else "каналов"
        raise json_error(400, f"В расписании {word} {_names(full)} нет свободного времени на ближайший год")


async def set_unsent_targets(db: Database, post_id: int, channel_ids: list[int]) -> None:
    """Make the post's unsent (pending/failed) targets equal to `channel_ids` (an empty selection
    removes them all); sent/deleted targets are never touched. A post claimed for sending
    meanwhile keeps its targets (review2 #3) → 409 with the post's state."""
    if not await db.set_post_targets(post_id, channel_ids):
        raise await _error_with_post(db, post_id, 409, "Пост уже публикуется — обновите страницу")


async def _guarded_update(db: Database, post_id: int, *, statuses: tuple[str, ...], unsent_only: bool,
                          **fields: Any) -> bool:
    """Update post columns only while the post is in one of `statuses` (and, with `unsent_only`,
    not sent to any channel) — a single statement, so a post claimed for sending meanwhile is
    left alone. Returns False when nothing was updated."""
    _encode_aliases(fields)
    for key in OPTION_KEYS:
        if key in fields:
            fields[key] = int(bool(fields[key]))
    fields.setdefault("updated_at", now())
    cols = ", ".join(f"{key} = ?" for key in fields)
    sql = f"UPDATE posts SET {cols} WHERE id = ? AND status IN ({','.join('?' * len(statuses))})"
    if unsent_only:
        sql += " AND NOT EXISTS (SELECT 1 FROM post_targets t WHERE t.post_id = posts.id AND t.status = 'sent')"
    cur = await db.execute(sql, (*fields.values(), post_id, *statuses))
    return cur.rowcount == 1


# ------------------------------------------------------------------ posts: read

async def list_posts(request: web.Request) -> web.Response:
    db = _db(request)
    status = request.query.get("status") or "all"
    if status not in STATUS_FILTERS:
        raise json_error(400, "Неизвестный фильтр постов")
    statuses = STATUS_FILTERS[status]
    limit = min(max(as_int(request.query.get("limit", "20")) or 20, 1), 100)
    offset = max(as_int(request.query.get("offset", "0")) or 0, 0)
    posts = await db.list_posts(statuses, limit=limit, offset=offset)
    return web.json_response({
        "posts": [serialize_post(p) for p in posts],
        "total": await db.count_posts(statuses),
    })


async def get_post(request: web.Request) -> web.Response:
    post = await get_post_or_404(_db(request), path_id(request))
    return web.json_response({"post": serialize_post(post)})


# ------------------------------------------------------------------ posts: create / update

async def create_post(request: web.Request) -> web.Response:
    db = _db(request)
    body = await read_json(request)
    action = _parse_action(body.get("action")) or "draft"
    if action == "keep":
        action = "draft"
    source = await _copy_source(db, body.get("copy_from"))
    data = _parse_post_input(body, _uploads_dir(request), existing=None, source=source,
                             strict=action != "draft")
    channel_ids = await parse_channel_ids(db, body.get("channel_ids"))
    scheduled_at = _parse_scheduled_at(body.get("scheduled_at")) if action == "schedule" else None
    if action != "draft" and not channel_ids:
        raise json_error(400, NO_CHANNELS_ERROR)
    if action == "queue":
        await _check_queue(db, channel_ids, _config(request).tz)

    post_id = await db.create_post(**data.fields(), status=POST_DRAFT, source="web")
    idempotency.created(request, post_id)  # a retry with the same key gets this post
    await set_unsent_targets(db, post_id, channel_ids)
    try:
        report, schedule = await _run_action(request, post_id, action, channel_ids, scheduled_at)
    except PlanError as exc:
        # Nothing was scheduled: do not leave a half-made draft behind (the client retries).
        cur = await db.execute("DELETE FROM posts WHERE id = ? AND status = ?", (post_id, POST_DRAFT))
        if cur.rowcount:
            await db.execute("DELETE FROM post_targets WHERE post_id = ?", (post_id,))
        raise service_error(exc) from exc
    except PostError as exc:  # publish refused before sending: the draft stays, the client edits it
        raise await _error_with_post(db, post_id, 400, str(exc)) from exc
    log.info("Web: post %s created (%s)", post_id, action)
    return web.json_response(await post_payload(db, post_id, report, schedule))


async def _run_action(request: web.Request, post_id: int, action: str, channel_ids: list[int],
                      scheduled_at: int | None) -> tuple[PublishReport | None, dict[int, int] | None]:
    """Final step of a save (content and targets are stored): publish / schedule / queue.

    Raises PostError / PlanError (the caller decides what to keep).
    """
    db = _db(request)
    if action == "publish":
        return await publish_post(_bot(request), db, _config(request), post_id), None
    if action == "schedule":
        assert scheduled_at is not None
        await plan_service.schedule_post_at(db, post_id, channel_ids, scheduled_at)
        return None, {cid: scheduled_at for cid in channel_ids}
    if action == "queue":
        return None, await plan_service.queue_post(db, post_id, channel_ids, _config(request).tz)
    return None, None


async def _publish(request: web.Request, post_id: int, channel_ids: list[int] | None = None) -> PublishReport:
    db = _db(request)
    try:
        return await publish_post(_bot(request), db, _config(request), post_id, channel_ids=channel_ids)
    except PostError as exc:
        raise await _error_with_post(db, post_id, 400, str(exc)) from exc


async def update_post(request: web.Request) -> web.Response:
    db = _db(request)
    post = await get_post_or_404(db, path_id(request))
    body = await read_json(request)
    if post["status"] == POST_SENDING:
        raise json_error(409, SENDING_ERROR, post=serialize_post(post))
    if post["status"] == POST_DELETED:
        raise json_error(400, DELETED_EDIT_ERROR)
    if _has_sent(post):
        return await _edit_published(request, post, body)
    return await _edit_unpublished(request, post, body)


async def _edit_unpublished(request: web.Request, post: dict, body: dict) -> web.Response:
    """Update a draft / scheduled / failed post (not sent anywhere); the action decides what
    happens next. Without an action a scheduled post keeps its schedule."""
    db = _db(request)
    post_id = post["id"]
    action = _parse_action(body.get("action"))
    if action is None:
        action = "keep" if post["status"] == POST_SCHEDULED else "draft"
    if action == "keep" and post["status"] != POST_SCHEDULED:
        action = "draft"
    source = await _copy_source(db, body.get("copy_from"))
    data = _parse_post_input(body, _uploads_dir(request), existing=post, source=source,
                             strict=action != "draft")
    channel_ids = await parse_channel_ids(db, body.get("channel_ids"))
    scheduled_at = None
    if action == "schedule":
        raw = body.get("scheduled_at")
        if raw is None and post["status"] == POST_SCHEDULED:
            raw = post["scheduled_at"]
        scheduled_at = _parse_scheduled_at(raw)
    if action != "draft" and not channel_ids:
        raise json_error(400, NO_CHANNELS_ERROR)
    if action == "queue":
        await _check_queue(db, channel_ids, _config(request).tz, post_id)

    # One statement saves the content and takes the post out of the scheduler's reach (a draft
    # is never claimed); a post claimed or sent meanwhile is refused instead.
    if not await _guarded_update(db, post_id, statuses=(post["status"],), unsent_only=True,
                                 **data.fields(), status=POST_DRAFT, error=None):
        raise await _error_with_post(db, post_id, 409, "Пост уже публикуется — обновите страницу")
    report = schedule = None
    failure: PlanError | PostError | None = None
    try:
        await set_unsent_targets(db, post_id, channel_ids)
        if action == "draft":
            await plan_service.unschedule_post(db, post_id)
        elif action == "keep":
            schedule = await _keep_schedule(db, post, channel_ids)
        else:
            report, schedule = await _run_action(request, post_id, action, channel_ids, scheduled_at)
    except (PlanError, PostError) as exc:
        failure = exc  # the content is saved; the post keeps the schedule it still has
    finally:
        await db.refresh_post_schedule(post_id)  # also restores 'scheduled' for kept times
    if failure is not None:
        raise await _error_with_post(db, post_id, service_error(failure).status, str(failure)) from failure
    return web.json_response(await post_payload(db, post_id, report, schedule))


async def _keep_schedule(db: Database, post: dict, channel_ids: list[int]) -> dict[int, int]:
    """Kept channels keep their times; newly selected ones get the post's earliest time (or the
    earliest usable moment when that time is already due)."""
    old = {t["channel_id"]: t for t in post["targets"]}
    added = [cid for cid in channel_ids if not (old.get(cid) or {}).get("scheduled_at")]
    if added:
        when = post.get("scheduled_at")
        if not when:
            raise PlanError("У поста нет времени публикации — выберите режим публикации")
        when = max(int(when), now() + plan_service.MIN_LEAD)
        await plan_service.schedule_post_at(db, post["id"], added, when)
    targets = await db.get_targets(post["id"])
    return {t["channel_id"]: t["scheduled_at"] for t in targets
            if t["status"] == TARGET_PENDING and t.get("scheduled_at")}


def _class_error(published: str, current: str) -> str:
    if published.startswith("album:") and current.startswith("album:"):
        count = published.split(":", 1)[1]
        return (f"В опубликованном альбоме должно остаться {count} файлов — Telegram не позволяет "
                "добавлять или убирать файлы альбома. Удалите пост и опубликуйте заново")
    return KIND_MISMATCH_ERROR


async def _edit_published(request: web.Request, post: dict, body: dict) -> web.Response:
    """Apply new text / media / buttons / options to the messages already in the channels.

    Channels and the action are ignored here: remaining targets are published or planned with
    ``POST /api/posts/{id}/publish`` and the plan endpoints. When no channel accepts the edit
    (report with ``ok_count == 0``), the previous content is restored so the stored post stays what
    the channels show; the per-channel errors are kept.
    """
    db = _db(request)
    post_id = post["id"]
    if body.get("apply") is not True:
        # Also the answer to a repeated «Опубликовать» of a post that went out meanwhile.
        raise json_error(400, "Пост уже опубликован: подтвердите изменение сообщений в каналах (apply)",
                         post=serialize_post(post))
    data = _parse_post_input(body, _uploads_dir(request), existing=post, source=None, strict=True)
    published = normalize_kind(post.get("published_kind")) or edit_class(post_media(post))
    current = edit_class(data.media)
    if current != published:
        raise json_error(400, _class_error(published, current))

    previous = {
        "text_md": post.get("text_md") or "",
        "text_html": post.get("text_html") or "",
        "media": post_media(post),
        "buttons": post.get("buttons") or [],
        **{key: bool(post.get(key)) for key in OPTION_KEYS},
    }
    if not await _guarded_update(db, post_id, statuses=PUBLISHED_STATUSES, unsent_only=False,
                                 **data.fields()):
        raise json_error(409, SENDING_ERROR)
    try:
        report = await edit_published_post(
            _bot(request), db, _config(request), post_id, media_changed=data.media_changed
        )
    except PostError as exc:
        await db.update_post(post_id, **previous)  # nothing was changed in Telegram
        raise json_error(400, str(exc)) from exc
    except Exception:
        await db.update_post(post_id, **previous)
        raise
    if report.results and not report.ok_count:
        # Every channel refused the edit: keep the DB equal to what the channels show (the
        # per-channel errors stay recorded). The panel keeps the unsaved edits for a retry.
        await db.update_post(post_id, **previous)
    log.info("Web: published post %s edited (%d ok, %d failed)", post_id, report.ok_count, report.fail_count)
    return web.json_response(await post_payload(db, post_id, report))


# ------------------------------------------------------------------ posts: actions

async def publish_now(request: web.Request) -> web.Response:
    """Publish now: every pending/failed channel, or only ``channel_ids`` from the body.

    A post being sent → 409, a post sent everywhere → 400 (`publish_post` refuses it); both
    answers carry the post's current state (``post``) — a repeated click never sends twice."""
    db = _db(request)
    post = await get_post_or_404(db, path_id(request))
    body = await read_json(request)
    if post["status"] == POST_SENDING:
        raise json_error(409, SENDING_ERROR, post=serialize_post(post))
    channel_ids = None
    if body.get("channel_ids") is not None:
        channel_ids = await parse_channel_ids(db, body.get("channel_ids"), active_only=False)
        if not channel_ids:
            raise json_error(400, NO_CHANNELS_ERROR)
    report = await _publish(request, post["id"], channel_ids)
    return web.json_response(await post_payload(db, post["id"], report))


async def preview(request: web.Request) -> web.Response:
    """Send the saved post to every admin's DM."""
    db = _db(request)
    post = await get_post_or_404(db, path_id(request))
    try:
        media = normalize_media(post_media(post))
        check_buttons(media, post.get("buttons"))
    except MediaError as exc:
        raise json_error(400, str(exc)) from exc
    _render(post.get("text_md") or "", has_media=bool(media), strict=True)
    sent = 0
    errors: list[str] = []
    for admin_id in sorted(await db.get_admin_ids()):
        try:
            await send_preview(_bot(request), db, _config(request), post["id"], admin_id)
            sent += 1
        except PostError as exc:
            log.info("Preview of post %s to %s failed: %s", post["id"], admin_id, exc)
            errors.append(str(exc))
    if not sent and errors:
        raise json_error(400, errors[0])
    return web.json_response({"sent": sent, "errors": errors})


async def unschedule(request: web.Request) -> web.Response:
    """Take the post out of the plan in every channel (a post not sent anywhere becomes a draft)."""
    db = _db(request)
    post = await get_post_or_404(db, path_id(request))
    if post["status"] == POST_SENDING:
        raise json_error(409, SENDING_ERROR)
    if post["status"] != POST_SCHEDULED:
        raise json_error(400, "Пост не запланирован")
    try:
        await plan_service.unschedule_post(db, post["id"])
    except PlanError as exc:
        raise service_error(exc) from exc
    return web.json_response(await post_payload(db, post["id"]))


async def delete_post(request: web.Request) -> web.Response:
    db = _db(request)
    post = await get_post_or_404(db, path_id(request))
    post_id = post["id"]
    if post["status"] == POST_SENDING:
        raise json_error(409, SENDING_ERROR)
    if request.query.get("from_channels") in ("1", "true", "yes"):
        try:
            report = await delete_published_post(_bot(request), db, post_id)
        except PostError as exc:
            if str(exc) == DELETE_BUSY_ERROR:  # claimed by a publish meanwhile
                raise await _error_with_post(db, post_id, 409, str(exc)) from exc
            raise json_error(400, str(exc)) from exc
        return web.json_response(await post_payload(db, post_id, report))

    marks = ",".join("?" * len(RECORD_DELETABLE_STATUSES))
    # Conditional delete: a post claimed by the scheduler meanwhile, or already sent to some
    # channel (a queued post may be scheduled and partly sent), is not removed.
    cur = await db.execute(
        f"DELETE FROM posts WHERE id = ? AND status IN ({marks}) AND NOT EXISTS "
        "(SELECT 1 FROM post_targets t WHERE t.post_id = posts.id AND t.status = 'sent')",
        (post_id, *RECORD_DELETABLE_STATUSES),
    )
    if cur.rowcount != 1:
        current = await db.get_post(post_id)
        if current is not None and current["status"] == POST_SENDING:
            raise json_error(409, SENDING_ERROR)
        raise json_error(400, "Опубликованный пост нельзя удалить из панели — сначала удалите его из каналов")
    await db.execute("DELETE FROM post_targets WHERE post_id = ?", (post_id,))
    log.info("Web: post %s record deleted", post_id)
    return web.json_response({"ok": True})


# ------------------------------------------------------------------ channels

async def list_channels(request: web.Request) -> web.Response:
    rows = await _db(request).list_channels()
    rows.sort(key=lambda row: CHANNEL_ORDER.get(row["status"], 2))  # stable: keeps title order
    return web.json_response({"channels": [serialize_channel(r) for r in rows]})


async def get_channel_or_404(db: Database, channel_id: int) -> dict:
    row = await db.get_channel(channel_id)
    if row is None:
        raise json_error(404, "Канал не найден")
    return row


async def add_channel(request: web.Request) -> web.Response:
    body = await read_json(request)
    ref = body.get("ref")
    if isinstance(ref, bool) or not isinstance(ref, (str, int)) or (isinstance(ref, str) and not ref.strip()):
        raise json_error(400, "Укажите @username, ссылку на канал или его ID")
    return await _add_channel(request, ref)


async def _add_channel(request: web.Request, ref: str | int) -> web.Response:
    try:
        row = await channels_service.add_channel(_bot(request), _db(request), ref, added_by=None)
    except ChannelError as exc:
        raise json_error(400, str(exc)) from exc
    return web.json_response({"channel": serialize_channel(row)})


async def approve_channel(request: web.Request) -> web.Response:
    channel = await get_channel_or_404(_db(request), path_id(request))
    return await _add_channel(request, channel["id"])


async def ignore_channel(request: web.Request) -> web.Response:
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    await db.set_channel_status(channel["id"], CH_IGNORED)
    return web.json_response({"channel": serialize_channel(await get_channel_or_404(db, channel["id"]))})


async def refresh_channel(request: web.Request) -> web.Response:
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    try:
        row = await channels_service.refresh_channel(_bot(request), db, channel["id"], _config(request).uploads_dir)
    except ChannelError as exc:
        raise json_error(400, str(exc)) from exc
    return web.json_response({"channel": serialize_channel(row)})


async def delete_channel(request: web.Request) -> web.Response:
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    await db.delete_channel(channel["id"])
    log.info("Web: channel %s removed from the panel", channel["id"])
    return web.json_response({"ok": True})


async def channel_stats(request: web.Request) -> web.Response:
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    days = as_int(request.query.get("days", "30"))
    days = min(max(days if days is not None else 30, 1), 365)
    try:
        stats = await stats_service.channel_stats(db, channel["id"], days, _config(request).tz)
    except ChannelError as exc:  # deleted concurrently
        raise json_error(404, str(exc)) from exc
    stats["channel"] = serialize_channel(stats["channel"])
    return web.json_response(stats)


async def combined_stats(request: web.Request) -> web.Response:
    """Summed statistics: `?channels=id,id` or, without it, every channel with statistics."""
    db = _db(request)
    days = as_int(request.query.get("days", "30"))
    days = min(max(days if days is not None else 30, 1), 365)
    available = [ch["id"] for ch in await db.list_channels([CH_ACTIVE, CH_LEFT])]
    raw = request.query.get("channels", "").strip()
    if raw:
        wanted = {as_int(part) for part in raw.split(",")}
        chat_ids = [cid for cid in available if cid in wanted]
        if not chat_ids:
            raise json_error(404, "Каналы не найдены")
    else:
        chat_ids = available
    stats = await stats_service.combined_stats(db, chat_ids, days, _config(request).tz)
    for item in stats["channels"]:
        item["channel"] = serialize_channel(item["channel"])
    return web.json_response(stats)


# ------------------------------------------------------------------ overview, admins, settings

async def fetch_bot_info(app: web.Application, *, timeout: float | None = None,
                         quiet: bool = False) -> dict | None:
    """bot.get_me() cached in the app after the first success; None when it failed (this function
    does not remember failures — see `cached_bot_info`). `timeout` bounds the request, `quiet`
    logs a failure at debug level only."""
    cache = app[BOT_INFO_KEY]
    if "info" in cache:
        return cache["info"]
    try:
        if timeout is None:
            me = await app[BOT_KEY].get_me()
        else:
            me = await asyncio.wait_for(app[BOT_KEY].get_me(), timeout)
    except Exception as exc:  # network down, Telegram error, timeout
        log.log(logging.DEBUG if quiet else logging.WARNING, "get_me failed: %s: %s",
                type(exc).__name__, mask_secrets(str(exc)))
        return None
    cache["info"] = {"id": me.id, "username": me.username, "name": me.full_name}
    return cache["info"]


_GETME_FAILED_AT = "failed_at"  # BOT_INFO_KEY entry: monotonic time of the last failed getMe
_GETME_LOCK = "lock"  # BOT_INFO_KEY entry: one getMe at a time


async def cached_bot_info(app: web.Application, *, timeout: float | None = None,
                          retry: float | None = None) -> dict | None:
    """The bot's getMe for the panel pages and /api/health, never slower than `timeout`
    (GETME_TIMEOUT) s: cached after the first success; after a failure None is returned at once
    for `retry` (GETME_RETRY) s before Telegram is asked again. Parallel callers share one getMe."""
    cache = app[BOT_INFO_KEY]
    if "info" in cache:
        return cache["info"]
    timeout = GETME_TIMEOUT if timeout is None else timeout
    retry = GETME_RETRY if retry is None else retry
    lock = cache.setdefault(_GETME_LOCK, asyncio.Lock())
    async with lock:
        if "info" in cache:
            return cache["info"]
        failed_at = cache.get(_GETME_FAILED_AT)
        if failed_at is not None and time.monotonic() - failed_at < retry:
            return None
        info = await fetch_bot_info(app, timeout=timeout, quiet=True)
        if info is None:
            if failed_at is None:
                log.warning("Telegram не ответил на getMe — панель покажет бота без имени, "
                            "следующая попытка через %d с", int(retry))
            cache[_GETME_FAILED_AT] = time.monotonic()
        else:
            cache.pop(_GETME_FAILED_AT, None)
        return info


async def _bot_info(request: web.Request) -> dict:
    info = await cached_bot_info(request.app)
    if info is None:  # Telegram unreachable: the id is part of the token, the rest is unknown
        return {"id": getattr(_bot(request), "id", None), "username": None, "name": None}
    return info


async def get_overview(request: web.Request) -> web.Response:
    config = _config(request)
    return web.json_response({
        "bot": await _bot_info(request),
        "stats": await stats_service.overview(_db(request)),
        "web_url": config.web_url,
        "timezone": str(config.tz),
    })


async def list_admins(request: web.Request) -> web.Response:
    return web.json_response({"admins": await _db(request).list_admins()})


async def add_admin(request: web.Request) -> web.Response:
    """Add an admin. While the bot has no owner (ADMIN_IDS empty and nobody sent /start yet) the
    added user becomes the owner — otherwise nobody could ever become one."""
    db = _db(request)
    body = await read_json(request)
    user_id = as_int(body.get("user_id"))
    if user_id is None or user_id <= 0:
        raise json_error(400, "Укажите числовой Telegram ID пользователя")
    has_owner = await db.fetchone("SELECT 1 FROM admins WHERE is_owner = 1") is not None
    await db.add_admin(user_id, is_owner=not has_owner)
    log.info("Web: admin %s added%s", user_id, "" if has_owner else " as the owner")
    return web.json_response({"admins": await db.list_admins()})


async def remove_admin(request: web.Request) -> web.Response:
    db = _db(request)
    user_id = path_id(request, "user_id")
    if not await db.is_admin(user_id):
        raise json_error(404, "Администратор не найден")
    if not await db.remove_admin(user_id):
        raise json_error(400, "Нельзя удалить владельца")
    log.info("Web: admin %s removed", user_id)
    return web.json_response({"admins": await db.list_admins()})


async def bot_settings(app: web.Application) -> dict:
    """The publishing bot for «Настройки» (SPEC_V22): who it is and where its token comes from —
    never the token itself (only `…` + its last 4 characters). Changing it: app.web.api_bot."""
    info = await cached_bot_info(app, timeout=SETTINGS_GETME_TIMEOUT)
    if info is None:  # Telegram unreachable: the id is part of the token, the rest is unknown
        info = {"id": getattr(app[BOT_KEY], "id", None), "username": None, "name": None}
    state = app[TOKEN_STATE_KEY]
    return {
        "bot": info,
        "bot_token_source": state.source,
        "bot_token_masked": state.masked,
        "panel_token_rejected": state.panel_token_rejected,
        "panel_token_set": await app[DB_KEY].get_setting(BOT_TOKEN_SETTING) is not None,
    }


async def get_settings(request: web.Request) -> web.Response:
    config = _config(request)
    return web.json_response({
        **await bot_settings(request.app),
        # SPEC_V23: the bot has a Fragment username → custom emoji are published as they are.
        "premium_emoji": await _db(request).get_premium_emoji(),
        "helper_bots": await helper_bots.get_helpers(_db(request)),
        "password_set": await get_password_hash(_db(request), config) is not None,
        "password_fixed": fixed_password(config) is not None,
        "password_source": await password_source(_db(request), config),
        "timezone": str(config.tz),
        **timezone_info(config),
        "web_url": config.web_url,
        "web_prefix": request.app[PREFIX_KEY],
        "hub_url": config.hub_url,
        "version": config.version,
        "slots_help": plan_service.SLOTS_HELP,
    })


def timezone_info(config: Config) -> dict:
    return {
        "timezone_label": timezone_service.describe(config.tz),
        "timezone_env": str(config.tz_env),
        "timezone_env_label": timezone_service.describe(config.tz_env),
        "timezone_source": timezone_service.source(config),
        "timezones": timezone_service.common_choices(),
    }


async def set_timezone(request: web.Request) -> web.Response:
    """``PUT /api/settings/timezone {timezone: "Europe/Moscow" | "UTC+3" | null}`` — null returns
    to TIMEZONE from .env. → ``{timezone, timezone_label, timezone_env, timezone_source, …}``."""
    body = await read_json(request)
    raw = body.get("timezone")
    if raw is not None and not isinstance(raw, str):
        raise json_error(400, "Ожидается timezone: строка или null")
    config = _config(request)
    try:
        await timezone_service.set_timezone(_db(request), config, raw)
    except ValueError as exc:
        raise json_error(400, str(exc)) from exc
    return web.json_response({"timezone": str(config.tz), **timezone_info(config)})


PREMIUM_EMOJI_VALUE_ERROR = "Ожидается enabled: true или false"


async def set_premium_emoji(request: web.Request) -> web.Response:
    """``PUT /api/settings/premium-emoji {enabled: bool}`` → ``{premium_emoji}`` (SPEC_V23)."""
    body = await read_json(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise json_error(400, PREMIUM_EMOJI_VALUE_ERROR)
    db = _db(request)
    await db.set_premium_emoji(enabled)
    log.info("Web: premium emoji %s", "enabled (the bot has a Fragment username)" if enabled else "disabled")
    return web.json_response({"premium_emoji": await db.get_premium_emoji()})


HELPER_BOTS_VALUE_ERROR = "Ожидается bots: список юзернеймов ботов или строка"


async def set_helper_bots(request: web.Request) -> web.Response:
    """``PUT /api/settings/helper-bots {bots: [..] | "@a_bot, @b_bot"}`` → ``{helper_bots}``."""
    body = await read_json(request)
    raw = body.get("bots")
    if not isinstance(raw, (str, list)):
        raise json_error(400, HELPER_BOTS_VALUE_ERROR)
    info = await cached_bot_info(request.app, timeout=SETTINGS_GETME_TIMEOUT)
    try:
        names = helper_bots.parse_list(raw, (info or {}).get("username"))
    except ValueError as exc:
        raise json_error(400, str(exc)) from exc
    await helper_bots.set_helpers(_db(request), names)
    log.info("Web: helper bots set to %s", names)
    return web.json_response({"helper_bots": names})


async def channel_helpers(request: web.Request) -> web.Response:
    """``GET /api/channels/{id}/helpers`` → ``{helpers: [{username, is_admin, url}]}``; is_admin is
    null when Telegram did not tell."""
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    return web.json_response({"helpers": await helper_bots.helpers_status(_bot(request), db, channel["id"])})


# ------------------------------------------------------------------ routes

POST_ID = r"{id:[0-9]{1,18}}"
CHANNEL_ID = r"{id:-?[0-9]{1,18}}"


async def _current_post(request: web.Request, post_id: int) -> dict | None:
    """The post as API answers show it (for replayed idempotent answers); None when deleted."""
    post = await _db(request).get_post(post_id)
    return serialize_post(post) if post is not None else None


def setup_routes(app: web.Application) -> None:
    r = prefixed_router(app)
    r.add_get("/api/overview", get_overview)
    r.add_get("/api/settings", get_settings)
    r.add_put("/api/settings/premium-emoji", set_premium_emoji)
    r.add_put("/api/settings/timezone", set_timezone)
    r.add_put("/api/settings/helper-bots", set_helper_bots)

    # The requests that may publish accept X-Idempotency-Key (app.web.idempotency).
    r.add_get("/api/posts", list_posts)
    r.add_post("/api/posts", idempotent(create_post, _current_post))
    r.add_get(f"/api/posts/{POST_ID}", get_post)
    r.add_put(f"/api/posts/{POST_ID}", idempotent(update_post, _current_post))
    r.add_delete(f"/api/posts/{POST_ID}", delete_post)
    r.add_post(f"/api/posts/{POST_ID}/publish", idempotent(publish_now, _current_post))
    r.add_post(f"/api/posts/{POST_ID}/preview", preview)
    r.add_post(f"/api/posts/{POST_ID}/unschedule", unschedule)

    r.add_get("/api/channels", list_channels)
    r.add_post("/api/channels", add_channel)
    r.add_post(f"/api/channels/{CHANNEL_ID}/approve", approve_channel)
    r.add_post(f"/api/channels/{CHANNEL_ID}/ignore", ignore_channel)
    r.add_post(f"/api/channels/{CHANNEL_ID}/refresh", refresh_channel)
    r.add_delete(f"/api/channels/{CHANNEL_ID}", delete_channel)
    r.add_get(f"/api/channels/{CHANNEL_ID}/stats", channel_stats)
    r.add_get(f"/api/channels/{CHANNEL_ID}/helpers", channel_helpers)
    r.add_get("/api/stats", combined_stats)

    r.add_get("/api/admins", list_admins)
    r.add_post("/api/admins", add_admin)
    r.add_delete("/api/admins/{user_id:[0-9]{1,19}}", remove_admin)

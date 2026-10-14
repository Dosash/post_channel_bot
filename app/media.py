"""Media attachments of a post: photos, videos, GIFs (animations) and albums.

Pure module (no aiogram). A post's media is a list of items::

    {"type": "photo" | "video" | "animation", "file_id": str | None, "path": str | None, ...}

`file_id` is a reusable Telegram file id, `path` a bare file name inside `config.uploads_dir`
(web uploads). At least one of them is set. A file_id only works for the bot that received it:
`file_bot` is the id of that bot (SPEC_V22; absent on items stored before V2.2 — "legacy").
Extra descriptive keys (`mime`, `name`, `size`, `width`, `height`, `duration`) are kept. One item
is a single media post; 2..10 photos/videos form an album (Telegram does not allow GIFs in albums,
nor buttons under an album).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MEDIA_TYPES = ("photo", "video", "animation")
ALBUM_TYPES = ("photo", "video")  # Telegram albums: photos and videos (GIF/animation must be alone)
MAX_ALBUM = 10
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # web upload limits (Bot API multipart upload limit is 50 MB)
VIDEO_MAX_BYTES = 50 * 1024 * 1024

EXTRA_KEYS = ("mime", "name", "size", "width", "height", "duration")

UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp|mp4|mov|gif)$")
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")

# File extensions an item of each type may be uploaded as (a short video may be sent "as GIF").
TYPE_EXTENSIONS = {
    "photo": ("jpg", "png", "webp"),
    "video": ("mp4", "mov"),
    "animation": ("gif", "mp4", "mov"),
}
CONTENT_TYPES = {
    "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "mp4": "video/mp4", "mov": "video/quicktime", "gif": "image/gif",
}
TYPE_LABELS = {"photo": "фото", "video": "видео", "animation": "GIF"}
MEDIA_ICONS = {"text": "📝", "photo": "🖼", "video": "🎬", "animation": "🎞", "album": "🗂"}

_TYPE_ALIASES = {"gif": "animation"}

ALBUM_GIF_ERROR = "GIF нельзя добавить в альбом — отправьте его отдельным постом"
ALBUM_BUTTONS_ERROR = (
    "Telegram не поддерживает кнопки у альбомов. Оставьте одно фото/видео или уберите кнопки."
)


class MediaError(ValueError):
    """Invalid media list. The message is Russian and user-presentable."""


def normalize_media(items: object) -> list[dict]:
    """Validate a media list (from the DB, the bot or a web request) and return a clean copy.

    Raises MediaError for a malformed item, an unknown type, a bad file name / file id, more than
    MAX_ALBUM items or a GIF inside an album.
    """
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise MediaError("Некорректный список медиа")
    if len(items) > MAX_ALBUM:
        raise MediaError(f"В посте может быть не больше {MAX_ALBUM} фото и видео")
    result = [_normalize_item(item) for item in items]
    if len(result) > 1 and any(item["type"] not in ALBUM_TYPES for item in result):
        raise MediaError(ALBUM_GIF_ERROR)
    return result


def _normalize_item(item: object) -> dict:
    if not isinstance(item, dict):
        raise MediaError("Некорректный элемент медиа")
    raw_type = item.get("type")
    kind = _TYPE_ALIASES.get(raw_type, raw_type) if isinstance(raw_type, str) else raw_type
    if kind not in MEDIA_TYPES:
        raise MediaError(f"Неизвестный тип медиа: {raw_type}")
    file_id = item.get("file_id") or None
    if file_id is not None and (not isinstance(file_id, str) or not FILE_ID_RE.fullmatch(file_id)):
        raise MediaError("Некорректный идентификатор файла")
    path = item.get("path") or None
    if path is not None:
        match = UPLOAD_NAME_RE.fullmatch(path) if isinstance(path, str) else None
        if match is None:
            raise MediaError("Некорректное имя файла")
        if match.group(1) not in TYPE_EXTENSIONS[kind]:
            raise MediaError(f"Файл {path} нельзя отправить как {TYPE_LABELS[kind]}")
    if file_id is None and path is None:
        raise MediaError("У медиа нет файла — загрузите его заново")
    clean: dict[str, Any] = {"type": kind, "file_id": file_id, "path": path}
    file_bot = item.get("file_bot")
    if file_id is not None and isinstance(file_bot, int) and not isinstance(file_bot, bool) and file_bot > 0:
        clean["file_bot"] = file_bot  # owner of the file_id (meaningless without one)
    for key in EXTRA_KEYS:
        value = item.get(key)
        if value is not None and isinstance(value, (str, int, float, bool)):
            clean[key] = value
    return clean


def post_media(post: dict) -> list[dict]:
    """Media list of a post dict: `media` when present, else the legacy single picture columns."""
    media = post.get("media")
    if isinstance(media, list):
        return media
    if post.get("photo_file_id") or post.get("photo_path"):
        return [{"type": "photo", "file_id": post.get("photo_file_id"), "path": post.get("photo_path")}]
    return []


def media_kind(items: list[dict] | None) -> str:
    """'text' (no media) | 'photo' | 'video' | 'animation' | 'album'."""
    items = items or []
    if not items:
        return "text"
    if len(items) > 1:
        return "album"
    return items[0].get("type") or "photo"


def edit_class(items: list[dict] | None) -> str:
    """'text' | 'media' | 'album:<n>' — a published post can only be edited within its class
    (a single media message may switch between photo / video / GIF)."""
    items = items or []
    if not items:
        return "text"
    if len(items) == 1:
        return "media"
    return f"album:{len(items)}"


def check_buttons(items: list[dict] | None, buttons: list | None) -> None:
    """Albums cannot carry inline buttons (Telegram limitation) → MediaError."""
    if len(items or []) > 1 and any(row for row in (buttons or [])):
        raise MediaError(ALBUM_BUTTONS_ERROR)


def describe_media(items: list[dict] | None) -> str:
    """Short Russian description: '' | '🖼 Фото' | '🎬 Видео' | '🎞 GIF' | '🗂 Альбом: 3 фото, 1 видео'."""
    items = items or []
    if not items:
        return ""
    if len(items) == 1:
        kind = items[0].get("type")
        return {"photo": "🖼 Фото", "video": "🎬 Видео", "animation": "🎞 GIF"}.get(kind, "🖼 Фото")
    counts: dict[str, int] = {}
    for item in items:
        kind = item.get("type") or "photo"
        counts[kind] = counts.get(kind, 0) + 1
    parts = [f"{counts[kind]} {TYPE_LABELS[kind]}" for kind in MEDIA_TYPES if counts.get(kind)]
    return "🗂 Альбом: " + ", ".join(parts)


def media_icon(items: list[dict] | None) -> str:
    """Emoji of the media kind ('📝' for text posts)."""
    return MEDIA_ICONS.get(media_kind(items), MEDIA_ICONS["photo"])


def media_signature(items: list[dict] | None) -> str:
    """Identity of the media as shown in a channel message (sha1 hex).

    Built from (type, path or file_id) per item, so it does not change when the Telegram file_id
    of an uploaded file is written back after the first send.
    """
    key = [[item.get("type"), item.get("path") or item.get("file_id")] for item in (items or [])]
    return hashlib.sha1(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()


def content_type(path: str) -> str:
    """MIME type of an uploaded media file by its extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return CONTENT_TYPES.get(ext, "application/octet-stream")

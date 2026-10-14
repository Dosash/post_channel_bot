"""Web API — media: markdown rendering, uploads and the files of a post's media.

    POST /api/render                       {text_md, has_media | has_photo} → {html, length, limit, error,
                                           has_custom_emoji, premium_emoji}
    POST /api/upload                       multipart field "file" → {path, type, url, size, mime}
    GET  /api/uploads/{name}               an uploaded file
    GET  /api/posts/{id}/media/{index}     a media item of a post (local upload, else downloaded
                                           from Telegram and cached in the uploads dir)
    GET  /api/posts/{id}/photo             V1 compatibility: the first item when it is a picture

Render (SPEC_V23): ``has_custom_emoji`` — the text has custom («premium») emoji; ``premium_emoji``
— the setting «у бота есть юзернейм с Fragment». While it is off, ``html`` shows such emoji as
their regular fallback emoji — what the channel will get (the composer warns about it).

Uploads are detected by magic bytes (never by the client's name or type): JPEG / PNG / WEBP →
photo (≤ 10 MB), MP4 / MOV (``ftyp`` box at offset 4) → video (≤ 50 MB), GIF → animation
(≤ 50 MB). Files are streamed to disk with the size cap checked on the way and stored under a
random name. The Bot API lets bots download files of at most 20 MB, so a bigger Telegram file
has no preview (404 with an explanation). A file_id belongs to the bot that received it
(`file_bot`, SPEC_V22): after the bot was changed such a file is shown from its upload or an
earlier downloaded copy, else 404 «Файл загружен через другого бота — предпросмотр недоступен».
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, ClientResponseError, web
from aiohttp.http_exceptions import HttpProcessingError
from aiogram.exceptions import TelegramBadRequest

from app.markdown import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    has_custom_emoji,
    md_to_html,
    strip_custom_emoji_html,
    visible_length,
)
from app.media import PHOTO_MAX_BYTES, UPLOAD_NAME_RE, VIDEO_MAX_BYTES, content_type
from app.services.posting import FILE_ID_ERRORS, PostError, render_text
from app.web.api import POST_ID, get_post_or_404, parse_text, path_id, stored_media
from app.web.auth import json_error, read_json
from app.web.keys import BOT_KEY, CONFIG_KEY, DB_KEY, DOWNLOAD_LOCKS_KEY
from app.web.logmask import mask_secrets
from app.web.urls import api_url, prefixed_router

log = logging.getLogger(__name__)

UPLOAD_OVERHEAD = 64 * 1024  # multipart headers/boundaries allowed on top of the file itself
SNIFF_BYTES = 16  # enough to recognise every supported format
READ_CHUNK = 256 * 1024
WRITE_BUFFER = 1024 * 1024

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT = 180  # seconds for a Telegram download (up to 20 MB)
CACHE_PREFIX = "tg_"
CACHE_EXTENSIONS = ("jpg", "png", "webp", "mp4", "mov", "gif")
DEFAULT_EXTENSION = {"photo": "jpg", "video": "mp4", "animation": "mp4"}

MEDIA_CACHE_CONTROL = "private, max-age=3600"
UPLOAD_CACHE_CONTROL = "private, max-age=86400"  # uploads are immutable (random names)

UNSUPPORTED_ERROR = "Поддерживаются только JPEG, PNG, WEBP, GIF, MP4 и MOV"
TOO_BIG_FOR_PREVIEW = "Файл больше 20 МБ — предпросмотр недоступен"
FOREIGN_FILE_PREVIEW = "Файл загружен через другого бота — предпросмотр недоступен"
SIZE_ERRORS = {
    "photo": f"Файл больше {PHOTO_MAX_BYTES // (1024 * 1024)} МБ",
    "video": f"Файл больше {VIDEO_MAX_BYTES // (1024 * 1024)} МБ",
    "animation": f"Файл больше {VIDEO_MAX_BYTES // (1024 * 1024)} МБ",
}
SIZE_LIMITS = {"photo": PHOTO_MAX_BYTES, "video": VIDEO_MAX_BYTES, "animation": VIDEO_MAX_BYTES}
DOWNLOAD_ERRORS = {
    "photo": "Не удалось получить картинку из Telegram",
    "video": "Не удалось получить видео из Telegram",
    "animation": "Не удалось получить GIF из Telegram",
}

# ISO-BMFF brands that are not videos Telegram can play (still images, audio).
_NON_VIDEO_BRANDS = frozenset({
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis",
    b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B ",
})


# ------------------------------------------------------------------ render

async def render(request: web.Request) -> web.Response:
    body = await read_json(request)
    text_md = parse_text(body.get("text_md"))
    has_media = bool(body.get("has_media") if "has_media" in body else body.get("has_photo"))
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT
    error = None
    try:
        html_text = render_text(text_md, has_media=has_media)
    except PostError as exc:
        error = str(exc)
        html_text = _best_effort_html(text_md)
    # What really renders as a custom emoji (not the same markdown inside `code`): a <tg-emoji>
    # element of the rendered HTML.
    fallback = strip_custom_emoji_html(html_text)
    custom = fallback != html_text if html_text else has_custom_emoji(text_md)
    premium = await request.app[DB_KEY].get_premium_emoji()
    if not premium:
        html_text = fallback  # the channel gets the regular emoji
    return web.json_response({
        "html": html_text,
        "length": visible_length(html_text),
        "limit": limit,
        "error": error,
        "has_custom_emoji": custom,
        "premium_emoji": premium,
    })


def _best_effort_html(text_md: str) -> str:
    """Preview HTML even when the text cannot be published (e.g. too long)."""
    source = text_md.strip()
    if not source:
        return ""
    try:
        return md_to_html(source)
    except Exception:  # the converter is total by design; never let the preview crash
        log.exception("md_to_html failed")
        return ""


# ------------------------------------------------------------------ uploads

def detect_media(head: bytes) -> tuple[str, str] | None:
    """(media type, file extension) by magic bytes, or None for an unsupported file."""
    if head.startswith(b"\xff\xd8\xff"):
        return "photo", "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "photo", "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "photo", "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "animation", "gif"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _NON_VIDEO_BRANDS:
            return None
        return "video", "mov" if brand == b"qt  " else "mp4"
    return None


class _UploadFile:
    """A temp file in the uploads dir written in the background (buffered), renamed when done."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.tmp = directory / f".upload.{uuid.uuid4().hex}.part"
        self.size = 0
        self._buffer = bytearray()
        self._handle: Any = None

    async def write(self, data: bytes) -> None:
        self.size += len(data)
        self._buffer += data
        if len(self._buffer) >= WRITE_BUFFER:
            await self._flush()

    async def _flush(self) -> None:
        if self._handle is None:
            await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)
            self._handle = await asyncio.to_thread(open, self.tmp, "wb")
        if self._buffer:
            data, self._buffer = bytes(self._buffer), bytearray()
            await asyncio.to_thread(self._handle.write, data)

    async def commit(self, name: str) -> Path:
        await self._flush()
        await asyncio.to_thread(self._handle.close)
        self._handle = None
        final = self.directory / name
        await asyncio.to_thread(os.replace, self.tmp, final)
        return final

    async def discard(self) -> None:
        if self._handle is not None:
            await asyncio.to_thread(self._handle.close)
            self._handle = None
        await asyncio.to_thread(self.tmp.unlink, missing_ok=True)


async def _receive_upload(request: web.Request, directory: Path) -> tuple[str, str, int]:
    """Stream the multipart field "file" into the uploads dir: (stored name, media type, size)."""
    if request.content_type != "multipart/form-data":
        raise json_error(400, "Ожидается загрузка файла (multipart/form-data)")
    try:
        reader = await request.multipart()
        while (part := await reader.next()) is not None:
            if not isinstance(part, BodyPartReader) or part.name != "file":
                await part.release()
                continue
            return await _store_part(part, directory)
    except (ValueError, AssertionError, KeyError, LookupError, UnicodeDecodeError, RuntimeError,
            HttpProcessingError) as exc:
        # Malformed multipart (bad boundary, part headers, charset…) — a client error, not a 500.
        raise json_error(400, "Некорректные данные формы") from exc
    raise json_error(400, "Файл не передан")


async def _store_part(part: BodyPartReader, directory: Path) -> tuple[str, str, int]:
    head = bytearray()
    kind: tuple[str, str] | None = None
    target = _UploadFile(directory)
    done = False
    try:
        while chunk := await part.read_chunk(READ_CHUNK):
            if kind is None:
                head += chunk
                if len(head) < SNIFF_BYTES:
                    continue
                kind = _detect_or_fail(bytes(head))
                chunk, head = bytes(head), bytearray()
            await target.write(chunk)
            if target.size > SIZE_LIMITS[kind[0]]:
                raise json_error(413, SIZE_ERRORS[kind[0]])
        if kind is None:  # the whole file is shorter than SNIFF_BYTES
            kind = _detect_or_fail(bytes(head))
            await target.write(bytes(head))
        media_type, ext = kind
        name = f"{uuid.uuid4().hex}.{ext}"
        await target.commit(name)
        done = True
        return name, media_type, target.size
    finally:
        if not done:
            await target.discard()


def _detect_or_fail(head: bytes) -> tuple[str, str]:
    kind = detect_media(head)
    if kind is None:
        raise json_error(400, UNSUPPORTED_ERROR)
    return kind


async def upload(request: web.Request) -> web.Response:
    if request.content_length is not None and request.content_length > VIDEO_MAX_BYTES + UPLOAD_OVERHEAD:
        raise json_error(413, SIZE_ERRORS["video"])
    directory = Path(request.app[CONFIG_KEY].uploads_dir)
    name, media_type, size = await _receive_upload(request, directory)
    log.info("Uploaded %s %s (%d bytes)", media_type, name, size)
    return web.json_response({
        "path": name,
        "type": media_type,
        "url": api_url(f"/api/uploads/{name}"),
        "size": size,
        "mime": content_type(name),
        "photo_path": name if media_type == "photo" else None,  # V1 compatibility
    })


def _file_response(path: Path, cache_control: str) -> web.FileResponse:
    return web.FileResponse(
        path, headers={"Cache-Control": cache_control, "Content-Type": content_type(path.name)}
    )


async def get_upload(request: web.Request) -> web.FileResponse:
    name = request.match_info["name"]
    if not UPLOAD_NAME_RE.fullmatch(name):
        raise json_error(404, "Файл не найден")
    path = Path(request.app[CONFIG_KEY].uploads_dir) / name
    if not path.is_file():
        raise json_error(404, "Файл не найден")
    return _file_response(path, UPLOAD_CACHE_CONTROL)


# ------------------------------------------------------------------ post media files

async def post_media_file(request: web.Request) -> web.FileResponse:
    post = await get_post_or_404(request.app[DB_KEY], path_id(request))
    index = int(request.match_info["index"])
    media = stored_media(post)
    if not 0 <= index < len(media):
        raise json_error(404, "Медиа не найдено")
    return await _serve_item(request, media[index])


async def post_photo(request: web.Request) -> web.FileResponse:
    """V1 route: the post's picture (the first media item when it is a photo)."""
    post = await get_post_or_404(request.app[DB_KEY], path_id(request))
    media = stored_media(post)
    if not media or media[0].get("type") != "photo":
        raise json_error(404, "У поста нет картинки")
    return await _serve_item(request, media[0])


async def _serve_item(request: web.Request, item: dict) -> web.FileResponse:
    """The uploaded file of a media item, else its Telegram file (downloaded once, cached)."""
    directory = Path(request.app[CONFIG_KEY].uploads_dir)
    name = item.get("path")
    if isinstance(name, str) and UPLOAD_NAME_RE.fullmatch(name) and (directory / name).is_file():
        return _file_response(directory / name, MEDIA_CACHE_CONTROL)
    file_id = item.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise json_error(404, "Файл не найден")
    owner = item.get("file_bot")
    running = getattr(request.app[BOT_KEY], "id", None)
    if owner is not None and running is not None and owner != running:
        # Another bot's file_id: this bot cannot download it; a copy fetched earlier still shows.
        cached = _find_cached(directory, _cache_stem(file_id))
        if cached is None:
            raise json_error(404, FOREIGN_FILE_PREVIEW)
        return _file_response(cached, MEDIA_CACHE_CONTROL)
    cached = await _cached_telegram_file(request, item, file_id, directory)
    return _file_response(cached, MEDIA_CACHE_CONTROL)


def _cache_stem(file_id: str) -> str:
    return CACHE_PREFIX + hashlib.sha1(file_id.encode("utf-8")).hexdigest()[:16]


def _find_cached(directory: Path, stem: str) -> Path | None:
    for ext in CACHE_EXTENSIONS:
        path = directory / f"{stem}.{ext}"
        if path.is_file():
            return path
    return None


def _extension(file_path: str | None, media_type: str) -> str:
    """Cache file extension: from Telegram's file_path ('videos/file_3.MP4' → 'mp4') when it is
    a known media extension, else by the item type."""
    suffix = (file_path or "").rsplit("/", 1)[-1]
    ext = suffix.rsplit(".", 1)[-1].lower() if "." in suffix else ""
    ext = {"jpeg": "jpg"}.get(ext, ext)
    return ext if ext in CACHE_EXTENSIONS else DEFAULT_EXTENSION.get(media_type, "jpg")


async def _cached_telegram_file(request: web.Request, item: dict, file_id: str, directory: Path) -> Path:
    stem = _cache_stem(file_id)
    cached = _find_cached(directory, stem)
    if cached is not None:
        return cached
    # One download per file at a time: parallel requests (thumbnail + preview) wait for it.
    locks = request.app[DOWNLOAD_LOCKS_KEY]
    entry = locks.setdefault(stem, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            cached = _find_cached(directory, stem)
            if cached is not None:
                return cached
            return await _download(request.app[BOT_KEY], item, file_id, directory, stem)
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            locks.pop(stem, None)


def _error_text(exc: BaseException, bot: Any = None) -> str:
    """`exc` for the log without secrets. aiohttp's ClientResponseError quotes the request URL,
    which for a file download is ``…/file/bot<token>/<path>``: only its status and reason are
    logged. Every other text goes through `mask_secrets` (the running bot's token included)."""
    if isinstance(exc, ClientResponseError):
        return f"{type(exc).__name__}: HTTP {exc.status} {exc.message or ''}".rstrip()
    return f"{type(exc).__name__}: {mask_secrets(str(exc), getattr(bot, 'token', None))}"


async def _fetch_file(bot: Any, file: Any, destination: Path) -> None:
    """Download the file Telegram described in `file` (the answer of getFile) to `destination`.

    `download_file` takes the known file_path — `bot.download(file)` would ask Telegram for the
    path once more (a second getFile). A bot without `download_file` (a test double) downloads
    by the File object."""
    file_path = getattr(file, "file_path", None)
    if not file_path:
        raise OSError("Telegram returned no file path")
    download_file = getattr(bot, "download_file", None)
    if download_file is None:
        await bot.download(file, destination=destination, timeout=DOWNLOAD_TIMEOUT)
    else:
        await download_file(file_path, destination=destination, timeout=DOWNLOAD_TIMEOUT)


async def _download(bot: Any, item: dict, file_id: str, directory: Path, stem: str) -> Path:
    """Download via the Bot API into a temp file, then move it into place atomically. Errors are
    logged without the URL (it contains the bot token)."""
    media_type = item.get("type") or "photo"
    failure = DOWNLOAD_ERRORS.get(media_type, DOWNLOAD_ERRORS["photo"])
    try:
        file = await bot.get_file(file_id)
    except TelegramBadRequest as exc:
        low = (exc.message or "").lower()
        if "file is too big" in low:
            raise json_error(404, TOO_BIG_FOR_PREVIEW) from exc
        if any(marker in low for marker in FILE_ID_ERRORS):
            # A file_id this bot does not know: stored before V2.2 by the previous bot.
            log.info("Telegram does not know the file %s: %s", file_id, _error_text(exc, bot))
            raise json_error(404, FOREIGN_FILE_PREVIEW) from exc
        log.warning("Could not get Telegram file %s: %s", file_id, _error_text(exc, bot))
        raise json_error(502, failure) from exc
    except Exception as exc:
        log.warning("Could not get Telegram file %s: %s", file_id, _error_text(exc, bot))
        raise json_error(502, failure) from exc
    size = getattr(file, "file_size", None)
    if isinstance(size, int) and size > TELEGRAM_DOWNLOAD_LIMIT:
        raise json_error(404, TOO_BIG_FOR_PREVIEW)
    destination = directory / f"{stem}.{_extension(getattr(file, 'file_path', None), media_type)}"
    tmp = directory / f".{stem}.{uuid.uuid4().hex}.part"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        await _fetch_file(bot, file, tmp)
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise OSError("empty download")
        os.replace(tmp, destination)
    except TelegramBadRequest as exc:
        if "file is too big" in (exc.message or "").lower():
            raise json_error(404, TOO_BIG_FOR_PREVIEW) from exc
        log.warning("Could not download Telegram file %s: %s", file_id, _error_text(exc, bot))
        raise json_error(502, failure) from exc
    except Exception as exc:
        log.warning("Could not download Telegram file %s: %s", file_id, _error_text(exc, bot))
        raise json_error(502, failure) from exc
    finally:
        tmp.unlink(missing_ok=True)
    return destination


# ------------------------------------------------------------------ routes

def setup_routes(app: web.Application) -> None:
    r = prefixed_router(app)
    r.add_post("/api/render", render)
    r.add_post("/api/upload", upload)
    r.add_get("/api/uploads/{name}", get_upload)
    r.add_get(f"/api/posts/{POST_ID}/media/{{index:[0-9]{{1,2}}}}", post_media_file)
    r.add_get(f"/api/posts/{POST_ID}/photo", post_photo)

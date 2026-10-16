"""Channel avatars for the panel: the small (160×160) chat photo, downloaded once and kept in
`<uploads>/avatars/<id>.jpg`.

`channels.photo_id` is the photo's file_unique_id (the same for every bot), so a photo is downloaded
again only when the channel changes it; `photo_checked_at` — when Telegram was last asked. The
scheduler re-checks every channel once a day (`sync_all`), «Обновить» on a channel re-checks it at once.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from app.db import CH_ACTIVE, CH_LEFT, now

log = logging.getLogger(__name__)

AVATARS_DIR = "avatars"
RECHECK_AFTER = 86400  # once a day
PAUSE = 0.3  # between channels: getChat + getFile + download for dozens of channels
DOWNLOAD_TIMEOUT = 60


def avatar_dir(uploads_dir: Path | str) -> Path:
    return Path(uploads_dir) / AVATARS_DIR


def avatar_path(uploads_dir: Path | str, chat_id: int) -> Path:
    return avatar_dir(uploads_dir) / f"{abs(int(chat_id))}.jpg"


async def sync_avatar(bot: Any, db: Any, uploads_dir: Path | str, chat_id: int, chat: Any = None) -> bool:
    """Bring the stored avatar of a channel in line with Telegram. `chat` — a getChat answer already
    at hand. Returns True when the avatar changed (new, replaced or removed). Telegram errors
    propagate (callers log them)."""
    if chat is None:
        chat = await bot.get_chat(chat_id=chat_id)
    row = await db.get_channel(chat_id)
    if row is None:
        return False
    photo = getattr(chat, "photo", None)
    path = avatar_path(uploads_dir, chat_id)
    if photo is None:
        changed = row.get("photo_id") is not None
        await asyncio.to_thread(path.unlink, missing_ok=True)
        await db.update_channel(chat_id, photo_id=None, photo_checked_at=now())
        return changed
    unique_id = photo.small_file_unique_id
    if unique_id == row.get("photo_id") and path.is_file():
        await db.update_channel(chat_id, photo_checked_at=now())
        return False
    await _download(bot, photo.small_file_id, path)
    await db.update_channel(chat_id, photo_id=unique_id, photo_checked_at=now())
    return True


async def _download(bot: Any, file_id: str, destination: Path) -> None:
    file = await bot.get_file(file_id)
    file_path = getattr(file, "file_path", None)
    if not file_path:
        raise OSError("Telegram returned no file path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.parent / f".{destination.stem}.{uuid.uuid4().hex}.part"
    try:
        download_file = getattr(bot, "download_file", None)
        if download_file is None:  # a test double
            await bot.download(file, destination=tmp, timeout=DOWNLOAD_TIMEOUT)
        else:
            await download_file(file_path, destination=tmp, timeout=DOWNLOAD_TIMEOUT)
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise OSError("empty download")
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


async def sync_all(bot: Any, db: Any, uploads_dir: Path | str, *, max_age: float = RECHECK_AFTER,
                   pause: float = PAUSE) -> int:
    """Re-check the avatars of channels not checked for `max_age` seconds, one by one. Returns how
    many changed. Flood control stops the pass (the rest — next time); other errors are logged one
    line per channel."""
    channels = [c for c in await db.list_channels([CH_ACTIVE, CH_LEFT])
                if (c.get("photo_checked_at") or 0) < now() - max_age]
    changed = failed = 0
    for i, channel in enumerate(channels):
        if i and pause:
            await asyncio.sleep(pause)
        try:
            changed += await sync_avatar(bot, db, uploads_dir, channel["id"])
        except TelegramRetryAfter as exc:
            log.info("Аватары каналов: Telegram просит подождать %s с — остальные в следующий раз", exc.retry_after)
            break
        except TelegramAPIError as exc:
            failed += 1
            log.info("Аватар канала %s не получен: %s", channel["id"], exc.message)
            await db.update_channel(channel["id"], photo_checked_at=now())  # no retry storm
        except Exception as exc:
            failed += 1
            # only the type: a download error may quote the file URL, which contains the bot token
            log.warning("Аватар канала %s не получен: %s", channel["id"], type(exc).__name__)
    if changed or failed:
        log.info("Аватары каналов: обновлено %s, ошибок %s, проверено %s", changed, failed, len(channels))
    return changed

"""Channel folders: named groups that keep a long channel list navigable in the bot and the panel.

A channel belongs to at most one folder (``channels.folder_id``; NULL = «Без папки»). Folders are
ordered by ``position`` (then id). Deleting a folder keeps its channels — they move to «Без папки».
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.services.plan import PlanError, normalize_slots, parse_slots

MAX_NAME_LENGTH = 40
NO_FOLDER_NAME = "Без папки"

EMPTY_NAME_ERROR = "Название папки не может быть пустым"
TOO_LONG_ERROR = f"Слишком длинное название (максимум {MAX_NAME_LENGTH} символов)"
RESERVED_NAME_ERROR = f"Название «{NO_FOLDER_NAME}» зарезервировано — выберите другое"
NOT_FOUND_ERROR = "Папка не найдена"
CHANNEL_NOT_FOUND_ERROR = "Канал не найден"


class FolderError(Exception):
    """Folder operation failed. The message is Russian and user-presentable."""


def clean_name(name: object) -> str:
    """Folder name with whitespace collapsed; FolderError when empty, too long or reserved."""
    text = " ".join(str(name if name is not None else "").split())
    if not text:
        raise FolderError(EMPTY_NAME_ERROR)
    if len(text) > MAX_NAME_LENGTH:
        raise FolderError(TOO_LONG_ERROR)
    if text.casefold() == NO_FOLDER_NAME.casefold():
        raise FolderError(RESERVED_NAME_ERROR)
    return text


async def _folder(db: Any, folder_id: int) -> dict:
    """Folder row with `channels_count`; FolderError when it does not exist."""
    try:
        wanted = int(folder_id)
    except (TypeError, ValueError):
        raise FolderError(NOT_FOUND_ERROR) from None
    for folder in await db.list_folders():
        if folder["id"] == wanted:
            return folder
    raise FolderError(NOT_FOUND_ERROR)


async def _check_unique(db: Any, name: str, exclude_id: int | None = None) -> None:
    key = name.casefold()
    for folder in await db.list_folders():
        if folder["id"] != exclude_id and folder["name"].casefold() == key:
            raise FolderError(f"Папка «{folder['name']}» уже есть")


async def create_folder(db: Any, name: str) -> dict:
    """Create a folder at the end of the list. Name: stripped, 1..40 chars, unique (any case)."""
    clean = clean_name(name)
    await _check_unique(db, clean)
    folder_id = await db.create_folder(clean)
    return await _folder(db, folder_id)


async def rename_folder(db: Any, folder_id: int, name: str) -> dict:
    """Rename a folder (changing only the letter case of its own name is allowed)."""
    folder = await _folder(db, folder_id)
    clean = clean_name(name)
    await _check_unique(db, clean, exclude_id=folder["id"])
    if clean != folder["name"]:
        await db.update_folder(folder["id"], name=clean)
    return await _folder(db, folder["id"])


async def delete_folder(db: Any, folder_id: int) -> None:
    """Delete a folder; its channels move to «Без папки»."""
    folder = await _folder(db, folder_id)
    await db.delete_folder(folder["id"])


async def move_folder(db: Any, folder_id: int, direction: int) -> None:
    """Move a folder one place up (direction < 0) or down (> 0) by swapping it with its neighbour.

    Positions are renumbered 0..n-1 on the way, so equal legacy positions cannot block a swap.
    Moving the first folder up or the last one down does nothing.
    """
    folder = await _folder(db, folder_id)
    if not direction:
        return
    folders = await db.list_folders()
    index = next(i for i, f in enumerate(folders) if f["id"] == folder["id"])
    other = index + (1 if direction > 0 else -1)
    if not 0 <= other < len(folders):
        return
    folders[index], folders[other] = folders[other], folders[index]
    for position, item in enumerate(folders):
        if item["position"] != position:
            await db.update_folder(item["id"], position=position)


async def set_channel_folder(db: Any, channel_id: int, folder_id: int | None) -> dict:
    """Put a channel into a folder (None = «Без папки»). Returns the updated channel row."""
    channel = await db.get_channel(int(channel_id))
    if channel is None:
        raise FolderError(CHANNEL_NOT_FOUND_ERROR)
    target = None if folder_id is None else (await _folder(db, folder_id))["id"]
    if channel.get("folder_id") != target:
        await db.set_channel_folder(channel["id"], target)
    return await db.get_channel(channel["id"])


def _channel_key(channel: dict) -> tuple[str, int]:
    return ((channel.get("title") or channel.get("username") or "").casefold(), channel["id"])


async def grouped_channels(db: Any, statuses: Iterable[str] | str | None = ("active",)) -> list[dict]:
    """Channels grouped by folder: [{"folder": {id, name, position, channels_count} | None,
    "channels": [rows]}].

    Folders in position order (then id), empty ones included; channels sorted by title
    (case-insensitive, Unicode-aware); the «Без папки» group (folder None) comes last and only
    when it is not empty. `statuses` None / 'all' = channels of any status. `channels_count` is
    the number of the folder's channels of any status.
    """
    if statuses is None or statuses == "all":
        channels = await db.list_channels()
    elif isinstance(statuses, str):
        channels = await db.list_channels([statuses])
    else:
        channels = await db.list_channels(list(statuses))
    folders = await db.list_folders()
    by_folder: dict[int, list[dict]] = {f["id"]: [] for f in folders}
    loose: list[dict] = []
    for channel in channels:
        by_folder.get(channel.get("folder_id"), loose).append(channel)
    groups = [
        {
            "folder": {
                "id": f["id"],
                "name": f["name"],
                "position": f["position"],
                "channels_count": f["channels_count"],
            },
            "channels": sorted(by_folder[f["id"]], key=_channel_key),
        }
        for f in folders
    ]
    if loose:
        groups.append({"folder": None, "channels": sorted(loose, key=_channel_key)})
    return groups


async def apply_slots_to_folder(db: Any, folder_id: int, slots: object) -> int:
    """Set the same publishing schedule for every channel of the folder (any status).

    `slots` is a slots dict (see plan.normalize_slots) or slots text (plan.parse_slots); empty
    clears the schedules. Returns the number of channels changed.
    """
    folder = await _folder(db, folder_id)
    try:
        clean = parse_slots(slots) if isinstance(slots, str) else normalize_slots(slots)
    except PlanError as exc:
        raise FolderError(str(exc)) from None
    rows = await db.fetchall("SELECT id FROM channels WHERE folder_id = ? ORDER BY id", (folder["id"],))
    for row in rows:
        await db.set_channel_slots(row["id"], clean)
    return len(rows)

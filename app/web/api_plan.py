"""Web API — channel folders, publishing slots and the content plan (SPEC_V2 §7).

    GET    /api/channels/grouped              → {"groups": [{"folder": {…} | null, "channels": [...]}]}
    GET    /api/folders                       → {"folders": [{id, name, position, channels_count}]}
    POST   /api/folders {name}                → {"folder", "folders"}
    PUT    /api/folders/{id} {name?, direction?}  → {"folder", "folders"}
    DELETE /api/folders/{id}                  → {"ok": true, "folders"}
    PUT    /api/folders/{id}/slots {slots | text} → {"changed": n, "folder"}
    PUT    /api/channels/{id}/folder {folder_id | null} → {"channel"}
    PUT    /api/channels/{id}/slots {slots | text}      → {"channel"}
    GET    /api/plan/channel/{id}?days=14&start=<ts>    → channel_plan (channel serialized,
                                                          slots as {"0": [...], …, "6": [...]})
    GET    /api/plan/timeline?days=7&folder_id=&channel_ids=&start=  → {"items": [...]}
    GET    /api/plan/next?channel_ids=1,2&post_id=      → {"next": {channel_id: ts | null}}
    POST   /api/plan/move {post_id, channel_id, ts, queued?} → {"ok", "post"}
    POST   /api/plan/shift {post_id, channel_id, direction}  → {"moved": bool, "post"}
    POST   /api/plan/compact {channel_id}               → {"moved": n}
    POST   /api/plan/unqueue {post_id, channel_id}      → {"ok", "post"}

Plan items' post summaries get an extra ``thumb_url`` (first media item) for thumbnails.
Service errors (PlanError / FolderError) become 400 (404 for a missing object, 409 while the post
is being sent).
"""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from app.db import Database
from app.media import post_media
from app.services import folders as folders_service
from app.services import plan as plan_service
from app.services.folders import FolderError
from app.services.plan import PlanError
from app.web.api import (
    CHANNEL_ID,
    as_int,
    get_channel_or_404,
    get_post_or_404,
    media_url,
    parse_channel_ids,
    parse_timestamp,
    path_id,
    serialize_channel,
    serialize_post,
    service_error,
    week_slots,
)
from app.web.auth import json_error, read_json
from app.web.keys import CONFIG_KEY, DB_KEY
from app.web.urls import prefixed_router

FOLDER_ID = r"{id:[0-9]{1,18}}"
PLAN_DAYS_DEFAULT = 14
TIMELINE_DAYS_DEFAULT = 7
MAX_DAYS = 366


def _db(request: web.Request) -> Database:
    return request.app[DB_KEY]


def _tz(request: web.Request) -> Any:
    return request.app[CONFIG_KEY].tz


def _folder_dict(folder: dict) -> dict:
    return {
        "id": folder["id"],
        "name": folder["name"],
        "position": folder["position"],
        "channels_count": folder.get("channels_count", 0),
    }


async def _folders(db: Database) -> list[dict]:
    return [_folder_dict(f) for f in await db.list_folders()]


def _days(request: web.Request, default: int) -> int:
    days = as_int(request.query.get("days", str(default)))
    return min(max(days if days is not None else default, 1), MAX_DAYS)


def _optional_ts(request: web.Request, name: str = "start") -> int | None:
    raw = request.query.get(name)
    if raw in (None, ""):
        return None
    value = as_int(raw)
    if value is None or value < 0:
        raise json_error(400, "Некорректная дата начала")
    return value


def _required_int(body: dict, key: str, message: str) -> int:
    value = as_int(body.get(key))
    if value is None:
        raise json_error(400, message)
    return value


async def _payload_post(db: Database, post_id: int) -> dict:
    return serialize_post(await get_post_or_404(db, post_id))


def _service(exc: PlanError | FolderError) -> web.HTTPException:
    return service_error(exc)


# ------------------------------------------------------------------ grouped channels / folders

async def grouped_channels(request: web.Request) -> web.Response:
    groups = await folders_service.grouped_channels(_db(request), statuses="all")
    return web.json_response({"groups": [
        {
            "folder": _folder_dict(group["folder"]) if group["folder"] else None,
            "channels": [serialize_channel(row) for row in group["channels"]],
        }
        for group in groups
    ]})


async def list_folders(request: web.Request) -> web.Response:
    return web.json_response({"folders": await _folders(_db(request))})


async def create_folder(request: web.Request) -> web.Response:
    db = _db(request)
    body = await read_json(request)
    name = body.get("name")
    if not isinstance(name, str):
        raise json_error(400, folders_service.EMPTY_NAME_ERROR)
    try:
        folder = await folders_service.create_folder(db, name)
    except FolderError as exc:
        raise _service(exc) from exc
    return web.json_response({"folder": _folder_dict(folder), "folders": await _folders(db)})


async def update_folder(request: web.Request) -> web.Response:
    """Rename (``name``) and/or move up (``direction`` < 0) / down (> 0) a folder."""
    db = _db(request)
    folder_id = path_id(request)
    body = await read_json(request)
    name, direction = body.get("name"), body.get("direction")
    if name is None and direction is None:
        raise json_error(400, "Укажите новое название папки или направление перемещения")
    if name is not None and not isinstance(name, str):
        raise json_error(400, folders_service.EMPTY_NAME_ERROR)
    step = None
    if direction is not None:
        step = as_int(direction)
        if step is None:
            raise json_error(400, plan_service.DIRECTION_ERROR)
    try:
        folder = await folders_service.rename_folder(db, folder_id, name) if name is not None else None
        if step:
            await folders_service.move_folder(db, folder_id, step)
        if folder is None or step:
            folder = next((f for f in await db.list_folders() if f["id"] == folder_id), None)
            if folder is None:
                raise FolderError(folders_service.NOT_FOUND_ERROR)
    except FolderError as exc:
        raise _service(exc) from exc
    return web.json_response({"folder": _folder_dict(folder), "folders": await _folders(db)})


async def delete_folder(request: web.Request) -> web.Response:
    db = _db(request)
    try:
        await folders_service.delete_folder(db, path_id(request))
    except FolderError as exc:
        raise _service(exc) from exc
    return web.json_response({"ok": True, "folders": await _folders(db)})


def _slots_input(body: dict) -> object:
    """``slots`` (object / list) or ``text`` (bot text format) from a request body."""
    if "text" in body and body["text"] is not None:
        if not isinstance(body["text"], str):
            raise json_error(400, plan_service.BAD_SLOTS_ERROR)
        return body["text"]
    if "slots" not in body:
        raise json_error(400, "Укажите расписание (slots или text)")
    slots = body["slots"]
    if slots is not None and not isinstance(slots, (dict, list)):
        raise json_error(400, plan_service.BAD_SLOTS_ERROR)
    return slots


async def folder_slots(request: web.Request) -> web.Response:
    """Set the same publishing schedule for every channel of the folder."""
    db = _db(request)
    folder_id = path_id(request)
    slots = _slots_input(await read_json(request))
    try:
        changed = await folders_service.apply_slots_to_folder(db, folder_id, slots)
    except FolderError as exc:
        raise _service(exc) from exc
    folder = next((f for f in await db.list_folders() if f["id"] == folder_id), None)
    return web.json_response({"changed": changed, "folder": _folder_dict(folder) if folder else None})


async def channel_folder(request: web.Request) -> web.Response:
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    body = await read_json(request)
    if "folder_id" not in body:
        raise json_error(400, "Укажите папку (folder_id или null)")
    raw = body["folder_id"]
    folder_id = None
    if raw not in (None, 0, "0", ""):
        folder_id = as_int(raw)
        if folder_id is None:
            raise json_error(400, folders_service.NOT_FOUND_ERROR)
    try:
        row = await folders_service.set_channel_folder(db, channel["id"], folder_id)
    except FolderError as exc:
        raise _service(exc) from exc
    return web.json_response({"channel": serialize_channel(row)})


async def channel_slots(request: web.Request) -> web.Response:
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    raw = _slots_input(await read_json(request))
    try:
        slots = plan_service.parse_slots(raw) if isinstance(raw, str) else plan_service.normalize_slots(raw)
    except PlanError as exc:
        raise _service(exc) from exc
    await db.set_channel_slots(channel["id"], slots)
    return web.json_response({"channel": serialize_channel(await get_channel_or_404(db, channel["id"]))})


# ------------------------------------------------------------------ plan views

async def _thumb_urls(db: Database, post_ids: set[int]) -> dict[int, str | None]:
    """URL of each post's first media item (None for text posts) — one query."""
    if not post_ids:
        return {}
    ids = sorted(post_ids)
    rows = await db.fetchall(
        f"SELECT id, media_json, photo_file_id, photo_path FROM posts WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    )
    result: dict[int, str | None] = {}
    for row in rows:
        media = row.pop("media_json")
        if media is not None:
            try:
                row["media"] = json.loads(media)
            except ValueError:
                row["media"] = []
        items = post_media(row)
        first = items[0] if items and isinstance(items[0], dict) else None
        result[row["id"]] = media_url(row["id"], 0, first) if first else None
    return result


async def _add_thumbs(db: Database, summaries: list[dict]) -> None:
    thumbs = await _thumb_urls(db, {s["id"] for s in summaries if s.get("id") is not None})
    for summary in summaries:
        summary["thumb_url"] = thumbs.get(summary.get("id"))


async def channel_plan(request: web.Request) -> web.Response:
    db = _db(request)
    channel = await get_channel_or_404(db, path_id(request))
    try:
        data = await plan_service.channel_plan(
            db, channel["id"], _tz(request), days=_days(request, PLAN_DAYS_DEFAULT),
            start_ts=_optional_ts(request),
        )
    except PlanError as exc:
        raise _service(exc) from exc
    data["channel"] = serialize_channel(data["channel"])
    data["slots"] = week_slots(data["slots"])
    await _add_thumbs(db, [item["post"] for day in data["days"] for item in day["items"] if item["post"]])
    return web.json_response(data)


async def timeline(request: web.Request) -> web.Response:
    db = _db(request)
    folder_id = None
    raw_folder = request.query.get("folder_id")
    if raw_folder not in (None, ""):
        folder_id = 0 if raw_folder in ("none", "null") else as_int(raw_folder)
        if folder_id is None or folder_id < 0:
            raise json_error(400, folders_service.NOT_FOUND_ERROR)
    channel_ids = None
    if request.query.get("channel_ids"):
        channel_ids = await parse_channel_ids(db, request.query["channel_ids"], active_only=False)
    try:
        items = await plan_service.timeline(
            db, _tz(request), days=_days(request, TIMELINE_DAYS_DEFAULT), folder_id=folder_id,
            channel_ids=channel_ids, start_ts=_optional_ts(request),
        )
    except PlanError as exc:
        raise _service(exc) from exc
    await _add_thumbs(db, [item["post"] for item in items])
    return web.json_response({"items": items})


async def plan_next(request: web.Request) -> web.Response:
    """Next free slot per channel (null = no schedule / no free slot), changing nothing."""
    db = _db(request)
    channel_ids = await parse_channel_ids(db, request.query.get("channel_ids") or None, active_only=False)
    post_id = None
    if request.query.get("post_id"):
        post_id = as_int(request.query["post_id"])
        if post_id is None:
            raise json_error(400, "Некорректный ID поста")
    try:
        result = await plan_service.preview_queue(db, channel_ids, _tz(request), post_id=post_id)
    except PlanError as exc:
        raise _service(exc) from exc
    return web.json_response({"next": {str(cid): ts for cid, ts in result.items()}})


# ------------------------------------------------------------------ plan editing

def _target_ref(body: dict) -> tuple[int, int]:
    return (
        _required_int(body, "post_id", "Некорректный ID поста"),
        _required_int(body, "channel_id", "Некорректный ID канала"),
    )


async def plan_move(request: web.Request) -> web.Response:
    """Move a post's time in one channel (swaps with a post standing at that time)."""
    db = _db(request)
    body = await read_json(request)
    post_id, channel_id = _target_ref(body)
    ts = parse_timestamp(body.get("ts"))
    queued = body.get("queued")
    if queued is not None and not isinstance(queued, bool):
        raise json_error(400, "Некорректный признак очереди")
    try:
        await plan_service.move_target(db, post_id, channel_id, ts, queued=queued)
    except PlanError as exc:
        raise _service(exc) from exc
    return web.json_response({"ok": True, "post": await _payload_post(db, post_id)})


async def plan_shift(request: web.Request) -> web.Response:
    """Swap the time with the previous (-1) / next (+1) post of the channel."""
    db = _db(request)
    body = await read_json(request)
    post_id, channel_id = _target_ref(body)
    direction = as_int(body.get("direction"))
    if direction not in (-1, 1):
        raise json_error(400, plan_service.DIRECTION_ERROR)
    try:
        moved = await plan_service.shift_target(db, post_id, channel_id, direction)
    except PlanError as exc:
        raise _service(exc) from exc
    return web.json_response({"moved": moved, "post": await _payload_post(db, post_id)})


async def plan_compact(request: web.Request) -> web.Response:
    db = _db(request)
    body = await read_json(request)
    channel_id = _required_int(body, "channel_id", "Некорректный ID канала")
    try:
        moved = await plan_service.compact_queue(db, channel_id, _tz(request))
    except PlanError as exc:
        raise _service(exc) from exc
    return web.json_response({"moved": moved})


async def plan_unqueue(request: web.Request) -> web.Response:
    """Take the post out of one channel's plan (the channel stays selected, without a time)."""
    db = _db(request)
    body = await read_json(request)
    post_id, channel_id = _target_ref(body)
    try:
        await plan_service.unqueue_target(db, post_id, channel_id)
    except PlanError as exc:
        raise _service(exc) from exc
    return web.json_response({"ok": True, "post": await _payload_post(db, post_id)})


# ------------------------------------------------------------------ routes

def setup_routes(app: web.Application) -> None:
    r = prefixed_router(app)
    r.add_get("/api/channels/grouped", grouped_channels)
    r.add_put(f"/api/channels/{CHANNEL_ID}/folder", channel_folder)
    r.add_put(f"/api/channels/{CHANNEL_ID}/slots", channel_slots)

    r.add_get("/api/folders", list_folders)
    r.add_post("/api/folders", create_folder)
    r.add_put(f"/api/folders/{FOLDER_ID}", update_folder)
    r.add_delete(f"/api/folders/{FOLDER_ID}", delete_folder)
    r.add_put(f"/api/folders/{FOLDER_ID}/slots", folder_slots)

    r.add_get(f"/api/plan/channel/{CHANNEL_ID}", channel_plan)
    r.add_get("/api/plan/timeline", timeline)
    r.add_get("/api/plan/next", plan_next)
    r.add_post("/api/plan/move", plan_move)
    r.add_post("/api/plan/shift", plan_shift)
    r.add_post("/api/plan/compact", plan_compact)
    r.add_post("/api/plan/unqueue", plan_unqueue)

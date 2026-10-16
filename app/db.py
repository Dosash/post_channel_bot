"""SQLite storage: schema and repository functions.

All rows are returned as plain dicts. Timestamps are unix seconds (int, UTC).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from app.media import edit_class

# Post statuses
POST_DRAFT = "draft"
POST_SCHEDULED = "scheduled"
POST_SENDING = "sending"
POST_SENT = "sent"
POST_PARTIAL = "partial"  # sent to some channels, failed for others
POST_FAILED = "failed"
POST_DELETED = "deleted"  # removed from channels

# Post target (post x channel) statuses
TARGET_PENDING = "pending"
TARGET_SENT = "sent"
TARGET_FAILED = "failed"
TARGET_DELETED = "deleted"

# Error of a target whose send was cut (cancelled / crash): Telegram may or may not have the message.
INTERRUPTED_ERROR = "Публикация прервана — проверьте, вышел ли пост в канале"

# settings key: the bot has a Fragment username, so custom («premium») emoji are sent as they are (SPEC_V23)
PREMIUM_EMOJI_SETTING = "premium_emoji"

# Channel statuses
CH_ACTIVE = "active"  # admin approved; available for posting
CH_PENDING = "pending"  # bot was made admin, waiting for admin approval
CH_IGNORED = "ignored"  # admin declined
CH_LEFT = "left"  # bot lost admin rights / was removed

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS admins (
    user_id   INTEGER PRIMARY KEY,
    username  TEXT,
    full_name TEXT,
    is_owner  INTEGER NOT NULL DEFAULT 0,
    added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY,          -- telegram chat_id (-100...)
    title         TEXT NOT NULL DEFAULT '',
    username      TEXT,                          -- without @
    status        TEXT NOT NULL DEFAULT 'pending',
    can_post      INTEGER NOT NULL DEFAULT 0,
    can_edit      INTEGER NOT NULL DEFAULT 0,
    can_delete    INTEGER NOT NULL DEFAULT 0,
    members_count INTEGER,
    added_by      INTEGER,
    discovered_at INTEGER NOT NULL,
    added_at      INTEGER,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text_md         TEXT NOT NULL DEFAULT '',   -- markdown source (what the user edits)
    text_html       TEXT NOT NULL DEFAULT '',   -- rendered Telegram HTML (what is sent)
    photo_file_id   TEXT,                        -- telegram file_id of the picture
    photo_path      TEXT,                        -- local file (web uploads), relative to uploads dir
    buttons_json    TEXT NOT NULL DEFAULT '[]', -- [[{"text","url","style"}]]
    disable_preview INTEGER NOT NULL DEFAULT 0,
    silent          INTEGER NOT NULL DEFAULT 0,
    protect         INTEGER NOT NULL DEFAULT 0,
    caption_above   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'draft',
    scheduled_at    INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    sent_at         INTEGER,
    created_by      INTEGER,
    source          TEXT NOT NULL DEFAULT 'bot', -- 'bot' | 'web'
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, scheduled_at);

CREATE TABLE IF NOT EXISTS post_targets (
    post_id         INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    channel_id      INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    message_id      INTEGER,
    error           TEXT,
    sent_at         INTEGER,
    reactions_total INTEGER NOT NULL DEFAULT 0,
    reactions_json  TEXT,
    PRIMARY KEY (post_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_targets_msg ON post_targets(channel_id, message_id);

CREATE TABLE IF NOT EXISTS member_snapshots (
    channel_id INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    count      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots ON member_snapshots(channel_id, ts);

CREATE TABLE IF NOT EXISTS member_events (
    channel_id INTEGER NOT NULL,
    user_id    INTEGER,
    ts         INTEGER NOT NULL,
    kind       TEXT NOT NULL                     -- 'join' | 'leave'
);
CREATE INDEX IF NOT EXISTS idx_member_events ON member_events(channel_id, ts);

CREATE TABLE IF NOT EXISTS channel_posts (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    via_bot    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_posts_ts ON channel_posts(channel_id, ts);

CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""

POST_FIELDS = {
    "text_md", "text_html", "photo_file_id", "photo_path", "buttons_json",
    "disable_preview", "silent", "protect", "caption_above", "status",
    "scheduled_at", "created_at", "updated_at", "sent_at", "created_by",
    "source", "error",
    "published_kind",  # added by Database.migrate(): edit class once the post is published
    "media_json",      # V2: list of media items, see app/media.py
}
TARGET_FIELDS = {
    "status", "message_id", "error", "sent_at", "reactions_total", "reactions_json",
    "scheduled_at", "queued", "message_ids_json",  # V2
    "media_sig",  # V2: signature of the media currently shown in this channel's message
    "sending_at",  # V2.3: set while the message is being sent to this channel (in-flight marker)
}
CHANNEL_FIELDS = {
    "title", "username", "status", "can_post", "can_edit", "can_delete",
    "members_count", "added_by", "added_at", "updated_at",
    "folder_id", "slots_json",  # V2
    "photo_id", "photo_checked_at",  # V2.6: avatar (app.services.avatars)
}


def now() -> int:
    return int(time.time())


def _json_list(raw: Any) -> list:
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _post_from_row(row: dict) -> dict:
    """Decode JSON/boolean columns of a posts row.

    `media` is the decoded media list; rows written before V2 (no media_json) fall back to the
    legacy single picture columns.
    """
    post = dict(row)
    post["buttons"] = _json_list(post.get("buttons_json"))
    if post.get("media_json") is not None:
        post["media"] = _json_list(post.get("media_json"))
    elif post.get("photo_file_id") or post.get("photo_path"):
        post["media"] = [{"type": "photo", "file_id": post.get("photo_file_id"), "path": post.get("photo_path")}]
    else:
        post["media"] = []
    for key in ("disable_preview", "silent", "protect", "caption_above"):
        post[key] = bool(post.get(key))
    return post


def _target_from_row(row: dict) -> dict:
    """Decode a post_targets row: `message_ids` = all message ids (album) or [message_id]."""
    target = dict(row)
    ids = _json_list(target.get("message_ids_json"))
    if not ids and target.get("message_id") is not None:
        ids = [target["message_id"]]
    target["message_ids"] = [int(i) for i in ids]
    target["queued"] = bool(target.get("queued"))
    return target


def _encode_aliases(fields: dict) -> None:
    """`buttons`/`media` lists → their JSON columns (in place).

    Writing `media` also mirrors the legacy `photo_file_id`/`photo_path` columns (first item when
    it is a photo, else NULL) unless the caller sets them explicitly, so pre-V2 readers stay
    consistent with the media list.
    """
    if "buttons" in fields:
        fields["buttons_json"] = json.dumps(fields.pop("buttons") or [], ensure_ascii=False)
    if "media" in fields:
        media = list(fields.pop("media") or [])
        fields["media_json"] = json.dumps(media, ensure_ascii=False)
        if "photo_file_id" not in fields and "photo_path" not in fields:
            first = media[0] if media and isinstance(media[0], dict) and media[0].get("type") == "photo" else {}
            fields["photo_file_id"] = first.get("file_id")
            fields["photo_path"] = first.get("path")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------ core
    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur

    # -------------------------------------------------------------- settings
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row is not None else default

    async def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            await self.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            await self.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def get_premium_emoji(self) -> bool:
        """SPEC_V23: the bot has a Fragment username — custom emoji are published as they are.
        Off by default: they are sent as their regular fallback emoji."""
        return await self.get_setting(PREMIUM_EMOJI_SETTING) == "1"

    async def set_premium_emoji(self, enabled: bool) -> None:
        await self.set_setting(PREMIUM_EMOJI_SETTING, "1" if enabled else None)

    # ---------------------------------------------------------------- admins
    async def list_admins(self) -> list[dict]:
        rows = await self.fetchall("SELECT * FROM admins ORDER BY is_owner DESC, added_at")
        for r in rows:
            r["is_owner"] = bool(r["is_owner"])
        return rows

    async def get_admin_ids(self) -> set[int]:
        rows = await self.fetchall("SELECT user_id FROM admins")
        return {r["user_id"] for r in rows}

    async def is_admin(self, user_id: int) -> bool:
        return await self.fetchone("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) is not None

    async def has_admins(self) -> bool:
        return await self.fetchone("SELECT 1 FROM admins LIMIT 1") is not None

    async def add_admin(
        self,
        user_id: int,
        username: str | None = None,
        full_name: str | None = None,
        is_owner: bool = False,
    ) -> None:
        await self.execute(
            "INSERT INTO admins(user_id, username, full_name, is_owner, added_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "username = COALESCE(excluded.username, admins.username), "
            "full_name = COALESCE(excluded.full_name, admins.full_name), "
            "is_owner = MAX(admins.is_owner, excluded.is_owner)",
            (user_id, username, full_name, int(is_owner), now()),
        )

    async def update_admin_profile(self, user_id: int, username: str | None, full_name: str | None) -> None:
        await self.execute(
            "UPDATE admins SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )

    async def remove_admin(self, user_id: int) -> bool:
        """Remove a non-owner admin. Returns False if the user is the owner or absent."""
        cur = await self.execute("DELETE FROM admins WHERE user_id = ? AND is_owner = 0", (user_id,))
        return cur.rowcount > 0

    # -------------------------------------------------------------- channels
    async def upsert_channel(
        self,
        chat_id: int,
        title: str,
        username: str | None,
        status: str | None = None,
        rights: dict | None = None,
        members_count: int | None = None,
        added_by: int | None = None,
    ) -> dict:
        """Insert or update a channel.

        status=None keeps the current status (new channels become 'pending').
        rights: {"can_post": bool, "can_edit": bool, "can_delete": bool} or None to keep.
        """
        ts = now()
        existing = await self.get_channel(chat_id)
        if existing is None:
            new_status = status or CH_PENDING
            r = rights or {}
            await self.execute(
                "INSERT INTO channels(id, title, username, status, can_post, can_edit, can_delete, "
                "members_count, added_by, discovered_at, added_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id, title or "", username, new_status,
                    int(bool(r.get("can_post"))), int(bool(r.get("can_edit"))), int(bool(r.get("can_delete"))),
                    members_count, added_by, ts, ts if new_status == CH_ACTIVE else None, ts,
                ),
            )
        else:
            fields: dict[str, Any] = {"title": title or existing["title"], "username": username, "updated_at": ts}
            if status is not None:
                fields["status"] = status
                if status == CH_ACTIVE and existing["status"] != CH_ACTIVE:
                    fields["added_at"] = ts
                    if added_by is not None:
                        fields["added_by"] = added_by
            if rights is not None:
                fields["can_post"] = int(bool(rights.get("can_post")))
                fields["can_edit"] = int(bool(rights.get("can_edit")))
                fields["can_delete"] = int(bool(rights.get("can_delete")))
            if members_count is not None:
                fields["members_count"] = members_count
            await self.update_channel(chat_id, **fields)
        channel = await self.get_channel(chat_id)
        assert channel is not None
        return channel

    async def update_channel(self, chat_id: int, **fields: Any) -> None:
        unknown = set(fields) - CHANNEL_FIELDS
        if unknown:
            raise ValueError(f"Unknown channel fields: {unknown}")
        if not fields:
            return
        fields.setdefault("updated_at", now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.execute(f"UPDATE channels SET {cols} WHERE id = ?", (*fields.values(), chat_id))

    async def get_channel(self, chat_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM channels WHERE id = ?", (chat_id,))

    async def list_channels(self, statuses: Iterable[str] | None = None) -> list[dict]:
        if statuses is None:
            return await self.fetchall("SELECT * FROM channels ORDER BY status, title COLLATE NOCASE")
        statuses = list(statuses)
        marks = ",".join("?" * len(statuses))
        return await self.fetchall(
            f"SELECT * FROM channels WHERE status IN ({marks}) ORDER BY title COLLATE NOCASE", statuses
        )

    async def set_channel_status(self, chat_id: int, status: str, added_by: int | None = None) -> None:
        fields: dict[str, Any] = {"status": status}
        if status == CH_ACTIVE:
            fields["added_at"] = now()
            if added_by is not None:
                fields["added_by"] = added_by
        await self.update_channel(chat_id, **fields)

    async def delete_channel(self, chat_id: int) -> None:
        await self.execute("DELETE FROM channels WHERE id = ?", (chat_id,))

    # ----------------------------------------------------------------- posts
    async def create_post(self, **fields: Any) -> int:
        """Insert a post. Accepts `buttons` and `media` lists as aliases of their JSON columns."""
        _encode_aliases(fields)
        unknown = set(fields) - POST_FIELDS
        if unknown:
            raise ValueError(f"Unknown post fields: {unknown}")
        ts = now()
        fields.setdefault("created_at", ts)
        fields.setdefault("updated_at", ts)
        for key in ("disable_preview", "silent", "protect", "caption_above"):
            if key in fields:
                fields[key] = int(bool(fields[key]))
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = await self.execute(f"INSERT INTO posts({cols}) VALUES({marks})", fields.values())
        return int(cur.lastrowid)

    async def update_post(self, post_id: int, **fields: Any) -> None:
        """Update post columns. Accepts `buttons` / `media` lists as aliases of their JSON columns.

        Compatibility: setting only the legacy picture columns (`photo_file_id` / `photo_path`)
        rewrites `media_json` to that single picture (or to no media when both end up empty).
        """
        if ("photo_file_id" in fields or "photo_path" in fields) and not {"media", "media_json"} & set(fields):
            row = await self.fetchone("SELECT photo_file_id, photo_path FROM posts WHERE id = ?", (post_id,))
            if row is not None:
                file_id = fields.get("photo_file_id", row["photo_file_id"])
                path = fields.get("photo_path", row["photo_path"])
                fields["media_json"] = json.dumps(
                    [{"type": "photo", "file_id": file_id, "path": path}] if (file_id or path) else [],
                    ensure_ascii=False,
                )
        _encode_aliases(fields)
        unknown = set(fields) - POST_FIELDS
        if unknown:
            raise ValueError(f"Unknown post fields: {unknown}")
        if not fields:
            return
        for key in ("disable_preview", "silent", "protect", "caption_above"):
            if key in fields:
                fields[key] = int(bool(fields[key]))
        fields.setdefault("updated_at", now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.execute(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), post_id))

    async def update_post_if(self, post_id: int, expected_status: str, **fields: Any) -> bool:
        """Update post columns only while the post still has `expected_status` (atomic, e.g. the
        release of a post claimed as 'sending'). Plain columns only; returns whether it applied."""
        unknown = set(fields) - POST_FIELDS
        if unknown:
            raise ValueError(f"Unknown post fields: {unknown}")
        fields.setdefault("updated_at", now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = await self.execute(
            f"UPDATE posts SET {cols} WHERE id = ? AND status = ?", (*fields.values(), post_id, expected_status)
        )
        return cur.rowcount == 1

    async def get_targets(self, post_id: int) -> list[dict]:
        """Targets of a post joined with channel info (title/username may be None if channel deleted).

        Each target has decoded `message_ids` and boolean `queued`.
        """
        rows = await self.fetchall(
            "SELECT t.*, c.title AS channel_title, c.username AS channel_username, "
            "c.status AS channel_status, c.folder_id AS channel_folder_id "
            "FROM post_targets t LEFT JOIN channels c ON c.id = t.channel_id "
            "WHERE t.post_id = ? ORDER BY c.title COLLATE NOCASE",
            (post_id,),
        )
        return [_target_from_row(r) for r in rows]

    async def get_post(self, post_id: int) -> dict | None:
        """Post with decoded `buttons` list, boolean flags and `targets` list."""
        row = await self.fetchone("SELECT * FROM posts WHERE id = ?", (post_id,))
        if row is None:
            return None
        post = _post_from_row(row)
        post["targets"] = await self.get_targets(post_id)
        return post

    async def list_posts(
        self, status: str | Iterable[str] | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """Posts with `targets`: only-scheduled lists soonest first, any other list newest first
        (by sent_at, else updated_at) — a freshly scheduled post is not pushed behind older ones."""
        params: list[Any] = []
        where = ""
        order = "COALESCE(sent_at, updated_at) DESC, id DESC"
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            where = f"WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
            if set(statuses) == {POST_SCHEDULED}:
                order = "scheduled_at IS NULL, scheduled_at, id"
        rows = await self.fetchall(
            f"SELECT * FROM posts {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        posts = []
        for row in rows:
            post = _post_from_row(row)
            post["targets"] = await self.get_targets(post["id"])
            posts.append(post)
        return posts

    async def count_posts(self, status: str | Iterable[str] | None = None) -> int:
        if status is None:
            row = await self.fetchone("SELECT COUNT(*) AS n FROM posts")
        else:
            statuses = [status] if isinstance(status, str) else list(status)
            row = await self.fetchone(
                f"SELECT COUNT(*) AS n FROM posts WHERE status IN ({','.join('?' * len(statuses))})",
                statuses,
            )
        return int(row["n"]) if row else 0

    async def delete_post_record(self, post_id: int) -> bool:
        """Remove a post and its targets — atomically and only while that is safe: the post is a
        draft / scheduled / failed / deleted one and no channel shows it (no 'sent' target).

        Returns False (nothing removed) otherwise, e.g. when the scheduler claimed the post
        ('sending') after the caller read it — its record must stay to manage the message.
        """
        cur = await self.execute(
            "DELETE FROM posts WHERE id = ? AND status IN ('draft', 'scheduled', 'failed', 'deleted') "
            "AND NOT EXISTS (SELECT 1 FROM post_targets t WHERE t.post_id = posts.id AND t.status = 'sent')",
            (post_id,),
        )
        if cur.rowcount != 1:
            return False
        # ON DELETE CASCADE removes the targets; this covers a connection without foreign keys.
        await self.execute("DELETE FROM post_targets WHERE post_id = ?", (post_id,))
        return True

    async def set_post_targets(self, post_id: int, channel_ids: Iterable[int]) -> bool:
        """Make the set of unsent targets equal to `channel_ids` (an empty list removes them all).

        Pending/failed targets of other channels are removed; failed ones that stay selected are
        reset to pending; pending ones keep their schedule (scheduled_at/queued). Targets already
        sent/deleted are never touched.

        Guarded: nothing is changed while the post is being sent or is deleted (every statement
        re-checks it, so a post claimed by the scheduler meanwhile keeps the targets it is sending).
        Returns False then — the caller should say «Пост сейчас публикуется».
        """
        channel_ids = list(dict.fromkeys(int(c) for c in channel_ids))
        if not await self._targets_editable(post_id):
            return False
        guard = ("AND EXISTS (SELECT 1 FROM posts p WHERE p.id = post_targets.post_id "
                 "AND p.status NOT IN ('sending', 'deleted'))")
        keep = f"AND channel_id NOT IN ({','.join('?' * len(channel_ids))})" if channel_ids else ""
        await self.conn.execute(
            f"DELETE FROM post_targets WHERE post_id = ? AND status IN ('pending', 'failed') {keep} {guard}",
            (post_id, *channel_ids),
        )
        await self.conn.execute(
            "UPDATE post_targets SET status = 'pending', error = NULL "
            f"WHERE post_id = ? AND status = 'failed' {guard}",
            (post_id,),
        )
        for cid in channel_ids:
            await self.conn.execute(
                "INSERT OR IGNORE INTO post_targets(post_id, channel_id, status) SELECT ?, ?, 'pending' "
                "WHERE EXISTS (SELECT 1 FROM posts p WHERE p.id = ? AND p.status NOT IN ('sending', 'deleted'))",
                (post_id, cid, post_id),
            )
        await self.conn.commit()
        return await self._targets_editable(post_id)

    async def _targets_editable(self, post_id: int) -> bool:
        row = await self.fetchone("SELECT status FROM posts WHERE id = ?", (post_id,))
        return row is not None and row["status"] not in (POST_SENDING, POST_DELETED)

    async def update_target(self, post_id: int, channel_id: int, **fields: Any) -> None:
        unknown = set(fields) - TARGET_FIELDS
        if unknown:
            raise ValueError(f"Unknown target fields: {unknown}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.execute(
            f"UPDATE post_targets SET {cols} WHERE post_id = ? AND channel_id = ?",
            (*fields.values(), post_id, channel_id),
        )

    async def get_due_scheduled_posts(self, ts: int | None = None) -> list[dict]:
        rows = await self.fetchall(
            "SELECT * FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at",
            (ts if ts is not None else now(),),
        )
        return [_post_from_row(r) for r in rows]

    async def claim_post_for_sending(
        self,
        post_id: int,
        from_statuses: Iterable[str] = (POST_DRAFT, POST_SCHEDULED, POST_FAILED, POST_PARTIAL),
        *,
        due_before: int | None = None,
    ) -> bool:
        """Atomically switch a post to 'sending'. Returns False if it was already taken.

        `due_before` (scheduler): additionally require posts.scheduled_at <= due_before, so a post
        cancelled or moved to a later time meanwhile is not claimed.
        """
        statuses = list(from_statuses)
        sql = (
            f"UPDATE posts SET status = 'sending', updated_at = ? "
            f"WHERE id = ? AND status IN ({','.join('?' * len(statuses))})"
        )
        params: list[Any] = [now(), post_id, *statuses]
        if due_before is not None:
            sql += " AND scheduled_at IS NOT NULL AND scheduled_at <= ?"
            params.append(due_before)
        cur = await self.execute(sql, params)
        return cur.rowcount == 1

    async def fail_due_targets(self, post_id: int, error: str, ts: int | None = None) -> int:
        """Scheduler: mark the post's pending targets due at `ts` as failed (their time is cleared).

        Atomic and conditional: nothing changes unless the post is still 'scheduled' and due, so a
        post cancelled / rescheduled / taken by another publisher meanwhile is left alone.
        Returns the number of targets marked failed.
        """
        ts = ts if ts is not None else now()
        cur = await self.execute(
            "UPDATE post_targets SET status = 'failed', error = ?, scheduled_at = NULL, queued = 0 "
            "WHERE post_id = ? AND status = 'pending' AND scheduled_at IS NOT NULL AND scheduled_at <= ? "
            "AND EXISTS (SELECT 1 FROM posts p WHERE p.id = post_targets.post_id AND p.status = 'scheduled' "
            "AND p.scheduled_at IS NOT NULL AND p.scheduled_at <= ?)",
            (error, post_id, ts, ts),
        )
        return cur.rowcount

    async def adopt_legacy_schedules(self, ts: int | None = None) -> int:
        """Due 'scheduled' posts whose time lives on the post only (V1-style scheduling, no pending
        target has its own time): copy posts.scheduled_at into their pending targets, as the
        migration does. Returns the number of targets updated."""
        ts = ts if ts is not None else now()
        cur = await self.execute(
            "UPDATE post_targets SET scheduled_at = (SELECT p.scheduled_at FROM posts p "
            "WHERE p.id = post_targets.post_id) "
            "WHERE status = 'pending' AND scheduled_at IS NULL AND post_id IN ("
            "SELECT p.id FROM posts p WHERE p.status = 'scheduled' AND p.scheduled_at IS NOT NULL "
            "AND p.scheduled_at <= ? AND NOT EXISTS (SELECT 1 FROM post_targets t WHERE t.post_id = p.id "
            "AND t.status = 'pending' AND t.scheduled_at IS NOT NULL))",
            (ts,),
        )
        return cur.rowcount

    async def fail_orphan_scheduled(self, error: str, ts: int | None = None) -> list[dict]:
        """Due 'scheduled' posts without any pending target can never be published: mark them
        failed (conditional, atomic per post) and return them. Such a post that was already sent
        somewhere just gets its status recomputed (sent / partial) and is not returned."""
        ts = ts if ts is not None else now()
        orphan_sql = (
            "status = 'scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= ? AND NOT EXISTS ("
            "SELECT 1 FROM post_targets t WHERE t.post_id = posts.id AND t.status = 'pending')"
        )
        rows = await self.fetchall(
            f"SELECT id, EXISTS (SELECT 1 FROM post_targets t WHERE t.post_id = posts.id "
            f"AND t.status = 'sent') AS has_sent FROM posts WHERE {orphan_sql}",
            (ts,),
        )
        result = []
        for row in rows:
            if row["has_sent"]:
                await self.refresh_post_schedule(row["id"])
                continue
            cur = await self.execute(
                f"UPDATE posts SET status = 'failed', error = ?, scheduled_at = NULL, updated_at = ? "
                f"WHERE id = ? AND {orphan_sql}",
                (error, now(), row["id"], ts),
            )
            if cur.rowcount:
                post = await self.get_post(row["id"])
                if post is not None:
                    result.append(post)
        return result

    async def recover_stuck_sending(self) -> list[dict]:
        """On startup: fix posts left in 'sending' by a crash / kill mid-publish (or mid-deletion).

        Only the target that was being sent — marked `sending_at` before the Telegram call — may
        already be in the channel: it becomes failed with INTERRUPTED_ERROR (never re-sent
        automatically, the owner checks the channel first). Every other pending target keeps its
        time: channels of the run that were never reached and later slots go back to the scheduler.
        The status is then recomputed: pending targets with a time → 'scheduled'; else every live
        target sent → 'sent'; some sent → 'partial'; none sent but some deleted and nothing was in
        flight (a deletion was cut) → 'deleted'; else 'failed'. sent_at / published_kind are filled
        like a normal publish would. Returns [{"id", "status", "created_by", "interrupted"}];
        interrupted = a channel may show the post without the bot knowing, or the post is left
        failed / partial (nothing will publish the rest automatically).
        """
        ts = now()
        rows = await self.fetchall("SELECT * FROM posts WHERE status = 'sending'")
        result = []
        for row in rows:
            post_id = row["id"]
            cur = await self.execute(
                "UPDATE post_targets SET status = 'failed', error = ?, scheduled_at = NULL, queued = 0, "
                "sending_at = NULL WHERE post_id = ? AND status IN ('pending', 'failed') AND sending_at IS NOT NULL",
                (INTERRUPTED_ERROR, post_id),
            )
            in_flight = cur.rowcount
            await self.execute(
                "UPDATE post_targets SET sending_at = NULL WHERE post_id = ? AND sending_at IS NOT NULL", (post_id,)
            )
            targets = await self.get_targets(post_id)
            times = [t["scheduled_at"] for t in targets if t["status"] == TARGET_PENDING and t["scheduled_at"]]
            live = [t for t in targets if t["status"] != TARGET_DELETED]
            sent = [t for t in live if t["status"] == TARGET_SENT]
            deleted = any(t["status"] == TARGET_DELETED for t in targets)
            fields: dict[str, Any] = {"scheduled_at": min(times) if times else None}
            if times:
                fields["status"] = POST_SCHEDULED
            elif live and len(sent) == len(live):
                fields["status"] = POST_SENT
            elif sent:
                fields["status"] = POST_PARTIAL
            elif deleted and not in_flight:
                fields["status"] = POST_DELETED
            else:
                fields["status"] = POST_FAILED
            interrupted = bool(in_flight) or fields["status"] in (POST_FAILED, POST_PARTIAL)
            fields["error"] = "Публикация прервана перезапуском" if interrupted else None
            if sent:
                fields["sent_at"] = max((t["sent_at"] or 0) for t in sent) or ts
                if not row.get("published_kind"):
                    fields["published_kind"] = edit_class(_post_from_row(row)["media"])
            await self.update_post(post_id, **fields)
            result.append({"id": post_id, "status": fields["status"], "created_by": row.get("created_by"),
                           "interrupted": interrupted})
        return result

    async def reset_stuck_sending(self) -> int:
        """On startup: posts left in 'sending' are recovered (see recover_stuck_sending)."""
        return len(await self.recover_stuck_sending())

    async def find_target_by_message(self, channel_id: int, message_id: int) -> dict | None:
        """Target whose first message, or any album message, is `message_id`."""
        row = await self.fetchone(
            "SELECT * FROM post_targets WHERE channel_id = ? AND (message_id = ? OR EXISTS ("
            "SELECT 1 FROM json_each(COALESCE(post_targets.message_ids_json, '[]')) WHERE value = ?))",
            (channel_id, message_id, message_id),
        )
        return _target_from_row(row) if row else None

    async def set_target_reactions(self, channel_id: int, message_id: int, total: int, reactions: list | dict) -> bool:
        cur = await self.execute(
            "UPDATE post_targets SET reactions_total = ?, reactions_json = ? "
            "WHERE channel_id = ? AND message_id = ?",
            (total, json.dumps(reactions, ensure_ascii=False), channel_id, message_id),
        )
        return cur.rowcount > 0

    # ----------------------------------------------------------------- stats
    async def add_member_snapshot(self, channel_id: int, count: int, ts: int | None = None) -> None:
        await self.execute(
            "INSERT INTO member_snapshots(channel_id, ts, count) VALUES(?, ?, ?)",
            (channel_id, ts if ts is not None else now(), count),
        )

    async def last_snapshot_times(self) -> dict[int, int]:
        """Time of the latest subscriber-count snapshot of every channel that has one."""
        rows = await self.fetchall("SELECT channel_id, MAX(ts) AS ts FROM member_snapshots GROUP BY channel_id")
        return {int(r["channel_id"]): int(r["ts"]) for r in rows}

    async def get_member_snapshots(self, channel_id: int, since_ts: int = 0) -> list[dict]:
        return await self.fetchall(
            "SELECT ts, count FROM member_snapshots WHERE channel_id = ? AND ts >= ? ORDER BY ts",
            (channel_id, since_ts),
        )

    async def get_member_count_at(self, channel_id: int, ts: int) -> int | None:
        """Last known subscriber count at or before `ts` (None if no snapshot that old)."""
        row = await self.fetchone(
            "SELECT count FROM member_snapshots WHERE channel_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (channel_id, ts),
        )
        return int(row["count"]) if row else None

    async def get_first_snapshot(self, channel_id: int) -> dict | None:
        return await self.fetchone(
            "SELECT ts, count FROM member_snapshots WHERE channel_id = ? ORDER BY ts LIMIT 1", (channel_id,)
        )

    async def add_member_event(self, channel_id: int, user_id: int | None, kind: str, ts: int | None = None) -> None:
        await self.execute(
            "INSERT INTO member_events(channel_id, user_id, ts, kind) VALUES(?, ?, ?, ?)",
            (channel_id, user_id, ts if ts is not None else now(), kind),
        )

    async def get_member_events(self, channel_id: int, since_ts: int = 0) -> list[dict]:
        return await self.fetchall(
            "SELECT user_id, ts, kind FROM member_events WHERE channel_id = ? AND ts >= ? ORDER BY ts",
            (channel_id, since_ts),
        )

    async def count_member_events(self, channel_id: int, kind: str, since_ts: int = 0) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM member_events WHERE channel_id = ? AND kind = ? AND ts >= ?",
            (channel_id, kind, since_ts),
        )
        return int(row["n"]) if row else 0

    async def add_channel_post(
        self, channel_id: int, message_id: int, via_bot: bool, ts: int | None = None,
        media_group_id: str | None = None,
    ) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO channel_posts(channel_id, message_id, ts, via_bot, media_group_id) "
            "VALUES(?, ?, ?, ?, ?)",
            (channel_id, message_id, ts if ts is not None else now(), int(via_bot), media_group_id),
        )

    async def get_channel_posts(self, channel_id: int, since_ts: int = 0) -> list[dict]:
        """Channel posts; messages of one album share `media_group_id` (count them once)."""
        return await self.fetchall(
            "SELECT message_id, ts, via_bot, media_group_id FROM channel_posts "
            "WHERE channel_id = ? AND ts >= ? ORDER BY ts",
            (channel_id, since_ts),
        )

    async def top_posts_by_reactions(self, channel_id: int, since_ts: int = 0, limit: int = 5) -> list[dict]:
        """Bot-published posts in a channel with the most reactions."""
        rows = await self.fetchall(
            "SELECT p.id AS post_id, p.text_html, p.text_md, p.photo_file_id, p.photo_path, "
            "t.message_id, t.sent_at, t.reactions_total, t.reactions_json "
            "FROM post_targets t JOIN posts p ON p.id = t.post_id "
            "WHERE t.channel_id = ? AND t.status = 'sent' AND COALESCE(t.sent_at, 0) >= ? "
            "ORDER BY t.reactions_total DESC, t.sent_at DESC LIMIT ?",
            (channel_id, since_ts, limit),
        )
        for r in rows:
            try:
                r["reactions"] = json.loads(r.pop("reactions_json") or "[]")
            except ValueError:
                r["reactions"] = []
        return rows

    async def reactions_total(self, channel_id: int, since_ts: int = 0) -> int:
        row = await self.fetchone(
            "SELECT COALESCE(SUM(reactions_total), 0) AS n FROM post_targets "
            "WHERE channel_id = ? AND status = 'sent' AND COALESCE(sent_at, 0) >= ?",
            (channel_id, since_ts),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------ migrations
    async def ensure_column(self, table: str, column: str, ddl: str) -> bool:
        """Add `column` to `table` (``ALTER TABLE ... ADD COLUMN column ddl``) if it is missing.

        Idempotent. Returns True when the column was added.
        """
        rows = await self.fetchall(f"PRAGMA table_info({table})")
        if any(r["name"] == column for r in rows):
            return False
        await self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True

    async def migrate(self) -> None:
        """Bring an existing database up to date. Called by connect(); safe to call repeatedly.

        V1.1: posts.published_kind — edit class the post was published with.
        V2:   channels.folder_id / slots_json, posts.media_json, post_targets.scheduled_at / queued /
              message_ids_json, channel_posts.media_group_id; data migration of legacy pictures
              into media_json and (once, when the column appears) of scheduled posts' time into
              their pending targets.
        V2.3: post_targets.sending_at — in-flight marker of a send (see recover_stuck_sending).
        V2.6: channels.photo_id / photo_checked_at — the channel avatar (app.services.avatars).
        """
        if getattr(self, "_migrated_conn", None) is self._conn and self._conn is not None:
            return
        await self.ensure_column("posts", "published_kind", "TEXT")
        await self.ensure_column("channels", "folder_id", "INTEGER")
        await self.ensure_column("channels", "slots_json", "TEXT")
        await self.ensure_column("posts", "media_json", "TEXT")
        v1_schedules = await self.ensure_column("post_targets", "scheduled_at", "INTEGER")
        await self.ensure_column("post_targets", "queued", "INTEGER NOT NULL DEFAULT 0")
        await self.ensure_column("post_targets", "message_ids_json", "TEXT")
        await self.ensure_column("channel_posts", "media_group_id", "TEXT")
        await self.ensure_column("channels", "photo_id", "TEXT")
        await self.ensure_column("channels", "photo_checked_at", "INTEGER")
        await self.ensure_column("post_targets", "media_sig", "TEXT")
        if await self.ensure_column("post_targets", "sending_at", "INTEGER"):
            # Posts left 'sending' by a version without the in-flight marker: any due pending target
            # may have been in flight — recover_stuck_sending must not let the scheduler re-send it.
            ts = now()
            await self.conn.execute(
                "UPDATE post_targets SET sending_at = ? WHERE status = 'pending' AND scheduled_at IS NOT NULL "
                "AND scheduled_at <= ? AND post_id IN (SELECT id FROM posts WHERE status = 'sending')",
                (ts, ts),
            )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_targets_schedule ON post_targets(status, scheduled_at)"
        )
        # Legacy single picture → media list (only rows never touched by V2 code).
        await self.conn.execute(
            "UPDATE posts SET media_json = json_array(json_object('type', 'photo', "
            "'file_id', photo_file_id, 'path', photo_path)) "
            "WHERE media_json IS NULL AND (photo_file_id IS NOT NULL OR photo_path IS NOT NULL)"
        )
        # V1 stored 'photo' for published picture posts; V2 edit classes are text | media | album:n.
        await self.conn.execute("UPDATE posts SET published_kind = 'media' WHERE published_kind = 'photo'")
        # V1 scheduled posts kept the time on the post only: a one-time conversion when the column
        # appears. Never again on later starts — a channel taken out of the plan («убрать из плана»)
        # of a still scheduled post must not get the post's time back on a restart.
        if v1_schedules:
            await self.conn.execute(
                "UPDATE post_targets SET scheduled_at = (SELECT p.scheduled_at FROM posts p "
                "WHERE p.id = post_targets.post_id) "
                "WHERE status = 'pending' AND scheduled_at IS NULL AND post_id IN ("
                "SELECT p.id FROM posts p WHERE p.status = 'scheduled' AND p.scheduled_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM post_targets t WHERE t.post_id = p.id "
                "AND t.status = 'pending' AND t.scheduled_at IS NOT NULL))"
            )
        await self.conn.commit()
        self._migrated_conn = self._conn

    # ---------------------------------------------------------------- V2: folders
    async def list_folders(self) -> list[dict]:
        """Folders in display order, each with `channels_count` (channels of any status)."""
        return await self.fetchall(
            "SELECT f.*, (SELECT COUNT(*) FROM channels c WHERE c.folder_id = f.id) AS channels_count "
            "FROM folders f ORDER BY f.position, f.id"
        )

    async def get_folder(self, folder_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM folders WHERE id = ?", (folder_id,))

    async def create_folder(self, name: str) -> int:
        row = await self.fetchone("SELECT COALESCE(MAX(position), -1) + 1 AS pos FROM folders")
        cur = await self.execute(
            "INSERT INTO folders(name, position, created_at) VALUES(?, ?, ?)", (name, row["pos"], now())
        )
        return int(cur.lastrowid)

    async def update_folder(self, folder_id: int, *, name: str | None = None, position: int | None = None) -> None:
        if name is not None:
            await self.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
        if position is not None:
            await self.execute("UPDATE folders SET position = ? WHERE id = ?", (position, folder_id))

    async def delete_folder(self, folder_id: int) -> None:
        """Delete a folder; its channels move to «Без папки»."""
        await self.conn.execute("UPDATE channels SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
        await self.conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        await self.conn.commit()

    async def set_channel_folder(self, chat_id: int, folder_id: int | None) -> None:
        await self.update_channel(chat_id, folder_id=folder_id)

    # ------------------------------------------------------------ V2: slot schedule
    async def get_channel_slots(self, chat_id: int) -> dict[int, list[str]]:
        """Weekly publishing slots {weekday 0=Mon..6: ["HH:MM", ...]} (only non-empty days)."""
        row = await self.fetchone("SELECT slots_json FROM channels WHERE id = ?", (chat_id,))
        if not row or not row["slots_json"]:
            return {}
        try:
            raw = json.loads(row["slots_json"])
        except ValueError:
            return {}
        return {int(day): list(times) for day, times in raw.items() if times}

    async def set_channel_slots(self, chat_id: int, slots: dict[int, list[str]] | None) -> None:
        """Store slots (already normalized by app.services.plan); empty/None clears the schedule."""
        clean = {str(int(day)): list(times) for day, times in (slots or {}).items() if times}
        await self.update_channel(chat_id, slots_json=json.dumps(clean) if clean else None)

    # ------------------------------------------------------- V2: per-target schedule
    async def set_target_schedule(
        self, post_id: int, channel_id: int, scheduled_at: int | None, *, queued: bool = False
    ) -> None:
        await self.update_target(post_id, channel_id, scheduled_at=scheduled_at, queued=int(bool(queued and scheduled_at)))

    async def clear_target_schedules(self, post_id: int) -> int:
        """Take every pending target of the post out of the plan (time and queued flag cleared).
        Returns how many targets changed."""
        cur = await self.execute(
            "UPDATE post_targets SET scheduled_at = NULL, queued = 0 WHERE post_id = ? AND status = 'pending' "
            "AND (scheduled_at IS NOT NULL OR queued != 0)",
            (post_id,),
        )
        return cur.rowcount

    async def get_due_targets(self, ts: int | None = None) -> list[dict]:
        """Pending targets whose time has come, of posts that are not being sent right now."""
        return await self.fetchall(
            "SELECT t.post_id, t.channel_id, t.scheduled_at FROM post_targets t "
            "JOIN posts p ON p.id = t.post_id "
            "WHERE t.status = 'pending' AND t.scheduled_at IS NOT NULL AND t.scheduled_at <= ? "
            "AND p.status IN ('scheduled', 'partial', 'failed', 'sent') "
            "ORDER BY t.scheduled_at, t.post_id",
            (ts if ts is not None else now(),),
        )

    async def get_scheduled_targets(
        self,
        *,
        channel_ids: Iterable[int] | None = None,
        from_ts: int | None = None,
        until_ts: int | None = None,
        queued_only: bool = False,
        include_sending: bool = True,
    ) -> list[dict]:
        """Pending targets with a time, joined with their post and channel, ordered by time.

        Each row: post_id, channel_id, scheduled_at, queued, post_status, text_md, media (list),
        buttons (list), channel_title, channel_username, channel_folder_id. Targets of a post being
        sent elsewhere right now are included (post_status 'sending') — their slot is still taken;
        `include_sending=False` leaves them out.
        """
        where = ["t.status = 'pending'", "t.scheduled_at IS NOT NULL"]
        if not include_sending:
            where.append("p.status != 'sending'")
        params: list[Any] = []
        if channel_ids is not None:
            ids = [int(c) for c in channel_ids]
            if not ids:
                return []
            where.append(f"t.channel_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if from_ts is not None:
            where.append("t.scheduled_at >= ?")
            params.append(from_ts)
        if until_ts is not None:
            where.append("t.scheduled_at < ?")
            params.append(until_ts)
        if queued_only:
            where.append("t.queued = 1")
        rows = await self.fetchall(
            "SELECT t.post_id, t.channel_id, t.scheduled_at, t.queued, p.status AS post_status, "
            "p.text_md, p.media_json, p.photo_file_id, p.photo_path, p.buttons_json, "
            "c.title AS channel_title, c.username AS channel_username, c.folder_id AS channel_folder_id "
            "FROM post_targets t JOIN posts p ON p.id = t.post_id "
            "LEFT JOIN channels c ON c.id = t.channel_id "
            f"WHERE {' AND '.join(where)} ORDER BY t.scheduled_at, t.post_id",
            params,
        )
        result = []
        for row in rows:
            decoded = _post_from_row({k: row[k] for k in ("media_json", "photo_file_id", "photo_path", "buttons_json")})
            row["media"] = decoded["media"]
            row["buttons"] = decoded["buttons"]
            row["queued"] = bool(row["queued"])
            for key in ("media_json", "photo_file_id", "photo_path", "buttons_json"):
                row.pop(key, None)
            result.append(row)
        return result

    async def occupied_times(self, channel_id: int, from_ts: int = 0) -> set[int]:
        """scheduled_at values already taken by pending targets of a channel."""
        rows = await self.fetchall(
            "SELECT scheduled_at FROM post_targets WHERE channel_id = ? AND status = 'pending' "
            "AND scheduled_at IS NOT NULL AND scheduled_at >= ?",
            (channel_id, from_ts),
        )
        return {int(r["scheduled_at"]) for r in rows}

    async def get_target(self, post_id: int, channel_id: int) -> dict | None:
        row = await self.fetchone(
            "SELECT * FROM post_targets WHERE post_id = ? AND channel_id = ?", (post_id, channel_id)
        )
        return _target_from_row(row) if row else None

    async def refresh_post_schedule(self, post_id: int) -> dict | None:
        """Recompute posts.status / posts.scheduled_at from the post's targets.

        * any pending target with scheduled_at → 'scheduled', scheduled_at = earliest of them
          (also when other targets are already sent — a post queued to several channels);
        * otherwise, over non-deleted targets: all sent → 'sent'; some sent → 'partial';
          none sent → 'failed' if the post was failed/partial/scheduled-and-failed and nothing is
          pending, else 'draft'.
        Posts in 'sending' or 'deleted' are never touched — also not one claimed between the read
        and the write (the write is conditional on the status read). Returns the fresh post.
        """
        post = await self.get_post(post_id)
        if post is None or post["status"] in (POST_SENDING, POST_DELETED):
            return post
        targets = post["targets"]
        times = [t["scheduled_at"] for t in targets if t["status"] == TARGET_PENDING and t["scheduled_at"]]
        if times:
            status, scheduled_at = POST_SCHEDULED, min(times)
        else:
            live = [t for t in targets if t["status"] != TARGET_DELETED]
            sent = sum(1 for t in live if t["status"] == TARGET_SENT)
            failed = sum(1 for t in live if t["status"] == TARGET_FAILED)
            pending = sum(1 for t in live if t["status"] == TARGET_PENDING)
            scheduled_at = None
            if sent and sent == len(live):
                status = POST_SENT
            elif sent:
                status = POST_PARTIAL
            elif failed and not pending and post["status"] != POST_DRAFT:
                status = POST_FAILED
            else:
                status = POST_DRAFT
        if status != post["status"] or scheduled_at != post.get("scheduled_at"):
            # Conditional: the scheduler may have claimed the post ('sending') meanwhile — writing
            # 'scheduled' back would let the next tick claim and send it a second time.
            if not await self.update_post_if(post_id, post["status"], status=status, scheduled_at=scheduled_at):
                return await self.get_post(post_id)
            post["status"], post["scheduled_at"] = status, scheduled_at
        return post


# ------------------------------------------------------------------ V2.3: premium emoji setting

async def get_premium_emoji(db: Database) -> bool:
    """SPEC_V23: whether custom («premium») emoji are published as they are (the bot has a
    username bought on Fragment). Default off: posts carry their regular fallback emoji."""
    return await db.get_premium_emoji()


async def set_premium_emoji(db: Database, enabled: bool) -> None:
    await db.set_premium_emoji(enabled)

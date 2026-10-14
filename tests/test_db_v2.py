"""Tests for the V2 parts of app.db: migration of a V1 database file, scheduling helpers used by
the publishing engine, stuck-post recovery and list ordering."""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import INTERRUPTED_ERROR, Database, now
from app.media import edit_class, media_signature

# Schema of the V1 production database (app/db.py before V2), verbatim.
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
    is_owner INTEGER NOT NULL DEFAULT 0, added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', username TEXT,
    status TEXT NOT NULL DEFAULT 'pending', can_post INTEGER NOT NULL DEFAULT 0,
    can_edit INTEGER NOT NULL DEFAULT 0, can_delete INTEGER NOT NULL DEFAULT 0,
    members_count INTEGER, added_by INTEGER, discovered_at INTEGER NOT NULL,
    added_at INTEGER, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text_md TEXT NOT NULL DEFAULT '',
    text_html TEXT NOT NULL DEFAULT '', photo_file_id TEXT, photo_path TEXT,
    buttons_json TEXT NOT NULL DEFAULT '[]', disable_preview INTEGER NOT NULL DEFAULT 0,
    silent INTEGER NOT NULL DEFAULT 0, protect INTEGER NOT NULL DEFAULT 0,
    caption_above INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft',
    scheduled_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    sent_at INTEGER, created_by INTEGER, source TEXT NOT NULL DEFAULT 'bot', error TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, scheduled_at);
CREATE TABLE IF NOT EXISTS post_targets (
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE, channel_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER, error TEXT, sent_at INTEGER,
    reactions_total INTEGER NOT NULL DEFAULT 0, reactions_json TEXT,
    PRIMARY KEY (post_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_targets_msg ON post_targets(channel_id, message_id);
CREATE TABLE IF NOT EXISTS member_snapshots (channel_id INTEGER NOT NULL, ts INTEGER NOT NULL, count INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_snapshots ON member_snapshots(channel_id, ts);
CREATE TABLE IF NOT EXISTS member_events (channel_id INTEGER NOT NULL, user_id INTEGER, ts INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_member_events ON member_events(channel_id, ts);
CREATE TABLE IF NOT EXISTS channel_posts (
    channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, ts INTEGER NOT NULL,
    via_bot INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_posts_ts ON channel_posts(channel_id, ts);
"""

CH_A = -1001000000001
CH_B = -1001000000002
CH_C = -1001000000003
PIC = "0123456789abcdef0123456789abcdef.jpg"


def build_v1_db(path, *, with_published_kind: bool) -> dict:
    """A V1 database file with a scheduled post, a sent photo post and a draft."""
    t = 1_760_000_000
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    if with_published_kind:
        conn.execute("ALTER TABLE posts ADD COLUMN published_kind TEXT")
    for chat_id, title in ((CH_A, "Alpha"), (CH_B, "Beta")):
        conn.execute("INSERT INTO channels(id, title, status, can_post, discovered_at, updated_at) "
                     "VALUES(?, ?, 'active', 1, ?, ?)", (chat_id, title, t, t))
    ids = {}
    cur = conn.execute("INSERT INTO posts(text_md, status, scheduled_at, created_at, updated_at, created_by) "
                       "VALUES('Запланирован', 'scheduled', ?, ?, ?, 1)", (t + 3600, t, t))
    ids["scheduled"] = cur.lastrowid
    conn.execute("INSERT INTO post_targets(post_id, channel_id) VALUES(?, ?)", (ids["scheduled"], CH_A))
    conn.execute("INSERT INTO post_targets(post_id, channel_id) VALUES(?, ?)", (ids["scheduled"], CH_B))
    cur = conn.execute("INSERT INTO posts(text_md, photo_file_id, photo_path, status, created_at, updated_at, sent_at) "
                       "VALUES('С картинкой', 'AgACphoto', ?, 'sent', ?, ?, ?)", (PIC, t, t, t))
    ids["photo"] = cur.lastrowid
    conn.execute("INSERT INTO post_targets(post_id, channel_id, status, message_id, sent_at) "
                 "VALUES(?, ?, 'sent', 77, ?)", (ids["photo"], CH_A, t))
    if with_published_kind:
        conn.execute("UPDATE posts SET published_kind = 'photo' WHERE id = ?", (ids["photo"],))
    cur = conn.execute("INSERT INTO posts(text_md, status, created_at, updated_at) VALUES('Черновик', 'draft', ?, ?)",
                       (t, t))
    ids["draft"] = cur.lastrowid
    conn.execute("INSERT INTO post_targets(post_id, channel_id) VALUES(?, ?)", (ids["draft"], CH_A))
    conn.commit()
    conn.close()
    ids["t"] = t
    return ids


@pytest.mark.parametrize("with_published_kind", [False, True])
async def test_migrate_v1_database_file(tmp_path, with_published_kind):
    path = tmp_path / "v1.db"
    ids = build_v1_db(path, with_published_kind=with_published_kind)

    database = Database(path)
    await database.connect()
    try:
        columns = {r["name"] for r in await database.fetchall("PRAGMA table_info(post_targets)")}
        assert {"scheduled_at", "queued", "message_ids_json", "media_sig"} <= columns
        columns = {r["name"] for r in await database.fetchall("PRAGMA table_info(posts)")}
        assert {"media_json", "published_kind"} <= columns
        columns = {r["name"] for r in await database.fetchall("PRAGMA table_info(channels)")}
        assert {"folder_id", "slots_json"} <= columns
        assert await database.list_folders() == []

        scheduled = await database.get_post(ids["scheduled"])
        assert scheduled["status"] == "scheduled" and scheduled["media"] == []
        assert {t["channel_id"]: t["scheduled_at"] for t in scheduled["targets"]} == {
            CH_A: ids["t"] + 3600, CH_B: ids["t"] + 3600}
        assert all(t["queued"] is False and t["message_ids"] == [] for t in scheduled["targets"])

        photo = await database.get_post(ids["photo"])
        assert photo["media"] == [{"type": "photo", "file_id": "AgACphoto", "path": PIC}]
        row = await database.fetchone("SELECT media_json FROM posts WHERE id = ?", (ids["photo"],))
        assert json.loads(row["media_json"]) == photo["media"]
        assert photo["targets"][0]["scheduled_at"] is None and photo["targets"][0]["message_ids"] == [77]
        assert photo["targets"][0]["media_sig"] is None  # legacy: edits rely on media_changed
        assert photo["published_kind"] == ("media" if with_published_kind else None)

        draft = await database.get_post(ids["draft"])
        assert draft["targets"][0]["scheduled_at"] is None

        due = await database.get_due_targets(ids["t"] + 3600)
        assert [(r["post_id"], r["channel_id"]) for r in due] == [(ids["scheduled"], CH_A), (ids["scheduled"], CH_B)]
    finally:
        await database.close()

    again = Database(path)  # second start: migration is idempotent
    await again.connect()
    try:
        await again.migrate()
        photo = await again.get_post(ids["photo"])
        assert photo["media"] == [{"type": "photo", "file_id": "AgACphoto", "path": PIC}]
        assert len(await again.get_targets(ids["scheduled"])) == 2
    finally:
        await again.close()


async def test_migration_keeps_v2_media(tmp_path):
    path = tmp_path / "v2.db"
    database = Database(path)
    await database.connect()
    post_id = await database.create_post(text_md="x", media=[{"type": "video", "file_id": "V", "path": None}])
    await database.close()
    database = Database(path)
    await database.connect()
    try:
        assert (await database.get_post(post_id))["media"] == [{"type": "video", "file_id": "V", "path": None}]
    finally:
        await database.close()


# ------------------------------------------------------------------ media columns

async def test_media_alias_mirrors_legacy_columns(db):
    post_id = await db.create_post(text_md="x", media=[{"type": "photo", "file_id": "F", "path": PIC}])
    post = await db.get_post(post_id)
    assert (post["photo_file_id"], post["photo_path"]) == ("F", PIC)
    await db.update_post(post_id, media=[{"type": "video", "file_id": "V", "path": None}])
    post = await db.get_post(post_id)
    assert (post["photo_file_id"], post["photo_path"]) == (None, None)
    assert post["media"][0]["type"] == "video"
    await db.update_post(post_id, media=[])
    assert (await db.get_post(post_id))["media"] == []


async def test_legacy_photo_update_rewrites_media(db):
    post_id = await db.create_post(text_md="x", media=[{"type": "photo", "file_id": "OLD", "path": None}])
    await db.update_post(post_id, photo_file_id="NEW", photo_path=None)
    assert (await db.get_post(post_id))["media"] == [{"type": "photo", "file_id": "NEW", "path": None}]
    await db.update_post(post_id, photo_path=PIC)  # partial update keeps the other column
    assert (await db.get_post(post_id))["media"] == [{"type": "photo", "file_id": "NEW", "path": PIC}]
    await db.update_post(post_id, photo_file_id=None, photo_path=None)
    assert (await db.get_post(post_id))["media"] == []
    await db.update_post(post_id, text_md="только текст")  # untouched without picture columns
    assert (await db.get_post(post_id))["media"] == []


# ------------------------------------------------------------------ claim & due helpers

async def make_scheduled(db, when, channels=(CH_A,), *, status_override=None) -> int:
    post_id = await db.create_post(text_md="x")
    await db.set_post_targets(post_id, channels)
    for channel_id in channels:
        await db.set_target_schedule(post_id, channel_id, when, queued=True)
    await db.refresh_post_schedule(post_id)
    if status_override:
        await db.update_post(post_id, status=status_override)
    return post_id


async def test_claim_with_due_before(db):
    t = now()
    post_id = await make_scheduled(db, t + 100)
    assert await db.claim_post_for_sending(post_id, ("scheduled",), due_before=t) is False
    assert (await db.get_post(post_id))["status"] == "scheduled"
    assert await db.claim_post_for_sending(post_id, ("scheduled",), due_before=t + 100) is True
    assert (await db.get_post(post_id))["status"] == "sending"
    draft = await db.create_post(text_md="d")
    assert await db.claim_post_for_sending(draft, ("scheduled",), due_before=t) is False
    assert await db.claim_post_for_sending(draft) is True  # manual path unchanged


async def test_fail_due_targets_is_conditional(db):
    t = now()
    post_id = await db.create_post(text_md="x")
    await db.set_post_targets(post_id, [CH_A, CH_B])
    await db.set_target_schedule(post_id, CH_A, t - 10)
    await db.set_target_schedule(post_id, CH_B, t + 3600)
    await db.refresh_post_schedule(post_id)

    assert await db.fail_due_targets(post_id, "Ошибка", t) == 1
    targets = {x["channel_id"]: x for x in await db.get_targets(post_id)}
    assert targets[CH_A]["status"] == "failed" and targets[CH_A]["error"] == "Ошибка"
    assert targets[CH_A]["scheduled_at"] is None and targets[CH_B]["status"] == "pending"

    moved = await make_scheduled(db, t - 10)
    await db.update_post(moved, scheduled_at=t + 50)  # moved: the post is no longer due
    assert await db.fail_due_targets(moved, "Ошибка", t) == 0
    cancelled = await make_scheduled(db, t - 10, status_override="draft")
    assert await db.fail_due_targets(cancelled, "Ошибка", t) == 0
    taken = await make_scheduled(db, t - 10, status_override="sending")
    assert await db.fail_due_targets(taken, "Ошибка", t) == 0


async def test_adopt_legacy_schedules(db):
    t = now()
    legacy = await db.create_post(text_md="x", status="scheduled", scheduled_at=t - 5)
    await db.set_post_targets(legacy, [CH_A, CH_B])
    future = await db.create_post(text_md="x", status="scheduled", scheduled_at=t + 500)
    await db.set_post_targets(future, [CH_A])
    v2 = await db.create_post(text_md="x")
    await db.set_post_targets(v2, [CH_A, CH_B])
    await db.set_target_schedule(v2, CH_A, t - 5)  # B intentionally without time
    await db.refresh_post_schedule(v2)

    assert await db.adopt_legacy_schedules(t) == 2
    assert {x["scheduled_at"] for x in await db.get_targets(legacy)} == {t - 5}
    assert (await db.get_targets(future))[0]["scheduled_at"] is None
    assert {x["channel_id"]: x["scheduled_at"] for x in await db.get_targets(v2)} == {CH_A: t - 5, CH_B: None}
    assert await db.adopt_legacy_schedules(t) == 0


async def test_fail_orphan_scheduled(db):
    t = now()
    orphan = await db.create_post(text_md="x", status="scheduled", scheduled_at=t - 5)
    sent_elsewhere = await db.create_post(text_md="x", status="scheduled", scheduled_at=t - 5)
    await db.set_post_targets(sent_elsewhere, [CH_A])
    await db.update_target(sent_elsewhere, CH_A, status="sent", message_id=3)
    not_due = await db.create_post(text_md="x", status="scheduled", scheduled_at=t + 50)

    failed = await db.fail_orphan_scheduled("Нет каналов", t)

    assert [p["id"] for p in failed] == [orphan]
    post = await db.get_post(orphan)
    assert post["status"] == "failed" and post["error"] == "Нет каналов" and post["scheduled_at"] is None
    assert (await db.get_post(sent_elsewhere))["status"] == "sent"
    assert (await db.get_post(not_due))["status"] == "scheduled"
    assert await db.fail_orphan_scheduled("Нет каналов", t) == []


# ------------------------------------------------------------------ stuck 'sending' (review #23)

async def stuck_post(db, statuses: dict[int, str], **fields) -> int:
    post_id = await db.create_post(text_md="x", created_by=7, **fields)
    await db.set_post_targets(post_id, list(statuses))
    for channel_id, status in statuses.items():
        if status == "sent":
            await db.update_target(post_id, channel_id, status="sent", message_id=10, sent_at=1000 + channel_id % 10)
        elif status != "pending":
            await db.update_target(post_id, channel_id, status=status)
    await db.execute("UPDATE posts SET status = 'sending' WHERE id = ?", (post_id,))
    return post_id


async def test_recover_fully_sent_post_becomes_sent(db):
    """Regression (review #23): a crash after the last channel was sent no longer yields 'partial'."""
    post_id = await stuck_post(db, {CH_A: "sent", CH_B: "sent"},
                               media=[{"type": "photo", "file_id": "F", "path": None}])
    result = await db.recover_stuck_sending()
    assert result == [{"id": post_id, "status": "sent", "created_by": 7, "interrupted": False}]
    post = await db.get_post(post_id)
    assert post["status"] == "sent" and post["error"] is None
    assert post["sent_at"] == max(t["sent_at"] for t in post["targets"])
    assert post["published_kind"] == "media" == edit_class(post["media"])


async def test_recover_partial_failed_and_deleted_targets(db):
    partial = await stuck_post(db, {CH_A: "sent", CH_B: "pending"})
    failed = await stuck_post(db, {CH_A: "failed", CH_B: "pending"})
    deleted_rest = await stuck_post(db, {CH_A: "sent", CH_B: "deleted"})
    assert await db.reset_stuck_sending() == 3
    post = await db.get_post(partial)
    assert post["status"] == "partial" and post["error"] == "Публикация прервана перезапуском"
    assert post["published_kind"] == "text" and post["sent_at"]
    post = await db.get_post(failed)
    assert post["status"] == "failed" and post["sent_at"] is None and post["published_kind"] is None
    assert (await db.get_post(deleted_rest))["status"] == "sent"
    assert await db.reset_stuck_sending() == 0


async def test_recover_scheduled_post_never_resends_the_target_in_flight(db):
    t = now()
    post_id = await stuck_post(db, {CH_A: "pending", CH_B: "pending"})
    await db.set_target_schedule(post_id, CH_A, t - 30, queued=True)  # was being sent by the scheduler
    await db.update_target(post_id, CH_A, sending_at=t - 29)          # …the in-flight marker
    await db.set_target_schedule(post_id, CH_B, t + 3600, queued=True)  # still waiting for its time
    result = await db.recover_stuck_sending()
    assert result[0]["status"] == "scheduled" and result[0]["interrupted"] is True
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["scheduled_at"] == t + 3600
    targets = {x["channel_id"]: x for x in post["targets"]}
    assert targets[CH_A]["status"] == "failed" and targets[CH_A]["error"] == INTERRUPTED_ERROR
    assert targets[CH_A]["scheduled_at"] is None and targets[CH_A]["queued"] is False
    assert targets[CH_A]["sending_at"] is None
    assert targets[CH_B]["scheduled_at"] == t + 3600
    assert await db.get_due_targets(t + 60) == []


async def test_recover_keeps_due_targets_that_were_never_attempted(db):
    """Regression (review2 #6): only the channel in flight is failed; due channels of the run that
    were never reached (and slots that came later) keep their time and go back to the scheduler."""
    t = now()
    post_id = await stuck_post(db, {CH_A: "pending", CH_B: "pending", CH_C: "pending"})
    for channel_id in (CH_A, CH_B):
        await db.set_target_schedule(post_id, channel_id, t - 3600, queued=True)
    await db.set_target_schedule(post_id, CH_C, t - 600, queued=True)  # its slot came after the claim
    await db.update_target(post_id, CH_A, sending_at=t - 3599)
    result = await db.recover_stuck_sending()
    assert result == [{"id": post_id, "status": "scheduled", "created_by": 7, "interrupted": True}]
    targets = {x["channel_id"]: x for x in await db.get_targets(post_id)}
    assert targets[CH_A]["status"] == "failed" and targets[CH_A]["error"] == INTERRUPTED_ERROR
    assert (targets[CH_B]["status"], targets[CH_B]["scheduled_at"], targets[CH_B]["error"]) == (
        "pending", t - 3600, None)
    assert targets[CH_C]["scheduled_at"] == t - 600
    due = await db.get_due_targets(t)
    assert sorted(r["channel_id"] for r in due) == sorted([CH_B, CH_C])


async def test_recover_claimed_but_nothing_in_flight_is_silent(db):
    """Killed between the claim and the first send: nothing may be in a channel — the post just
    goes back to the scheduler, no «прервана» notice."""
    t = now()
    post_id = await stuck_post(db, {CH_A: "pending"})
    await db.set_target_schedule(post_id, CH_A, t - 5, queued=True)
    result = await db.recover_stuck_sending()
    assert result == [{"id": post_id, "status": "scheduled", "created_by": 7, "interrupted": False}]
    assert (await db.get_post(post_id))["error"] is None
    assert [r["channel_id"] for r in await db.get_due_targets(t)] == [CH_A]


async def test_recover_cut_deletion_becomes_deleted(db):
    post_id = await stuck_post(db, {CH_A: "deleted", CH_B: "deleted"})
    result = await db.recover_stuck_sending()
    assert result == [{"id": post_id, "status": "deleted", "created_by": 7, "interrupted": False}]


async def test_recover_failed_target_retried_when_cut(db):
    """A failed target re-sent by «Повторить» and cut mid-send gets the «проверьте» error."""
    post_id = await stuck_post(db, {CH_A: "sent", CH_B: "failed"})
    await db.update_target(post_id, CH_B, error="Сетевая ошибка: timeout", sending_at=now())
    result = await db.recover_stuck_sending()
    assert result[0]["status"] == "partial" and result[0]["interrupted"] is True
    target = await db.get_target(post_id, CH_B)
    assert target["status"] == "failed" and target["error"] == INTERRUPTED_ERROR


async def test_migration_marks_due_targets_of_posts_stuck_before_the_marker(tmp_path):
    """A post left 'sending' by a version without `sending_at` (V2.2): its due pending targets may
    have been in flight — after the upgrade they are not re-sent automatically (old semantics),
    later slots keep their time."""
    path = tmp_path / "v22.db"
    t = now()
    database = Database(path)
    await database.connect()
    post_id = await database.create_post(text_md="x", created_by=7)
    await database.set_post_targets(post_id, [CH_A, CH_B])
    await database.set_target_schedule(post_id, CH_A, t - 60, queued=True)
    await database.set_target_schedule(post_id, CH_B, t + 3600, queued=True)
    await database.update_post(post_id, status="sending")
    await database.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE post_targets DROP COLUMN sending_at")  # the V2.2 schema
    conn.commit()
    conn.close()

    database = Database(path)
    await database.connect()
    try:
        recovered = await database.recover_stuck_sending()
        assert recovered == [{"id": post_id, "status": "scheduled", "created_by": 7, "interrupted": True}]
        targets = {x["channel_id"]: x for x in await database.get_targets(post_id)}
        assert targets[CH_A]["status"] == "failed" and targets[CH_A]["error"] == INTERRUPTED_ERROR
        assert targets[CH_B]["status"] == "pending" and targets[CH_B]["scheduled_at"] == t + 3600
        assert await database.get_due_targets(t + 60) == []
    finally:
        await database.close()


async def test_recovered_partial_post_can_be_retried_or_is_complete(db):
    """The old bug: a fully sent post became 'partial' and «Повторить» always failed."""
    post_id = await stuck_post(db, {CH_A: "sent"})
    await db.reset_stuck_sending()
    post = await db.get_post(post_id)
    assert post["status"] == "sent"
    assert [t for t in post["targets"] if t["status"] in ("pending", "failed")] == []


# ------------------------------------------------------------------ list ordering (review #27)

async def test_list_all_posts_newest_first_with_fresh_scheduled_post(db):
    t = now()
    for i in range(5):
        await db.create_post(text_md=f"draft{i}", created_at=t - 1000 + i, updated_at=t - 1000 + i)
    for i in range(2):
        await db.create_post(text_md=f"sent{i}", status="sent", sent_at=t - 500 + i, created_at=t - 2000,
                             updated_at=t - 2000)
    fresh = await db.create_post(text_md="scheduled_new", status="scheduled", scheduled_at=t + 3600)

    everything = await db.list_posts(None, limit=20)
    assert everything[0]["id"] == fresh
    assert [p["text_md"] for p in everything] == [
        "scheduled_new", "sent1", "sent0", "draft4", "draft3", "draft2", "draft1", "draft0"]
    assert fresh in [p["id"] for p in await db.list_posts(None, limit=5)]  # overview «Последние посты»


async def test_list_scheduled_tab_soonest_first(db):
    t = now()
    late = await db.create_post(text_md="late", status="scheduled", scheduled_at=t + 900)
    soon = await db.create_post(text_md="soon", status="scheduled", scheduled_at=t + 100)
    mid = await db.create_post(text_md="mid", status="scheduled", scheduled_at=t + 500)
    assert [p["id"] for p in await db.list_posts("scheduled")] == [soon, mid, late]
    assert [p["id"] for p in await db.list_posts(["scheduled"], limit=2)] == [soon, mid]


async def test_list_mixed_tabs_by_recency(db):
    a = await db.create_post(text_md="a", status="sent", sent_at=100)
    b = await db.create_post(text_md="b", status="partial", sent_at=300)
    c = await db.create_post(text_md="c", status="deleted", sent_at=200)
    assert [p["id"] for p in await db.list_posts(("sent", "partial", "deleted"))] == [b, c, a]


# ------------------------------------------------------------------ misc V2 rows

async def test_target_rows_decode_album_ids_and_signature(db):
    post_id = await db.create_post(text_md="x")
    await db.set_post_targets(post_id, [CH_A])
    sig = media_signature([{"type": "photo", "file_id": "A"}])
    await db.update_target(post_id, CH_A, status="sent", message_id=5, message_ids_json=json.dumps([5, 6, 7]),
                           media_sig=sig)
    target = await db.get_target(post_id, CH_A)
    assert target["message_ids"] == [5, 6, 7] and target["media_sig"] == sig
    assert (await db.find_target_by_message(CH_A, 7))["post_id"] == post_id
    assert await db.find_target_by_message(CH_A, 8) is None


async def test_set_post_targets_empty_list_removes_unsent_targets(db):
    await db.upsert_channel(-1001, "A", "a", status="active")
    await db.upsert_channel(-1002, "B", "b", status="active")
    post_id = await db.create_post(text_md="x", status="draft")
    await db.set_post_targets(post_id, [-1001, -1002])
    await db.update_target(post_id, -1002, status="sent", message_id=7)
    await db.set_post_targets(post_id, [])
    targets = {t["channel_id"]: t["status"] for t in await db.get_targets(post_id)}
    assert targets == {-1002: "sent"}


# ------------------------------------------------------------------ review2: guards against 'sending'

async def test_delete_post_record_refuses_a_post_being_sent_or_shown(db):
    """Regression (review2 #3): the scheduler claimed the post after the caller read it — the
    record stays, so the published message can still be managed."""
    sending = await db.create_post(text_md="x")
    await db.set_post_targets(sending, [CH_A])
    await db.update_post(sending, status="sending")
    assert await db.delete_post_record(sending) is False
    assert (await db.get_post(sending))["targets"][0]["channel_id"] == CH_A

    shown = await db.create_post(text_md="x", status="deleted")
    await db.execute("INSERT INTO post_targets(post_id, channel_id, status, message_id) VALUES(?, ?, 'sent', 5)",
                     (shown, CH_A))
    assert await db.delete_post_record(shown) is False  # a 'deleted' post still shown somewhere
    queued = await db.create_post(text_md="x")
    await db.set_post_targets(queued, [CH_A, CH_B])
    await db.update_target(queued, CH_A, status="sent", message_id=7)
    await db.update_post(queued, status="scheduled")
    assert await db.delete_post_record(queued) is False

    draft = await db.create_post(text_md="x")
    await db.set_post_targets(draft, [CH_A])
    assert await db.delete_post_record(draft) is True
    assert await db.get_post(draft) is None
    assert await db.fetchall("SELECT * FROM post_targets WHERE post_id = ?", (draft,)) == []
    assert await db.delete_post_record(draft) is False


async def test_set_post_targets_leaves_a_post_being_sent_alone(db):
    """Regression (review2 #3): unticking the in-flight channel of a claimed post does not delete
    its target (the send would then leave no record)."""
    post_id = await db.create_post(text_md="x")
    assert await db.set_post_targets(post_id, [CH_A, CH_B]) is True
    await db.update_target(post_id, CH_B, status="failed", error="Нет прав")
    await db.update_post(post_id, status="sending")
    assert await db.set_post_targets(post_id, [CH_B, CH_C]) is False
    assert await db.set_post_targets(post_id, []) is False
    targets = {t["channel_id"]: (t["status"], t["error"]) for t in await db.get_targets(post_id)}
    assert targets == {CH_A: ("pending", None), CH_B: ("failed", "Нет прав")}
    await db.update_post(post_id, status="deleted")
    assert await db.set_post_targets(post_id, [CH_C]) is False
    assert await db.set_post_targets(999, [CH_A]) is False
    await db.update_post(post_id, status="draft")
    assert await db.set_post_targets(post_id, [CH_B, CH_C]) is True
    targets = {t["channel_id"]: (t["status"], t["error"]) for t in await db.get_targets(post_id)}
    assert targets == {CH_B: ("pending", None), CH_C: ("pending", None)}


async def test_restart_does_not_put_unqueued_channels_back_into_the_plan(tmp_path):
    """Regression (review2 #4): the V1 time conversion runs once — «убрать из плана» for one
    channel of a scheduled post survives a restart."""
    path = tmp_path / "bot.db"
    t = now()
    database = Database(path)
    await database.connect()
    post_id = await database.create_post(text_md="x")
    await database.set_post_targets(post_id, [CH_A, CH_B])
    for channel_id in (CH_A, CH_B):
        await database.set_target_schedule(post_id, channel_id, t + 3600, queued=True)
    await database.refresh_post_schedule(post_id)
    await database.set_target_schedule(post_id, CH_B, None)  # plan.unqueue_target
    await database.refresh_post_schedule(post_id)
    await database.close()

    database = Database(path)
    await database.connect()
    try:
        targets = {x["channel_id"]: x["scheduled_at"] for x in await database.get_targets(post_id)}
        assert targets == {CH_A: t + 3600, CH_B: None}
        assert await database.get_due_targets(t + 7200) == [
            {"post_id": post_id, "channel_id": CH_A, "scheduled_at": t + 3600}]
    finally:
        await database.close()


async def test_scheduled_targets_include_posts_being_sent(db):
    """Regression (review2 #9): a post being sent to one channel keeps its slot in the others."""
    t = now()
    post_id = await db.create_post(text_md="x")
    await db.set_post_targets(post_id, [CH_A, CH_B])
    await db.set_target_schedule(post_id, CH_A, t - 5, queued=True)
    await db.set_target_schedule(post_id, CH_B, t + 3600, queued=True)
    await db.refresh_post_schedule(post_id)
    assert await db.claim_post_for_sending(post_id, ("scheduled",), due_before=t)
    rows = await db.get_scheduled_targets(channel_ids=[CH_B])
    assert [(r["post_id"], r["scheduled_at"], r["post_status"]) for r in rows] == [(post_id, t + 3600, "sending")]
    assert await db.get_scheduled_targets(channel_ids=[CH_B], include_sending=False) == []


async def test_update_post_if_and_clear_target_schedules(db):
    post_id = await db.create_post(text_md="x")
    await db.set_post_targets(post_id, [CH_A, CH_B])
    await db.set_target_schedule(post_id, CH_A, now() + 60, queued=True)
    assert await db.clear_target_schedules(post_id) == 1
    assert {(x["scheduled_at"], x["queued"]) for x in await db.get_targets(post_id)} == {(None, False)}
    assert await db.update_post_if(post_id, "sending", status="sent") is False
    assert await db.update_post_if(post_id, "draft", status="failed", error="e") is True
    post = await db.get_post(post_id)
    assert (post["status"], post["error"]) == ("failed", "e")


async def test_premium_emoji_setting(db):
    from app.db import PREMIUM_EMOJI_SETTING, get_premium_emoji, set_premium_emoji

    assert await get_premium_emoji(db) is False  # default: off
    await set_premium_emoji(db, True)
    assert await get_premium_emoji(db) is True and await db.get_setting(PREMIUM_EMOJI_SETTING) == "1"
    await db.set_premium_emoji(False)
    assert await db.get_premium_emoji() is False and await db.get_setting(PREMIUM_EMOJI_SETTING) is None


async def test_refresh_post_schedule_never_overwrites_a_claim(db, monkeypatch):
    """The status is recomputed from a read: a claim landing between the read and the write must
    survive — writing 'scheduled' back would let the next tick claim and send the post again."""
    t = now()
    post_id = await db.create_post(text_md="x")
    await db.set_post_targets(post_id, [CH_A, CH_B])
    await db.set_target_schedule(post_id, CH_A, t - 10, queued=True)
    await db.set_target_schedule(post_id, CH_B, t - 5, queued=True)
    await db.refresh_post_schedule(post_id)
    await db.set_target_schedule(post_id, CH_A, None)  # the earliest channel is taken out of the plan
    original = db.get_post
    claims = []

    async def claimed_after_read(pid):
        post = await original(pid)
        if not claims:  # the scheduler's tick lands right after the read
            claims.append(await db.claim_post_for_sending(pid, ("scheduled",), due_before=t))  # B is due
        return post

    monkeypatch.setattr(db, "get_post", claimed_after_read)
    fresh = await db.refresh_post_schedule(post_id)
    monkeypatch.undo()
    assert claims == [True]
    assert fresh["status"] == "sending"
    assert (await db.get_post(post_id))["status"] == "sending"

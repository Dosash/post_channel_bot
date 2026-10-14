"""Retried publishing requests never publish twice (review finding #22, ``X-Idempotency-Key``)."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.web import create_web_app, idempotency
from app.web.idempotency import IDEMPOTENCY_HEADER, IdempotencyStore
from app.web.keys import IDEMPOTENCY_KEY
from tests.fakes import bad_request

H = {"X-Requested-With": "pcb"}
CHANNEL = -1001111111111
CHANNEL2 = -1002222222222
KEY = "4f9c1e8a-7b2d-4c3e-9a10-0123456789ab"


def _h(key: str = KEY) -> dict[str, str]:
    return {**H, IDEMPOTENCY_HEADER: key}


@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    bot.add_chat(CHANNEL, "Первый", "first_chan")
    bot.add_chat(CHANNEL2, "Второй", None)
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        for ref in (CHANNEL, CHANNEL2):
            resp = await c.post("/api/channels", json={"ref": str(ref)}, headers=H)
            assert resp.status == 200, await resp.text()
        yield c


def _slow_sends(bot) -> tuple[asyncio.Event, asyncio.Event]:
    """send_message waits for `release`; `started` is set when the first send began."""
    started, release = asyncio.Event(), asyncio.Event()
    original = bot.send_message

    async def send_message(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    bot.send_message = send_message
    return started, release


async def _post_count(db) -> int:
    return (await db.fetchone("SELECT COUNT(*) AS n FROM posts"))["n"]


def _sent_to(bot) -> list[int]:
    return [call["chat_id"] for call in bot.calls_of("send_message")]


# ------------------------------------------------------------------ POST /api/posts

async def test_retry_while_the_first_publish_runs_publishes_once(client, db, bot):
    """The proxy gave up on the first request (the server keeps publishing); the panel retries."""
    started, release = _slow_sends(bot)
    body = {"text_md": "Новость", "channel_ids": [CHANNEL, CHANNEL2], "action": "publish"}
    try:
        first = asyncio.create_task(client.post("/api/posts", json=body, headers=_h()))
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(client.post("/api/posts", json=body, headers=_h()))
        await asyncio.sleep(0.05)
        assert not second.done()  # waits for the first request instead of publishing again
    finally:
        release.set()
    r1, r2 = await asyncio.wait_for(first, 5), await asyncio.wait_for(second, 5)
    d1, d2 = await r1.json(), await r2.json()
    assert r1.status == 200 and r2.status == 200
    assert d2["replayed"] is True and "replayed" not in d1
    assert d1["post"]["id"] == d2["post"]["id"] and d2["post"]["status"] == "sent"
    assert d2["report"] == d1["report"]
    assert await _post_count(db) == 1
    assert sorted(_sent_to(bot)) == sorted([CHANNEL, CHANNEL2])  # one message per channel


async def test_retry_after_the_publish_finished_is_replayed(client, db, bot):
    body = {"text_md": "Раз", "channel_ids": [CHANNEL], "action": "publish"}
    r1 = await client.post("/api/posts", json=body, headers=_h())
    d1 = await r1.json()
    await db.update_post(d1["post"]["id"], text_md="Раз (исправлено в боте)")
    r2 = await client.post("/api/posts", json=body, headers=_h())
    d2 = await r2.json()
    assert r2.status == 200 and d2["replayed"] is True
    assert d2["post"]["id"] == d1["post"]["id"] and d2["post"]["text_md"] == "Раз (исправлено в боте)"  # current
    assert await _post_count(db) == 1 and _sent_to(bot) == [CHANNEL]

    # another key is another action: a new post
    r3 = await client.post("/api/posts", json=body, headers=_h("another-key-0001"))
    assert r3.status == 200 and (await r3.json())["post"]["id"] != d1["post"]["id"]
    assert await _post_count(db) == 2


async def test_without_the_header_nothing_changes(client, db, bot):
    body = {"text_md": "x", "channel_ids": [CHANNEL], "action": "publish"}
    for _ in range(2):
        resp = await client.post("/api/posts", json=body, headers=H)
        assert resp.status == 200 and "replayed" not in await resp.json()
    assert await _post_count(db) == 2


async def test_rejected_request_does_not_burn_the_key(client, db):
    resp = await client.post("/api/posts", json={"text_md": "", "channel_ids": [CHANNEL], "action": "publish"},
                             headers=_h())
    assert resp.status == 400 and "post" not in await resp.json()
    resp = await client.post("/api/posts", json={"text_md": "Исправил", "channel_ids": [CHANNEL],
                                                 "action": "publish"}, headers=_h())
    data = await resp.json()
    assert resp.status == 200 and "replayed" not in data and data["post"]["status"] == "sent"


async def test_kept_draft_is_replayed_not_duplicated(client, db, bot, monkeypatch):
    """Publishing refused after the draft was created (400 with the post): the retry gets that
    draft instead of creating another one."""
    from app.services.posting import PostError
    from app.web import api

    async def refuse(*args, **kwargs):
        raise PostError("Медиа загружено через другого бота — загрузите файл заново")

    monkeypatch.setattr(api, "publish_post", refuse)
    body = {"text_md": "x", "channel_ids": [CHANNEL], "action": "publish"}
    r1 = await client.post("/api/posts", json=body, headers=_h())
    d1 = await r1.json()
    assert r1.status == 400 and d1["post"]["status"] == "draft"
    r2 = await client.post("/api/posts", json=body, headers=_h())
    d2 = await r2.json()
    assert r2.status == 400 and d2["replayed"] is True and d2["error"] == d1["error"]
    assert d2["post"]["id"] == d1["post"]["id"]
    assert await _post_count(db) == 1


async def test_failed_channels_are_part_of_the_replayed_answer(client, db, bot):
    bot.fail_on["send_message"] = bad_request("Bad Request: chat not found")
    body = {"text_md": "x", "channel_ids": [CHANNEL], "action": "publish"}
    d1 = await (await client.post("/api/posts", json=body, headers=_h())).json()
    d2 = await (await client.post("/api/posts", json=body, headers=_h())).json()
    assert d1["post"]["status"] == "failed" and d2["replayed"] is True and d2["report"] == d1["report"]
    assert await _post_count(db) == 1 and len(_sent_to(bot)) == 1


async def test_still_running_after_the_wait_is_409_with_the_post(client, db, bot, monkeypatch):
    monkeypatch.setattr(idempotency, "IDEMPOTENCY_WAIT", 0.05)
    started, release = _slow_sends(bot)
    body = {"text_md": "Долго", "channel_ids": [CHANNEL], "action": "publish"}
    first = asyncio.create_task(client.post("/api/posts", json=body, headers=_h()))
    try:
        await asyncio.wait_for(started.wait(), 5)
        r2 = await asyncio.wait_for(client.post("/api/posts", json=body, headers=_h()), 2)
        d2 = await r2.json()
    finally:
        release.set()
    assert r2.status == 409 and d2["replayed"] is True
    assert d2["error"] == "Пост сейчас публикуется — подождите и обновите страницу"
    assert d2["post"]["status"] == "sending"  # the panel adopts the post id
    d1 = await (await asyncio.wait_for(first, 5)).json()
    assert d1["post"]["id"] == d2["post"]["id"] and await _post_count(db) == 1 and _sent_to(bot) == [CHANNEL]


async def test_malformed_key(client, db):
    for key in ("short", "has space in it", "x" * 65, "ключ-на-кириллице"):
        resp = await client.post("/api/posts", json={"text_md": "x"}, headers=_h(key))
        assert resp.status == 400 and "X-Idempotency-Key" in (await resp.json())["error"], key
    assert await _post_count(db) == 0


async def test_unexpected_error_after_create_is_not_run_again(client, db, bot, monkeypatch):
    from app.web import api

    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(api, "publish_post", boom)
    body = {"text_md": "x", "channel_ids": [CHANNEL], "action": "publish"}
    r1 = await client.post("/api/posts", json=body, headers=_h())
    assert r1.status == 500
    r2 = await client.post("/api/posts", json=body, headers=_h())
    d2 = await r2.json()
    assert r2.status == 500 and d2["replayed"] is True and d2["post"]["status"] == "draft"
    assert await _post_count(db) == 1


# ------------------------------------------------------------------ publish endpoints

async def test_publish_now_retry_does_not_resend_failed_channels(client, db, bot):
    resp = await client.post("/api/posts", json={"text_md": "x", "channel_ids": [CHANNEL, CHANNEL2]}, headers=H)
    post_id = (await resp.json())["post"]["id"]
    bot.fail_on["send_message"] = (
        lambda kw: bad_request("Bad Request: chat not found") if kw["chat_id"] == CHANNEL2 else None)
    r1 = await client.post(f"/api/posts/{post_id}/publish", headers=_h())
    d1 = await r1.json()
    assert r1.status == 200 and d1["post"]["status"] == "partial"
    r2 = await client.post(f"/api/posts/{post_id}/publish", headers=_h())
    d2 = await r2.json()
    assert r2.status == 200 and d2["replayed"] is True and d2["report"] == d1["report"]
    assert sorted(_sent_to(bot)) == sorted([CHANNEL, CHANNEL2])  # the retry tries the failed channel no more

    # a new click (new key) does publish to the failed channel again
    bot.fail_on.clear()
    r3 = await client.post(f"/api/posts/{post_id}/publish", headers=_h("second-click-0002"))
    assert r3.status == 200 and (await r3.json())["post"]["status"] == "sent"
    assert _sent_to(bot).count(CHANNEL2) == 2 and _sent_to(bot).count(CHANNEL) == 1


async def test_publish_now_of_a_post_being_sent_returns_its_state(client, db):
    resp = await client.post("/api/posts", json={"text_md": "x", "channel_ids": [CHANNEL]}, headers=H)
    post_id = (await resp.json())["post"]["id"]
    await db.update_post(post_id, status="sending")
    for call in (client.post(f"/api/posts/{post_id}/publish", headers=H),
                 client.put(f"/api/posts/{post_id}", json={"text_md": "y", "action": "publish"}, headers=H)):
        resp = await call
        data = await resp.json()
        assert resp.status == 409 and data["post"]["id"] == post_id and data["post"]["status"] == "sending"


async def test_put_publish_retry_of_a_sent_post(client, db, bot):
    resp = await client.post("/api/posts", json={"text_md": "x", "channel_ids": [CHANNEL]}, headers=H)
    post_id = (await resp.json())["post"]["id"]
    body = {"text_md": "x", "channel_ids": [CHANNEL], "action": "publish"}
    r1 = await client.put(f"/api/posts/{post_id}", json=body, headers=_h())
    assert r1.status == 200 and (await r1.json())["post"]["status"] == "sent"
    r2 = await client.put(f"/api/posts/{post_id}", json=body, headers=_h())
    assert r2.status == 200 and (await r2.json())["replayed"] is True
    # without a key the repeated click is refused with the post's state (never a second send)
    r3 = await client.put(f"/api/posts/{post_id}", json=body, headers=H)
    d3 = await r3.json()
    assert r3.status == 400 and d3["post"]["status"] == "sent"
    assert _sent_to(bot) == [CHANNEL]


async def test_keys_are_scoped_by_path(client, db, bot):
    resp = await client.post("/api/posts", json={"text_md": "a", "channel_ids": [CHANNEL]}, headers=H)
    first = (await resp.json())["post"]["id"]
    resp = await client.post("/api/posts", json={"text_md": "b", "channel_ids": [CHANNEL2]}, headers=H)
    second = (await resp.json())["post"]["id"]
    for post_id in (first, second):
        resp = await client.post(f"/api/posts/{post_id}/publish", headers=_h())
        assert resp.status == 200 and "replayed" not in await resp.json()
    assert sorted(_sent_to(bot)) == sorted([CHANNEL, CHANNEL2])


# ------------------------------------------------------------------ the store

def test_store_expiry_and_bound():
    clock = [0.0]
    store = IdempotencyStore(ttl=60, max_entries=3, clock=lambda: clock[0])
    scopes = [("POST", "/api/posts", f"key-{i:08d}") for i in range(5)]
    entry = store.start(scopes[0], None)
    store.finish(entry, 200, {"post": {"id": 7}})
    assert entry.post_id == 7 and store.get(scopes[0]) is entry
    clock[0] = 61
    assert store.get(scopes[0]) is None  # expired
    running = store.start(scopes[1], None)  # never finished: never dropped
    for scope in scopes[2:]:
        store.finish(store.start(scope, None), 200, None)
        clock[0] += 1
    assert len(store) <= 3 and store.get(scopes[1]) is running
    store.forget(scopes[1], running)
    assert store.get(scopes[1]) is None


async def test_app_has_a_store(client):
    assert isinstance(client.server.app[IDEMPOTENCY_KEY], IdempotencyStore)

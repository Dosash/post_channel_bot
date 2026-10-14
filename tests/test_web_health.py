"""``GET /api/health`` (SPEC_V21 §2) and the scheduler status it reports."""
from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import __version__
from app.db import CH_ACTIVE, CH_IGNORED, CH_PENDING, POST_DRAFT, POST_FAILED, POST_PARTIAL, POST_SCHEDULED
from app.services.scheduler import Scheduler
from app.web import create_web_app, health
from tests.fakes import bad_request

H = {"X-Requested-With": "pcb"}
CH1, CH2, CH3, CH4 = -1001000000001, -1001000000002, -1001000000003, -1001000000004
KEYS = {"status", "version", "bot", "scheduled", "failed_24h", "pending_channels", "channels", "problems"}


@pytest.fixture
async def make_client(db, config, bot):
    await db.migrate()
    clients: list[TestClient] = []

    async def make(scheduler=None) -> TestClient:
        client = TestClient(TestServer(create_web_app(bot, db, config, scheduler)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


@pytest.fixture
async def client(make_client):
    return await make_client()


async def _health(client: TestClient, path: str = "/api/health", status: int = 200) -> dict:
    resp = await client.get(path)
    assert resp.status == status, await resp.text()
    assert resp.headers["Cache-Control"] == "no-store"
    return await resp.json()


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


# ------------------------------------------------------------------ shape and counts

async def test_health_ok(client):
    data = await _health(client)
    assert data == {"status": "ok", "version": __version__, "bot": "@test_bot", "scheduled": 0, "failed_24h": 0,
                    "pending_channels": 0, "channels": 0, "problems": []}


async def test_health_counts(client, db):
    for chat_id, status in ((CH1, CH_ACTIVE), (CH2, CH_ACTIVE), (CH3, CH_PENDING), (CH4, CH_IGNORED)):
        await db.upsert_channel(chat_id, "Канал", None, status=status, rights={"can_post": True})
    ts = int(time.time())
    # In the plan: one post queued to two channels → 2 targets.
    planned = await db.create_post(text_md="план", status=POST_SCHEDULED, scheduled_at=ts + 3600)
    await db.set_post_targets(planned, [CH1, CH2])
    await db.set_target_schedule(planned, CH1, ts + 3600, queued=True)
    await db.set_target_schedule(planned, CH2, ts + 7200)
    # Not in the plan: a pending target without a time and a draft's timed target.
    draft = await db.create_post(text_md="черновик", status=POST_DRAFT)
    await db.set_post_targets(draft, [CH1])
    await db.set_target_schedule(draft, CH1, ts + 3600)
    loose = await db.create_post(text_md="без времени", status=POST_SCHEDULED, scheduled_at=ts + 60)
    await db.set_post_targets(loose, [CH2])
    # Failures: two within 24 h, one older.
    await db.create_post(text_md="ошибка", status=POST_FAILED, updated_at=ts - 3600)
    await db.create_post(text_md="частично", status=POST_PARTIAL, updated_at=ts - 60)
    await db.create_post(text_md="давно", status=POST_FAILED, updated_at=ts - 3 * 86400)

    data = await _health(client)
    assert set(data) == KEYS
    assert (data["scheduled"], data["failed_24h"], data["pending_channels"], data["channels"]) == (2, 2, 1, 2)
    assert data["status"] == "ok"


async def test_health_is_always_open(make_client, config, db):
    config.web_password = "env-secret-1"
    client = await make_client()
    assert (await client.get("/api/posts")).status == 401
    data = await _health(client)
    assert data["status"] == "ok"
    resp = await client.get("/api/health", headers={"Host": "evil.example.com"})
    assert resp.status == 200
    resp = await client.post("/api/health")  # no CSRF check, but only GET is served
    assert resp.status == 405 and "error" in await resp.json()


async def test_health_under_prefix(make_client, config):
    config.web_prefix = "/posts"
    client = await make_client()
    data = await _health(client, "/posts/api/health")
    assert data["status"] == "ok" and data["version"] == __version__
    assert (await client.get("/api/health")).status == 404


async def test_health_database_error(client, monkeypatch):
    async def broken(db, ts=None):
        raise RuntimeError("disk I/O error at /some/server/path")

    monkeypatch.setattr(health, "health_counts", broken)
    data = await _health(client, status=503)
    assert data == {"status": "error", "version": __version__, "bot": None, "problems": [health.DATABASE_PROBLEM]}


# ------------------------------------------------------------------ Telegram

async def test_health_degraded_while_telegram_is_unreachable(client, bot, monkeypatch):
    bot.fail_on["get_me"] = bad_request("Bad Gateway")
    data = await _health(client)
    assert data["status"] == "degraded" and data["bot"] is None
    assert data["problems"] == [health.TELEGRAM_PROBLEM]
    calls = len(bot.calls_of("get_me"))
    await _health(client)
    assert len(bot.calls_of("get_me")) == calls  # not retried within GETME_RETRY seconds

    bot.fail_on.clear()
    monkeypatch.setattr(health, "GETME_RETRY", 0.0)
    data = await _health(client)
    assert data["status"] == "ok" and data["bot"] == "@test_bot" and data["problems"] == []
    calls = len(bot.calls_of("get_me"))
    await _health(client)
    overview = await (await client.get("/api/overview")).json()
    assert overview["bot"]["username"] == "test_bot"
    assert len(bot.calls_of("get_me")) == calls  # cached and shared with the overview


async def test_health_does_not_hang_on_telegram(client, bot, monkeypatch):
    monkeypatch.setattr(health, "GETME_TIMEOUT", 0.05)
    original = bot.get_me

    async def hanging_get_me(*args, **kwargs):
        await asyncio.sleep(3600)
        return await original(*args, **kwargs)

    bot.get_me = hanging_get_me
    data = await asyncio.wait_for(_health(client), 5)
    assert data["status"] == "degraded" and data["problems"] == [health.TELEGRAM_PROBLEM]


# ------------------------------------------------------------------ scheduler

async def test_scheduler_status_lifecycle(db, bot, config):
    await db.migrate()
    scheduler = Scheduler(bot, db, config, poll_interval=0.01)
    status = scheduler.status()
    assert (status.running, status.last_ok_ts, status.last_error, status.telegram_down) == (False, None, None, False)
    failures = [RuntimeError("boom")]

    async def job() -> int:
        if failures:
            raise failures.pop()
        return 0

    scheduler.run_due_posts = job
    scheduler.start()
    try:
        await _wait_for(lambda: scheduler.status().last_ok_ts is not None)
        status = scheduler.status()
        assert status.running and status.last_error is None and status.last_error_ts is None
        assert status.last_ok_ts >= int(time.time()) - 5
        failures.append(ValueError("broken"))
        await _wait_for(lambda: scheduler.status().last_error is not None)
        status = scheduler.status()
        assert status.last_error == "posts: ValueError: broken" and status.last_error_ts is not None
        await _wait_for(lambda: scheduler.status().last_error is None)  # the next iteration succeeded
    finally:
        await scheduler.stop()
    assert scheduler.status().running is False


async def test_health_reports_scheduler_problems(make_client, db, bot, config):
    scheduler = Scheduler(bot, db, config, poll_interval=0.01)
    client = await make_client(scheduler)
    data = await _health(client)
    assert data["status"] == "degraded" and data["problems"] == [health.SCHEDULER_STOPPED_PROBLEM]

    failing = {"on": True}

    async def job() -> int:
        if failing["on"]:
            raise RuntimeError("secret details of the failure")
        return 0

    scheduler.run_due_posts = job
    scheduler.start()
    try:
        await _wait_for(lambda: scheduler.status().last_error is not None)
        data = await _health(client)
        assert data["status"] == "degraded" and data["problems"] == [health.SCHEDULER_ERROR_PROBLEM]
        assert "secret" not in str(data)  # exception texts stay in the log
        failing["on"] = False
        await _wait_for(lambda: scheduler.status().last_error is None)
        assert (await _health(client))["status"] == "ok"
    finally:
        await scheduler.stop()
    assert (await _health(client))["problems"] == [health.SCHEDULER_STOPPED_PROBLEM]


async def test_health_reports_telegram_down_seen_by_the_scheduler(make_client, db, bot, config):
    bot.add_chat(CH1, "Канал")
    await db.upsert_channel(CH1, "Канал", None, status=CH_ACTIVE, rights={"can_post": True})
    post_id = await db.create_post(text_md="пора", status=POST_SCHEDULED, scheduled_at=int(time.time()) - 5)
    await db.set_post_targets(post_id, [CH1])
    await db.set_target_schedule(post_id, CH1, int(time.time()) - 5)
    scheduler = Scheduler(bot, db, config, poll_interval=0.01)
    client = await make_client(scheduler)
    assert (await _health(client))["bot"] == "@test_bot"  # identity cached before the outage

    bot.fail_on["get_me"] = bad_request("Bad Gateway")
    scheduler.start()
    try:
        await _wait_for(lambda: scheduler.status().telegram_down)
        data = await _health(client)
        assert data["status"] == "degraded" and data["problems"] == [health.SCHEDULER_TELEGRAM_PROBLEM]
        assert data["scheduled"] == 1  # the due post waits
        bot.fail_on.clear()
        await _wait_for(lambda: not scheduler.status().telegram_down)
    finally:
        await scheduler.stop()
    assert (await db.get_post(post_id))["status"] == "sent"

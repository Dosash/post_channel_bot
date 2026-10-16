"""Web API: channels, statistics, overview, admins, settings, static files and error handling."""
from __future__ import annotations

import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import __version__
from app.db import CH_ACTIVE, CH_IGNORED, CH_LEFT, CH_PENDING
from app.services import stats as stats_service
from app.web import create_web_app

H = {"X-Requested-With": "pcb"}
CHAN = -1001234567890


@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    bot.add_chat(CHAN, "Тест канал", "test_channel", members=42)
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


async def _channels(client: TestClient) -> list[dict]:
    resp = await client.get("/api/channels")
    assert resp.status == 200
    return (await resp.json())["channels"]


# ------------------------------------------------------------------ channels

async def test_add_channel_by_username(client, db):
    resp = await client.post("/api/channels", json={"ref": "https://t.me/test_channel"}, headers=H)
    data = await resp.json()
    assert resp.status == 200, data
    channel = data["channel"]
    assert channel["id"] == CHAN and channel["status"] == CH_ACTIVE
    assert channel["link"] == "https://t.me/test_channel"
    assert channel["members_count"] == 42
    assert channel["can_post"] is True and channel["can_edit"] is True
    assert channel["added_by"] is None
    assert [c["id"] for c in await _channels(client)] == [CHAN]
    assert await db.get_member_count_at(CHAN, 2**40) == 42  # snapshot taken


async def test_add_channel_errors(client, bot):
    bot.add_chat(-100555, "Группа", "some_group", type="supergroup")
    bot.add_chat(-100666, "Без прав", "noright", can_post_messages=False)
    bot.add_chat(-100777, "Не админ", "notadmin", bot_status="left")
    cases = {
        "": "Укажите",
        "@missing_channel": "Канал не найден",
        "https://t.me/+AbCdEf": "Ссылки-приглашения",
        "@some_group": "Это не канал",
        "@noright": "нет права публиковать",
        "@notadmin": "не является администратором",
    }
    for ref, message in cases.items():
        resp = await client.post("/api/channels", json={"ref": ref}, headers=H)
        assert resp.status == 400, ref
        assert message in (await resp.json())["error"], ref
    resp = await client.post("/api/channels", json={"ref": True}, headers=H)
    assert resp.status == 400
    assert await _channels(client) == []


async def test_approve_and_ignore_pending(client, db, bot):
    bot.add_chat(-100888, "Ещё канал", None)
    await db.upsert_channel(CHAN, "Тест канал", "test_channel", status=CH_PENDING)
    await db.upsert_channel(-100888, "Ещё канал", None, status=CH_PENDING)

    resp = await client.post(f"/api/channels/{CHAN}/approve", headers=H)
    data = await resp.json()
    assert resp.status == 200, data
    assert data["channel"]["status"] == CH_ACTIVE

    resp = await client.post("/api/channels/-100888/ignore", headers=H)
    data = await resp.json()
    assert resp.status == 200 and data["channel"]["status"] == CH_IGNORED
    assert data["channel"]["link"] is None

    assert (await client.post("/api/channels/-100999/approve", headers=H)).status == 404
    assert (await client.post("/api/channels/-100999/ignore", headers=H)).status == 404

    # An ignored channel can still be connected later.
    resp = await client.post("/api/channels/-100888/approve", headers=H)
    assert (await resp.json())["channel"]["status"] == CH_ACTIVE


async def test_approve_fails_without_rights(client, db, bot):
    bot.bot_status[CHAN] = "left"
    await db.upsert_channel(CHAN, "Тест канал", "test_channel", status=CH_PENDING)
    resp = await client.post(f"/api/channels/{CHAN}/approve", headers=H)
    assert resp.status == 400
    assert (await db.get_channel(CHAN))["status"] == CH_PENDING


async def test_channels_order(client, db):
    await db.upsert_channel(-1003, "А ушёл", None, status=CH_LEFT)
    await db.upsert_channel(-1002, "Б ждёт", None, status=CH_PENDING)
    await db.upsert_channel(-1004, "В скрыт", None, status=CH_IGNORED)
    await db.upsert_channel(-1001, "Г активный", None, status=CH_ACTIVE)
    await db.upsert_channel(-1005, "Д активный", None, status=CH_ACTIVE)
    statuses = [c["status"] for c in await _channels(client)]
    assert statuses[:3] == [CH_ACTIVE, CH_ACTIVE, CH_PENDING]
    assert sorted(statuses[3:]) == [CH_IGNORED, CH_LEFT]


async def test_refresh_channel(client, bot):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    bot.chats[CHAN]["title"] = "Новое имя"
    bot.member_counts[CHAN] = 50
    resp = await client.post(f"/api/channels/{CHAN}/refresh", headers=H)
    channel = (await resp.json())["channel"]
    assert resp.status == 200
    assert channel["title"] == "Новое имя" and channel["members_count"] == 50

    bot.bot_status[CHAN] = "left"
    resp = await client.post(f"/api/channels/{CHAN}/refresh", headers=H)
    assert (await resp.json())["channel"]["status"] == CH_LEFT
    assert (await client.post("/api/channels/-1009/refresh", headers=H)).status == 404


async def test_delete_channel(client, db):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    resp = await client.delete(f"/api/channels/{CHAN}", headers=H)
    assert resp.status == 200 and await resp.json() == {"ok": True}
    assert await db.get_channel(CHAN) is None
    assert (await client.delete(f"/api/channels/{CHAN}", headers=H)).status == 404


async def test_channel_stats(client, db):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    await db.add_member_event(CHAN, 5, "join")
    resp = await client.get(f"/api/channels/{CHAN}/stats?days=7")
    data = await resp.json()
    assert resp.status == 200, data
    assert data["days"] == 7 and len(data["series"]) == 7
    assert data["members_now"] == 42 and data["joins"] == 1
    assert data["channel"]["link"] == "https://t.me/test_channel"
    for key in ("delta", "tracking_since", "leaves", "posts", "posts_bot", "reactions", "top_posts"):
        assert key in data

    data = await (await client.get(f"/api/channels/{CHAN}/stats")).json()
    assert data["days"] == 30
    data = await (await client.get(f"/api/channels/{CHAN}/stats?days=5000")).json()
    assert data["days"] == 365
    data = await (await client.get(f"/api/channels/{CHAN}/stats?days=0")).json()
    assert data["days"] == 1
    data = await (await client.get(f"/api/channels/{CHAN}/stats?days=abc")).json()
    assert data["days"] == 30

    resp = await client.get("/api/channels/-100404/stats")
    assert resp.status == 404 and (await resp.json())["error"] == "Канал не найден"


async def test_combined_stats(client, db):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    await db.add_member_event(CHAN, 5, "join")
    data = await (await client.get("/api/stats?days=7")).json()
    assert data["days"] == 7 and len(data["series"]) == 7
    assert data["members_now"] == 42 and data["joins"] == 1
    assert [c["channel"]["link"] for c in data["channels"]] == ["https://t.me/test_channel"]

    data = await (await client.get(f"/api/stats?channels={CHAN},-100404")).json()
    assert data["days"] == 30 and len(data["channels"]) == 1

    resp = await client.get("/api/stats?channels=-100404,abc")
    assert resp.status == 404 and (await resp.json())["error"] == "Каналы не найдены"


# ------------------------------------------------------------------ overview / settings

async def test_overview(client, bot, config):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    resp = await client.get("/api/overview")
    data = await resp.json()
    assert resp.status == 200
    assert data["bot"] == {"id": bot.id, "username": "test_bot", "name": "Test bot"}
    assert data["stats"]["channels_active"] == 1 and data["stats"]["members_total"] == 42
    assert data["web_url"] == config.web_url
    assert data["timezone"] == "Asia/Tashkent"
    await client.get("/api/overview")
    assert len(bot.calls_of("get_me")) == 1  # cached


async def test_overview_when_get_me_fails(client, bot, monkeypatch):
    from app.web import api
    from tests.fakes import bad_request

    bot.fail_on["get_me"] = bad_request()
    data = await (await client.get("/api/overview")).json()
    assert data["bot"] == {"id": bot.id, "username": None, "name": None}
    bot.fail_on.clear()
    data = await (await client.get("/api/overview")).json()
    assert data["bot"]["username"] is None  # a failure is remembered for GETME_RETRY seconds
    assert len(bot.calls_of("get_me")) == 1
    monkeypatch.setattr(api, "GETME_RETRY", 0.0)
    data = await (await client.get("/api/overview")).json()
    assert data["bot"]["username"] == "test_bot"  # asked again after GETME_RETRY


async def test_overview_and_settings_never_hang_on_telegram(client, bot, monkeypatch):
    """Review finding #23: while getMe hangs (Telegram unreachable, packets dropped), «Обзор» and
    «Настройки» wait at most GETME_TIMEOUT once, then answer at once for GETME_RETRY seconds."""
    import asyncio
    import time

    from app.web import api

    monkeypatch.setattr(api, "GETME_TIMEOUT", 0.2)
    monkeypatch.setattr(api, "SETTINGS_GETME_TIMEOUT", 0.2)
    calls = []

    async def hanging_get_me(*args, **kwargs):
        calls.append(1)
        await asyncio.sleep(3600)

    bot.get_me = hanging_get_me
    started = time.monotonic()
    pages = await asyncio.wait_for(asyncio.gather(
        client.get("/api/overview"), client.get("/api/settings"), client.get("/api/overview")), 5)
    assert [r.status for r in pages] == [200, 200, 200]
    assert len(calls) == 1  # parallel pages share one getMe
    assert time.monotonic() - started < 2
    started = time.monotonic()
    for path in ("/api/overview", "/api/settings", "/api/health"):
        data = await (await client.get(path)).json()
        assert data["bot"] in (None, {"id": bot.id, "username": None, "name": None}), path
    assert len(calls) == 1 and time.monotonic() - started < 0.15  # negative cache: no waiting


async def test_settings(client, config):
    data = await (await client.get("/api/settings")).json()
    assert data["slots_help"].startswith("Расписание")
    del data["slots_help"]
    assert len(data.pop("timezones")) > 10
    assert data.pop("timezone_label") == data.pop("timezone_env_label") == "Asia/Tashkent (UTC+5)"
    assert data.pop("timezone_env") == "Asia/Tashkent" and data.pop("timezone_source") == "env"
    assert data == {"password_set": False, "password_fixed": False, "password_source": None,
                    "timezone": "Asia/Tashkent", "web_url": "http://127.0.0.1:8080/", "web_prefix": "",
                    "hub_url": None, "version": __version__,
                    # SPEC_V22: the running bot and where its token comes from (masked)
                    "bot": {"id": 987654321, "username": "test_bot", "name": "Test bot"},
                    "bot_token_source": "env", "bot_token_masked": "…" + config.bot_token[-4:],
                    "panel_token_rejected": False, "panel_token_set": False,
                    # SPEC_V23: custom emoji are sent as they are only with a Fragment username
                    "premium_emoji": False}


# ------------------------------------------------------------------ admins

async def test_admins(client):
    data = await (await client.get("/api/admins")).json()
    assert [(a["user_id"], a["is_owner"]) for a in data["admins"]] == [(1, True)]

    resp = await client.post("/api/admins", json={"user_id": 42}, headers=H)
    assert resp.status == 200
    assert [a["user_id"] for a in (await resp.json())["admins"]] == [1, 42]
    for bad in ("abc", None, True, -5, 1.5, [42]):
        resp = await client.post("/api/admins", json={"user_id": bad}, headers=H)
        assert resp.status == 400, bad

    resp = await client.delete("/api/admins/1", headers=H)
    assert resp.status == 400 and (await resp.json())["error"] == "Нельзя удалить владельца"
    resp = await client.delete("/api/admins/42", headers=H)
    assert resp.status == 200
    assert [a["user_id"] for a in (await resp.json())["admins"]] == [1]
    assert (await client.delete("/api/admins/42", headers=H)).status == 404


# ------------------------------------------------------------------ static & errors

async def test_static_files(client):
    resp = await client.get("/")
    assert resp.status == 200 and resp.content_type == "text/html"
    assert resp.headers["Cache-Control"] == "no-cache"
    page = await resp.text()
    assert '<base href="/">' in page
    assert re.search(r'src="static/app\.js\?v=[0-9a-f]{10}"', page)
    assert re.search(r'href="static/style\.css\?v=[0-9a-f]{10}"', page)
    for name in ("app.js", "style.css"):
        resp = await client.get(f"/static/{name}")
        assert resp.status == 200, name
    assert (await client.get("/static/../app/db.py")).status in (403, 404)
    assert (await client.get("/static/%2e%2e/__init__.py")).status in (403, 404)


async def test_unexpected_error_is_500_json(client, monkeypatch):
    async def boom(db):
        raise RuntimeError("boom")

    monkeypatch.setattr(stats_service, "overview", boom)
    resp = await client.get("/api/overview")
    assert resp.status == 500
    assert await resp.json() == {"error": "Внутренняя ошибка сервера"}


async def test_first_admin_added_from_panel_becomes_owner(db, config, bot):
    """Review finding #16: with no owner yet, the first admin added in the panel is the owner
    (otherwise has_admins() blocks the /start bootstrap forever)."""
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as client:
        resp = await client.post("/api/admins", json={"user_id": 555}, headers=H)
        assert resp.status == 200
        assert [(a["user_id"], a["is_owner"]) for a in (await resp.json())["admins"]] == [(555, True)]
        resp = await client.post("/api/admins", json={"user_id": 777}, headers=H)
        assert [(a["user_id"], a["is_owner"]) for a in (await resp.json())["admins"]] == [(555, True), (777, False)]
        # Re-adding the owner keeps it the owner; it cannot be removed.
        await client.post("/api/admins", json={"user_id": 555}, headers=H)
        assert (await client.delete("/api/admins/555", headers=H)).status == 400


# ------------------------------------------------------------------ V2 channel fields

async def test_channel_rows_have_folder_and_slots(client, db):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    channel = (await _channels(client))[0]
    assert channel["folder_id"] is None
    assert channel["slots"] == {str(day): [] for day in range(7)}
    assert channel["slots_text"] == "" and channel["slots_summary"] == "не настроено"
    assert "slots_json" not in channel

    folder_id = await db.create_folder("Новости")
    await db.set_channel_folder(CHAN, folder_id)
    await db.set_channel_slots(CHAN, {0: ["09:00", "18:00"], 1: ["09:00", "18:00"], 2: ["09:00", "18:00"],
                                      3: ["09:00", "18:00"], 4: ["09:00", "18:00"], 5: ["12:00"], 6: ["12:00"]})
    channel = (await _channels(client))[0]
    assert channel["folder_id"] == folder_id
    assert channel["slots"]["0"] == ["09:00", "18:00"] and channel["slots"]["6"] == ["12:00"]
    assert channel["slots_text"] == "пн-пт 09:00, 18:00\nсб, вс 12:00"
    assert channel["slots_summary"] == "пн-пт 09:00, 18:00 · сб, вс 12:00"


async def test_broken_slots_json_does_not_break_the_list(client, db):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    await db.update_channel(CHAN, slots_json='{"9": ["25:00"]}')
    channel = (await _channels(client))[0]
    assert channel["slots_summary"] == "не настроено"


async def test_set_timezone(client, db, config):
    resp = await client.put("/api/settings/timezone", json={"timezone": "europe/moscow"}, headers=H)
    data = await resp.json()
    assert resp.status == 200, data
    assert data["timezone"] == "Europe/Moscow" and data["timezone_source"] == "settings"
    assert str(config.tz) == "Europe/Moscow" and await db.get_setting("timezone") == "Europe/Moscow"
    assert (await (await client.get("/api/settings")).json())["timezone"] == "Europe/Moscow"

    resp = await client.put("/api/settings/timezone", json={"timezone": "Mars/Base"}, headers=H)
    assert resp.status == 400 and "Неизвестный часовой пояс" in (await resp.json())["error"]
    assert str(config.tz) == "Europe/Moscow"
    assert (await client.put("/api/settings/timezone", json={"timezone": 5}, headers=H)).status == 400

    data = await (await client.put("/api/settings/timezone", json={"timezone": None}, headers=H)).json()
    assert data["timezone"] == "Asia/Tashkent" and data["timezone_source"] == "env"
    assert await db.get_setting("timezone") is None


async def test_channel_avatar(client, db, bot, config):
    await client.post("/api/channels", json={"ref": "@test_channel"}, headers=H)
    assert (await client.get(f"/api/channels/{CHAN}/avatar")).status == 404
    bot.chats[CHAN]["photo"] = "ph"
    data = await (await client.post(f"/api/channels/{CHAN}/refresh", headers=H)).json()
    assert data["channel"]["photo_id"] == "ph_su"
    resp = await client.get(f"/api/channels/{CHAN}/avatar?v=ph_su")
    assert resp.status == 200 and resp.headers["Content-Type"] == "image/jpeg"
    assert "max-age" in resp.headers["Cache-Control"]
    assert (await resp.read()).startswith(b"\xff\xd8")

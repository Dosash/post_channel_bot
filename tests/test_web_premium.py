"""SPEC_V23 — premium (custom) emoji: the setting in the panel API and the composer preview."""
from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.web import create_web_app

H = {"X-Requested-With": "pcb"}
EMOJI_ID = "5368324170671202286"
TEXT = f"Привет ![😀](tg://emoji?id={EMOJI_ID})!"
TG_EMOJI = f'<tg-emoji emoji-id="{EMOJI_ID}">😀</tg-emoji>'


@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


async def _render(client: TestClient, text_md: str, **extra) -> dict:
    resp = await client.post("/api/render", json={"text_md": text_md, **extra}, headers=H)
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def _set(client: TestClient, value) -> tuple[int, dict]:
    resp = await client.put("/api/settings/premium-emoji", json={"enabled": value}, headers=H)
    return resp.status, await resp.json()


async def test_setting_is_off_by_default_and_toggles(client, db):
    assert (await (await client.get("/api/settings")).json())["premium_emoji"] is False
    assert await _set(client, True) == (200, {"premium_emoji": True})
    assert await db.get_premium_emoji() is True
    assert (await (await client.get("/api/settings")).json())["premium_emoji"] is True
    assert await _set(client, False) == (200, {"premium_emoji": False})
    assert await db.get_premium_emoji() is False


async def test_setting_validation(client, db):
    for value in (None, 1, "true", "1", [], {}):
        status, data = await _set(client, value)
        assert status == 400 and data == {"error": "Ожидается enabled: true или false"}, value
    resp = await client.put("/api/settings/premium-emoji", json={"enabled": True})  # no CSRF header
    assert resp.status == 403
    resp = await client.put("/api/settings/premium-emoji", data="nope", headers=H)
    assert resp.status == 400
    assert await db.get_premium_emoji() is False


async def test_render_shows_the_fallback_emoji_while_off(client):
    data = await _render(client, TEXT)
    assert data["has_custom_emoji"] is True and data["premium_emoji"] is False
    assert data["html"] == "Привет 😀!" and data["length"] == 10 and data["error"] is None


async def test_render_keeps_custom_emoji_while_on(client):
    await _set(client, True)
    data = await _render(client, TEXT)
    assert data["has_custom_emoji"] is True and data["premium_emoji"] is True
    assert data["html"] == f"Привет {TG_EMOJI}!"


async def test_render_without_custom_emoji(client):
    data = await _render(client, "Просто 😀")
    assert data["has_custom_emoji"] is False and data["premium_emoji"] is False and data["html"] == "Просто 😀"
    # the same markdown inside code is not an emoji
    data = await _render(client, f"`![😀](tg://emoji?id={EMOJI_ID})`")
    assert data["has_custom_emoji"] is False and "<code>" in data["html"]


async def test_render_of_a_too_long_text_still_reports_custom_emoji(client):
    data = await _render(client, TEXT + "a" * 1100, has_media=True)
    assert data["error"] and data["has_custom_emoji"] is True
    assert "tg-emoji" not in data["html"]


async def test_settings_page_contract_under_a_prefix(db, config, bot):
    config.web_prefix = "/posts"
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as client:
        resp = await client.put("/posts/api/settings/premium-emoji", json={"enabled": True}, headers=H)
        assert resp.status == 200 and await resp.json() == {"premium_emoji": True}
        assert (await client.put("/api/settings/premium-emoji", json={"enabled": True}, headers=H)).status == 404

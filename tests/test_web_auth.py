"""Web panel authentication: open by default, password, sessions, CSRF, lockout, host check."""
from __future__ import annotations

import asyncio
import ipaddress
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.web import create_web_app
from app.web.auth import (
    COOKIE_NAME,
    FIXED_PASSWORD_ERROR,
    JSON_BODY_LIMIT,
    PASSWORD_HASH_SETTING,
    SESSION_TTL,
    LoginGuard,
    env_password_hash,
    get_password_hash,
    hash_password,
    host_allowed,
    make_session_token,
    reset_password,
    trusted_proxies,
    verify_password,
    verify_session_token,
)
from app.web.keys import LOGIN_GUARD_KEY, TRUSTED_PROXIES_KEY

H = {"X-Requested-With": "pcb"}


@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


async def _set_password(client: TestClient, password: str = "secret1") -> None:
    resp = await client.post("/api/auth/password", json={"new": password}, headers=H)
    assert resp.status == 200, await resp.text()


def _logout_locally(client: TestClient) -> None:
    client.session.cookie_jar.clear()


# ------------------------------------------------------------------ pure helpers

def test_hash_format_and_verify():
    stored = hash_password("пароль123")
    scheme, salt, digest = stored.split("$")
    assert scheme == "scrypt" and len(salt) == 32 and len(digest) == 64
    assert verify_password("пароль123", stored)
    assert not verify_password("пароль124", stored)


@pytest.mark.parametrize("stored", ["", "plain", "scrypt$zz$zz", "md5$00$00", "scrypt$abc$00", "scrypt$$"])
def test_verify_rejects_malformed_hash(stored):
    assert verify_password("x", stored) is False


def test_session_token_validation():
    token = make_session_token("secret", "hash1", 1_000_000)
    assert verify_session_token(token, "secret", "hash1", now_ts=1_000_100)
    assert not verify_session_token(token, "secret", "hash2", now_ts=1_000_100)  # password changed
    assert not verify_session_token(token, "other", "hash1", now_ts=1_000_100)
    assert not verify_session_token(token, "secret", "hash1", now_ts=1_000_000 + SESSION_TTL + 1)
    assert not verify_session_token(token[:-1] + ("0" if token[-1] != "0" else "1"), "secret", "hash1",
                                    now_ts=1_000_100)
    assert not verify_session_token("garbage", "secret", "hash1", now_ts=1_000_100)
    assert not verify_session_token("²³.abc", "secret", "hash1", now_ts=1_000_100)


def test_login_guard_locks_after_five_failures():
    clock = [100.0]
    guard = LoginGuard(clock=lambda: clock[0])
    for _ in range(4):
        guard.failure("ip")
        assert guard.retry_after("ip") == 0
    guard.failure("ip")
    assert guard.retry_after("ip") == 30
    clock[0] += 29.5
    assert guard.retry_after("ip") == 1
    clock[0] += 1
    assert guard.retry_after("ip") == 0
    guard.failure("other")
    guard.success("other")
    assert guard.retry_after("other") == 0


@pytest.mark.parametrize("host, allowed", [
    ("127.0.0.1:8080", True),
    ("[::1]:8080", True),
    ("10.20.30.40", True),
    ("localhost:8080", True),
    ("panel.localhost", True),
    ("evil.example.com", False),
    ("127.0.0.1.evil.com:8080", False),
    (None, True),
])
def test_host_allowed(config, host, allowed):
    assert host_allowed(host, config) is allowed


def test_host_allowed_public_url(config):
    config.web_public_url = "https://panel.example.org/"
    assert host_allowed("panel.example.org", config)
    assert not host_allowed("other.example.org", config)


async def test_reset_password_helper(db):
    await db.set_setting(PASSWORD_HASH_SETTING, hash_password("secret1"))
    await reset_password(db)
    assert await db.get_setting(PASSWORD_HASH_SETTING) is None


# ------------------------------------------------------------------ endpoints

async def test_open_by_default(client):
    resp = await client.get("/api/auth/status")
    assert await resp.json() == {"password_set": False, "password_fixed": False, "password_source": None,
                                 "authenticated": True}
    resp = await client.get("/api/channels")
    assert resp.status == 200
    resp = await client.get("/api/settings")
    assert (await resp.json())["password_set"] is False


async def test_password_required_after_setting_it(client):
    await _set_password(client)
    # The author of the change keeps a session.
    assert (await client.get("/api/channels")).status == 200
    status = await (await client.get("/api/auth/status")).json()
    assert status == {"password_set": True, "password_fixed": False, "password_source": "panel",
                      "authenticated": True}

    _logout_locally(client)
    resp = await client.get("/api/channels")
    assert resp.status == 401
    assert await resp.json() == {"error": "auth_required"}
    status = await (await client.get("/api/auth/status")).json()
    assert status == {"password_set": True, "password_fixed": False, "password_source": "panel",
                      "authenticated": False}
    # Static files are always served (the SPA shows the login form).
    assert (await client.get("/")).status == 200
    assert (await client.get("/static/app.js")).status == 200


async def test_login_wrong_and_right(client):
    await _set_password(client)
    _logout_locally(client)

    resp = await client.post("/api/auth/login", json={"password": "wrong-pass"}, headers=H)
    assert resp.status == 401
    assert (await resp.json())["error"] == "Неверный пароль"
    assert (await client.get("/api/posts")).status == 401

    resp = await client.post("/api/auth/login", json={"password": ""}, headers=H)
    assert resp.status == 400

    resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=H)
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert f"Max-Age={SESSION_TTL}" in cookie
    assert "Secure" not in cookie  # plain http
    assert (await client.get("/api/posts")).status == 200


async def test_login_lockout(client):
    await _set_password(client)
    _logout_locally(client)
    guard = client.server.app[LOGIN_GUARD_KEY]
    clock = [1000.0]
    guard._clock = lambda: clock[0]

    for _ in range(5):
        resp = await client.post("/api/auth/login", json={"password": "bad-pass"}, headers=H)
        assert resp.status == 401
    resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=H)
    assert resp.status == 429
    assert "Retry-After" in resp.headers
    assert "Подождите" in (await resp.json())["error"]

    clock[0] += 31
    resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=H)
    assert resp.status == 200


async def test_csrf_header_required_for_mutations(client):
    resp = await client.post("/api/render", json={"text_md": "x"})
    assert resp.status == 403
    assert "X-Requested-With" in (await resp.json())["error"]
    resp = await client.post("/api/render", json={"text_md": "x"}, headers={"X-Requested-With": "other"})
    assert resp.status == 403
    resp = await client.post("/api/auth/login", json={"password": "x"})
    assert resp.status == 403
    resp = await client.delete("/api/admins/5")
    assert resp.status == 403
    resp = await client.put("/api/posts/1", json={})
    assert resp.status == 403
    # GET is not a mutation.
    assert (await client.get("/api/overview")).status == 200


async def test_logout(client):
    await _set_password(client)
    assert (await client.get("/api/posts")).status == 200
    resp = await client.post("/api/auth/logout", headers=H)
    assert resp.status == 200
    assert (await client.get("/api/posts")).status == 401


async def test_change_password(client, db):
    await _set_password(client, "first-pass")
    old_token = client.session.cookie_jar.filter_cookies(client.make_url("/"))[COOKIE_NAME].value

    resp = await client.post("/api/auth/password", json={"new": "second-pass"}, headers=H)
    assert resp.status == 400  # current password is required
    resp = await client.post("/api/auth/password", json={"current": "nope-nope", "new": "second-pass"}, headers=H)
    assert resp.status == 403
    resp = await client.post("/api/auth/password", json={"current": "first-pass", "new": "123"}, headers=H)
    assert resp.status == 400
    resp = await client.post("/api/auth/password", json={"current": "first-pass", "new": "second-pass"}, headers=H)
    assert resp.status == 200
    assert (await client.get("/api/posts")).status == 200  # re-issued session

    # Sessions issued for the old password are invalid now.
    _logout_locally(client)
    resp = await client.get("/api/posts", headers={"Cookie": f"{COOKIE_NAME}={old_token}"})
    assert resp.status == 401

    resp = await client.post("/api/auth/login", json={"password": "first-pass"}, headers=H)
    assert resp.status == 401
    resp = await client.post("/api/auth/login", json={"password": "second-pass"}, headers=H)
    assert resp.status == 200


async def test_short_password_rejected(client):
    resp = await client.post("/api/auth/password", json={"new": "12345"}, headers=H)
    assert resp.status == 400
    assert "6" in (await resp.json())["error"]
    assert (await (await client.get("/api/auth/status")).json())["password_set"] is False


async def test_remove_password(client):
    resp = await client.delete("/api/auth/password", json={"current": "x"}, headers=H)
    assert resp.status == 400  # nothing to remove

    await _set_password(client)
    resp = await client.delete("/api/auth/password", json={"current": "wrong-one"}, headers=H)
    assert resp.status == 403
    resp = await client.delete("/api/auth/password", json={"current": "secret1"}, headers=H)
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "password_set": False}

    _logout_locally(client)
    status = await (await client.get("/api/auth/status")).json()
    assert status == {"password_set": False, "password_fixed": False, "password_source": None,
                      "authenticated": True}
    assert (await client.get("/api/posts")).status == 200


async def test_bot_reset_password_opens_panel(client, db):
    await _set_password(client)
    _logout_locally(client)
    assert (await client.get("/api/posts")).status == 401
    await reset_password(db)
    assert (await client.get("/api/posts")).status == 200


async def test_host_check_only_while_panel_is_open(client):
    resp = await client.get("/api/overview", headers={"Host": "evil.example.com"})
    assert resp.status == 403
    resp = await client.get("/api/overview", headers={"Host": "localhost:8080"})
    assert resp.status == 200
    # Pages themselves are still served.
    assert (await client.get("/", headers={"Host": "evil.example.com"})).status == 200

    await _set_password(client)
    resp = await client.get("/api/overview", headers={"Host": "evil.example.com"})
    assert resp.status == 200  # with a password the session cookie protects the API


async def test_unknown_api_route_is_json(client):
    resp = await client.get("/api/does-not-exist")
    assert resp.status == 404
    assert resp.content_type == "application/json"
    assert (await resp.json())["error"] == "Не найдено"
    resp = await client.patch("/api/overview", headers=H)
    assert resp.status == 405
    assert "error" in await resp.json()


async def test_invalid_json_body(client):
    resp = await client.post("/api/render", data="{not json", headers={**H, "Content-Type": "application/json"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "Некорректный JSON в запросе"
    resp = await client.post("/api/render", json=[1, 2], headers=H)
    assert resp.status == 400


async def test_security_headers(client):
    resp = await client.get("/")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    resp = await client.get("/api/overview")
    assert resp.headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------ review finding #15: parallel logins

def test_login_guard_begin_reserves_attempts():
    clock = [0.0]
    guard = LoginGuard(clock=lambda: clock[0])
    assert [guard.begin("ip") for _ in range(5)] == [0, 0, 0, 0, 0]
    assert guard.begin("ip") == 30  # five checks in flight already lock the key
    guard.success("other")
    assert guard.begin("ip") == 30
    clock[0] += 30
    assert guard.begin("ip") == 0
    guard.success("ip")
    assert guard.retry_after("ip") == 0


async def test_parallel_wrong_logins_are_limited(client):
    """Concurrent guesses must not slip past the 5-attempt limit while scrypt runs."""
    await _set_password(client)
    _logout_locally(client)

    async def attempt(password: str) -> int:
        resp = await client.post("/api/auth/login", json={"password": password}, headers=H)
        return resp.status

    statuses = await asyncio.gather(*(attempt("wrong-pass") for _ in range(20)), attempt("secret1"))
    assert statuses.count(401) <= 5
    assert statuses.count(401) + statuses.count(429) == 21
    assert statuses[-1] == 429  # the right password inside the burst is refused as well
    assert (await client.get("/api/posts")).status == 401


async def test_parallel_wrong_current_password_is_limited(client):
    await _set_password(client)

    async def attempt() -> int:
        resp = await client.post("/api/auth/password", json={"current": "nope-nope", "new": "x" * 8}, headers=H)
        return resp.status

    statuses = await asyncio.gather(*(attempt() for _ in range(12)))
    assert statuses.count(403) <= 5 and statuses.count(429) >= 7


# ------------------------------------------------------------------ review finding #17: reverse proxy

@pytest.mark.parametrize("raw, expected", [
    ("", ()), ("0", ()), ("no", ()), ("1", ("127.0.0.0/8", "::1/128")), ("true", ("127.0.0.0/8", "::1/128")),
    ("10.0.0.5, 172.17.0.0/16", ("10.0.0.5/32", "172.17.0.0/16")), ("garbage, 10.0.0.1", ("10.0.0.1/32",)),
])
def test_trusted_proxies_parsing(raw, expected):
    assert trusted_proxies(None, {"TRUST_PROXY": raw}) == tuple(ipaddress.ip_network(n) for n in expected)
    assert trusted_proxies(None, {}) == ()


def test_trusted_proxies_from_config(config):
    config.trust_proxy = True  # an integrator may add the field to Config; it wins over the env
    assert trusted_proxies(config, {"TRUST_PROXY": "0"}) == trusted_proxies(None, {"TRUST_PROXY": "1"})


@pytest.fixture
async def proxied_client(db, config, bot, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


def _via_proxy(ip: str, proto: str = "http") -> dict[str, str]:
    # nginx with $proxy_add_x_forwarded_for appends the real peer after anything the client sent.
    return {**H, "X-Forwarded-For": f"6.6.6.6, {ip}", "X-Forwarded-Proto": proto}


async def test_proxy_lockout_is_per_forwarded_ip(proxied_client):
    client = proxied_client
    assert client.server.app[TRUSTED_PROXIES_KEY]
    await _set_password(client)
    _logout_locally(client)
    for _ in range(5):
        resp = await client.post("/api/auth/login", json={"password": "bad-pass"}, headers=_via_proxy("203.0.113.66"))
        assert resp.status == 401
    resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=_via_proxy("203.0.113.66"))
    assert resp.status == 429
    # The owner behind the same proxy is not locked out by the attacker.
    resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=_via_proxy("198.51.100.7"))
    assert resp.status == 200
    guard = client.server.app[LOGIN_GUARD_KEY]
    assert guard.retry_after("203.0.113.66") > 0 and guard.retry_after("127.0.0.1") == 0


async def test_proxy_https_sets_secure_cookie(proxied_client):
    client = proxied_client
    await _set_password(client)
    _logout_locally(client)
    resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=_via_proxy("198.51.100.7", "https"))
    assert resp.status == 200
    assert "Secure" in resp.headers["Set-Cookie"]
    resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=_via_proxy("198.51.100.7"))
    assert "Secure" not in resp.headers["Set-Cookie"]


async def test_forwarded_headers_ignored_by_default(client):
    """Without TRUST_PROXY a client cannot dodge the lockout by forging X-Forwarded-For."""
    assert client.server.app[TRUSTED_PROXIES_KEY] == ()
    await _set_password(client)
    _logout_locally(client)
    for n in range(5):
        resp = await client.post("/api/auth/login", json={"password": "bad-pass"},
                                 headers={**H, "X-Forwarded-For": f"10.0.0.{n}", "X-Forwarded-Proto": "https"})
        assert resp.status == 401
        assert "Secure" not in resp.headers.get("Set-Cookie", "")
    resp = await client.post("/api/auth/login", json={"password": "secret1"},
                             headers={**H, "X-Forwarded-For": "10.0.0.99"})
    assert resp.status == 429


async def test_forwarded_headers_ignored_from_untrusted_peer(db, config, bot, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "10.1.2.3")  # the test client connects from 127.0.0.1
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as client:
        await _set_password(client)
        _logout_locally(client)
        for n in range(5):
            await client.post("/api/auth/login", json={"password": "bad-pass"}, headers=_via_proxy(f"10.9.0.{n}"))
        resp = await client.post("/api/auth/login", json={"password": "secret1"}, headers=_via_proxy("10.9.0.77"))
        assert resp.status == 429


async def test_proxy_with_garbage_forwarded_for(proxied_client):
    resp = await proxied_client.get("/api/auth/status", headers={"X-Forwarded-For": "not-an-ip, ???"})
    assert resp.status == 200


# ------------------------------------------------------------------ review finding #18: malformed bodies

async def test_malformed_bodies_are_400(client, caplog):
    caplog.set_level(logging.ERROR)
    resp = await client.post("/api/render", data=b'{"text_md": "x"}',
                             headers={**H, "Content-Type": "application/json; charset=xxx"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "Некорректная кодировка запроса"

    resp = await client.post("/api/render", data=b'{"text_md": "\xff\xfe"}',
                             headers={**H, "Content-Type": "application/json"})
    assert resp.status == 400

    deep = "[" * 100_000 + "]" * 100_000
    resp = await client.post("/api/posts", data=deep, headers={**H, "Content-Type": "application/json"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "Некорректный JSON в запросе"

    # Unauthenticated login requests are parsed too: still a clean 400.
    await _set_password(client)
    _logout_locally(client)
    resp = await client.post("/api/auth/login", data=b'{"password": "x"}',
                             headers={**H, "Content-Type": "application/json; charset=nope"})
    assert resp.status == 400
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_json_body_size_limit(client):
    resp = await client.post("/api/render", data=b'{"text_md": "' + b"a" * (JSON_BODY_LIMIT + 10) + b'"}',
                             headers={**H, "Content-Type": "application/json"})
    assert resp.status == 413
    assert (await resp.json())["error"] == "Запрос слишком большой"


async def test_chunked_json_body(client):
    async def gen():
        yield b'{"text_md": '
        yield b'"**x**"}'

    resp = await client.post("/api/render", data=gen(), headers={**H, "Content-Type": "application/json"})
    assert resp.status == 200
    assert (await resp.json())["html"] == "<b>x</b>"


# ------------------------------------------------------------------ WEB_PASSWORD from .env

@pytest.fixture
async def fixed_client(db, config, bot):
    config.web_password = "env-secret-1"
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


async def test_fixed_password_protects_the_panel(fixed_client, db, config):
    client = fixed_client
    assert await (await client.get("/api/auth/status")).json() == {
        "password_set": True, "password_fixed": True, "password_source": "env", "authenticated": False}
    assert (await client.get("/api/posts")).status == 401
    assert (await client.post("/api/auth/login", json={"password": "wrong"}, headers=H)).status == 401
    resp = await client.post("/api/auth/login", json={"password": "env-secret-1"}, headers=H)
    assert resp.status == 200
    assert (await client.get("/api/posts")).status == 200
    settings = await (await client.get("/api/settings")).json()
    assert settings["password_set"] is True and settings["password_fixed"] is True
    assert settings["password_source"] == "env"
    assert await get_password_hash(db, config) == env_password_hash("env-secret-1")
    assert await get_password_hash(db) is None  # the panel setting itself is untouched


async def test_fixed_password_cannot_be_changed_in_panel(fixed_client, db):
    client = fixed_client
    await client.post("/api/auth/login", json={"password": "env-secret-1"}, headers=H)
    for method in (client.post, client.delete):
        resp = await method("/api/auth/password", json={"current": "env-secret-1", "new": "another-1"}, headers=H)
        assert resp.status == 400
        assert (await resp.json())["error"] == FIXED_PASSWORD_ERROR
    assert await db.get_setting(PASSWORD_HASH_SETTING) is None
    # Resetting the panel password from the bot does not open a panel protected by .env.
    await reset_password(db)
    _logout_locally(client)
    assert (await client.get("/api/posts")).status == 401


async def test_fixed_password_overrides_panel_password_and_rotates_sessions(db, config, bot):
    await db.migrate()
    await db.set_setting(PASSWORD_HASH_SETTING, hash_password("panel-pass"))
    config.web_password = "env-one-111"
    async with TestClient(TestServer(create_web_app(bot, db, config))) as client:
        assert (await client.post("/api/auth/login", json={"password": "panel-pass"}, headers=H)).status == 401
        assert (await client.post("/api/auth/login", json={"password": "env-one-111"}, headers=H)).status == 200
        token = client.session.cookie_jar.filter_cookies(client.make_url("/"))[COOKIE_NAME].value
    config.web_password = "env-two-222"  # changed in .env + restart: old sessions are invalid
    async with TestClient(TestServer(create_web_app(bot, db, config))) as client:
        resp = await client.get("/api/posts", headers={"Cookie": f"{COOKIE_NAME}={token}"})
        assert resp.status == 401


def test_env_password_hash_verifies():
    stored = env_password_hash("пароль-1")
    assert stored.startswith("env$")
    assert verify_password("пароль-1", stored) and not verify_password("пароль-2", stored)


# ------------------------------------------------------------------ V2.1: password_source, env flows

def test_fixed_password_error_text():
    assert FIXED_PASSWORD_ERROR == "Пароль задан в .env (WEB_PASSWORD) — изменить его можно только там"


async def test_password_source_follows_the_panel_password(client):
    assert (await (await client.get("/api/settings")).json())["password_source"] is None
    await _set_password(client)
    assert (await (await client.get("/api/settings")).json())["password_source"] == "panel"
    resp = await client.delete("/api/auth/password", json={"current": "secret1"}, headers=H)
    assert resp.status == 200
    assert (await (await client.get("/api/auth/status")).json())["password_source"] is None


async def test_fixed_password_first_set_is_refused_too(fixed_client, db):
    resp = await fixed_client.post("/api/auth/password", json={"new": "another-1"}, headers=H)
    assert resp.status == 400 and (await resp.json())["error"] == FIXED_PASSWORD_ERROR
    assert await db.get_setting(PASSWORD_HASH_SETTING) is None


async def test_fixed_password_lockout_and_logout(fixed_client):
    client = fixed_client
    for _ in range(5):
        resp = await client.post("/api/auth/login", json={"password": "wrong-pass"}, headers=H)
        assert resp.status == 401
    resp = await client.post("/api/auth/login", json={"password": "env-secret-1"}, headers=H)
    assert resp.status == 429 and resp.headers["Retry-After"]  # locked out even with the right password
    client.app[LOGIN_GUARD_KEY].success("127.0.0.1")
    resp = await client.post("/api/auth/login", json={"password": "env-secret-1"}, headers=H)
    assert resp.status == 200
    assert (await (await client.get("/api/auth/status")).json())["authenticated"] is True
    resp = await client.post("/api/auth/logout", headers=H)
    assert resp.status == 200
    status = await (await client.get("/api/auth/status")).json()
    assert status == {"password_set": True, "password_fixed": True, "password_source": "env",
                      "authenticated": False}


# ------------------------------------------------------------------ sessions: the .env secret

async def test_db_copy_alone_cannot_mint_a_session(client, db, config, bot):
    """Review finding #25: `web_secret` and the password hash are both in bot.db; the session key
    also needs BOT_TOKEN from .env, so a copy of the DB (a backup) is not a panel login."""
    import time

    from app.web.auth import SECRET_SETTING, session_key

    await _set_password(client)
    _logout_locally(client)
    secret = await db.get_setting(SECRET_SETTING)
    stored = await db.get_setting(PASSWORD_HASH_SETTING)
    forged = make_session_token(secret, stored, int(time.time()))
    assert (await client.get("/api/posts", headers={"Cookie": f"{COOKIE_NAME}={forged}"})).status == 401
    valid = make_session_token(session_key(config, secret), stored, int(time.time()))
    assert (await client.get("/api/posts", headers={"Cookie": f"{COOKIE_NAME}={valid}"})).status == 200

    # Changing the bot in the panel restarts the app with the same .env: sessions survive.
    async with TestClient(TestServer(create_web_app(bot, db, config))) as other:
        assert (await other.get("/api/posts", headers={"Cookie": f"{COOKIE_NAME}={valid}"})).status == 200
    # A new BOT_TOKEN in .env logs every session out.
    config.bot_token = "123456:" + "B" * 35
    async with TestClient(TestServer(create_web_app(bot, db, config))) as other:
        assert (await other.get("/api/posts", headers={"Cookie": f"{COOKIE_NAME}={valid}"})).status == 401


def test_session_key_mixes_both_secrets(config):
    from app.web.auth import session_key

    key = session_key(config, "db-secret")
    assert key != "db-secret" and len(key) == 64
    assert session_key(config, "other-secret") != key
    config.bot_token = "654321:" + "C" * 35
    assert session_key(config, "db-secret") != key


async def test_any_valid_session_cookie_is_accepted(client):
    await _set_password(client)
    token = client.session.cookie_jar.filter_cookies(client.make_url("/"))[COOKIE_NAME].value
    _logout_locally(client)
    for cookie in (f"{COOKIE_NAME}=garbage; {COOKIE_NAME}={token}", f'{COOKIE_NAME}="{token}"',
                   f"other=1; {COOKIE_NAME}={token}; x=y"):
        assert (await client.get("/api/posts", headers={"Cookie": cookie})).status == 200, cookie
    for cookie in (f"{COOKIE_NAME}=garbage", f"{COOKIE_NAME}=", f"x{COOKIE_NAME}={token}",
                   f"{COOKIE_NAME}={token}{'0' * 200}"):
        assert (await client.get("/api/posts", headers={"Cookie": cookie})).status == 401, cookie

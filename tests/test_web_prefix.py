"""Web panel under a URL prefix (WEB_PREFIX, SPEC_V21 §2): index injection, static files, API,
redirects, cookie path, auth / CSRF / host guard and the URLs the API returns."""
from __future__ import annotations

import re
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import __version__
from app.db import CH_ACTIVE
from app.web import STATIC_DIR, create_web_app
from app.web.auth import COOKIE_NAME, FIXED_PASSWORD_ERROR
from app.web.pages import asset_version, render_index
from app.web.urls import api_url

H = {"X-Requested-With": "pcb"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 100
CHANNEL = -1001234567890

TEMPLATE = """<!doctype html>
<html lang="ru" data-app="posts">
<head>
  <meta charset="utf-8">
  <base href="/">
  <meta name="pcb-hub" content="">
  <meta name="pcb-version" content="">
  <link rel="icon" href="static/missing.svg">
  <link rel="stylesheet" href="static/style.css">
  <script src="static/app.js" defer></script>
</head>
<body><a href="#/posts">Посты</a></body>
</html>
"""


@pytest.fixture
def static_dir(tmp_path) -> Path:
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "index.html").write_text(TEMPLATE, encoding="utf-8")
    (directory / "app.js").write_text("console.log('panel');\n")
    (directory / "style.css").write_text("body { margin: 0; }\n")
    (tmp_path / "secret.txt").write_text("outside of static")
    return directory


@pytest.fixture
async def make_client(db, config, bot, static_dir):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    bot.add_chat(CHANNEL, "Канал", "some_channel")
    await db.upsert_channel(CHANNEL, "Канал", "some_channel", status=CH_ACTIVE,
                            rights={"can_post": True, "can_edit": True, "can_delete": True})
    clients: list[TestClient] = []

    async def make(prefix: str = "/posts", **config_fields) -> TestClient:
        config.web_prefix = prefix
        for key, value in config_fields.items():
            setattr(config, key, value)
        client = TestClient(TestServer(create_web_app(bot, db, config, static_dir=static_dir)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


@pytest.fixture
async def client(make_client):
    return await make_client("/posts", hub_url="/")


async def _upload(client: TestClient, prefix: str = "/posts") -> dict:
    form = aiohttp.FormData()
    form.add_field("file", PNG, filename="pic.png", content_type="image/png")
    resp = await client.post(f"{prefix}/api/upload", data=form, headers=H)
    assert resp.status == 200, await resp.text()
    return await resp.json()


# ------------------------------------------------------------------ index.html

async def test_index_is_served_with_injected_values(client, static_dir):
    resp = await client.get("/posts/")
    assert resp.status == 200 and resp.content_type == "text/html" and resp.charset == "utf-8"
    assert resp.headers["Cache-Control"] == "no-cache"
    page = await resp.text()
    assert '<base href="/posts/">' in page and '<base href="/">' not in page
    assert '<meta name="pcb-hub" content="/">' in page
    assert f'<meta name="pcb-version" content="{__version__}">' in page
    css, js = asset_version(static_dir / "style.css"), asset_version(static_dir / "app.js")
    assert re.fullmatch(r"[0-9a-f]{10}", css) and css != js
    assert f'href="static/style.css?v={css}"' in page
    assert f'src="static/app.js?v={js}" defer' in page
    assert 'href="static/missing.svg"' in page  # a missing file gets no version
    assert '<a href="#/posts">' in page


async def test_asset_version_changes_with_the_file(client, static_dir):
    before = re.search(r"app\.js\?v=([0-9a-f]+)", await (await client.get("/posts/")).text()).group(1)
    (static_dir / "app.js").write_text("console.log('a new version of the panel');\n")
    after = re.search(r"app\.js\?v=([0-9a-f]+)", await (await client.get("/posts/")).text()).group(1)
    assert before != after


async def test_index_without_hub_and_prefix(make_client):
    client = await make_client("")
    resp = await client.get("/")
    page = await resp.text()
    assert resp.status == 200
    assert '<base href="/">' in page and '<meta name="pcb-hub" content="">' in page
    assert (await client.get("/posts")).status == 404  # no redirects without a prefix
    assert (await client.get("/posts/")).status == 404


async def test_nested_prefix(make_client):
    client = await make_client("tools/posts/")
    page = await (await client.get("/tools/posts/")).text()
    assert '<base href="/tools/posts/">' in page
    assert (await client.get("/tools/posts/api/channels")).status == 200
    resp = await client.get("/tools/posts", allow_redirects=False)
    assert resp.status == 301 and resp.headers["Location"] == "/tools/posts/"


def test_render_index_is_robust_to_attribute_order(tmp_path):
    (tmp_path / "app.js").write_text("x")
    template = ("<html><head><meta content='' name='pcb-version' >\n"
                "<BASE  href='/' >\n<meta content=\"\"   name=\"pcb-hub\"/>"
                "<meta name=\"pcb-hub-extra\" content=\"keep\">"
                "<script defer src='./static/app.js?v=old'></script></head></html>")
    page = render_index(template, prefix="/p", hub_url='/hub?a=1&b="2"', version="9.9.9", static_dir=tmp_path)
    assert '<base href="/p/">' in page
    assert '<meta name="pcb-hub" content="/hub?a=1&amp;b=&quot;2&quot;">' in page
    assert '<meta name="pcb-version" content="9.9.9">' in page
    assert '<meta name="pcb-hub-extra" content="keep">' in page
    assert f"src='./static/app.js?v={asset_version(tmp_path / 'app.js')}'" in page


def test_render_index_inserts_a_missing_base(tmp_path):
    page = render_index("<html><head><title>x</title></head></html>", prefix="/p", hub_url=None,
                        version="1", static_dir=tmp_path)
    assert page.startswith('<html><head><base href="/p/"><title>')


def test_render_index_ignores_links_leaving_static(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    page = render_index('<link href="static/../secret.txt">', prefix="", hub_url=None, version="1",
                        static_dir=static)
    assert page.endswith('<link href="static/../secret.txt">')


def test_real_index_has_the_placeholders():
    """The SPA template shipped in app/web/static carries the tags the server fills."""
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    page = render_index(template, prefix="/posts", hub_url="/", version=__version__, static_dir=STATIC_DIR)
    assert '<base href="/posts/">' in page
    assert '<meta name="pcb-hub" content="/">' in page
    assert f'<meta name="pcb-version" content="{__version__}">' in page
    assert re.search(r'src="static/app\.js\?v=[0-9a-f]{10}"', page)
    assert re.search(r'href="static/style\.css\?v=[0-9a-f]{10}"', page)
    assert not re.search(r"""(?:href|src)=["']/(?:static|api)/""", page)  # assets are relative to <base>


async def test_index_missing(make_client, static_dir):
    (static_dir / "index.html").unlink()
    client = await make_client("/posts")
    assert (await client.get("/posts/")).status == 404


# ------------------------------------------------------------------ redirects and static files

async def test_redirects(client):
    resp = await client.get("/posts", allow_redirects=False)
    assert resp.status == 301 and resp.headers["Location"] == "/posts/"
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/posts/"
    resp = await client.get("/?from=hub", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/posts/?from=hub"
    resp = await client.get("/posts")  # followed
    assert resp.status == 200 and '<base href="/posts/">' in await resp.text()


async def test_static_under_prefix(client):
    resp = await client.get("/posts/static/app.js?v=whatever")
    assert resp.status == 200 and "panel" in await resp.text()
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert (await client.get("/posts/static/style.css")).status == 200
    assert (await client.get("/static/app.js")).status == 404  # outside the prefix
    assert (await client.get("/posts/static/../secret.txt")).status in (403, 404)
    assert (await client.get("/posts/static/%2e%2e/secret.txt")).status in (403, 404)
    assert (await client.get("/postsx/static/app.js")).status == 404


# ------------------------------------------------------------------ API

async def test_api_under_prefix(client):
    resp = await client.get("/posts/api/channels")
    assert resp.status == 200 and resp.headers["Cache-Control"] == "no-store"
    assert (await client.get("/api/channels")).status == 404  # not served outside the prefix
    resp = await client.get("/posts/api/nothing-here")
    assert resp.status == 404 and await resp.json() == {"error": "Не найдено"}
    resp = await client.delete("/posts/api/overview", headers=H)
    assert resp.status == 405 and (await resp.json())["error"] == "Метод не поддерживается"


async def test_settings_report_deployment_values(client, config):
    data = await (await client.get("/posts/api/settings")).json()
    assert data["web_prefix"] == "/posts" and data["hub_url"] == "/" and data["version"] == __version__
    assert data["web_url"] == config.web_url == "http://127.0.0.1:8080/posts/"
    overview = await (await client.get("/posts/api/overview")).json()
    assert overview["web_url"] == "http://127.0.0.1:8080/posts/"


async def test_csrf_and_host_guard_under_prefix(client):
    resp = await client.post("/posts/api/folders", json={"name": "Новости"})
    assert resp.status == 403 and "X-Requested-With" in (await resp.json())["error"]
    resp = await client.get("/posts/api/channels", headers={"Host": "evil.example.com"})
    assert resp.status == 403 and "без пароля" in (await resp.json())["error"]
    # The SPA page and the health check are not guarded.
    assert (await client.get("/posts/", headers={"Host": "evil.example.com"})).status == 200
    assert (await client.get("/posts/api/health", headers={"Host": "evil.example.com"})).status == 200


async def test_password_session_cookie_is_scoped_to_the_prefix(client):
    resp = await client.post("/posts/api/auth/password", json={"new": "secret1"}, headers=H)
    assert resp.status == 200, await resp.text()
    cookie = resp.cookies[COOKIE_NAME]
    assert cookie["path"] == "/posts/" and cookie["httponly"]
    assert (await client.get("/posts/api/channels")).status == 200

    resp = await client.post("/posts/api/auth/logout", headers=H)
    assert resp.cookies[COOKIE_NAME]["path"] == "/posts/" and resp.cookies[COOKIE_NAME].value == ""
    client.session.cookie_jar.clear()
    resp = await client.get("/posts/api/channels")
    assert resp.status == 401 and (await resp.json())["error"] == "auth_required"
    assert (await client.get("/posts/api/health")).status == 200  # always open

    resp = await client.post("/posts/api/auth/login", json={"password": "wrong-1"}, headers=H)
    assert resp.status == 401
    resp = await client.post("/posts/api/auth/login", json={"password": "secret1"}, headers=H)
    assert resp.status == 200 and resp.cookies[COOKIE_NAME]["path"] == "/posts/"
    assert (await client.get("/posts/api/channels")).status == 200
    status = await (await client.get("/posts/api/auth/status")).json()
    assert status["authenticated"] is True and status["password_source"] == "panel"


async def test_env_password_under_prefix(make_client):
    client = await make_client("/posts", web_password="env-secret-1")
    assert (await client.get("/posts/api/posts")).status == 401
    status = await (await client.get("/posts/api/auth/status")).json()
    assert status["password_source"] == "env" and status["authenticated"] is False
    resp = await client.post("/posts/api/auth/login", json={"password": "env-secret-1"}, headers=H)
    assert resp.status == 200 and resp.cookies[COOKIE_NAME]["path"] == "/posts/"
    assert (await client.get("/posts/api/posts")).status == 200
    resp = await client.post("/posts/api/auth/password", json={"current": "env-secret-1", "new": "other-pass"},
                             headers=H)
    assert resp.status == 400 and (await resp.json())["error"] == FIXED_PASSWORD_ERROR


# ------------------------------------------------------------------ URLs returned by the API

async def test_upload_and_media_urls_carry_the_prefix(client):
    upload = await _upload(client)
    assert upload["url"] == f"/posts/api/uploads/{upload['path']}"
    resp = await client.get(upload["url"])
    assert resp.status == 200 and await resp.read() == PNG

    resp = await client.post("/posts/api/posts", json={
        "text_md": "С картинкой", "media": [{"type": "photo", "path": upload["path"]}],
        "channel_ids": [CHANNEL], "action": "draft"}, headers=H)
    assert resp.status == 200, await resp.text()
    post = (await resp.json())["post"]
    url = post["media"][0]["url"]
    assert url.startswith(f"/posts/api/posts/{post['id']}/media/0?v=")
    assert post["photo_url"] == url
    resp = await client.get(url)
    assert resp.status == 200 and await resp.read() == PNG

    listed = await (await client.get("/posts/api/posts?status=draft")).json()
    assert listed["posts"][0]["media"][0]["url"] == url
    # The prefix belongs to the request only: no leak into code running outside of it.
    assert api_url("/api/x") == "/api/x"


async def test_plan_thumbnails_carry_the_prefix(client):
    every_day = {str(day): ["09:00", "21:00"] for day in range(7)}
    resp = await client.put(f"/posts/api/channels/{CHANNEL}/slots", json={"slots": every_day}, headers=H)
    assert resp.status == 200, await resp.text()
    upload = await _upload(client)
    resp = await client.post("/posts/api/posts", json={
        "text_md": "В очередь", "media": [{"type": "photo", "path": upload["path"]}],
        "channel_ids": [CHANNEL], "action": "queue"}, headers=H)
    assert resp.status == 200, await resp.text()
    post_id = (await resp.json())["post"]["id"]
    items = (await (await client.get("/posts/api/plan/timeline?days=7")).json())["items"]
    (item,) = [i for i in items if i["post"]]
    assert item["post"]["thumb_url"].startswith(f"/posts/api/posts/{post_id}/media/0?v=")


async def test_no_prefix_urls_are_unchanged(make_client):
    client = await make_client("")
    upload = await _upload(client, prefix="")
    assert upload["url"] == f"/api/uploads/{upload['path']}"
    resp = await client.post("/api/auth/password", json={"new": "secret1"}, headers=H)
    assert resp.cookies[COOKIE_NAME]["path"] == "/"


# ------------------------------------------------------------------ stale cookies of an unprefixed panel

async def test_stale_root_cookie_does_not_shadow_the_session(make_client, db):
    """Review finding #24: a `pcb_session` with Path=/ (panel without a prefix on the same host,
    the password changed since) is sent after the current /posts/ cookie — the request must still
    be authenticated, and login / logout expire the cookie on the legacy paths."""
    old = await make_client("")
    resp = await old.post("/api/auth/password", json={"new": "old-pass-1"}, headers=H)
    stale = resp.cookies[COOKIE_NAME].value
    await db.set_setting("web_password_hash", None)  # «Сбросить пароль» in the bot

    client = await make_client("/posts")
    resp = await client.post("/posts/api/auth/password", json={"new": "new-pass-1"}, headers=H)
    fresh = resp.cookies[COOKIE_NAME].value
    client.session.cookie_jar.clear()
    browser_order = {"Cookie": f"{COOKIE_NAME}={fresh}; {COOKIE_NAME}={stale}"}  # longer path first
    assert (await client.get("/posts/api/channels", headers=browser_order)).status == 200
    status = await (await client.get("/posts/api/auth/status", headers=browser_order)).json()
    assert status["authenticated"] is True
    reverse = {"Cookie": f"{COOKIE_NAME}={stale}; {COOKIE_NAME}={fresh}"}
    assert (await client.get("/posts/api/channels", headers=reverse)).status == 200
    only_stale = {"Cookie": f"{COOKIE_NAME}={stale}"}
    assert (await client.get("/posts/api/channels", headers=only_stale)).status == 401
    many = {"Cookie": "; ".join([f"{COOKIE_NAME}={stale}"] * 10 + [f"{COOKIE_NAME}={fresh}"])}
    assert (await client.get("/posts/api/channels", headers=many)).status == 401  # at most 5 are checked

    resp = await client.post("/posts/api/auth/login", json={"password": "new-pass-1"}, headers=H)
    cookies = resp.headers.getall("Set-Cookie")
    assert any("Path=/;" in c or c.endswith("Path=/") for c in cookies if "Max-Age=0" in c)
    assert any(c.endswith("Path=/posts") for c in cookies if "Max-Age=0" in c)
    assert resp.cookies[COOKIE_NAME]["path"] == "/posts/" and resp.cookies[COOKIE_NAME].value  # still logged in
    session = [c for c in cookies if "Max-Age=0" not in c]
    assert len(session) == 1 and "Path=/posts/" in session[0]

    resp = await client.post("/posts/api/auth/logout", headers=H)
    expired = sorted(c.rsplit("Path=", 1)[1] for c in resp.headers.getall("Set-Cookie") if "Max-Age=0" in c)
    assert expired == ["/", "/posts", "/posts/"]


async def test_no_legacy_cookie_paths_without_a_prefix(make_client):
    client = await make_client("")
    resp = await client.post("/api/auth/password", json={"new": "secret1"}, headers=H)
    assert resp.headers.getall("Set-Cookie") and all("Max-Age=0" not in c for c in resp.headers.getall("Set-Cookie"))
    resp = await client.post("/api/auth/logout", headers=H)
    assert [c.rsplit("Path=", 1)[1] for c in resp.headers.getall("Set-Cookie")] == ["/"]

"""The shipped SPA (app/web/static) keeps the V2.1 deployment contract.

* every request and asset path is relative to ``<base>`` — the panel works under any WEB_PREFIX;
* the unified panel design: tokens + the «Посты» accent, the shared theme key ``panel-theme``;
* the files go to a public repository: no server addresses, paths or other panels' names.
"""
from __future__ import annotations

import re

import pytest

from app.web import STATIC_DIR

APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("name, text", [("app.js", APP_JS), ("index.html", INDEX_HTML)])
def test_no_absolute_api_or_static_paths(name, text):
    # '/api/…' or '/static/…' at the start of a string literal or an attribute would bypass <base>
    # and break the panel under a prefix.
    assert not re.search(r"""["'`(]/(?:api|static)(?:/|["'`?])""", text), name


def test_requests_go_through_relative_paths():
    assert "fetch('api/health'" in APP_JS
    assert "xhr.open('POST', 'api/upload')" in APP_JS
    assert re.search(r"api\.get\('api/auth/status'", APP_JS)


def test_index_shell():
    assert re.search(r'<html lang="ru" data-app="posts">', INDEX_HTML)
    assert '<base href="/">' in INDEX_HTML
    assert '<meta name="pcb-hub" content="">' in INDEX_HTML
    assert '<meta name="pcb-version" content="">' in INDEX_HTML
    # theme before the first paint, with the one-time migration of the old key
    assert "'panel-theme'" in INDEX_HTML and "'pcb.theme'" in INDEX_HTML
    # the hub link is hidden until the server provides HUB_URL
    assert re.search(r'id="hub-link"[^>]*hidden', INDEX_HTML)
    for group in ("Публикации", "Каналы", "Система"):
        assert f'<div class="nav-label">{group}</div>' in INDEX_HTML


def test_design_tokens_and_accent():
    for token in ("--sidebar-w: 240px", "--topbar-h: 56px", "--radius-lg: 14px", "--control-h: 36px"):
        assert token in STYLE_CSS, token
    assert ':root[data-app="posts"] { --accent: #2481cc;' in STYLE_CSS
    assert ':root[data-app="posts"][data-theme="dark"] { --accent: #4fa8ee;' in STYLE_CSS
    assert "THEME_KEY = 'panel-theme'" in APP_JS


@pytest.mark.parametrize("name, text", [("app.js", APP_JS), ("style.css", STYLE_CSS), ("index.html", INDEX_HTML)])
def test_no_private_deployment_details(name, text):
    # no private network addresses or server paths
    assert not re.search(r"\b(?:10|127|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text), name
    assert "/root/" not in text, name
    # only this panel's accent: the design tokens of other panels are not shipped
    assert set(re.findall(r'data-app="([^"]+)"', text)) <= {"posts"}, name


def test_bot_change_contract():
    """SPEC_V22 «Настройки» → «Бот»: the token goes only to api/settings/bot-token from a password
    field that is cleared before the request; the restart is followed on api/health."""
    assert "api.post('api/settings/bot-token', { token })" in APP_JS
    assert "api.del('api/settings/bot-token')" in APP_JS
    assert 'type="password" id="bot-token-input"' in APP_JS and 'autocomplete="off"' in APP_JS
    clear = APP_JS.index("input.value = '';", APP_JS.index("async function changeBot"))
    assert clear < APP_JS.index("api.post('api/settings/bot-token'")
    # Back to .env waits for the bot the server named; a third bot answering = fallback to .env.
    assert "res && res.bot && typeof res.bot === 'object' ? res.bot : {}" in APP_JS
    assert "fallback: true" in APP_JS and "Telegram не принял новый токен при запуске" in APP_JS
    # No token is ever logged in the browser console.
    assert not re.search(r"console\.\w+\([^)]*token", APP_JS)

"""Typed keys for objects stored in the aiohttp application (`app[KEY]`)."""
from __future__ import annotations

import ipaddress
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from app.config import Config, TokenState
from app.db import Database

if TYPE_CHECKING:
    from app.services.scheduler import Scheduler
    from app.web.auth import LoginGuard
    from app.web.idempotency import IdempotencyStore

# The aiogram Bot (or tests.fakes.FakeBot in tests).
BOT_KEY: web.AppKey[Any] = web.AppKey("bot")
DB_KEY: web.AppKey[Database] = web.AppKey("db", Database)
CONFIG_KEY: web.AppKey[Config] = web.AppKey("config", Config)
SCHEDULER_KEY: web.AppKey[Scheduler | None] = web.AppKey("scheduler")
# In-memory brute-force guard of the login form.
LOGIN_GUARD_KEY: web.AppKey[LoginGuard] = web.AppKey("login_guard")
# Mutable cache {"info": {...}} for the result of bot.get_me() (filled on first use).
BOT_INFO_KEY: web.AppKey[dict[str, Any]] = web.AppKey("bot_info", dict)
# Reverse proxies whose X-Forwarded-* headers are trusted (empty tuple = none; see auth.trusted_proxies).
TRUSTED_PROXIES_KEY: web.AppKey[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = web.AppKey(
    "trusted_proxies", tuple
)
# Telegram file downloads in progress: cache name -> [lock, users] (one download per file at a time).
DOWNLOAD_LOCKS_KEY: web.AppKey[dict[str, list[Any]]] = web.AppKey("download_locks", dict)
# URL prefix of the panel (normalized WEB_PREFIX: '' or '/posts'); see app.web.urls.
PREFIX_KEY: web.AppKey[str] = web.AppKey("prefix", str)
# Directory with index.html, app.js, style.css (tests may serve another one).
STATIC_DIR_KEY: web.AppKey[Path] = web.AppKey("static_dir", Path)
# The running bot's token: source (.env / panel), masked token, panel token rejected (SPEC_V22).
TOKEN_STATE_KEY: web.AppKey[TokenState] = web.AppKey("token_state", TokenState)
# async checker(token) -> {"id", "username", "name"} of that bot; raises aiogram's Telegram errors
# (TelegramUnauthorizedError for a wrong token). Default: app.web.api_bot.check_token; tests stub it.
TOKEN_CHECKER_KEY: web.AppKey[Callable[[str], Awaitable[dict[str, Any]]]] = web.AppKey("token_checker")
# Callable that restarts the bot in-process (set by app.__main__.run; None = no restart available).
RESTART_KEY: web.AppKey[Callable[[], None] | None] = web.AppKey("restart")
# Answers of requests sent with X-Idempotency-Key (see app.web.idempotency).
IDEMPOTENCY_KEY: web.AppKey[IdempotencyStore] = web.AppKey("idempotency")

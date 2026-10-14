"""Web panel: aiohttp application serving the SPA (`app/web/static/`) and the JSON API (`/api`).

    create_web_app(bot, db, config, scheduler=None) -> aiohttp.web.Application

Everything is served under ``config.web_prefix`` (``WEB_PREFIX``, '' by default — see
`app.web.urls`): ``<prefix>/`` returns `static/index.html` with the deployment values injected
(`app.web.pages`), ``<prefix>/static/<file>`` serves the other static files, everything under
``<prefix>/api`` is handled by `app.web.auth` (login/password), `app.web.api` (posts, channels,
admins, settings), `app.web.api_bot` (changing the bot by its token), `app.web.api_media` (render,
uploads, media files), `app.web.api_plan` (folders, slots, content plan) and `app.web.health`
(``/api/health``, always open). With a prefix, ``GET <prefix>`` → 301 ``<prefix>/`` and
``GET /`` → 302 ``<prefix>/``.

The requests that publish accept ``X-Idempotency-Key`` (`app.web.idempotency`: a retry never
publishes twice). `app.web.logmask` keeps bot tokens out of the log (installed here).

`app.__main__` sets ``app[TOKEN_STATE_KEY]`` (where the running bot's token comes from) and
``app[RESTART_KEY]`` (in-process restart after the token was changed) before the app starts;
the defaults describe the .env token and offer no restart.

Environment: ``TRUST_PROXY`` (see `app.web.auth.trusted_proxies`) — trust X-Forwarded-For /
X-Forwarded-Proto from a reverse proxy on this machine (``1``) or from the listed addresses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from app.config import Config, TokenState, mask_token, normalize_prefix
from app.db import Database
from app.media import VIDEO_MAX_BYTES
from app.web import api, api_bot, api_media, api_plan, auth, health, pages, urls
from app.web.idempotency import IdempotencyStore
from app.web.keys import (
    BOT_INFO_KEY,
    BOT_KEY,
    CONFIG_KEY,
    DB_KEY,
    DOWNLOAD_LOCKS_KEY,
    IDEMPOTENCY_KEY,
    LOGIN_GUARD_KEY,
    PREFIX_KEY,
    RESTART_KEY,
    SCHEDULER_KEY,
    STATIC_DIR_KEY,
    TOKEN_CHECKER_KEY,
    TOKEN_STATE_KEY,
    TRUSTED_PROXIES_KEY,
)
from app.web.logmask import install_log_mask

__all__ = ["create_web_app", "STATIC_DIR", "CLIENT_MAX_SIZE"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Videos / GIFs are uploaded up to 50 MB (+ multipart overhead). Uploads are streamed with their
# own caps and JSON bodies are capped in auth.read_json, so this only bounds other body reads.
CLIENT_MAX_SIZE = VIDEO_MAX_BYTES + 2 * 1024 * 1024


def create_web_app(bot: Any, db: Database, config: Config, scheduler: Any = None, *,
                   static_dir: Path | None = None) -> web.Application:
    """Build the panel application. `bot` is an aiogram Bot (or a compatible fake in tests).

    Also installs the log mask (`app.web.logmask`): no bot token — BOT_TOKEN from .env, the
    running bot's token, anything shaped like a token — reaches the log."""
    install_log_mask(config.bot_token, getattr(bot, "token", None))
    Path(config.uploads_dir).mkdir(parents=True, exist_ok=True)
    app = web.Application(
        middlewares=[auth.proxy_middleware, urls.prefix_middleware, api.error_middleware, auth.auth_middleware],
        client_max_size=CLIENT_MAX_SIZE,
    )
    app[BOT_KEY] = bot
    app[DB_KEY] = db
    app[CONFIG_KEY] = config
    app[SCHEDULER_KEY] = scheduler
    app[LOGIN_GUARD_KEY] = auth.LoginGuard()
    app[BOT_INFO_KEY] = {}
    app[TRUSTED_PROXIES_KEY] = auth.trusted_proxies(config)
    app[DOWNLOAD_LOCKS_KEY] = {}
    app[IDEMPOTENCY_KEY] = IdempotencyStore()
    app[TOKEN_STATE_KEY] = TokenState(masked=mask_token(config.bot_token))
    app[TOKEN_CHECKER_KEY] = api_bot.check_token
    app[RESTART_KEY] = None
    prefix = normalize_prefix(config.web_prefix)
    app[PREFIX_KEY] = prefix
    app[STATIC_DIR_KEY] = Path(static_dir) if static_dir is not None else STATIC_DIR

    health.setup_routes(app)
    auth.setup_routes(app)
    api.setup_routes(app)
    api_bot.setup_routes(app)
    api_media.setup_routes(app)
    api_plan.setup_routes(app)
    router = urls.prefixed_router(app)
    router.add_get("/", pages.index)
    router.add_static("/static/", app[STATIC_DIR_KEY], follow_symlinks=False)
    if prefix:
        app.router.add_get(prefix, pages.redirect_permanent)
        app.router.add_get("/", pages.redirect_root)
    app.on_response_prepare.append(_add_security_headers)
    return app


async def _add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    path = urls.local_path(request)
    if path is None:
        return
    if auth.is_api_path(path):
        # API data is private and changes constantly; media files set their own Cache-Control.
        headers.setdefault("Cache-Control", "no-store")
    elif path.startswith("/static/"):
        # Revalidate (cheap 304) so an updated app.js is picked up right after a restart.
        headers.setdefault("Cache-Control", "no-cache")

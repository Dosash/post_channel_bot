"""Web API — changing the publishing bot by its token (SPEC_V22, «Настройки» → «Бот»).

    POST   /api/settings/bot-token   {"token": "123456789:AA…"}
                                     → {"ok": true, "bot": {"id", "username", "name"}, "restarting": bool}
    DELETE /api/settings/bot-token   → {"ok": true, "restarting": bool, "bot": {…} | null}
                                     (back to BOT_TOKEN from .env)

``GET /api/settings`` describes the running bot (`app.web.api.bot_settings`). A new token is
checked with Telegram (getMe) by the checker in ``app[TOKEN_CHECKER_KEY]`` (default `check_token`:
a temporary aiogram Bot, 10 s timeout), stored in the DB setting ``bot_token`` and the bot is
restarted in-process (``app[RESTART_KEY]``, set by ``app.__main__``) shortly after the answer
was sent. ``restarting`` is false when nothing has to restart (no restart hook — e.g. tests — or,
for DELETE, the bot already runs on the .env token because the panel token was rejected).

DELETE while the panel token is in use first asks Telegram about BOT_TOKEN from .env (the same
checker): a rejected .env token would stop the bot for good after the restart (exit code 1 — the
panel goes down with it), so the panel token is kept and 400 is returned. ``bot`` is the .env bot
then (the panel waits for it), null when the bot already runs on .env.

The token is a secret: it is never logged nor returned — only masked (``…`` + last 4 characters).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramUnauthorizedError,
)
from aiogram.utils.token import TokenValidationError
from aiohttp import web

from app.config import (
    BOT_TOKEN_SETTING,
    TOKEN_SOURCE_PANEL,
    install_log_redaction,
    is_bot_token,
    mask_token,
    redact_tokens,
)
from app.web.auth import json_error, read_json
from app.web.keys import BOT_KEY, CONFIG_KEY, DB_KEY, RESTART_KEY, TOKEN_CHECKER_KEY, TOKEN_STATE_KEY
from app.web.urls import prefixed_router

log = logging.getLogger(__name__)
# Saving the token goes through aiosqlite, which logs SQL parameters at DEBUG level.
install_log_redaction(root_handlers=False)

TOKEN_CHECK_TIMEOUT = 10.0  # seconds for the getMe of a new token
RESTART_DELAY = 0.5  # seconds between the answer and the restart (the answer must reach the browser)

TOKEN_FORMAT_ERROR = "Это не похоже на токен бота (пример: 123456789:AA…)"
TOKEN_REJECTED_ERROR = "Telegram не принял токен"
TOKEN_NETWORK_ERROR = "Не удалось связаться с Telegram, попробуйте ещё раз"
SAME_BOT_ERROR = "Это тот же бот, что работает сейчас"
NO_PANEL_TOKEN_ERROR = "Токен из панели не задан"
ENV_TOKEN_REJECTED_ERROR = ("Telegram не принимает токен из .env (BOT_TOKEN) — бот остаётся на токене из панели. "
                            "Исправьте BOT_TOKEN в .env или вставьте токен другого бота.")

# Telegram's answers meaning "this token is not valid" (401 Unauthorized; 404 for a mangled token).
_REJECTED = (TelegramUnauthorizedError, TelegramNotFound, TelegramForbiddenError, TelegramBadRequest,
             TokenValidationError)


async def check_token(token: str) -> dict[str, Any]:
    """getMe of the bot with `token` through a temporary aiogram Bot (its session is closed
    afterwards). Returns {"id", "username", "name"}; Telegram / network errors propagate."""
    bot = Bot(token, session=AiohttpSession(timeout=TOKEN_CHECK_TIMEOUT))
    try:
        me = await bot.get_me(request_timeout=int(TOKEN_CHECK_TIMEOUT))
    finally:
        await bot.session.close()
    return {"id": me.id, "username": me.username, "name": me.full_name}


def _bot_info(raw: Any) -> dict[str, Any]:
    """The checker's answer as {"id", "username", "name"} (a dict or an aiogram User)."""
    if isinstance(raw, dict):
        return {"id": raw.get("id"), "username": raw.get("username"), "name": raw.get("name")}
    return {"id": getattr(raw, "id", None), "username": getattr(raw, "username", None),
            "name": getattr(raw, "full_name", None)}


def schedule_restart(app: web.Application, delay: float | None = None) -> bool:
    """Restart the bot `delay` seconds from now (after the current answer was sent). False when
    this app has no restart hook."""
    restart = app.get(RESTART_KEY)
    if restart is None:
        return False
    asyncio.get_running_loop().call_later(RESTART_DELAY if delay is None else delay, restart)
    return True


async def _check(app: web.Application, token: str, *, rejected_error: str = TOKEN_REJECTED_ERROR) -> dict[str, Any]:
    """Ask Telegram who `token` is: bot info, or a JSON error (400 `rejected_error` / 502 unreachable)."""
    masked = mask_token(token)
    try:
        info = _bot_info(await asyncio.wait_for(app[TOKEN_CHECKER_KEY](token), TOKEN_CHECK_TIMEOUT + 2))
    except _REJECTED as exc:
        log.warning("Web: Telegram rejected the bot token %s: %s", masked, redact_tokens(str(exc)))
        raise json_error(400, rejected_error) from None
    except Exception as exc:  # network down, timeout, Telegram 5xx, flood control…
        log.warning("Web: could not check the bot token %s: %s: %s", masked, type(exc).__name__,
                    redact_tokens(str(exc)))
        raise json_error(502, TOKEN_NETWORK_ERROR) from None
    if not isinstance(info.get("id"), int):
        log.warning("Web: the token check of %s returned no bot id", masked)
        raise json_error(502, TOKEN_NETWORK_ERROR)
    return info


async def set_bot_token(request: web.Request) -> web.Response:
    app = request.app
    body = await read_json(request)
    raw = body.get("token")
    token = raw.strip() if isinstance(raw, str) else ""
    if not is_bot_token(token):
        raise json_error(400, TOKEN_FORMAT_ERROR)
    info = await _check(app, token)
    if info["id"] == getattr(app[BOT_KEY], "id", None):
        raise json_error(400, SAME_BOT_ERROR)
    db = app[DB_KEY]
    if token == app[CONFIG_KEY].bot_token:
        # The bot from .env: no need to keep a copy of its token in the database.
        await db.set_setting(BOT_TOKEN_SETTING, None)
    else:
        await db.set_setting(BOT_TOKEN_SETTING, token)
    restarting = schedule_restart(app)
    log.warning("Web: the publishing bot is changed to @%s (id %s, token %s)%s", info.get("username"),
                info["id"], mask_token(token), " — restarting" if restarting else "")
    return web.json_response({"ok": True, "bot": info, "restarting": restarting})


async def delete_bot_token(request: web.Request) -> web.Response:
    app = request.app
    db = app[DB_KEY]
    state = app[TOKEN_STATE_KEY]
    if await db.get_setting(BOT_TOKEN_SETTING) is None:
        raise json_error(400, NO_PANEL_TOKEN_ERROR)
    env_bot = None
    in_use = state.source == TOKEN_SOURCE_PANEL
    if in_use:
        # Running on the panel token: make sure the bot can start with BOT_TOKEN from .env at all.
        env_bot = await _check(app, app[CONFIG_KEY].bot_token, rejected_error=ENV_TOKEN_REJECTED_ERROR)
    await db.set_setting(BOT_TOKEN_SETTING, None)
    # A rejected panel token is not in use: the bot already runs on BOT_TOKEN from .env and keeps
    # running — only its "rejected" note goes away (the same TokenState is shown by the bot).
    state.panel_token_rejected = False
    restarting = in_use and schedule_restart(app)
    log.warning("Web: the bot token set in the panel is removed — back to BOT_TOKEN from .env%s",
                " — restarting" if restarting else "")
    return web.json_response({"ok": True, "restarting": restarting, "bot": env_bot})


def setup_routes(app: web.Application) -> None:
    r = prefixed_router(app)
    r.add_post("/api/settings/bot-token", set_bot_token)
    r.add_delete("/api/settings/bot-token", delete_bot_token)

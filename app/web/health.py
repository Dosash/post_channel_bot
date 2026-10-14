"""``GET <prefix>/api/health`` — state of the bot for the panel's status pill and for monitoring.

Always open (no password, CSRF or host check — see app.web.auth), ``Cache-Control: no-store``::

    {"status": "ok" | "degraded", "version": "2.1.0", "bot": "@username" | null,
     "scheduled": 3,          # pending post targets with a time (the content plan)
     "failed_24h": 0,         # posts that became failed / partial in the last 24 h (by updated_at)
     "pending_channels": 1,   # channels waiting for approval
     "channels": 5,           # active channels
     "problems": ["…"]}       # Russian reasons for "degraded"

``degraded`` when the bot identity (getMe) could not be fetched yet, the scheduler is not running,
its last iteration raised, or it found Telegram unreachable before publishing due posts. When the
database cannot be read the answer is 503 with ``"status": "error"``. Problems never carry raw
exception texts (the endpoint is open) — the details are in the log.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from app.db import (
    CH_ACTIVE,
    CH_PENDING,
    POST_DELETED,
    POST_DRAFT,
    POST_FAILED,
    POST_PARTIAL,
    TARGET_PENDING,
    Database,
    now,
)
from app.web.api import cached_bot_info
from app.web.keys import CONFIG_KEY, DB_KEY, SCHEDULER_KEY
from app.web.urls import prefixed_router

log = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_ERROR = "error"

GETME_TIMEOUT = 5.0  # seconds a health request may wait for Telegram
GETME_RETRY = 30.0  # after a failed getMe, health does not ask Telegram again for this long
# (the negative cache is shared with «Обзор» and «Настройки» — app.web.api.cached_bot_info)
FAILED_WINDOW = 24 * 3600

TELEGRAM_PROBLEM = "Нет связи с Telegram: бот ещё не получил свои данные (getMe)"
SCHEDULER_STOPPED_PROBLEM = "Планировщик остановлен — отложенные посты не публикуются"
SCHEDULER_ERROR_PROBLEM = "Последний цикл планировщика завершился ошибкой — подробности в логе бота"
SCHEDULER_TELEGRAM_PROBLEM = "Telegram недоступен — отложенные посты выйдут, когда связь восстановится"
DATABASE_PROBLEM = "База данных недоступна — подробности в логе бота"


async def health_counts(db: Database, ts: int | None = None) -> dict[str, int]:
    """The numbers of the health answer, in one query."""
    current = ts if ts is not None else now()
    row = await db.fetchone(
        "SELECT "
        "(SELECT COUNT(*) FROM post_targets t JOIN posts p ON p.id = t.post_id "
        " WHERE t.status = ? AND t.scheduled_at IS NOT NULL AND p.status NOT IN (?, ?)) AS scheduled, "
        "(SELECT COUNT(*) FROM posts WHERE status IN (?, ?) AND updated_at >= ?) AS failed_24h, "
        "(SELECT COUNT(*) FROM channels WHERE status = ?) AS pending_channels, "
        "(SELECT COUNT(*) FROM channels WHERE status = ?) AS channels",
        (TARGET_PENDING, POST_DRAFT, POST_DELETED, POST_FAILED, POST_PARTIAL, current - FAILED_WINDOW,
         CH_PENDING, CH_ACTIVE),
    )
    row = row or {}
    return {key: int(row.get(key) or 0) for key in ("scheduled", "failed_24h", "pending_channels", "channels")}


async def _bot_identity(app: web.Application) -> dict | None:
    """Cached getMe; while Telegram is unreachable it is retried at most every GETME_RETRY s."""
    return await cached_bot_info(app, timeout=GETME_TIMEOUT, retry=GETME_RETRY)


def _scheduler_problems(scheduler: Any) -> list[str]:
    if scheduler is None:
        return []
    status = scheduler.status()
    if not status.running:
        return [SCHEDULER_STOPPED_PROBLEM]
    problems = []
    if status.last_error:
        problems.append(SCHEDULER_ERROR_PROBLEM)
    if status.telegram_down:
        problems.append(SCHEDULER_TELEGRAM_PROBLEM)
    return problems


async def health(request: web.Request) -> web.Response:
    app = request.app
    version = app[CONFIG_KEY].version
    headers = {"Cache-Control": "no-store"}
    try:
        counts = await health_counts(app[DB_KEY])
    except Exception:
        log.exception("Health check: the database cannot be read")
        return web.json_response({"status": STATUS_ERROR, "version": version, "bot": None,
                                  "problems": [DATABASE_PROBLEM]}, status=503, headers=headers)
    problems: list[str] = []
    info = await _bot_identity(app)
    username = info.get("username") if info else None
    if info is None:
        problems.append(TELEGRAM_PROBLEM)
    problems += _scheduler_problems(app[SCHEDULER_KEY])
    return web.json_response({
        "status": STATUS_DEGRADED if problems else STATUS_OK,
        "version": version,
        "bot": f"@{username}" if username else None,
        **counts,
        "problems": problems,
    }, headers=headers)


def setup_routes(app: web.Application) -> None:
    prefixed_router(app).add_get("/api/health", health)

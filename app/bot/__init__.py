"""Telegram bot: aiogram dispatcher with the admin-only Russian UI.

`create_dispatcher()` builds a fresh dispatcher with its own routers every time (routers can be
attached to one parent only), so several dispatchers may coexist (tests).
"""
from __future__ import annotations

from typing import Any

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand

from app.bot.albums import AlbumCollector
from app.bot.handlers import create_routers
from app.bot.handlers.chat_events import DiscoveryLimiter
from app.bot.handlers.common import on_error
from app.bot.middlewares import AdminMiddleware
from app.bot.utils import ConfirmGuard

BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="new", description="Новый пост"),
    BotCommand(command="plan", description="Контент-план и расписание публикаций"),
    BotCommand(command="channels", description="Каналы и папки"),
    BotCommand(command="stats", description="Статистика каналов"),
    BotCommand(command="posts", description="Запланированные, опубликованные и черновики"),
    BotCommand(command="settings", description="Настройки и веб-панель"),
    BotCommand(command="timezone", description="Часовой пояс бота"),
    BotCommand(command="helperbots", description="Мои боты: добавлять их в новые каналы"),
    BotCommand(command="help", description="Справка по форматированию и кнопкам"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


def create_dispatcher(db: Any, config: Any, scheduler: Any = None) -> Dispatcher:
    """Dispatcher with in-memory FSM; handlers receive `db`, `config`, `scheduler`, `albums`
    (the album collector), `discovery` (rate limiter of channel discovery notices) and `confirms`
    (one-shot publish confirmations) kwargs.

    Updates of one user are processed one at a time (SimpleEventIsolation); album items are
    buffered by `albums` and handed over as one input under the same per-user lock.
    """
    isolation = SimpleEventIsolation()
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=isolation)
    dp["db"] = db
    dp["config"] = config
    dp["scheduler"] = scheduler
    albums = AlbumCollector(isolation=isolation)
    dp["albums"] = albums
    dp["discovery"] = DiscoveryLimiter()
    dp["confirms"] = ConfirmGuard()
    dp.shutdown.register(albums.close)

    admin_only = AdminMiddleware()
    dp.message.outer_middleware(admin_only)
    dp.callback_query.outer_middleware(admin_only)

    dp.include_routers(*create_routers())
    dp.errors.register(on_error)
    return dp


__all__ = ["BOT_COMMANDS", "create_dispatcher"]

"""Bot handlers, one router per section.

Order matters: section entry points (commands and main menu buttons) work in any FSM state, so
they are registered before the composer, whose idle state accepts any text / media; the
fallback router comes last.
"""
from __future__ import annotations

from aiogram import Router


def create_routers() -> list[Router]:
    """Fresh routers in dispatch order."""
    from app.bot.handlers import channels, chat_events, common, compose, plan, posts, settings, stats

    return [
        common.create_router(),
        chat_events.create_router(),
        channels.create_router(),
        plan.create_router(),
        posts.create_router(),
        stats.create_router(),
        settings.create_router(),
        compose.create_router(),
        common.create_fallback_router(),
    ]

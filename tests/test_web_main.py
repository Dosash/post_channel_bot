"""Entry point helpers (`app/__main__.py`): admin seeding, loopback detection, update types."""
from __future__ import annotations

import pytest

main_module = pytest.importorskip("app.__main__", reason="app.bot is not available yet", exc_type=ImportError)


async def test_seed_admins_first_is_owner(db):
    await main_module.seed_admins(db, [10, 20])
    admins = {a["user_id"]: a["is_owner"] for a in await db.list_admins()}
    assert admins == {10: True, 20: False}


async def test_seed_admins_keeps_existing_owner(db):
    await db.add_admin(5, is_owner=True)
    await main_module.seed_admins(db, [10, 20])
    admins = {a["user_id"]: a["is_owner"] for a in await db.list_admins()}
    assert admins == {5: True, 10: False, 20: False}
    await main_module.seed_admins(db, [10])  # idempotent
    assert len(await db.list_admins()) == 3


async def test_seed_admins_empty(db):
    await main_module.seed_admins(db, [])
    assert await db.list_admins() == []


@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1", True), ("::1", True), ("localhost", True), ("127.0.0.2", True),
    ("0.0.0.0", False), ("::", False), ("10.0.0.10", False), ("example.com", False),
])
def test_is_loopback(host, expected):
    assert main_module.is_loopback(host) is expected


async def test_allowed_updates_include_channel_events(db, config):
    dp = main_module.create_dispatcher(db, config)
    updates = set(main_module.allowed_updates(dp))
    assert {"message", "callback_query", "my_chat_member", "chat_member", "channel_post",
            "message_reaction_count"} <= updates


def test_bot_commands_are_defined():
    assert main_module.BOT_COMMANDS
    assert all(cmd.command and cmd.description for cmd in main_module.BOT_COMMANDS)

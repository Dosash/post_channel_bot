"""Tests for app.services.avatars: channel photos for the panel."""
from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat

from app.db import CH_ACTIVE, CH_IGNORED
from app.services import avatars
from app.services.channels import refresh_channel

CH = -1001000000001
CH2 = -1001000000002


async def setup(db, bot, *ids):
    await db.migrate()
    for chat_id in ids:
        bot.add_chat(chat_id, f"Канал {chat_id}", None)
        await db.upsert_channel(chat_id, f"Канал {chat_id}", None, status=CH_ACTIVE)


async def test_sync_downloads_once_and_follows_changes(db, bot, tmp_path):
    await setup(db, bot, CH)
    path = avatars.avatar_path(tmp_path, CH)
    bot.chats[CH]["photo"] = "p1"

    assert await avatars.sync_avatar(bot, db, tmp_path, CH) is True
    assert path.read_bytes().startswith(b"\xff\xd8")
    assert (await db.get_channel(CH))["photo_id"] == "p1_su"
    assert [c["file_id"] for c in bot.calls_of("get_file")] == ["p1_s"]

    assert await avatars.sync_avatar(bot, db, tmp_path, CH) is False  # same photo: no download
    assert len(bot.calls_of("get_file")) == 1

    bot.chats[CH]["photo"] = "p2"
    assert await avatars.sync_avatar(bot, db, tmp_path, CH) is True
    assert (await db.get_channel(CH))["photo_id"] == "p2_su"

    bot.chats[CH]["photo"] = None  # the photo was removed in Telegram
    assert await avatars.sync_avatar(bot, db, tmp_path, CH) is True
    assert not path.exists() and (await db.get_channel(CH))["photo_id"] is None


async def test_sync_all_skips_recent_and_survives_errors(db, bot, tmp_path):
    await setup(db, bot, CH, CH2)
    await db.upsert_channel(-1001000000003, "Игнор", None, status=CH_IGNORED)
    bot.chats[CH]["photo"] = "a"

    real_get_chat = bot.get_chat

    async def get_chat(chat_id, **kw):
        if chat_id == CH2:
            raise TelegramBadRequest(method=GetChat(chat_id=chat_id), message="Bad Request: chat not found")
        return await real_get_chat(chat_id, **kw)

    bot.get_chat = get_chat
    assert await avatars.sync_all(bot, db, tmp_path, pause=0) == 1
    assert avatars.avatar_path(tmp_path, CH).is_file()
    assert (await db.get_channel(CH2))["photo_checked_at"]  # failed, but not asked again at once

    bot.chats[CH]["photo"] = "b"
    assert await avatars.sync_all(bot, db, tmp_path, pause=0) == 0  # checked less than a day ago
    assert await avatars.sync_all(bot, db, tmp_path, pause=0, max_age=-1) == 1


async def test_refresh_channel_updates_avatar(db, bot, tmp_path):
    await setup(db, bot, CH)
    bot.chats[CH]["photo"] = "r"
    row = await refresh_channel(bot, db, CH, tmp_path)
    assert row["photo_id"] == "r_su" and avatars.avatar_path(tmp_path, CH).is_file()

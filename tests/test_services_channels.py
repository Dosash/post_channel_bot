"""Tests for app.services.channels."""
from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetChat, GetChatMemberCount
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberLeft, User

from app.db import CH_ACTIVE, CH_IGNORED, CH_LEFT, CH_PENDING, now
from app.services.channels import (
    INVITE_LINK_ERROR,
    NO_POST_RIGHT_ERROR,
    NOT_ADMIN_ERROR,
    NOT_FOUND_ERROR,
    ChannelError,
    add_channel,
    check_bot_rights,
    normalize_rights,
    on_bot_status_change,
    parse_channel_ref,
    refresh_channel,
    rights_from_member,
    take_snapshots,
)
from tests.fakes import bad_request

CH = -1001234567890


@pytest.fixture
async def db(db):
    await db.migrate()
    return db


def channel_chat(chat_id=CH, title="Канал", username="test_chan", type="channel") -> Chat:
    return Chat(id=chat_id, type=type, title=title, username=username)


# ------------------------------------------------------------------ parse_channel_ref

@pytest.mark.parametrize("ref, expected", [
    ("@example_channel", "@example_channel"),
    ("example_channel", "@example_channel"),
    ("  @example_channel  ", "@example_channel"),
    ("t.me/example_channel", "@example_channel"),
    ("https://t.me/example_channel", "@example_channel"),
    ("http://t.me/example_channel/", "@example_channel"),
    ("https://t.me/example_channel/123", "@example_channel"),
    ("https://t.me/example_channel?start=1", "@example_channel"),
    ("https://telegram.me/example_channel", "@example_channel"),
    ("https://www.t.me/example_channel", "@example_channel"),
    ("https://t.me/s/example_channel", "@example_channel"),
    ("tg://resolve?domain=example_channel", "@example_channel"),
    ("-1001234567890", -1001234567890),
    ("1234567890", -1001234567890),
    ("t.me/c/1234567890/5", -1001234567890),
    ("https://t.me/c/1234567890", -1001234567890),
    (-1001234567890, -1001234567890),
])
def test_parse_channel_ref(ref, expected):
    assert parse_channel_ref(ref) == expected


@pytest.mark.parametrize("ref", ["t.me/+AbCdEf123", "https://t.me/joinchat/AbCdEf", "https://t.me/+abc"])
def test_parse_channel_ref_invite_links(ref):
    with pytest.raises(ChannelError) as err:
        parse_channel_ref(ref)
    assert str(err.value) == INVITE_LINK_ERROR


@pytest.mark.parametrize("ref", ["", "   ", "@ab", "имя канала", "https://t.me/", "t.me/c/abc", "a b",
                                 "https://example.com/x"])
def test_parse_channel_ref_rejects_garbage(ref):
    with pytest.raises(ChannelError):
        parse_channel_ref(ref)


# ------------------------------------------------------------------ rights

async def test_check_bot_rights(bot):
    bot.add_chat(CH, "Канал", can_edit_messages=False)
    assert await check_bot_rights(bot, CH) == {"is_admin": True, "can_post": True, "can_edit": False,
                                               "can_delete": True}
    bot.add_chat(-1002, "Owned", bot_status="creator")
    assert await check_bot_rights(bot, -1002) == {"is_admin": True, "can_post": True, "can_edit": True,
                                                  "can_delete": True}
    bot.add_chat(-1003, "Gone", bot_status="left")
    assert (await check_bot_rights(bot, -1003))["is_admin"] is False
    (call,) = [c for c in bot.calls_of("get_chat_member") if c["chat_id"] == CH]
    assert call["user_id"] == bot.id


def test_rights_helpers():
    left = ChatMemberLeft(user=User(id=1, is_bot=True, first_name="b"))
    assert rights_from_member(left)["is_admin"] is False
    assert normalize_rights({"can_post_messages": True, "can_edit_messages": False}, "administrator") == {
        "is_admin": True, "can_post": True, "can_edit": False, "can_delete": False}
    assert normalize_rights({"can_post": True, "can_delete": True}, "administrator")["can_delete"] is True
    assert normalize_rights(None, "creator")["can_post"] is True
    assert normalize_rights(None, "administrator") == {"is_admin": True, "can_post": False,
                                                       "can_edit": False, "can_delete": False}
    admin = ChatMemberAdministrator(
        user=User(id=1, is_bot=True, first_name="b"), can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=False,
        can_restrict_members=False, can_promote_members=False, can_change_info=False,
        can_invite_users=False, can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
        can_send_welcome_messages=False, can_post_messages=True, can_edit_messages=True,
    )
    assert normalize_rights(admin, "administrator") == {"is_admin": True, "can_post": True, "can_edit": True,
                                                        "can_delete": True}


# ------------------------------------------------------------------ add_channel

async def test_add_channel_by_username(db, bot):
    bot.add_chat(CH, "Тестовый канал", "example_channel", members=42)
    row = await add_channel(bot, db, "https://t.me/example_channel", added_by=7)
    assert row["id"] == CH and row["status"] == CH_ACTIVE
    assert row["title"] == "Тестовый канал" and row["username"] == "example_channel"
    assert (row["can_post"], row["can_edit"], row["can_delete"]) == (1, 1, 1)
    assert row["members_count"] == 42 and row["added_by"] == 7 and row["added_at"]
    assert [s["count"] for s in await db.get_member_snapshots(CH)] == [42]
    assert bot.calls_of("get_chat")[0]["chat_id"] == "@example_channel"


async def test_add_channel_by_id_activates_pending(db, bot):
    bot.add_chat(CH, "Канал")
    await db.upsert_channel(CH, "Канал", None)  # discovered → pending
    row = await add_channel(bot, db, CH, added_by=1)
    assert row["status"] == CH_ACTIVE and row["added_by"] == 1


async def test_add_channel_not_found(db, bot):
    with pytest.raises(ChannelError) as err:
        await add_channel(bot, db, "@missing_chan", added_by=1)
    assert str(err.value) == NOT_FOUND_ERROR


async def test_add_channel_network_error(db, bot):
    bot.fail_on["get_chat"] = TelegramNetworkError(method=GetChat(chat_id=CH), message="timeout")
    with pytest.raises(ChannelError, match="Не удалось получить данные канала"):
        await add_channel(bot, db, CH, added_by=1)


async def test_add_channel_not_a_channel(db, bot):
    bot.add_chat(-1005, "Группа", "some_group", type="supergroup")
    with pytest.raises(ChannelError) as err:
        await add_channel(bot, db, "@some_group", added_by=1)
    assert str(err.value) == "Это не канал (тип: супергруппа)"
    assert await db.get_channel(-1005) is None


async def test_add_channel_bot_not_admin(db, bot):
    bot.add_chat(CH, "Канал", "test_chan", bot_status="left")
    with pytest.raises(ChannelError) as err:
        await add_channel(bot, db, "@test_chan", added_by=1)
    assert str(err.value) == NOT_ADMIN_ERROR


async def test_add_channel_without_post_right(db, bot):
    bot.add_chat(CH, "Канал", "test_chan", can_post_messages=False)
    with pytest.raises(ChannelError) as err:
        await add_channel(bot, db, "@test_chan", added_by=1)
    assert str(err.value) == NO_POST_RIGHT_ERROR
    assert await db.get_channel(CH) is None


async def test_add_channel_member_count_failure_is_not_fatal(db, bot):
    bot.add_chat(CH, "Канал")
    bot.fail_on["get_chat_member_count"] = bad_request("Bad Request: member list is inaccessible")
    row = await add_channel(bot, db, CH, added_by=1)
    assert row["status"] == CH_ACTIVE and row["members_count"] is None
    assert await db.get_member_snapshots(CH) == []


async def test_add_channel_as_owner(db, bot):
    bot.add_chat(CH, "Свой", bot_status="creator")
    row = await add_channel(bot, db, CH, added_by=None)
    assert row["status"] == CH_ACTIVE and row["can_post"] == 1


# ------------------------------------------------------------------ refresh / snapshots

async def test_refresh_updates_info(db, bot):
    bot.add_chat(CH, "Старое", "old_name", members=5)
    await add_channel(bot, db, CH, added_by=1)
    bot.add_chat(CH, "Новое", "new_name", members=8, can_delete_messages=False)
    row = await refresh_channel(bot, db, CH)
    assert (row["title"], row["username"], row["members_count"]) == ("Новое", "new_name", 8)
    assert row["can_delete"] == 0 and row["status"] == CH_ACTIVE
    assert [s["count"] for s in await db.get_member_snapshots(CH)] == [5, 8]


async def test_refresh_marks_left_when_not_admin(db, bot):
    bot.add_chat(CH, "Канал")
    await add_channel(bot, db, CH, added_by=1)
    bot.bot_status[CH] = "left"
    row = await refresh_channel(bot, db, CH)
    assert row["status"] == CH_LEFT and row["can_post"] == 0


async def test_refresh_marks_left_when_chat_gone(db, bot):
    bot.add_chat(CH, "Канал", "chan_name")
    await add_channel(bot, db, CH, added_by=1)
    bot.chats.clear()
    row = await refresh_channel(bot, db, CH)
    assert row["status"] == CH_LEFT and row["title"] == "Канал"


async def test_refresh_reactivates_left_channel(db, bot):
    bot.add_chat(CH, "Канал")
    await db.upsert_channel(CH, "Канал", None, status=CH_LEFT)
    row = await refresh_channel(bot, db, CH)
    assert row["status"] == CH_ACTIVE and row["can_post"] == 1


async def test_refresh_network_error_keeps_status(db, bot):
    bot.add_chat(CH, "Канал")
    await add_channel(bot, db, CH, added_by=1)
    bot.fail_on["get_chat"] = TelegramNetworkError(method=GetChat(chat_id=CH), message="timeout")
    with pytest.raises(ChannelError, match="Не удалось обновить канал"):
        await refresh_channel(bot, db, CH)
    assert (await db.get_channel(CH))["status"] == CH_ACTIVE
    with pytest.raises(ChannelError, match="не найден"):
        await refresh_channel(bot, db, -100777)


async def test_take_snapshots(db, bot):
    bot.add_chat(-1001, "A", members=10)
    bot.add_chat(-1002, "B", members=20)
    bot.add_chat(-1003, "Pending", members=30)
    await db.upsert_channel(-1001, "A", None, status=CH_ACTIVE, members_count=10)
    await db.upsert_channel(-1002, "B", None, status=CH_ACTIVE)
    await db.upsert_channel(-1003, "Pending", None)
    bot.member_counts[-1001] = 11
    bot.fail_on["get_chat_member_count"] = lambda kw: bad_request() if kw["chat_id"] == -1002 else None

    stored = await take_snapshots(bot, db)

    assert stored == 1
    assert [s["count"] for s in await db.get_member_snapshots(-1001)] == [11]
    assert (await db.get_channel(-1001))["members_count"] == 11
    assert await db.get_member_snapshots(-1002) == []
    assert await db.get_member_snapshots(-1003) == []


def flood(chat_id, seconds):
    return TelegramRetryAfter(method=GetChatMemberCount(chat_id=chat_id), message="Too Many Requests",
                              retry_after=seconds)


async def test_take_snapshots_retries_after_flood_control(db, bot, monkeypatch, caplog):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("app.services.channels.asyncio.sleep", fake_sleep)
    for cid in (-1001, -1002, -1003):
        bot.add_chat(cid, str(cid), members=5)
        await db.upsert_channel(cid, str(cid), None, status=CH_ACTIVE)
    flooded = {-1002}

    def fail(kw):
        if kw["chat_id"] in flooded:
            flooded.discard(kw["chat_id"])
            return flood(kw["chat_id"], 20)
        return None

    bot.fail_on["get_chat_member_count"] = fail

    assert await take_snapshots(bot, db, pause=0.3) == 3
    assert 21 in sleeps and sleeps.count(0.3) == 2  # pauses between the 3 channels, then the flood wait
    assert len(await db.get_member_snapshots(-1002)) == 1
    assert "Traceback" not in caplog.text


async def test_take_snapshots_gives_up_on_long_flood_wait(db, bot):
    bot.add_chat(-1001, "A", members=5)
    await db.upsert_channel(-1001, "A", None, status=CH_ACTIVE)
    bot.fail_on["get_chat_member_count"] = flood(-1001, 600)

    assert await take_snapshots(bot, db) == 0
    assert len([c for c in bot.calls if c[0] == "get_chat_member_count"]) == 1


async def test_take_snapshots_skips_fresh_channels(db, bot):
    for cid in (-1001, -1002):
        bot.add_chat(cid, str(cid), members=5)
        await db.upsert_channel(cid, str(cid), None, status=CH_ACTIVE)
    await db.add_member_snapshot(-1001, 4, ts=now() - 600)
    await db.add_member_snapshot(-1002, 4, ts=now() - 4000)

    assert await take_snapshots(bot, db, min_age=1800, pause=0) == 1
    assert [c[1]["chat_id"] for c in bot.calls if c[0] == "get_chat_member_count"] == [-1002]


# ------------------------------------------------------------------ on_bot_status_change

ADMIN_RIGHTS = {"can_post": True, "can_edit": True, "can_delete": False}


async def test_status_change_ignores_non_channels(db, bot):
    chat = channel_chat(chat_id=-1009, type="supergroup")
    assert await on_bot_status_change(bot, db, chat, "administrator", ADMIN_RIGHTS, None) == "ignored"
    assert await db.get_channel(-1009) is None


async def test_status_change_discovers_new_channel(db, bot):
    bot.add_chat(CH, "Канал", "test_chan", members=77)
    result = await on_bot_status_change(bot, db, channel_chat(), "administrator", ADMIN_RIGHTS,
                                        User(id=1, is_bot=False, first_name="Admin"))
    assert result == "discovered"
    row = await db.get_channel(CH)
    assert row["status"] == CH_PENDING and row["members_count"] == 77
    assert (row["can_post"], row["can_edit"], row["can_delete"]) == (1, 1, 0)


async def test_status_change_member_count_failure_is_not_fatal(db, bot):
    # The chat is unknown to FakeBot, so get_chat_member_count fails.
    result = await on_bot_status_change(bot, db, channel_chat(), "creator", None, None)
    assert result == "discovered"
    row = await db.get_channel(CH)
    assert row["status"] == CH_PENDING and row["can_post"] == 1 and row["members_count"] is None


async def test_status_change_rights_updated_for_active(db, bot):
    await db.upsert_channel(CH, "Старое", None, status=CH_ACTIVE, rights=ADMIN_RIGHTS)
    result = await on_bot_status_change(bot, db, channel_chat(title="Новое"), "administrator",
                                        {"can_post_messages": True, "can_delete_messages": True}, None)
    assert result == "rights_updated"
    row = await db.get_channel(CH)
    assert row["status"] == CH_ACTIVE and row["title"] == "Новое"
    assert (row["can_edit"], row["can_delete"]) == (0, 1)


async def test_status_change_reoffers_ignored_and_left(db, bot):
    await db.upsert_channel(CH, "Канал", None, status=CH_IGNORED)
    assert await on_bot_status_change(bot, db, channel_chat(), "administrator", ADMIN_RIGHTS, None) == "discovered"
    assert (await db.get_channel(CH))["status"] == CH_PENDING
    await db.set_channel_status(CH, CH_LEFT)
    assert await on_bot_status_change(bot, db, channel_chat(), "administrator", ADMIN_RIGHTS, None) == "discovered"
    assert (await db.get_channel(CH))["status"] == CH_PENDING


@pytest.mark.parametrize("new_status", ["left", "kicked", "member", "restricted"])
async def test_status_change_removed_from_active(db, bot, new_status):
    await db.upsert_channel(CH, "Канал", None, status=CH_ACTIVE, rights=ADMIN_RIGHTS)
    assert await on_bot_status_change(bot, db, channel_chat(), new_status, {}, None) == "removed"
    row = await db.get_channel(CH)
    assert row["status"] == CH_LEFT and row["can_post"] == 0


async def test_status_change_pending_lost_is_ignored(db, bot):
    await db.upsert_channel(CH, "Канал", None, status=CH_PENDING)
    assert await on_bot_status_change(bot, db, channel_chat(), "left", {}, None) == "ignored"
    assert (await db.get_channel(CH))["status"] == CH_LEFT


async def test_status_change_unknown_or_ignored_channel_left(db, bot):
    assert await on_bot_status_change(bot, db, channel_chat(), "kicked", {}, None) == "ignored"
    assert await db.get_channel(CH) is None
    await db.upsert_channel(CH, "Канал", None, status=CH_IGNORED)
    assert await on_bot_status_change(bot, db, channel_chat(), "left", {}, None) == "ignored"
    assert (await db.get_channel(CH))["status"] == CH_IGNORED


async def test_rights_edit_does_not_reoffer_ignored_channel(db, bot):
    """Finding #4: a mere rights edit (administrator → administrator) keeps «Игнорировать»."""
    await db.upsert_channel(CH, "Канал", None, status=CH_IGNORED)
    result = await on_bot_status_change(bot, db, channel_chat(title="Ваш бот взломан"), "administrator",
                                        ADMIN_RIGHTS, None, old_status="administrator")
    assert result == "ignored"
    row = await db.get_channel(CH)
    assert row["status"] == CH_IGNORED and row["can_post"] == 1
    # a real re-promotion (the bot was not an admin before) still offers the channel again
    result = await on_bot_status_change(bot, db, channel_chat(), "administrator", ADMIN_RIGHTS, None,
                                        old_status="left")
    assert result == "discovered" and (await db.get_channel(CH))["status"] == CH_PENDING


async def test_rights_edit_of_pending_channel_reoffers_only_when_posting_allowed(db, bot):
    await db.upsert_channel(CH, "Канал", None, status=CH_PENDING, rights={"can_post": False})
    no_post = {"can_post": False, "can_edit": True}
    assert await on_bot_status_change(bot, db, channel_chat(), "administrator", no_post, None,
                                      old_status="administrator") == "ignored"
    assert (await db.get_channel(CH))["status"] == CH_PENDING
    assert await on_bot_status_change(bot, db, channel_chat(), "administrator", ADMIN_RIGHTS, None,
                                      old_status="administrator") == "discovered"
    row = await db.get_channel(CH)
    assert row["status"] == CH_PENDING and row["can_post"] == 1
    # toggling another right afterwards is not a new discovery
    assert await on_bot_status_change(bot, db, channel_chat(), "administrator",
                                      {**ADMIN_RIGHTS, "can_delete": True}, None,
                                      old_status="administrator") == "ignored"

"""«Мои боты»: the owner's other bots offered as «add as channel admin» links (app.services.helper_bots)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.methods import EditMessageText
from aiogram.types import MessageOriginUser, User
from aiohttp.test_utils import TestClient, TestServer

from app.bot.callbacks import ChannelCb, HelperBotCb, SettingsCb
from app.db import CH_ACTIVE
from app.services import helper_bots
from app.web import create_web_app
from tests.mocked_session import BOT_USERNAME, Harness
from tests.test_bot_channels import CH, OWNER, promotion

H = {"X-Requested-With": "pcb"}
LINK = "https://t.me/{}?startchannel&admin=post_messages+edit_messages+delete_messages"


def url_buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.url) for row in markup.inline_keyboard for b in row if b.url]


# ------------------------------------------------------------------ parsing

def test_parse_list_accepts_usernames_links_and_separators():
    raw = "@First_bot, t.me/second_bot\nhttps://t.me/ThirdBot?start=x ; @first_BOT"
    assert helper_bots.parse_list(raw) == ["First_bot", "second_bot", "ThirdBot"]
    assert helper_bots.parse_list(["@a_news_bot", " "]) == ["a_news_bot"]
    assert helper_bots.parse_list("") == []


@pytest.mark.parametrize("raw", ["@channel", "not a bot", "@ab", "@1digit_bot", "@" + "x" * 31 + "bot"])
def test_parse_list_rejects_non_bot_names(raw):
    with pytest.raises(ValueError, match="не юзернейм бота"):
        helper_bots.parse_list(raw)


def test_parse_list_rejects_itself_and_too_many():
    with pytest.raises(ValueError, match="сам бот публикаций"):
        helper_bots.parse_list("@Post_Test_Bot", "post_test_bot")
    with pytest.raises(ValueError, match="Не больше"):
        helper_bots.parse_list([f"@n{i}_bot" for i in range(helper_bots.MAX_HELPERS + 1)])


def test_add_link():
    assert helper_bots.add_link("my_bot") == LINK.format("my_bot")


async def test_setting_roundtrip(db):
    await db.migrate()
    assert await helper_bots.get_helpers(db) == []
    await helper_bots.set_helpers(db, ["a_bot", "b_bot"])
    assert await helper_bots.get_helpers(db) == ["a_bot", "b_bot"]
    await helper_bots.set_helpers(db, [])
    assert await db.get_setting(helper_bots.HELPER_BOTS_SETTING) is None
    await db.set_setting(helper_bots.HELPER_BOTS_SETTING, "{broken")
    assert await helper_bots.get_helpers(db) == []


# ------------------------------------------------------------------ bot

@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    yield harness
    await harness.close()


async def test_discovery_notice_offers_only_missing_helpers(h):
    await helper_bots.set_helpers(h.db, ["first_bot", "Second_bot"])
    h.session.add_chat(CH, "Тестовый канал", "example_channel")
    h.session.admin_bots[CH] = ["second_bot"]
    await h.feed(my_chat_member=promotion())
    message = h.last_to(OWNER)
    assert "Ваших ботов тоже можно сделать админами" in message.text
    assert url_buttons(message.reply_markup) == [("➕ @first_bot", LINK.format("first_bot"))]


async def test_discovery_notice_without_helpers_is_unchanged(h):
    await h.feed(my_chat_member=promotion())
    message = h.last_to(OWNER)
    assert "Ваших ботов" not in message.text and url_buttons(message.reply_markup) == []


async def test_discovery_notice_when_admins_are_unknown_offers_every_helper(h):
    await helper_bots.set_helpers(h.db, ["first_bot"])
    await h.feed(my_chat_member=promotion())  # the chat is unknown to the mock: getChatAdministrators fails
    assert url_buttons(h.last_to(OWNER).reply_markup) == [("➕ @first_bot", LINK.format("first_bot"))]


async def test_approve_offers_missing_helpers(h):
    await helper_bots.set_helpers(h.db, ["first_bot"])
    await h.feed(my_chat_member=promotion())
    h.session.add_chat(CH, "Тестовый канал", "example_channel")
    await h.press(OWNER, ChannelCb(action="approve", chat_id=CH))
    edit = h.session.of(EditMessageText)[-1]
    assert "своих ботов" in edit.text
    assert url_buttons(edit.reply_markup) == [("➕ @first_bot", LINK.format("first_bot"))]
    h.session.admin_bots[CH] = ["first_bot"]
    await h.press(OWNER, ChannelCb(action="approve", chat_id=CH))
    edit = h.session.of(EditMessageText)[-1]
    assert "своих ботов" not in edit.text and url_buttons(edit.reply_markup) == []


async def test_channel_card_helpers_screen(h):
    h.session.add_chat(CH, "Тестовый канал", "example_channel")
    await h.db.upsert_channel(CH, "Тестовый канал", "example_channel", status=CH_ACTIVE, rights={"can_post": True})
    await h.press(OWNER, ChannelCb(action="card", chat_id=CH))
    card = h.session.of(EditMessageText)[-1]
    assert "🤖 Мои боты" not in [b.text for row in card.reply_markup.inline_keyboard for b in row]

    await helper_bots.set_helpers(h.db, ["first_bot", "second_bot"])
    h.session.admin_bots[CH] = ["first_bot"]
    await h.press(OWNER, ChannelCb(action="card", chat_id=CH))
    card = h.session.of(EditMessageText)[-1]
    assert "🤖 Мои боты" in [b.text for row in card.reply_markup.inline_keyboard for b in row]
    await h.press(OWNER, ChannelCb(action="helpers", chat_id=CH))
    screen = h.session.of(EditMessageText)[-1]
    assert "@first_bot — ✅ админ" in screen.text and "@second_bot — ➖ не добавлен" in screen.text
    assert url_buttons(screen.reply_markup) == [("➕ @second_bot", LINK.format("second_bot"))]


def callback_buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row if b.callback_data]


async def test_helperbots_command(h):
    await h.send(OWNER, "/helperbots @first_bot t.me/second_bot")
    assert await helper_bots.get_helpers(h.db) == ["first_bot", "second_bot"]
    message = h.last_to(OWNER)
    assert "Сохранено" in message.text and "1. @first_bot\n2. @second_bot" in message.text
    assert callback_buttons(message.reply_markup)[:3] == ["🗑 @first_bot", "🗑 @second_bot", "➕ Добавить бота"]
    await h.send(OWNER, "/helperbots @channel")
    assert "не юзернейм бота" in h.last_to(OWNER).text
    await h.send(OWNER, f"/helperbots @{BOT_USERNAME}")
    assert "сам бот публикаций" in h.last_to(OWNER).text
    assert await helper_bots.get_helpers(h.db) == ["first_bot", "second_bot"]
    await h.send(OWNER, "/helperbots -")
    assert await helper_bots.get_helpers(h.db) == []
    assert "Пока ни одного бота" in h.last_to(OWNER).text


async def test_settings_buttons_add_and_remove_bots(h):
    await h.send(OWNER, "/settings")
    settings = h.last_to(OWNER)
    assert "🤖 Мои боты: не заданы" in settings.text and "🤖 Мои боты" in callback_buttons(settings.reply_markup)

    await h.press(OWNER, HelperBotCb(action="show"))
    assert "Пока ни одного бота" in h.session.of(EditMessageText)[-1].text
    await h.press(OWNER, HelperBotCb(action="add"))
    assert "Добавить бота" in h.session.of(EditMessageText)[-1].text

    await h.send(OWNER, "@channel")  # not a bot: asked again, the prompt stays
    assert "не юзернейм бота" in h.last_to(OWNER).text
    await h.send(OWNER, "@first_bot t.me/second_bot")
    message = h.last_to(OWNER)
    assert "✅ Добавлено: @first_bot, @second_bot" in message.text
    assert await helper_bots.get_helpers(h.db) == ["first_bot", "second_bot"]
    assert (await h.fsm(OWNER))[0] is None

    await h.send(OWNER, "@third_bot")  # the prompt is over: free text is not taken as a bot
    assert await helper_bots.get_helpers(h.db) == ["first_bot", "second_bot"]

    await h.press(OWNER, HelperBotCb(action="add"))
    await h.send(OWNER, "@FIRST_BOT")
    assert "уже в списке" in h.last_to(OWNER).text

    await h.press(OWNER, HelperBotCb(action="del", name="first_bot"))
    screen = h.session.of(EditMessageText)[-1]
    assert "@first_bot убран" in screen.text and "1. @second_bot" in screen.text
    assert await helper_bots.get_helpers(h.db) == ["second_bot"]


async def test_add_bot_by_forwarding_its_message(h):
    await h.press(OWNER, HelperBotCb(action="add"))
    origin = MessageOriginUser(date=datetime.now(timezone.utc),
                               sender_user=User(id=777, is_bot=True, first_name="News", username="News_bot"))
    await h.send(OWNER, "что-то от бота", forward_origin=origin)
    assert "✅ Добавлено: @News_bot" in h.last_to(OWNER).text
    assert await helper_bots.get_helpers(h.db) == ["News_bot"]


async def test_back_from_the_prompt_cancels_it(h):
    await h.press(OWNER, HelperBotCb(action="add"))
    await h.press(OWNER, HelperBotCb(action="show"))
    assert (await h.fsm(OWNER))[0] is None
    await h.press(OWNER, HelperBotCb(action="add"))
    await h.press(OWNER, SettingsCb(action="show"))
    assert (await h.fsm(OWNER))[0] is None


# ------------------------------------------------------------------ web

@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    bot.add_chat(CH, "Тест канал", "test_channel")
    await db.upsert_channel(CH, "Тест канал", "test_channel", status=CH_ACTIVE)
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


async def _put(client, bots) -> tuple[int, dict]:
    resp = await client.put("/api/settings/helper-bots", json={"bots": bots}, headers=H)
    return resp.status, await resp.json()


async def test_web_setting(client, db):
    assert (await (await client.get("/api/settings")).json())["helper_bots"] == []
    assert await _put(client, "@first_bot, second_bot") == (200, {"helper_bots": ["first_bot", "second_bot"]})
    assert (await (await client.get("/api/settings")).json())["helper_bots"] == ["first_bot", "second_bot"]
    assert await _put(client, ["third_bot"]) == (200, {"helper_bots": ["third_bot"]})
    assert await _put(client, "") == (200, {"helper_bots": []})
    assert await helper_bots.get_helpers(db) == []


async def test_web_setting_validation(client, db):
    await helper_bots.set_helpers(db, ["first_bot"])
    for value in (None, 1, {"a": 1}):
        status, data = await _put(client, value)
        assert status == 400 and "Ожидается bots" in data["error"]
    status, data = await _put(client, "@channel")
    assert status == 400 and "не юзернейм бота" in data["error"]
    status, data = await _put(client, "@test_bot")  # the publishing bot itself (FakeBot's username)
    assert status == 400 and "сам бот публикаций" in data["error"]
    resp = await client.put("/api/settings/helper-bots", json={"bots": "a_bot"})  # no CSRF header
    assert resp.status == 403
    assert await helper_bots.get_helpers(db) == ["first_bot"]


async def test_web_channel_helpers(client, db, bot):
    resp = await client.get(f"/api/channels/{CH}/helpers")
    assert resp.status == 200 and await resp.json() == {"helpers": []}
    await helper_bots.set_helpers(db, ["first_bot", "second_bot"])
    bot.admin_bots[CH] = ["FIRST_BOT"]
    data = await (await client.get(f"/api/channels/{CH}/helpers")).json()
    assert data == {"helpers": [
        {"username": "first_bot", "is_admin": True, "url": LINK.format("first_bot")},
        {"username": "second_bot", "is_admin": False, "url": LINK.format("second_bot")},
    ]}
    assert (await client.get("/api/channels/-100777/helpers")).status == 404

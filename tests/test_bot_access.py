"""Bot access control, /start, /help, /cancel and the dispatcher setup."""
from __future__ import annotations

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Chat, ReplyKeyboardMarkup

from app.bot import BOT_COMMANDS, create_dispatcher
from app.bot.callbacks import MenuCb
from app.bot.keyboards import BTN_CHANNELS, BTN_HELP, BTN_NEW, BTN_PLAN, BTN_POSTS, BTN_SETTINGS, BTN_STATS
from app.bot.middlewares import PRIVATE_TEXT
from app.db import CH_ACTIVE, CH_PENDING
from tests.mocked_session import Harness, private_message

OWNER = 111
STRANGER = 999


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    yield harness
    await harness.close()


@pytest.fixture
async def owner(h):
    await h.db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    return OWNER


def test_used_update_types_cover_channel_events(db, config):
    dp = create_dispatcher(db, config)
    types = set(dp.resolve_used_update_types())
    assert {"message", "callback_query", "my_chat_member", "chat_member", "channel_post",
            "message_reaction_count"} <= types


def test_bot_commands():
    commands = [c.command for c in BOT_COMMANDS]
    assert commands == ["start", "new", "plan", "channels", "stats", "posts", "settings", "help", "cancel"]


async def test_non_admin_start_gets_private_reply_only(h, owner):
    await h.send(STRANGER, "/start")
    sent = h.session.sent_to(STRANGER)
    assert len(sent) == 1
    assert sent[0].text == PRIVATE_TEXT
    assert not await h.db.is_admin(STRANGER)
    assert h.session.sent_to(OWNER) == []


async def test_non_admin_other_messages_are_ignored(h, owner):
    await h.send(STRANGER, "привет")
    await h.send(STRANGER, "/new")
    await h.send(STRANGER, BTN_CHANNELS)
    assert h.session.of(SendMessage) == []


async def test_non_admin_callback_gets_lock(h, owner):
    await h.press(STRANGER, MenuCb(action="channels"))
    answers = h.session.of(AnswerCallbackQuery)
    assert len(answers) == 1 and answers[0].text == "🔒"
    assert h.session.of(SendMessage) == []


async def test_first_start_with_zero_admins_becomes_owner(h):
    await h.send(STRANGER, "/start")
    admins = await h.db.list_admins()
    assert [(a["user_id"], a["is_owner"]) for a in admins] == [(STRANGER, True)]
    greeting = h.session.sent_to(STRANGER)[0]
    assert "👑 Вы назначены владельцем бота" in greeting.text
    assert isinstance(greeting.reply_markup, ReplyKeyboardMarkup)
    # The second user is a stranger now.
    await h.send(STRANGER + 1, "/start")
    assert h.session.sent_to(STRANGER + 1)[0].text == PRIVATE_TEXT


async def test_admin_start_shows_main_menu(h, owner):
    await h.db.upsert_channel(-1001, "Мой канал", "my", status=CH_ACTIVE)
    await h.send(OWNER, "/start")
    first = h.session.sent_to(OWNER)[0]
    assert "👑" not in first.text
    assert "Подключено: 1 канал" in first.text
    labels = [b.text for row in first.reply_markup.keyboard for b in row]
    assert set(labels) == {BTN_NEW, BTN_PLAN, BTN_CHANNELS, BTN_STATS, BTN_POSTS, BTN_SETTINGS, BTN_HELP}
    assert [b.text for b in first.reply_markup.keyboard[0]] == [BTN_NEW, BTN_PLAN]
    assert first.reply_markup.resize_keyboard is True


async def test_start_lists_pending_channels(h, owner):
    await h.db.upsert_channel(-1001, "Активный", None, status=CH_ACTIVE)
    await h.db.upsert_channel(-1002, "Новый канал", None, status=CH_PENDING)
    await h.send(OWNER, "/start")
    texts = h.session.texts_to(OWNER)
    assert any("ждут подтверждения" in t for t in texts)
    pending_msg = h.session.sent_to(OWNER)[-1]
    data = [b.callback_data for row in pending_msg.reply_markup.inline_keyboard for b in row]
    assert "ch:approve:-1002" in data and "ch:ignore:-1002" in data


async def test_start_without_channels_suggests_adding(h, owner):
    await h.send(OWNER, "/start")
    texts = h.session.texts_to(OWNER)
    assert any("Добавить канал" in t for t in texts)


async def test_admin_profile_is_updated(h, owner):
    message = private_message(OWNER, "/help", first_name="Новое имя")
    await h.feed(message=message)
    admin = (await h.db.list_admins())[0]
    assert admin["full_name"] == "Новое имя"


async def test_help(h, owner):
    await h.send(OWNER, "/help")
    text = h.session.texts_to(OWNER)[0]
    assert "<code>**жирный**</code>" in text and "зелёная" in text
    await h.send(OWNER, BTN_HELP)
    assert len(h.session.texts_to(OWNER)) == 2


async def test_group_messages_are_ignored(h, owner):
    message = private_message(OWNER, "/start").model_copy(
        update={"chat": Chat(id=-500, type="supergroup", title="Группа")})
    await h.feed(message=message)
    assert h.session.of(SendMessage) == []


async def test_cancel_without_state(h, owner):
    await h.send(OWNER, "/cancel")
    assert "Нечего отменять" in h.session.texts_to(OWNER)[0]


async def test_unknown_text_gets_menu_hint(h, owner):
    await h.send(OWNER, "что-то непонятное")
    reply = h.last_to(OWNER)
    assert "Не понял" in reply.text
    assert isinstance(reply.reply_markup, ReplyKeyboardMarkup)


async def test_stale_callback_is_answered(h, owner):
    await h.press(OWNER, "zzz:unknown")
    answers = h.session.of(AnswerCallbackQuery)
    assert answers and "устарела" in answers[0].text

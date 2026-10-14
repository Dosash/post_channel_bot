"""Channels section and channel-side updates (my_chat_member, chat_member, posts, reactions)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetChat, SendMessage
from aiogram.types import (
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    ChatShared,
    Message,
    MessageOriginChannel,
    MessageOriginHiddenUser,
    MessageReactionCountUpdated,
    PhotoSize,
    ReactionCount,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    User,
)

from app.bot.callbacks import ChannelCb, StatsCb
from app.bot.keyboards import BTN_CANCEL, BTN_CHANNELS, BTN_STATS
from app.bot.states import ChannelStates
from app.db import CH_ACTIVE, CH_IGNORED, CH_LEFT, CH_PENDING
from tests.mocked_session import BOT_ID, Harness, bot_admin_member, channel_chat, tg_user

OWNER = 111
ADMIN2 = 222
CH = -1001234567890


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    await db.add_admin(ADMIN2, "helper", "Helper")
    yield harness
    await harness.close()


def now() -> datetime:
    return datetime.now(timezone.utc)


def promotion(chat_id: int = CH, title: str = "Тестовый канал", username: str | None = "example_channel",
              **rights: bool) -> ChatMemberUpdated:
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Test bot")
    return ChatMemberUpdated(
        chat=channel_chat(chat_id, title, username), from_user=tg_user(OWNER, "Boss"), date=now(),
        old_chat_member=ChatMemberLeft(user=bot_user),
        new_chat_member=bot_admin_member(**rights),
    )


def demotion(chat_id: int = CH, title: str = "Тестовый канал") -> ChatMemberUpdated:
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Test bot")
    return ChatMemberUpdated(
        chat=channel_chat(chat_id, title), from_user=tg_user(OWNER, "Boss"), date=now(),
        old_chat_member=bot_admin_member(), new_chat_member=ChatMemberLeft(user=bot_user),
    )


def state_key(h: Harness, user_id: int):
    from aiogram.fsm.storage.base import StorageKey

    return StorageKey(bot_id=h.bot.id, chat_id=user_id, user_id=user_id)


async def fsm_state(h: Harness, user_id: int) -> str | None:
    return await h.dp.storage.get_state(state_key(h, user_id))


# ------------------------------------------------------------------ my_chat_member

async def test_promotion_creates_pending_channel_and_notifies_every_admin(h):
    await h.feed(my_chat_member=promotion())
    channel = await h.db.get_channel(CH)
    assert channel["status"] == CH_PENDING
    assert channel["title"] == "Тестовый канал"
    for admin_id in (OWNER, ADMIN2):
        message = h.last_to(admin_id)
        assert "Бот назначен администратором канала «Тестовый канал» (@example_channel)" in message.text
        buttons = [b for row in message.reply_markup.inline_keyboard for b in row]
        assert buttons[0].text == "✅ Добавить канал"
        assert buttons[0].callback_data == ChannelCb(action="approve", chat_id=CH).pack()
        assert buttons[1].callback_data == ChannelCb(action="ignore", chat_id=CH).pack()


async def test_promotion_without_post_right_warns(h):
    await h.feed(my_chat_member=promotion(can_post_messages=False))
    assert "нет права «Публикация сообщений»" in h.last_to(OWNER).text


async def test_approve_callback_adds_channel(h):
    await h.feed(my_chat_member=promotion())
    h.session.add_chat(CH, "Тестовый канал", "example_channel", members=321)
    h.session.clear()
    await h.press(OWNER, ChannelCb(action="approve", chat_id=CH))
    channel = await h.db.get_channel(CH)
    assert channel["status"] == CH_ACTIVE
    assert channel["members_count"] == 321 and channel["added_by"] == OWNER
    edit = h.session.of(EditMessageText)[-1]
    assert edit.text.startswith("✅ Канал «Тестовый канал» добавлен")
    assert len(h.session.of(AnswerCallbackQuery)) == 1


async def test_approve_without_rights_shows_error(h):
    await h.feed(my_chat_member=promotion())
    h.session.add_chat(CH, "Тестовый канал", "example_channel", can_post_messages=False)
    h.session.clear()
    await h.press(OWNER, ChannelCb(action="approve", chat_id=CH))
    assert (await h.db.get_channel(CH))["status"] == CH_PENDING
    answer = h.session.of(AnswerCallbackQuery)[0]
    assert answer.show_alert and "нет права" in answer.text
    assert h.session.of(EditMessageText) == []


async def test_ignore_callback(h):
    await h.feed(my_chat_member=promotion())
    await h.press(OWNER, ChannelCb(action="ignore", chat_id=CH))
    assert (await h.db.get_channel(CH))["status"] == CH_IGNORED
    assert "проигнорирован" in h.session.of(EditMessageText)[-1].text


async def test_ignore_does_not_hide_an_active_channel(h):
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    await h.press(ADMIN2, ChannelCb(action="ignore", chat_id=CH))
    assert (await h.db.get_channel(CH))["status"] == CH_ACTIVE


async def test_demotion_of_active_channel_notifies(h):
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE, rights={"can_post": True})
    await h.feed(my_chat_member=demotion())
    assert (await h.db.get_channel(CH))["status"] == CH_LEFT
    assert "Бот больше не администратор канала «Тестовый канал»" in h.last_to(OWNER).text


async def test_my_chat_member_in_group_is_ignored(h):
    event = promotion().model_copy(update={"chat": channel_chat(-100777).model_copy(update={"type": "supergroup"})})
    await h.feed(my_chat_member=event)
    assert await h.db.get_channel(-100777) is None
    assert h.session.of(SendMessage) == []


# ------------------------------------------------------------------ list, card, add

async def test_channels_list_and_card(h):
    await h.db.upsert_channel(CH, "Тестовый канал", "example_channel", status=CH_ACTIVE, members_count=42,
                              rights={"can_post": True, "can_edit": True, "can_delete": True})
    await h.db.upsert_channel(-1002, "Ждёт", None, status=CH_PENDING)
    await h.send(OWNER, BTN_CHANNELS)
    message = h.last_to(OWNER)
    assert "Тестовый канал" in message.text and "Ждут подтверждения" in message.text
    data = [b.callback_data for row in message.reply_markup.inline_keyboard for b in row]
    assert ChannelCb(action="card", chat_id=CH).pack() in data
    assert ChannelCb(action="approve", chat_id=-1002).pack() in data

    await h.press(OWNER, ChannelCb(action="card", chat_id=CH))
    card = h.session.of(EditMessageText)[-1]
    assert "👥 Подписчиков: 42" in card.text
    card_data = [b.callback_data for row in card.reply_markup.inline_keyboard for b in row]
    assert StatsCb(action="ch", chat_id=CH, days=30).pack() in card_data
    assert ChannelCb(action="remove", chat_id=CH).pack() in card_data


async def test_refresh_and_remove_channel(h):
    await h.db.upsert_channel(CH, "Старое имя", None, status=CH_ACTIVE)
    h.session.add_chat(CH, "Новое имя", "example_channel", members=77)
    await h.press(OWNER, ChannelCb(action="refresh", chat_id=CH))
    channel = await h.db.get_channel(CH)
    assert channel["title"] == "Новое имя" and channel["members_count"] == 77

    await h.press(OWNER, ChannelCb(action="remove", chat_id=CH))
    assert "Удалить канал «Новое имя»" in h.session.of(EditMessageText)[-1].text
    assert await h.db.get_channel(CH) is not None
    await h.press(OWNER, ChannelCb(action="remove_ok", chat_id=CH))
    assert await h.db.get_channel(CH) is None


async def test_add_channel_by_username(h):
    h.session.add_chat(CH, "Тестовый канал", "example_channel", members=5)
    await h.press(OWNER, ChannelCb(action="add"))
    prompt = h.last_to(OWNER)
    assert "Добавление канала" in prompt.text
    assert isinstance(prompt.reply_markup, ReplyKeyboardMarkup)
    assert prompt.reply_markup.keyboard[0][0].request_chat.chat_is_channel is True
    assert await fsm_state(h, OWNER) == ChannelStates.waiting_ref.state

    await h.send(OWNER, "https://t.me/example_channel")
    assert (await h.db.get_channel(CH))["status"] == CH_ACTIVE
    reply = h.last_to(OWNER)
    assert "✅ Канал «Тестовый канал» добавлен" in reply.text
    assert isinstance(reply.reply_markup, ReplyKeyboardMarkup)  # main menu restored
    assert await fsm_state(h, OWNER) is None


async def test_add_channel_error_keeps_dialog(h):
    await h.press(OWNER, ChannelCb(action="add"))
    await h.send(OWNER, "@unknown_channel")
    assert "Канал не найден" in h.last_to(OWNER).text
    assert await fsm_state(h, OWNER) == ChannelStates.waiting_ref.state
    await h.send(OWNER, "t.me/+invite123")
    assert "приглашения" in h.last_to(OWNER).text
    await h.send(OWNER, BTN_CANCEL)
    assert await fsm_state(h, OWNER) is None
    assert isinstance(h.last_to(OWNER).reply_markup, ReplyKeyboardMarkup)


async def test_add_channel_by_forwarded_post(h):
    h.session.add_chat(CH, "Тестовый канал", None)
    await h.press(OWNER, ChannelCb(action="add"))
    origin = MessageOriginChannel(date=now(), chat=channel_chat(CH, "Тестовый канал"), message_id=5)
    await h.send(OWNER, "текст поста", forward_origin=origin)
    assert (await h.db.get_channel(CH))["status"] == CH_ACTIVE

    await h.press(OWNER, ChannelCb(action="add"))
    hidden = MessageOriginHiddenUser(date=now(), sender_user_name="Кто-то")
    await h.send(OWNER, "текст", forward_origin=hidden)
    assert "не из канала" in h.last_to(OWNER).text


async def test_add_channel_via_native_picker(h):
    h.session.add_chat(CH, "Тестовый канал", "example_channel")
    await h.press(OWNER, ChannelCb(action="add"))
    await h.send(OWNER, None, chat_shared=ChatShared(request_id=1, chat_id=CH, title="Тестовый канал"))
    assert (await h.db.get_channel(CH))["status"] == CH_ACTIVE


async def test_menu_button_leaves_add_dialog_and_restores_menu(h):
    await h.press(OWNER, ChannelCb(action="add"))
    await h.send(OWNER, BTN_STATS)
    assert await fsm_state(h, OWNER) is None
    texts = h.session.texts_to(OWNER)
    assert any("Добавление канала отменено" in t for t in texts)
    assert "📊 <b>Статистика</b>" in texts[-1]


# ------------------------------------------------------------------ chat_member, posts, reactions

def member_update(old, new, user_id: int = 5000, chat_id: int = CH) -> ChatMemberUpdated:
    return ChatMemberUpdated(chat=channel_chat(chat_id), from_user=tg_user(user_id), date=now(),
                             old_chat_member=old, new_chat_member=new)


async def test_chat_member_join_and_leave_are_recorded(h):
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    user = tg_user(5000)
    await h.feed(chat_member=member_update(ChatMemberLeft(user=user), ChatMemberMember(user=user)))
    await h.feed(chat_member=member_update(ChatMemberMember(user=user), ChatMemberLeft(user=user)))
    # Not a membership change → ignored.
    await h.feed(chat_member=member_update(ChatMemberMember(user=user), ChatMemberMember(user=user)))
    events = await h.db.get_member_events(CH)
    assert [(e["user_id"], e["kind"]) for e in events] == [(5000, "join"), (5000, "leave")]


async def test_chat_member_of_unknown_channel_is_ignored(h):
    user = tg_user(5000)
    await h.feed(chat_member=member_update(ChatMemberLeft(user=user), ChatMemberMember(user=user),
                                           chat_id=-100555))
    assert await h.db.get_member_events(-100555) == []


async def test_channel_post_is_counted(h):
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    post = Message(message_id=77, date=now(), chat=channel_chat(CH), text="пост из клиента")
    await h.feed(channel_post=post)
    rows = await h.db.get_channel_posts(CH)
    assert [(r["message_id"], r["via_bot"]) for r in rows] == [(77, 0)]


async def test_reaction_count_updates_target(h):
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    post_id = await h.db.create_post(text_md="x", status="sent")
    await h.db.set_post_targets(post_id, [CH])
    await h.db.update_target(post_id, CH, status="sent", message_id=88)
    event = MessageReactionCountUpdated(
        chat=channel_chat(CH), message_id=88, date=now(),
        reactions=[ReactionCount(type=ReactionTypeEmoji(emoji="👍"), total_count=3),
                   ReactionCount(type=ReactionTypeCustomEmoji(custom_emoji_id="555"), total_count=2)],
    )
    await h.feed(message_reaction_count=event)
    target = (await h.db.get_targets(post_id))[0]
    assert target["reactions_total"] == 5
    assert json.loads(target["reactions_json"]) == [
        {"type": "emoji", "count": 3, "emoji": "👍"},
        {"type": "custom_emoji", "count": 2, "custom_emoji_id": "555"},
    ]


async def test_untrusted_titles_are_escaped(h):
    """The harness validates every sent HTML text, so unescaped titles would fail here."""
    await h.feed(my_chat_member=promotion(title="Tom & Jerry <script>", username=None))
    assert "Tom &amp; Jerry &lt;script&gt;" in h.last_to(OWNER).text
    await h.send(OWNER, BTN_CHANNELS)
    await h.press(OWNER, ChannelCb(action="card", chat_id=CH))
    await h.press(OWNER, ChannelCb(action="ignore", chat_id=CH))


# ------------------------------------------------------------------ finding #4: discovery notices

def rights_toggle(chat_id: int = CH, title: str = "Тестовый канал", **rights: bool) -> ChatMemberUpdated:
    """The channel owner edits the bot's admin rights (administrator → administrator)."""
    return ChatMemberUpdated(
        chat=channel_chat(chat_id, title, None), from_user=tg_user(9999, "Stranger"), date=now(),
        old_chat_member=bot_admin_member(), new_chat_member=bot_admin_member(**rights),
    )


def discovery_notices(h: Harness, admin_id: int = OWNER) -> list[str]:
    return [t for t in h.session.texts_to(admin_id) if "назначен администратором" in t]


async def test_ignore_sticks_when_bot_rights_are_toggled(h):
    await h.feed(my_chat_member=promotion())
    await h.press(OWNER, ChannelCb(action="ignore", chat_id=CH))
    h.session.clear()
    for flag in (False, True, False):
        await h.feed(my_chat_member=rights_toggle(title="Ваш бот взломан! t.me/phish_example",
                                                  can_delete_messages=flag))
    assert (await h.db.get_channel(CH))["status"] == CH_IGNORED
    assert h.session.of(SendMessage) == []


async def test_discovery_notices_are_rate_limited_per_channel(h):
    await h.feed(my_chat_member=promotion())
    await h.feed(my_chat_member=demotion())
    await h.feed(my_chat_member=promotion())  # a remove / re-add cycle within the cooldown
    assert len(discovery_notices(h)) == 1
    assert (await h.db.get_channel(CH))["status"] == CH_PENDING  # the channel is still offered in the list


async def test_pending_channel_is_reoffered_once_it_may_post(h):
    await h.feed(my_chat_member=promotion(can_post_messages=False))
    await h.feed(my_chat_member=rights_toggle(can_delete_messages=False, can_post_messages=False))
    assert len(discovery_notices(h)) == 1
    await h.feed(my_chat_member=rights_toggle(can_post_messages=True))
    notices = discovery_notices(h)
    assert len(notices) == 2 and "нет права" not in notices[-1]


async def test_untrusted_names_are_shortened_in_notices(h):
    long_title = "Очень " * 40 + "<b>"
    await h.feed(my_chat_member=promotion(title=long_title, username=None))
    text = discovery_notices(h)[0]
    assert "&lt;b&gt;" not in text and "…" in text and len(text) < 600
    assert "Если вы не знаете этот канал" in text


# ------------------------------------------------------------------ finding #5: channel posts

async def test_service_messages_are_not_counted_and_albums_count_once(h):
    from app.services.stats import channel_stats

    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    for message_id in (10, 11, 12):  # an album posted from a Telegram client
        await h.feed(channel_post=Message(message_id=message_id, date=now(), chat=channel_chat(CH),
                                          photo=[PhotoSize(file_id=f"p{message_id}", file_unique_id="u",
                                                           width=10, height=10)],
                                          media_group_id="album"))
    await h.feed(channel_post=Message(message_id=13, date=now(), chat=channel_chat(CH), text="пост"))
    pinned = Message(message_id=13, date=now(), chat=channel_chat(CH), text="пост")
    await h.feed(channel_post=Message(message_id=14, date=now(), chat=channel_chat(CH), pinned_message=pinned))
    await h.feed(channel_post=Message(message_id=15, date=now(), chat=channel_chat(CH), new_chat_title="Новое"))

    rows = await h.db.get_channel_posts(CH)
    assert [(r["message_id"], r["media_group_id"]) for r in rows] == [
        (10, "album"), (11, "album"), (12, "album"), (13, None)]
    stats = await channel_stats(h.db, CH, 1, h.config.tz)
    assert stats["posts"] == 2


async def test_reactions_on_a_later_album_message_reach_the_post(h):
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    post_id = await h.db.create_post(text_md="x", status="sent")
    await h.db.set_post_targets(post_id, [CH])
    await h.db.update_target(post_id, CH, status="sent", message_id=88, message_ids_json="[88, 89, 90]")
    event = MessageReactionCountUpdated(
        chat=channel_chat(CH), message_id=89, date=now(),
        reactions=[ReactionCount(type=ReactionTypeEmoji(emoji="🔥"), total_count=4)],
    )
    await h.feed(message_reaction_count=event)
    assert (await h.db.get_targets(post_id))[0]["reactions_total"] == 4


# ------------------------------------------------------------------ finding #18: many channels

def button_data(message) -> list[str]:
    return [b.callback_data for row in message.reply_markup.inline_keyboard for b in row if b.callback_data]


async def add_many(h: Harness, count: int, *, start: int = 0, folder_id: int | None = None) -> list[int]:
    ids = []
    for i in range(start, start + count):
        cid = -1003000000000 - i
        await h.db.upsert_channel(cid, f"Канал новостей города {i:03d}", f"city_news_{i:03d}", status=CH_ACTIVE,
                                  members_count=1000 + i)
        if folder_id:
            await h.db.set_channel_folder(cid, folder_id)
        ids.append(cid)
    return ids


async def test_channels_screen_with_many_channels(h):
    """«📢 Каналы» opens with 120 channels outside folders (the Harness fails on a text over 4096
    characters or a keyboard over 100 buttons); every channel card stays reachable via pages."""
    from app.bot.callbacks import ChannelPageCb, FolderCb

    ids = await add_many(h, 120)
    await h.send(OWNER, BTN_CHANNELS)
    first = h.last_to(OWNER)
    assert "✅ <b>Подключены</b> (120)" in first.text and "выберите кнопками ниже" in first.text
    assert "Страница 1 из 3" in first.text
    assert ChannelPageCb(page=1).pack() in button_data(first)
    assert FolderCb(action="new").pack() in button_data(first)
    seen = {d for d in button_data(first) if d.startswith("ch:card:")}
    for page in (1, 2):
        await h.press(OWNER, ChannelPageCb(page=page))
        seen |= {d for d in button_data(h.edits()[-1]) if d.startswith("ch:card:")}
    assert seen == {ChannelCb(action="card", chat_id=cid).pack() for cid in ids}


async def test_folder_screen_with_many_channels(h):
    from app.bot.callbacks import FolderCb, FolderPageCb

    fid = await h.db.create_folder("Города")
    ids = await add_many(h, 90, folder_id=fid)
    for cid in ids:
        await h.db.set_channel_slots(cid, {d: ["09:00", "18:00"] for d in range(5)})
    await h.press(OWNER, FolderCb(action="open", folder_id=fid))
    screen = h.edits()[-1]
    assert "Каналов: 90" in screen.text and "выберите кнопками ниже" in screen.text
    assert FolderPageCb(folder_id=fid, page=1).pack() in button_data(screen)
    assert FolderCb(action="slots", folder_id=fid).pack() in button_data(screen)
    await h.press(OWNER, FolderPageCb(folder_id=fid, page=2))
    cards = [d for d in button_data(h.edits()[-1]) if d.startswith("ch:card:")]
    assert len(cards) == 10 and "Страница 3 из 3" in h.edits()[-1].text


async def test_old_channel_buttons_still_parse(h):
    """Callback data sent before an update (e.g. «✅ Добавить канал» of a discovery notice) keeps
    working: existing factories get no new fields."""
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    await h.press(OWNER, f"ch:card:{CH}")
    assert "Тестовый канал" in h.edits()[-1].text
    await h.press(OWNER, "fo:new:0")
    assert "Новая папка" in h.edits()[-1].text


# ------------------------------------------------------------------ finding #19: albums in dialogs

def album_items(count: int, group: str, **fields) -> list[dict]:
    return [{"photo": [PhotoSize(file_id=f"{group}_{i}", file_unique_id=f"{group}_{i}_u", width=800, height=600)],
             "media_group_id": group, **fields} for i in range(count)]


async def replies_after(h: Harness, action) -> list[str]:
    before = len(h.session.texts_to(OWNER))
    await action()
    return h.session.texts_to(OWNER)[before:]


async def test_album_in_the_schedule_dialog_gets_one_reply(h):
    await h.db.upsert_channel(CH, "Тестовый канал", None, status=CH_ACTIVE)
    await h.press(OWNER, ChannelCb(action="slots", chat_id=CH))

    async def send_album():
        for fields in album_items(6, "g_slots"):
            await h.send(OWNER, None, **fields)

    replies = await replies_after(h, send_album)
    assert replies == ["Сейчас я жду текст. Или нажмите «↩️ Назад»."]
    assert await fsm_state(h, OWNER) == ChannelStates.slots.state


async def test_album_in_the_add_dialog_gets_one_reply(h):
    await h.press(OWNER, ChannelCb(action="add"))

    async def send_album():
        for fields in album_items(5, "g_add"):
            await h.send(OWNER, None, **fields)

    replies = await replies_after(h, send_album)
    assert len(replies) == 1 and "перешлите пост из канала" in replies[0]


async def test_forwarded_album_adds_the_channel_once(h):
    h.session.add_chat(CH, "Новый канал", None)
    await h.press(OWNER, ChannelCb(action="add"))
    origin = MessageOriginChannel(date=now(), chat=channel_chat(CH, "Новый канал"), message_id=5)

    async def forward_album():
        for fields in album_items(4, "g_fwd", forward_origin=origin):
            await h.send(OWNER, None, **fields)

    replies = await replies_after(h, forward_album)
    assert len(replies) == 1 and replies[0].startswith("✅ Канал «Новый канал» добавлен")
    assert (await h.db.get_channel(CH))["status"] == CH_ACTIVE


async def test_forwarded_album_from_a_channel_without_the_bot_errs_once(h):
    h.session.add_chat(CH, "Чужой канал", None, bot_status="left")
    await h.press(OWNER, ChannelCb(action="add"))
    origin = MessageOriginChannel(date=now(), chat=channel_chat(CH, "Чужой канал"), message_id=5)
    h.session.clear()

    async def forward_album():
        for fields in album_items(5, "g_fwd2", forward_origin=origin):
            await h.send(OWNER, None, **fields)

    replies = await replies_after(h, forward_album)
    assert len(replies) == 1 and replies[0].startswith("❌")
    assert len(h.session.of(GetChat)) <= 1  # one lookup for the whole album

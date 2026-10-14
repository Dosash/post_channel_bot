"""Post composer: drafts, preview + panel, buttons, channels, options, scheduling, publishing, edits.

Media / albums: tests/test_bot_media.py; publish modes and review findings: tests/test_bot_publish.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Document, MessageEntity, PhotoSize, ReplyKeyboardMarkup

from app.bot.callbacks import ComposeCb, ComposeChannelCb, ComposeOptionCb, MenuCb, PostCb
from app.bot.keyboards import BTN_NEW, BTN_POSTS
from app.bot.states import ComposeStates
from app.db import CH_ACTIVE, POST_DRAFT, POST_SCHEDULED, POST_SENT
from tests.mocked_session import Harness, photo_sizes

OWNER = 111
CH_A = -1001000000001
CH_B = -1001000000002


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    harness.albums.delay = 0.05
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    yield harness
    await harness.close()


async def add_channel(h: Harness, chat_id: int, title: str, username: str | None = None) -> None:
    await h.db.upsert_channel(chat_id, title, username, status=CH_ACTIVE,
                              rights={"can_post": True, "can_edit": True, "can_delete": True})
    h.session.add_chat(chat_id, title, username)


@pytest.fixture
async def one_channel(h):
    await add_channel(h, CH_A, "Канал А", "kanal_a")


async def fsm(h: Harness) -> tuple[str | None, dict]:
    key = StorageKey(bot_id=h.bot.id, chat_id=OWNER, user_id=OWNER)
    return await h.dp.storage.get_state(key), await h.dp.storage.get_data(key)


async def latest_post(h: Harness) -> dict:
    row = await h.db.fetchone("SELECT id FROM posts ORDER BY id DESC LIMIT 1")
    return await h.db.get_post(row["id"])


def photo(file_id: str = "photo_big") -> list[PhotoSize]:
    return photo_sizes(file_id)


def panel(h: Harness) -> SendMessage:
    """The last control panel sent to the owner."""
    panels = [r for r in h.session.sent_to(OWNER) if isinstance(r, SendMessage) and "Длина:" in r.text]
    assert panels, "no control panel was sent"
    return panels[-1]


def button_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def new_draft(h: Harness, text: str = "**Привет**") -> dict:
    await h.send(OWNER, "/new")
    await h.send(OWNER, text)
    return await latest_post(h)


# ------------------------------------------------------------------ creating a draft

async def test_new_without_channels_hints_to_add_one(h):
    await h.send(OWNER, "/new")
    message = h.last_to(OWNER)
    assert "нет подключённых каналов" in message.text
    assert (await fsm(h))[0] is None


async def test_new_post_creates_draft_preview_and_panel(h, one_channel):
    await h.send(OWNER, BTN_NEW)
    assert "Пришлите текст поста" in h.last_to(OWNER).text
    assert (await fsm(h))[0] == ComposeStates.start.state

    h.session.clear()
    await h.send(OWNER, "**Привет**")
    post = await latest_post(h)
    assert post["text_md"] == "**Привет**"
    assert post["status"] == POST_DRAFT and post["source"] == "bot" and post["created_by"] == OWNER
    assert [t["channel_id"] for t in post["targets"]] == [CH_A]  # the only channel is preselected

    sent = h.session.sent_to(OWNER)
    preview, panel_msg = sent[0], sent[1]
    assert preview.text == "<b>Привет</b>"
    assert f"📝 <b>Черновик #{post['id']}</b>" in panel_msg.text
    assert "Канал А" in panel_msg.text and "📏 Длина: 6 / 4096" in panel_msg.text
    assert "🖼 Медиа: нет" in panel_msg.text
    data = button_data(panel_msg.reply_markup)
    for action in ("text", "media", "buttons", "channels", "options", "publish", "close", "delete"):
        assert ComposeCb(action=action, post_id=post["id"]).pack() in data
    state, fsm_data = await fsm(h)
    assert state == ComposeStates.menu.state
    assert fsm_data["post_id"] == post["id"] and fsm_data["panel_id"]
    assert len(fsm_data["preview_ids"]) == 1 and fsm_data["preview_ids"][0] < fsm_data["panel_id"]
    # the /new prompt was cleaned up
    assert h.session.deleted_ids(OWNER)


async def test_telegram_formatting_is_converted_to_markdown(h, one_channel):
    await h.send(OWNER, "/new")
    await h.send(OWNER, "Привет мир", entities=[MessageEntity(type="bold", offset=0, length=6)])
    post = await latest_post(h)
    assert post["text_md"] == "**Привет** мир"


async def test_draft_with_two_channels_has_no_default_targets(h):
    await add_channel(h, CH_A, "Канал А")
    await add_channel(h, CH_B, "Канал Б")
    post = await new_draft(h)
    assert post["targets"] == []
    assert "⚠️ не выбраны" in panel(h).text


async def test_photo_post(h, one_channel):
    await h.send(OWNER, "/new")
    h.session.clear()
    await h.send(OWNER, None, photo=photo(), caption="Подпись *курсив*")
    post = await latest_post(h)
    assert post["media"] == [{"type": "photo", "file_id": "photo_big", "path": None, "width": 1280, "height": 960,
                              "file_bot": h.bot.id}]  # SPEC_V22: the file_id belongs to this bot
    assert post["photo_file_id"] == "photo_big"  # legacy column mirrors the first photo
    assert post["text_md"] == "Подпись *курсив*"
    assert "🖼 Медиа: 🖼 Фото" in panel(h).text
    preview = h.session.of(SendPhoto)[0]
    assert preview.chat_id == OWNER and preview.photo == "photo_big"
    assert preview.caption == "Подпись <i>курсив</i>"
    assert "📏 Длина: 14 / 1024" in panel(h).text


async def test_unsupported_content(h, one_channel):
    await h.send(OWNER, "/new")
    document = Document(file_id="doc", file_unique_id="d", mime_type="image/png")
    await h.send(OWNER, None, document=document)
    assert "как фото" in h.last_to(OWNER).text
    assert await h.db.count_posts() == 0




# ------------------------------------------------------------------ editing the draft

async def test_free_text_and_photo_replace_content(h, one_channel):
    post = await new_draft(h)
    h.session.clear()
    await h.send(OWNER, "Новый текст")
    assert (await h.db.get_post(post["id"]))["text_md"] == "Новый текст"
    # old preview and panel are deleted, fresh ones sent
    assert len(h.session.deleted_ids(OWNER)) >= 2
    assert h.session.sent_to(OWNER)[0].text == "Новый текст"

    await h.send(OWNER, None, photo=photo("pic"))
    updated = await h.db.get_post(post["id"])
    assert updated["media"][0]["file_id"] == "pic" and updated["text_md"] == "Новый текст"


async def test_text_sub_state(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="text", post_id=post["id"]))
    assert (await fsm(h))[0] == ComposeStates.text.state
    prompt = h.edits()[-1]
    assert "Пришлите новый текст" in prompt.text and "<pre>**Привет**</pre>" in prompt.text
    await h.send(OWNER, "~~Другой~~ текст")
    assert (await h.db.get_post(post["id"]))["text_md"] == "~~Другой~~ текст"
    assert (await fsm(h))[0] == ComposeStates.menu.state


async def test_render_error_is_shown_in_panel(h, one_channel):
    post = await new_draft(h)
    h.session.clear()
    await h.send(OWNER, "a" * 3000)
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    assert (await fsm(h))[0] == ComposeStates.media.state
    await h.send(OWNER, None, photo=photo())
    text = panel(h).text
    assert "Ошибка:" in text and "слишком длинный" in text
    assert "📏 Длина: 3000 / 1024 ⚠️" in text


async def test_buttons_input(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="buttons", post_id=post["id"]))
    assert (await fsm(h))[0] == ComposeStates.buttons.state
    help_text = h.edits()[-1].text
    assert "зелёная" in help_text and "Купить - https://shop.com - зелёная" in help_text

    await h.send(OWNER, "Кнопка без ссылки")
    error = h.last_to(OWNER).text
    assert error.startswith("❌ Строка 1:") and "ссылка" in error
    assert (await fsm(h))[0] == ComposeStates.buttons.state

    h.session.clear()
    await h.send(OWNER, "Купить - https://shop.com - зелёная | 🔴 Выход - t.me/x")
    saved = await h.db.get_post(post["id"])
    assert saved["buttons"] == [[
        {"text": "Купить", "url": "https://shop.com", "style": "success"},
        {"text": "Выход", "url": "https://t.me/x", "style": "danger"},
    ]]
    preview = h.session.sent_to(OWNER)[0]
    buttons = preview.reply_markup.inline_keyboard[0]
    assert (buttons[0].style, buttons[1].style) == ("success", "danger")
    assert "🔘 Кнопки: 2" in panel(h).text

    await h.press(OWNER, ComposeCb(action="rmbuttons", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["buttons"] == []


async def test_channel_picker_and_options(h):
    await add_channel(h, CH_A, "Канал А")
    await add_channel(h, CH_B, "Канал Б")
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="channels", post_id=post["id"]))
    picker = h.edits()[-1]
    assert ComposeChannelCb(post_id=post["id"], channel_id=CH_B).pack() in button_data(picker.reply_markup)

    await h.press(OWNER, ComposeChannelCb(post_id=post["id"], channel_id=CH_B))
    assert [t["channel_id"] for t in (await h.db.get_post(post["id"]))["targets"]] == [CH_B]
    await h.press(OWNER, ComposeCb(action="ch_all", post_id=post["id"]))
    assert len((await h.db.get_post(post["id"]))["targets"]) == 2
    await h.press(OWNER, ComposeChannelCb(post_id=post["id"], channel_id=CH_A))
    assert [t["channel_id"] for t in (await h.db.get_post(post["id"]))["targets"]] == [CH_B]

    await h.press(OWNER, ComposeOptionCb(post_id=post["id"], option="silent"))
    await h.press(OWNER, ComposeOptionCb(post_id=post["id"], option="disable_preview"))
    saved = await h.db.get_post(post["id"])
    assert saved["silent"] and saved["disable_preview"] and not saved["protect"]
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="opts_done", post_id=post["id"]))
    preview = h.session.sent_to(OWNER)[0]
    assert preview.disable_notification is True
    assert preview.link_preview_options.is_disabled is True
    # every callback got exactly one answer
    assert len(h.session.of(AnswerCallbackQuery)) == 1


# ------------------------------------------------------------------ publishing

async def test_publish_with_confirmation(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="publish", post_id=post["id"]))
    choice = h.edits()[-1]
    assert "Как опубликовать?" in choice.text
    for action in ("now", "queue", "schedule"):
        assert ComposeCb(action=action, post_id=post["id"]).pack() in button_data(choice.reply_markup)
    await h.press(OWNER, ComposeCb(action="now", post_id=post["id"]))
    confirm = h.edits()[-1]
    assert "Опубликовать в 1 канал?" in confirm.text
    assert ComposeCb(action="confirm", post_id=post["id"]).pack() in button_data(confirm.reply_markup)
    assert (await h.db.get_post(post["id"]))["status"] == POST_DRAFT

    h.session.clear()
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    published = await h.db.get_post(post["id"])
    assert published["status"] == POST_SENT
    to_channel = h.session.sent_to(CH_A)
    assert len(to_channel) == 1 and to_channel[0].text == "<b>Привет</b>"
    report = h.last_to(OWNER)
    assert report.text.startswith("✅ Опубликовано: 1 из 1")
    assert "https://t.me/kanal_a/" in report.text
    assert isinstance(report.reply_markup, ReplyKeyboardMarkup)
    assert (await fsm(h))[0] is None


async def test_publish_without_channels_is_refused(h):
    await add_channel(h, CH_A, "Канал А")
    await add_channel(h, CH_B, "Канал Б")
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="publish", post_id=post["id"]))
    answer = h.session.of(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "выберите каналы" in answer.text
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["status"] == POST_DRAFT
    assert "Не выбраны каналы" in panel_or_edit_text(h)


def panel_or_edit_text(h: Harness) -> str:
    return h.edits()[-1].text


async def test_panel_works_after_restart(h, one_channel):
    """Panel buttons carry the post id: a lost FSM session is restored from the DB."""
    post = await new_draft(h)
    await h.dp.storage.close()
    key = StorageKey(bot_id=h.bot.id, chat_id=OWNER, user_id=OWNER)
    await h.dp.storage.set_state(key, None)
    await h.dp.storage.set_data(key, {})
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["status"] == POST_SENT


# ------------------------------------------------------------------ scheduling

async def test_schedule_by_text(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="schedule", post_id=post["id"]))
    prompt = h.edits()[-1].text
    assert "<code>+2h</code>" in prompt and "Asia/Tashkent" in prompt
    await h.send(OWNER, "вчера когда-то")
    assert h.last_to(OWNER).text.startswith("❌")
    assert (await fsm(h))[0] == ComposeStates.schedule.state

    await h.send(OWNER, "+2h")
    saved = await h.db.get_post(post["id"])
    assert saved["status"] == POST_SCHEDULED
    expected = datetime.now(timezone.utc).timestamp() + 7200
    assert abs(saved["scheduled_at"] - expected) < 120
    target = saved["targets"][0]
    assert target["scheduled_at"] == saved["scheduled_at"] and not target["queued"]  # per-channel time (V2)
    message = h.last_to(OWNER).text
    assert message.startswith(f"⏰ Пост #{post['id']} запланирован на") and "(Asia/Tashkent)" in message
    assert (await fsm(h))[0] is None


async def test_schedule_preset_and_unschedule(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="at_1h", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["status"] == POST_SCHEDULED
    # reopening the scheduled post shows its time; unschedule returns it to drafts
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    assert "⏰ <b>Запланированный пост" in panel(h).text
    await h.press(OWNER, ComposeCb(action="unschedule", post_id=post["id"]))
    saved = await h.db.get_post(post["id"])
    assert saved["status"] == POST_DRAFT and saved["scheduled_at"] is None


# ------------------------------------------------------------------ closing / deleting

async def test_delete_draft(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="delete", post_id=post["id"]))
    assert await h.db.get_post(post["id"]) is not None
    await h.press(OWNER, ComposeCb(action="delete_ok", post_id=post["id"]))
    assert await h.db.get_post(post["id"]) is None
    assert "удалён" in h.edits()[-1].text
    assert (await fsm(h))[0] is None


async def test_cancel_keeps_draft(h, one_channel):
    post = await new_draft(h)
    await h.send(OWNER, "/cancel")
    assert f"Черновик #{post['id']} сохранён" in h.last_to(OWNER).text
    assert (await h.db.get_post(post["id"]))["status"] == POST_DRAFT


async def test_menu_buttons_work_inside_composer(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="text", post_id=post["id"]))
    await h.send(OWNER, BTN_POSTS)
    assert "🗂 <b>Посты</b>" in h.last_to(OWNER).text
    assert (await h.db.get_post(post["id"]))["text_md"] == "**Привет**"


async def test_new_post_mentions_previous_draft(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, MenuCb(action="new"))
    assert f"Черновик #{post['id']} сохранён" in h.last_to(OWNER).text


# ------------------------------------------------------------------ editing a published post

async def publish(h: Harness, text: str = "**Привет**", **photo_fields) -> dict:
    post = await new_draft(h, text)
    if photo_fields:
        await h.db.update_post(post["id"], **photo_fields)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    return await h.db.get_post(post["id"])


async def test_edit_published_text_post(h, one_channel):
    post = await publish(h)
    message_id = post["targets"][0]["message_id"]
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    edit_panel = panel(h)
    assert "Редактирование поста" in edit_panel.text
    data = button_data(edit_panel.reply_markup)
    assert ComposeCb(action="save", post_id=post["id"]).pack() in data
    assert ComposeCb(action="channels", post_id=post["id"]).pack() not in data
    assert ComposeCb(action="schedule", post_id=post["id"]).pack() not in data

    await h.send(OWNER, "Исправленный **текст**")
    # nothing is written before saving
    assert (await h.db.get_post(post["id"]))["text_md"] == "**Привет**"
    assert "Изменения ещё не применены" in panel(h).text

    await h.press(OWNER, ComposeCb(action="save", post_id=post["id"]))
    assert "Применить изменения?" in h.edits()[-1].text
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    saved = await h.db.get_post(post["id"])
    assert saved["text_md"] == "Исправленный **текст**"
    edit = [e for e in h.session.of(EditMessageText) if e.chat_id == CH_A]
    assert len(edit) == 1 and edit[0].message_id == message_id
    assert edit[0].text == "Исправленный <b>текст</b>"
    assert h.last_to(OWNER).text.startswith("✅ Обновлено: 1 из 1")


async def test_discard_edit_keeps_published_post(h, one_channel):
    post = await publish(h)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "черновая правка")
    await h.press(OWNER, ComposeCb(action="discard", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["text_md"] == "**Привет**"
    assert not [e for e in h.session.of(EditMessageText) if e.chat_id == CH_A]


async def test_edit_photo_post_replaces_picture(h, one_channel):
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo("old_pic"), caption="Подпись")
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, None, photo=photo("new_pic"))
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    media = [m for m in h.session.of(EditMessageMedia) if m.chat_id == CH_A]
    assert len(media) == 1 and media[0].media.media == "new_pic"
    assert (await h.db.get_post(post["id"]))["photo_file_id"] == "new_pic"

    # a caption-only change edits the caption
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "Новая подпись")
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    captions = [c for c in h.session.of(EditMessageCaption) if c.chat_id == CH_A]
    assert captions and captions[0].caption == "Новая подпись"


async def test_cannot_add_photo_to_published_text_post(h, one_channel):
    post = await publish(h)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, None, photo=photo())
    assert "нельзя добавить фото, видео или GIF" in h.last_to(OWNER).text
    saved = await h.db.get_post(post["id"])
    assert saved["media"] == [] and saved["photo_file_id"] is None
    _, data = await fsm(h)
    assert not data["overrides"] and not data["media_changed"]
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    assert "нельзя добавить" in h.callback_answers()[-1].text


async def test_second_confirm_click_does_not_reopen_composer(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    assert (await fsm(h))[0] is None
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    answer = h.session.of(AnswerCallbackQuery)[0]
    assert answer.show_alert and "устарела" in answer.text
    assert len(h.session.sent_to(CH_A)) == 0  # not published twice
    assert (await fsm(h))[0] is None  # no hidden edit session was started


async def test_edit_mode_offers_only_applicable_option(h, one_channel):
    post = await publish(h)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="options", post_id=post["id"]))
    data = button_data(h.edits()[-1].reply_markup)
    assert ComposeOptionCb(post_id=post["id"], option="disable_preview").pack() in data
    assert ComposeOptionCb(post_id=post["id"], option="silent").pack() not in data
    await h.press(OWNER, ComposeOptionCb(post_id=post["id"], option="silent"))
    assert not (await h.db.get_post(post["id"]))["silent"]
    await h.press(OWNER, ComposeOptionCb(post_id=post["id"], option="disable_preview"))
    # kept as an unsaved change until «💾 Сохранить изменения»
    assert not (await h.db.get_post(post["id"]))["disable_preview"]
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["disable_preview"]
    edit = [e for e in h.session.of(EditMessageText) if e.chat_id == CH_A][-1]
    assert edit.link_preview_options.is_disabled is True


async def test_preview_error_from_telegram_is_shown_in_panel(h, one_channel):
    from aiogram.exceptions import TelegramBadRequest

    h.session.fail_on[SendMessage] = lambda m: (
        TelegramBadRequest(method=m, message="Bad Request: can't parse entities")
        if m.chat_id == OWNER and m.text == "<b>Сломано</b>" else None)
    await h.send(OWNER, "/new")
    await h.send(OWNER, "**Сломано**")
    text = panel(h).text
    assert "Не удалось показать превью" in text and "Ошибка разметки" in text


async def test_promotion_and_native_picker_race_ends_active(h):
    """my_chat_member (→ pending) and chat_shared (→ active) arriving together."""
    import asyncio

    from aiogram.types import ChatMemberLeft, ChatMemberUpdated, ChatShared, User

    from tests.mocked_session import BOT_ID, bot_admin_member, channel_chat, private_message, tg_user

    h.session.add_chat(CH_A, "Канал А", "kanal_a")
    promotion = ChatMemberUpdated(
        chat=channel_chat(CH_A, "Канал А", "kanal_a"), from_user=tg_user(OWNER), date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(user=User(id=BOT_ID, is_bot=True, first_name="bot")),
        new_chat_member=bot_admin_member(),
    )
    shared = private_message(OWNER, None, message_id=77,
                             chat_shared=ChatShared(request_id=1, chat_id=CH_A, title="Канал А"))
    await asyncio.gather(h.feed(my_chat_member=promotion), h.feed(message=shared))
    assert (await h.db.get_channel(CH_A))["status"] == CH_ACTIVE


async def test_html_like_post_text_is_escaped_everywhere(h, one_channel):
    post = await new_draft(h, "a < b && <i>не тег</i>")
    assert post["text_md"] == "a < b && <i>не тег</i>"
    assert h.session.sent_to(OWNER)[-2].text == "a &lt; b &amp;&amp; &lt;i&gt;не тег&lt;/i&gt;"
    await h.press(OWNER, ComposeCb(action="text", post_id=post["id"]))
    assert "<pre>a &lt; b &amp;&amp; &lt;i&gt;не тег&lt;/i&gt;</pre>" in h.edits()[-1].text
    await h.press(OWNER, PostCb(action="open", post_id=post["id"]))


# ------------------------------------------------------------------ SPEC_V23: premium emoji

EMOJI_ID = "5000000000000000001"


async def test_panel_warns_about_premium_emoji_without_fragment_username(h, one_channel):
    """Custom emoji in the text while «✨ Премиум-эмодзи» is off: the panel says they will become
    regular emoji; with the setting on (or without custom emoji) there is no warning."""
    await h.send(OWNER, "/new")
    entity = MessageEntity(type="custom_emoji", offset=7, length=2, custom_emoji_id=EMOJI_ID)
    await h.send(OWNER, "Привет 😀", entities=[entity])
    post = await latest_post(h)
    assert f"tg://emoji?id={EMOJI_ID}" in post["text_md"]
    assert "⚠️ Премиум-эмодзи станут обычными (нет юзернейма с Fragment)" in panel(h).text

    await h.db.set_premium_emoji(True)
    await h.press(OWNER, ComposeCb(action="options", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="back", post_id=post["id"]))  # the panel in place
    assert "Длина:" in h.edits()[-1].text and "Премиум-эмодзи" not in h.edits()[-1].text

    await h.db.set_premium_emoji(False)
    await h.send(OWNER, "Обычный текст 😀")
    assert "Премиум-эмодзи" not in panel(h).text


# ------------------------------------------------------------------ review2 #3: a claim lands meanwhile

def claim_before(h: Harness, method: str) -> None:
    """The scheduler claims the post right before `db.<method>` runs (after the handler's check)."""
    original = getattr(h.db, method)

    async def claimed_first(post_id, *args, **kwargs):
        await h.db.execute("UPDATE posts SET status = 'sending' WHERE id = ?", (post_id,))
        return await original(post_id, *args, **kwargs)

    setattr(h.db, method, claimed_first)


@pytest.mark.parametrize("press", ["channel", "none"])
async def test_unticking_channels_of_a_post_claimed_meanwhile_keeps_its_targets(h, press):
    """Regression (review2 #3, bot side): the scheduler claims the post between the picker's check
    and its write — the channel being sent to keeps its target (and the owner is told)."""
    await add_channel(h, CH_A, "Канал А")
    await add_channel(h, CH_B, "Канал Б")
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="ch_all", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="channels", post_id=post["id"]))
    claim_before(h, "set_post_targets")
    if press == "channel":
        await h.press(OWNER, ComposeChannelCb(post_id=post["id"], channel_id=CH_A))
    else:
        await h.press(OWNER, ComposeCb(action="ch_none", post_id=post["id"]))
    saved = await h.db.get_post(post["id"])
    assert saved["status"] == "sending"
    assert sorted(t["channel_id"] for t in saved["targets"]) == sorted([CH_A, CH_B])
    assert "публикуется" in h.session.of(AnswerCallbackQuery)[-1].text


async def test_delete_draft_claimed_meanwhile_keeps_the_record(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="delete", post_id=post["id"]))
    claim_before(h, "delete_post_record")
    await h.press(OWNER, ComposeCb(action="delete_ok", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["status"] == "sending"
    assert "публикуется" in h.session.of(AnswerCallbackQuery)[-1].text
    assert all("удалён" not in (e.text or "") for e in h.edits())

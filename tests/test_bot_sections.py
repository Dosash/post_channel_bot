"""«🗂 Посты», «📊 Статистика», «⚙️ Настройки» and Telegram edit-error handling."""
from __future__ import annotations

import time

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage

from app.bot.callbacks import ChannelCb, PostCb, PostListCb, SettingsCb, StatsCb, TimezoneCb
from app.bot.keyboards import BTN_POSTS, BTN_SETTINGS, BTN_STATS
from app.bot.states import PostStates
from app.db import CH_ACTIVE, POST_DELETED, POST_DRAFT, POST_SCHEDULED, POST_SENT
from app.services.timezone import COMMON as TZ_COMMON
from tests.mocked_session import Harness

OWNER = 111
ADMIN2 = 222
CH = -1001000000001


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    await db.add_admin(ADMIN2, "helper", "Helper")
    await db.upsert_channel(CH, "Канал", "kanal", status=CH_ACTIVE, members_count=150,
                            rights={"can_post": True, "can_edit": True, "can_delete": True})
    harness.session.add_chat(CH, "Канал", "kanal", members=150)
    yield harness
    await harness.close()


async def make_post(h: Harness, text: str = "Текст поста", status: str = POST_DRAFT, **fields) -> int:
    post_id = await h.db.create_post(text_md=text, status=status, **fields)
    await h.db.set_post_targets(post_id, [CH])
    return post_id


async def make_sent_post(h: Harness, text: str = "Опубликованный", message_id: int = 70) -> int:
    post_id = await make_post(h, text, status=POST_SENT, sent_at=int(time.time()))
    await h.db.update_target(post_id, CH, status="sent", message_id=message_id, sent_at=int(time.time()))
    return post_id


def markup_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def state_of(h: Harness, user_id: int = OWNER) -> str | None:
    return await h.dp.storage.get_state(StorageKey(bot_id=h.bot.id, chat_id=user_id, user_id=user_id))


# ------------------------------------------------------------------ posts

async def test_posts_list_tabs_and_pagination(h):
    for i in range(10):
        await make_post(h, f"Черновик {i}")
    scheduled = await make_post(h, "Отложенный", status=POST_SCHEDULED, scheduled_at=int(time.time()) + 3600)
    await h.send(OWNER, BTN_POSTS)
    message = h.last_to(OWNER)
    assert "Запланированные: 1" in message.text  # scheduled tab is the default when non-empty
    data = markup_data(message.reply_markup)
    assert PostCb(action="open", post_id=scheduled, tab="s", page=0).pack() in data

    await h.press(OWNER, PostListCb(tab="d", page=0))
    drafts = h.edits()[-1]
    assert "Черновики: 10" in drafts.text and "Страница 1 из 2" in drafts.text
    assert len([d for d in markup_data(drafts.reply_markup) if d.startswith("p:open:")]) == 8
    await h.press(OWNER, PostListCb(tab="d", page=1))
    assert len([d for d in markup_data(h.edits()[-1].reply_markup) if d.startswith("p:open:")]) == 2


async def test_scheduled_card_unschedule_and_reschedule(h):
    post_id = await make_post(h, "Отложенный **пост**", status=POST_SCHEDULED,
                              scheduled_at=int(time.time()) + 3600)
    await h.press(OWNER, PostCb(action="open", post_id=post_id, tab="s"))
    card = h.edits()[-1]
    assert f"Пост #{post_id}" in card.text and "запланирован" in card.text
    data = markup_data(card.reply_markup)
    for action in ("now", "resched", "unschedule", "edit"):
        assert PostCb(action=action, post_id=post_id, tab="s").pack() in data

    await h.press(OWNER, PostCb(action="resched", post_id=post_id, tab="s"))
    assert await state_of(h) == PostStates.reschedule.state
    await h.send(OWNER, "+3h")
    post = await h.db.get_post(post_id)
    assert post["status"] == POST_SCHEDULED
    assert abs(post["scheduled_at"] - (time.time() + 3 * 3600)) < 120
    assert "перенесён" in h.edits()[-1].text
    assert await state_of(h) is None

    await h.press(OWNER, PostCb(action="unschedule", post_id=post_id, tab="s"))
    post = await h.db.get_post(post_id)
    assert post["status"] == POST_DRAFT and post["scheduled_at"] is None


async def test_publish_scheduled_now(h):
    post_id = await make_post(h, "Срочно", status=POST_SCHEDULED, scheduled_at=int(time.time()) + 3600)
    await h.press(OWNER, PostCb(action="now", post_id=post_id, tab="s"))
    assert "Опубликовать пост" in h.edits()[-1].text
    await h.press(OWNER, PostCb(action="pub_ok", post_id=post_id, tab="s"))
    assert (await h.db.get_post(post_id))["status"] == POST_SENT
    assert h.session.sent_to(CH)[0].text == "Срочно"
    assert h.edits()[-1].text.startswith("✅ Опубликовано: 1 из 1")


async def test_delete_from_channels_asks_confirmation(h):
    post_id = await make_sent_post(h)
    await h.press(OWNER, PostCb(action="open", post_id=post_id, tab="p"))
    card = h.edits()[-1]
    assert 'href="https://t.me/kanal/70"' in card.text
    await h.press(OWNER, PostCb(action="delch", post_id=post_id, tab="p"))
    assert "Удалить пост" in h.edits()[-1].text
    assert h.session.of(DeleteMessage) == []
    await h.press(OWNER, PostCb(action="delch_ok", post_id=post_id, tab="p"))
    deleted = h.session.of(DeleteMessage)
    assert [(d.chat_id, d.message_id) for d in deleted] == [(CH, 70)]
    assert (await h.db.get_post(post_id))["status"] == POST_DELETED


async def test_delete_draft_record_and_duplicate(h):
    post_id = await make_post(h, "Копируемый", buttons=[[{"text": "A", "url": "https://a.ru", "style": None}]])
    await h.press(OWNER, PostCb(action="dup", post_id=post_id, tab="d"))
    copy = await h.db.fetchone("SELECT MAX(id) AS id FROM posts")
    duplicate = await h.db.get_post(copy["id"])
    assert duplicate["id"] != post_id and duplicate["text_md"] == "Копируемый"
    assert duplicate["buttons"] == [[{"text": "A", "url": "https://a.ru", "style": None}]]
    assert [t["channel_id"] for t in duplicate["targets"]] == [CH]
    assert any("копия поста" in (r.text or "") for r in h.session.of(SendMessage))

    await h.press(OWNER, PostCb(action="delete", post_id=post_id, tab="d"))
    assert await h.db.get_post(post_id) is not None
    await h.press(OWNER, PostCb(action="delete_ok", post_id=post_id, tab="d"))
    assert await h.db.get_post(post_id) is None


async def test_sent_post_record_cannot_be_deleted(h):
    post_id = await make_sent_post(h)
    await h.press(OWNER, PostCb(action="delete_ok", post_id=post_id, tab="p"))
    assert await h.db.get_post(post_id) is not None


async def test_preview_from_card(h):
    post_id = await make_post(h, "__Превью__")
    await h.press(OWNER, PostCb(action="preview", post_id=post_id, tab="d"))
    assert h.last_to(OWNER).text == "<u>Превью</u>"


# ------------------------------------------------------------------ stats

async def test_stats_overview_and_channel(h):
    await h.db.add_member_snapshot(CH, 140, ts=int(time.time()) - 86400 * 3)
    await h.db.add_member_snapshot(CH, 150)
    await h.send(OWNER, BTN_STATS)
    overview = h.last_to(OWNER)
    assert "👥 Подписчиков всего: 150" in overview.text
    assert StatsCb(action="ch", chat_id=CH, days=30).pack() in markup_data(overview.reply_markup)

    await h.press(OWNER, StatsCb(action="ch", chat_id=CH, days=7))
    stats = h.edits()[-1]
    assert "📊 <b>Канал</b>" in stats.text and "Период: 7 дн." in stats.text
    assert "<code>http://127.0.0.1:8080/</code>" in stats.text
    urls = [b.url for row in stats.reply_markup.inline_keyboard for b in row if b.url]
    assert urls == []  # no URL button for a local panel address
    assert StatsCb(action="ch", chat_id=CH, days=90).pack() in markup_data(stats.reply_markup)


async def test_stats_web_button_with_public_url(h):
    h.config.web_public_url = "https://panel.example.com"
    await h.press(OWNER, StatsCb(action="ch", chat_id=CH, days=30))
    urls = [b.url for row in h.edits()[-1].reply_markup.inline_keyboard for b in row if b.url]
    assert urls == ["https://panel.example.com"]


async def test_channel_card_stats_button(h):
    await h.press(OWNER, ChannelCb(action="card", chat_id=CH))
    stats_cb = StatsCb(action="ch", chat_id=CH, days=30).pack()
    assert stats_cb in markup_data(h.edits()[-1].reply_markup)
    await h.press(OWNER, stats_cb)
    assert "Период: 30 дн." in h.edits()[-1].text


# ------------------------------------------------------------------ settings

async def test_settings_screen(h):
    await h.send(OWNER, BTN_SETTINGS)
    text = h.last_to(OWNER).text
    assert "http://127.0.0.1:8080" in text and "не установлен" in text
    assert "Asia/Tashkent" in text and "/addadmin" in text
    # the admin profile is refreshed from Telegram on every message
    assert f"👑 Admin — <code>{OWNER}</code>" in text and "• Helper (@helper)" in text


async def test_premium_emoji_toggle(h):
    """SPEC_V23: «✨ Премиум-эмодзи» in the settings — status line and a toggle for any admin."""
    await h.send(ADMIN2, BTN_SETTINGS)
    message = h.last_to(ADMIN2)
    assert "✨ Премиум-эмодзи: недоступны" in message.text and "Fragment" in message.text
    labels = [b.text for row in message.reply_markup.inline_keyboard for b in row]
    assert "✨ Премиум-эмодзи: выкл" in labels
    assert SettingsCb(action="premium").pack() in markup_data(message.reply_markup)

    await h.press(ADMIN2, SettingsCb(action="premium"))
    assert await h.db.get_premium_emoji() is True
    screen = h.edits()[-1]
    assert "✨ Премиум-эмодзи: доступны" in screen.text
    assert "✨ Премиум-эмодзи: вкл" in [b.text for row in screen.reply_markup.inline_keyboard for b in row]

    await h.press(ADMIN2, SettingsCb(action="premium"))
    assert await h.db.get_premium_emoji() is False
    assert "недоступны" in h.edits()[-1].text


async def test_timezone_from_settings(h):
    """«🕒 Часовой пояс»: a list of zones, a choice applies at once, «Как в .env» returns it."""
    await h.send(ADMIN2, BTN_SETTINGS)
    assert "Asia/Tashkent (UTC+5) — из .env" in h.last_to(ADMIN2).text
    assert TimezoneCb(i=-2).pack() in markup_data(h.last_to(ADMIN2).reply_markup)

    await h.press(ADMIN2, TimezoneCb(i=-2))
    screen = h.edits()[-1]
    assert "🕒 <b>Часовой пояс</b>" in screen.text and "/timezone" in screen.text
    moscow = next(i for i, (name, _) in enumerate(TZ_COMMON) if name == "Europe/Moscow")
    assert TimezoneCb(i=moscow).pack() in markup_data(screen.reply_markup)
    assert TimezoneCb(i=-1).pack() not in markup_data(screen.reply_markup)

    await h.press(ADMIN2, TimezoneCb(i=moscow))
    assert str(h.config.tz) == "Europe/Moscow" and await h.db.get_setting("timezone") == "Europe/Moscow"
    screen = h.edits()[-1]
    assert "Europe/Moscow (UTC+3)" in screen.text
    assert "✅ Москва (UTC+3)" in [b.text for row in screen.reply_markup.inline_keyboard for b in row]

    await h.press(ADMIN2, TimezoneCb(i=-1))
    assert str(h.config.tz) == "Asia/Tashkent" and await h.db.get_setting("timezone") is None


async def test_timezone_command(h):
    await h.send(OWNER, "/timezone UTC+3")
    assert "✅ Часовой пояс: <b>UTC+3</b>" in h.last_to(OWNER).text
    assert str(h.config.tz) == "Etc/GMT-3"
    await h.send(OWNER, "/timezone Mars/Base")
    assert "Неизвестный часовой пояс" in h.last_to(OWNER).text and str(h.config.tz) == "Etc/GMT-3"
    await h.send(OWNER, "/timezone env")
    assert str(h.config.tz) == "Asia/Tashkent"


async def test_owner_resets_web_password(h):
    await h.db.set_setting("web_password_hash", "scrypt$aa$bb")
    await h.send(OWNER, "/settings")
    message = h.last_to(OWNER)
    assert "установлен ✅" in message.text
    assert SettingsCb(action="reset").pack() in markup_data(message.reply_markup)
    await h.press(OWNER, SettingsCb(action="reset"))
    assert await h.db.get_setting("web_password_hash") is not None  # confirmation first
    await h.press(OWNER, SettingsCb(action="reset_ok"))
    assert await h.db.get_setting("web_password_hash") is None
    assert "Пароль веб-панели сброшен" in h.edits()[-1].text


async def test_non_owner_cannot_reset_password(h):
    await h.db.set_setting("web_password_hash", "scrypt$aa$bb")
    await h.send(ADMIN2, "/settings")
    assert SettingsCb(action="reset").pack() not in markup_data(h.last_to(ADMIN2).reply_markup)
    await h.press(ADMIN2, SettingsCb(action="reset_ok"))
    assert await h.db.get_setting("web_password_hash") is not None


def url_buttons(markup) -> list[str]:
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


async def test_settings_with_env_password(h):
    """SPEC_V21 §3: WEB_PASSWORD from .env is shown as such and cannot be reset from the bot."""
    h.config.web_password = "env-secret-1"
    await h.db.set_setting("web_password_hash", "scrypt$aa$bb")  # a panel password is overridden
    await h.send(OWNER, "/settings")
    message = h.last_to(OWNER)
    assert "🔒 Пароль задан в .env" in message.text and "не установлен" not in message.text
    assert SettingsCb(action="reset").pack() not in markup_data(message.reply_markup)
    for action in ("reset", "reset_ok"):
        await h.press(OWNER, SettingsCb(action=action))
        answer = h.callback_answers()[-1]
        assert answer.show_alert and "Пароль задан в .env (WEB_PASSWORD)" in answer.text
    assert await h.db.get_setting("web_password_hash") == "scrypt$aa$bb"
    assert not any("сброшен" in edit.text for edit in h.edits())


async def test_settings_without_password_mentions_env(h):
    await h.send(OWNER, "/settings")
    text = h.last_to(OWNER).text
    assert "не установлен" in text and "WEB_PASSWORD" in text


async def test_settings_panel_address_rules(h):
    """The panel URL is a button only for https://; otherwise it is shown as <code> (SPEC_V21 §3)."""
    h.config.web_prefix = "/posts"
    await h.send(OWNER, "/settings")
    message = h.last_to(OWNER)
    assert "<code>http://127.0.0.1:8080/posts/</code>" in message.text
    assert url_buttons(message.reply_markup) == []

    h.config.web_public_url = "http://10.0.0.5/posts/"  # a LAN address: Telegram may reject the button
    await h.send(OWNER, "/settings")
    message = h.last_to(OWNER)
    assert "<code>http://10.0.0.5/posts/</code>" in message.text
    assert url_buttons(message.reply_markup) == []

    h.config.web_public_url = "https://panel.example.com/posts/"
    await h.send(OWNER, "/settings")
    message = h.last_to(OWNER)
    assert '<a href="https://panel.example.com/posts/">https://panel.example.com/posts/</a>' in message.text
    assert url_buttons(message.reply_markup) == ["https://panel.example.com/posts/"]


async def test_stats_link_rules_for_http_public_url(h):
    h.config.web_public_url = "http://10.0.0.5/posts/"
    await h.press(OWNER, StatsCb(action="ch", chat_id=CH, days=30))
    stats = h.edits()[-1]
    assert url_buttons(stats.reply_markup) == []
    assert "<code>http://10.0.0.5/posts/</code>" in stats.text


async def test_help_and_start_show_the_panel_address(h):
    await h.send(OWNER, "/help")
    assert "🌐 <b>Веб-панель</b>: <code>http://127.0.0.1:8080/</code>" in h.last_to(OWNER).text
    await h.send(OWNER, "/start")
    assert any("🌐 Веб-панель: <code>http://127.0.0.1:8080/</code>" in t for t in h.session.texts_to(OWNER))
    h.config.web_public_url = "https://panel.example.com/"
    await h.send(OWNER, "/help")
    assert '<a href="https://panel.example.com/">https://panel.example.com/</a>' in h.last_to(OWNER).text


async def test_addadmin_and_deladmin(h):
    await h.send(OWNER, "/addadmin 333")
    assert await h.db.is_admin(333)
    assert "добавлен" in h.last_to(OWNER).text
    assert h.session.texts_to(333)  # the new admin is notified
    await h.send(OWNER, "/addadmin abc")
    assert "Использование" in h.last_to(OWNER).text
    await h.send(OWNER, "/deladmin 333")
    assert not await h.db.is_admin(333)
    await h.send(OWNER, f"/deladmin {OWNER}")
    assert "Владельца бота удалить нельзя" in h.last_to(OWNER).text
    await h.send(ADMIN2, "/addadmin 444")
    assert not await h.db.is_admin(444)
    assert "Только владелец" in h.last_to(ADMIN2).text


# ------------------------------------------------------------------ Telegram edit errors

async def test_message_not_modified_is_ignored(h):
    h.session.fail_on[EditMessageText] = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: message is not modified")
    await h.press(OWNER, SettingsCb(action="show"))
    assert h.session.of(SendMessage) == []


async def test_failed_edit_falls_back_to_new_message(h):
    h.session.fail_on[EditMessageText] = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: message to edit not found")
    await h.press(OWNER, SettingsCb(action="show"))
    assert "Настройки" in h.last_to(OWNER).text


# ------------------------------------------------------------------ V2: media icons, per-channel times

async def test_posts_list_and_card_show_media_and_channel_times(h):
    import time as _time

    from app.services import plan as plan_service

    video = await make_post(h, "Ролик дня", media=[{"type": "video", "file_id": "v"}])
    album = await make_post(h, "", media=[{"type": "photo", "file_id": "a"}, {"type": "photo", "file_id": "b"}])
    await h.press(OWNER, PostListCb(tab="d", page=0))
    labels = [b.text for row in h.edits()[-1].reply_markup.inline_keyboard for b in row]
    assert any("🎬 Ролик дня" in label for label in labels)
    assert any("🗂 Альбом: 2 фото" in label for label in labels)

    await h.db.set_channel_slots(CH, plan_service.parse_slots("ежедневно 09:00"))
    await plan_service.queue_post(h.db, video, [CH], h.config.tz)
    await h.press(OWNER, PostCb(action="open", post_id=video, tab="s"))
    card = h.edits()[-1].text
    assert "🎬 Видео" in card and "(по расписанию)" in card
    ts = (await h.db.get_target(video, CH))["scheduled_at"]
    assert plan_service.slot_label(ts, h.config.tz) in card
    assert ts > _time.time() and album


# ------------------------------------------------------------------ finding #10: double taps on a card

async def test_second_publish_tap_keeps_the_report(h):
    post_id = await make_post(h, "Срочно", status=POST_SCHEDULED, scheduled_at=int(time.time()) + 3600)
    await h.press(OWNER, PostCb(action="now", post_id=post_id, tab="s"))
    await h.press(OWNER, PostCb(action="pub_ok", post_id=post_id, tab="s"))
    progress, report = h.edits()[-2], h.edits()[-1]
    assert progress.text.startswith("⏳ Публикую") and progress.reply_markup is None  # no second tap possible
    assert report.text.startswith("✅ Опубликовано: 1 из 1")
    edits = len(h.edits())
    await h.press(OWNER, PostCb(action="pub_ok", post_id=post_id, tab="s"))
    assert len(h.edits()) == edits  # the report stays on screen
    assert "уже опубликован" in h.callback_answers()[-1].text
    assert len(h.session.sent_to(CH)) == 1


async def test_second_delete_tap_keeps_the_report(h):
    post_id = await make_sent_post(h)
    await h.press(OWNER, PostCb(action="delch_ok", post_id=post_id, tab="p"))
    assert h.edits()[-1].text.startswith("✅ Удалено: 1 из 1")
    edits = len(h.edits())
    await h.press(OWNER, PostCb(action="delch_ok", post_id=post_id, tab="p"))
    assert len(h.edits()) == edits
    assert "уже удалён" in h.callback_answers()[-1].text
    assert len(h.session.of(DeleteMessage)) == 1


async def test_published_record_of_a_queued_post_cannot_be_deleted(h):
    post_id = await make_post(h, "Частично", status=POST_SCHEDULED, scheduled_at=int(time.time()) + 3600)
    await h.db.update_target(post_id, CH, status="sent", message_id=9)
    await h.press(OWNER, PostCb(action="delete_ok", post_id=post_id, tab="s"))
    assert await h.db.get_post(post_id) is not None
    await h.press(OWNER, PostCb(action="open", post_id=post_id, tab="s"))
    data = markup_data(h.edits()[-1].reply_markup)
    assert PostCb(action="delch", post_id=post_id, tab="s").pack() in data
    assert PostCb(action="edit", post_id=post_id, tab="s").pack() in data


# ------------------------------------------------------------------ finding #12: stale «❌ Отменить»

@pytest.mark.parametrize("status, expected", [("sending", "публикуется"), ("sent", "уже опубликован")])
async def test_unschedule_of_a_post_that_moved_on_does_not_claim_success(h, status, expected):
    post_id = await make_post(h, "Отложенный", status=POST_SCHEDULED, scheduled_at=int(time.time()) + 3600)
    await h.db.update_post(post_id, status=status)
    await h.press(OWNER, PostCb(action="unschedule", post_id=post_id, tab="s"))
    answer = h.callback_answers()[-1]
    assert answer.show_alert and expected in answer.text and "отменена" not in answer.text
    assert (await h.db.get_post(post_id))["status"] == status


async def test_unschedule_after_the_scheduler_published(h, config):
    from app.services.scheduler import Scheduler

    post_id = await make_post(h, "Отложенный", status=POST_SCHEDULED, scheduled_at=int(time.time()) + 3600)
    await h.db.update_post(post_id, scheduled_at=int(time.time()) - 5)
    await h.db.adopt_legacy_schedules()
    await Scheduler(h.bot, h.db, config).run_due_posts()
    assert (await h.db.get_post(post_id))["status"] == POST_SENT
    await h.press(OWNER, PostCb(action="unschedule", post_id=post_id, tab="s"))
    assert "уже опубликован" in h.callback_answers()[-1].text
    assert (await h.db.get_post(post_id))["status"] == POST_SENT


async def test_unschedule_of_a_queued_post(h):
    from app.services import plan as plan_service

    post_id = await make_post(h, "В очереди")
    await h.db.set_channel_slots(CH, plan_service.parse_slots("ежедневно 09:00"))
    await plan_service.queue_post(h.db, post_id, [CH], h.config.tz)
    await h.press(OWNER, PostCb(action="unschedule", post_id=post_id, tab="s"))
    assert h.callback_answers()[-1].text == "Публикация отменена — пост теперь черновик"
    post = await h.db.get_post(post_id)
    assert post["status"] == POST_DRAFT and post["targets"][0]["scheduled_at"] is None


async def test_stats_overview_with_many_channels_is_paged(h):
    from app.bot.callbacks import StatsPageCb

    for i in range(110):
        await h.db.upsert_channel(-1006000000000 - i, f"Канал {i:03d}", None, status=CH_ACTIVE)
    await h.send(OWNER, BTN_STATS)  # ≤ 100 buttons: checked by the Harness
    first = h.last_to(OWNER)
    assert StatsPageCb(page=1).pack() in markup_data(first.reply_markup)
    await h.press(OWNER, StatsPageCb(page=2))
    assert len([d for d in markup_data(h.edits()[-1].reply_markup) if d.startswith("st:ch:")]) == 31


# ------------------------------------------------------------------ review2 #3: a claim lands meanwhile

def claim_before(h: Harness, method: str) -> None:
    """The scheduler claims every post right before `db.<method>` runs (between the handler's check
    and its write)."""
    original = getattr(h.db, method)

    async def claimed_first(post_id, *args, **kwargs):
        await h.db.execute("UPDATE posts SET status = 'sending' WHERE id = ?", (post_id,))
        return await original(post_id, *args, **kwargs)

    setattr(h.db, method, claimed_first)


async def test_record_delete_claimed_meanwhile_is_not_reported_as_done(h):
    """Regression (review2 #3, bot side): «🗑 Да, удалить» of a scheduled post the scheduler claims
    at that moment — the record stays (it manages the message being sent) and the bot says so."""
    post_id = await make_post(h, "Отложенный", status=POST_SCHEDULED, scheduled_at=int(time.time()) + 3600)
    claim_before(h, "delete_post_record")
    await h.press(OWNER, PostCb(action="delete_ok", post_id=post_id, tab="s"))
    assert (await h.db.get_post(post_id))["status"] == "sending"
    assert "публикуется" in h.callback_answers()[-1].text
    assert all("Пост удалён" not in (a.text or "") for a in h.callback_answers())


async def test_deleted_post_still_shown_somewhere_offers_delete_from_channels(h):
    """A 'deleted' post with a channel still showing it (left by an older version) offers «Удалить
    из каналов», never «Удалить запись» (which would orphan the message)."""
    post_id = await make_sent_post(h)
    await h.db.update_post(post_id, status=POST_DELETED)
    await h.press(OWNER, PostCb(action="open", post_id=post_id, tab="p"))
    data = markup_data(h.edits()[-1].reply_markup)
    assert PostCb(action="delch", post_id=post_id, tab="p").pack() in data
    assert PostCb(action="delete", post_id=post_id, tab="p").pack() not in data
    await h.press(OWNER, PostCb(action="delete_ok", post_id=post_id, tab="p"))  # an old button
    assert await h.db.get_post(post_id) is not None
    assert "нельзя удалить" in h.callback_answers()[-1].text
    await h.press(OWNER, PostCb(action="delch_ok", post_id=post_id, tab="p"))
    assert h.edits()[-1].text.startswith("✅ Удалено: 1 из 1")
    assert (await h.db.get_post(post_id))["status"] == POST_DELETED

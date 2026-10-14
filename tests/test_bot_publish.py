"""Publish modes of the composer (now / queue / at a time) and the composer review findings
#7, #8/#20, #9, #11/#22, #13, #14."""
from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageMedia, EditMessageText, SendMessage

from app.bot.callbacks import ComposeCb, ComposeChannelCb, ComposeOptionCb, PostCb
from app.bot.handlers.compose import LOST_CHANGES_TEXT, update_draft
from app.bot.keyboards import BTN_NEW, BTN_POSTS
from app.bot.states import ComposeStates
from app.db import CH_LEFT, POST_DRAFT, POST_SCHEDULED, POST_SENDING, POST_SENT
from app.services import plan as plan_service
from app.services.posting import publish_post
from tests.mocked_session import Harness, photo_sizes

OWNER = 111
CH_A = -1001000000001
CH_B = -1001000000002
EVERY_HOUR = "ежедневно " + ", ".join(f"{h:02d}:00" for h in range(24))


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    harness.albums.delay = 0.05
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    yield harness
    await harness.close()


@pytest.fixture
async def one_channel(h):
    await h.add_channel(CH_A, "Канал А", "kanal_a")


@pytest.fixture
async def two_channels(h):
    await h.add_channel(CH_A, "Канал А", "kanal_a")
    await h.add_channel(CH_B, "Канал Б", "kanal_b")


async def latest_post(h: Harness) -> dict:
    row = await h.db.fetchone("SELECT id FROM posts ORDER BY id DESC LIMIT 1")
    return await h.db.get_post(row["id"])


async def new_draft(h: Harness, text: str = "**Привет**") -> dict:
    await h.send(OWNER, "/new")
    await h.send(OWNER, text)
    return await latest_post(h)


def panel(h: Harness) -> SendMessage:
    panels = [r for r in h.session.sent_to(OWNER) if isinstance(r, SendMessage) and "Длина:" in r.text]
    assert panels, "no control panel was sent"
    return panels[-1]


def button_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def channel_edits(h: Harness, method=EditMessageText) -> list:
    return [e for e in h.session.of(method) if e.chat_id in (CH_A, CH_B)]


async def publish(h: Harness, text: str = "Старый текст") -> dict:
    post = await new_draft(h, text)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    return await h.db.get_post(post["id"])


async def schedule_in_an_hour(h: Harness, post: dict) -> None:
    await h.press(OWNER, ComposeCb(action="at_1h", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["status"] == POST_SCHEDULED


# ------------------------------------------------------------------ queue by schedule

async def test_queue_publish_flow_gives_each_channel_its_slot(h, two_channels, config):
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    await h.db.set_channel_slots(CH_B, plan_service.parse_slots("ежедневно 03:30, 15:30"))
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="ch_all", post_id=post["id"]))
    expected = await plan_service.preview_queue(h.db, [CH_A, CH_B], config.tz, post_id=post["id"])

    await h.press(OWNER, ComposeCb(action="publish", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="queue", post_id=post["id"]))
    screen = h.edits()[-1]
    for cid in (CH_A, CH_B):
        assert plan_service.slot_label(expected[cid], config.tz) in screen.text
    assert ComposeCb(action="queue_ok", post_id=post["id"]).pack() in button_data(screen.reply_markup)

    await h.press(OWNER, ComposeCb(action="queue_ok", post_id=post["id"]))
    saved = await h.db.get_post(post["id"])
    assert saved["status"] == POST_SCHEDULED
    times = {t["channel_id"]: (t["scheduled_at"], t["queued"]) for t in saved["targets"]}
    assert times == {CH_A: (expected[CH_A], True), CH_B: (expected[CH_B], True)}
    assert saved["scheduled_at"] == min(expected.values())
    report = h.last_to(OWNER).text
    assert report.startswith(f"📅 Пост #{post['id']} поставлен в очередь")
    assert "Канал А" in report and "Канал Б" in report
    assert (await h.fsm(OWNER))[0] is None

    # the next post queued to A takes A's next free slot
    second = await new_draft(h, "Второй")
    await h.press(OWNER, ComposeChannelCb(post_id=second["id"], channel_id=CH_A))
    await h.press(OWNER, ComposeCb(action="queue_ok", post_id=second["id"]))
    second_ts = (await h.db.get_target(second["id"], CH_A))["scheduled_at"]
    assert second_ts > expected[CH_A]


async def test_queue_drops_channels_without_schedule(h, two_channels):
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="ch_all", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="queue", post_id=post["id"]))
    screen = h.edits()[-1].text
    assert "⚠️ Нет расписания (1): «Канал Б»" in screen and "В очередь (1 из 2)" in str(h.edits()[-1].reply_markup)
    await h.press(OWNER, ComposeCb(action="queue_ok", post_id=post["id"]))
    saved = await h.db.get_post(post["id"])
    assert [t["channel_id"] for t in saved["targets"]] == [CH_A] and saved["targets"][0]["queued"]
    assert "Сняты с поста" in h.last_to(OWNER).text


LONG_TITLE = "Новости и аналитика рынка недвижимости {:03d}"


async def many_channels(h: Harness, count: int, *, start: int = 0, slots: str | None = None) -> list[int]:
    ids = []
    for i in range(start, start + count):
        cid = -1002000000000 - i
        await h.add_channel(cid, LONG_TITLE.format(i), f"news_{i:03d}")
        if slots:
            await h.db.set_channel_slots(cid, plan_service.parse_slots(slots))
        ids.append(cid)
    return ids


async def test_long_channel_lists_fit_one_message(h):
    """Finding #17: with many channels the publish screens stay below 4096 characters (the Harness
    fails on a longer text or a keyboard over 100 buttons) — «📅 В очередь» no longer repeats the
    schedule hint on every line."""
    no_schedule = await many_channels(h, 40)
    scheduled = await many_channels(h, 70, start=40, slots=EVERY_HOUR)
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="channels", post_id=post["id"]))
    picker = h.edits()[-1]
    assert ComposeCb(action="chpg1", post_id=post["id"]).pack() in button_data(picker.reply_markup)
    await h.press(OWNER, ComposeCb(action="ch_all", post_id=post["id"]))
    assert len((await h.db.get_post(post["id"]))["targets"]) == 110
    await h.press(OWNER, ComposeCb(action="chpg2", post_id=post["id"]))
    assert "страница 3 из 3" in h.edits()[-1].text

    await h.press(OWNER, ComposeCb(action="publish", post_id=post["id"]))
    assert "…и ещё" in h.edits()[-1].text
    await h.press(OWNER, ComposeCb(action="now", post_id=post["id"]))
    assert "Опубликовать в 110 каналов?" in h.edits()[-1].text and "…и ещё" in h.edits()[-1].text
    await h.press(OWNER, ComposeCb(action="queue", post_id=post["id"]))
    screen = h.edits()[-1].text
    assert "⚠️ Нет расписания (40)" in screen and screen.count("Задайте его") == 1
    assert "В очередь (70 из 110)" in str(h.edits()[-1].reply_markup)

    await h.press(OWNER, ComposeCb(action="queue_ok", post_id=post["id"]))
    report = h.last_to(OWNER).text
    assert report.startswith(f"📅 Пост #{post['id']} поставлен в очередь")
    assert "Сняты с поста (нет времени по расписанию, 40)" in report and "…и ещё" in report
    saved = await h.db.get_post(post["id"])
    assert sorted(t["channel_id"] for t in saved["targets"]) == sorted(scheduled)
    assert no_schedule

    # the composer of the queued post lists its 70 timed channels within the limit too
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    assert "…и ещё" in panel(h).text


async def test_queue_is_disabled_without_any_schedule(h, one_channel):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="queue", post_id=post["id"]))
    data = button_data(h.edits()[-1].reply_markup)
    assert ComposeCb(action="queue_ok", post_id=post["id"]).pack() not in data
    await h.press(OWNER, ComposeCb(action="queue_none", post_id=post["id"]))
    assert "нет расписания" in h.callback_answers()[-1].text
    await h.press(OWNER, ComposeCb(action="queue_ok", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["status"] == POST_DRAFT


async def test_publish_now_takes_a_queued_post_out_of_the_plan(h, one_channel):
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="queue_ok", post_id=post["id"]))
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    assert "(по расписанию)" in panel(h).text
    await h.press(OWNER, ComposeCb(action="now", post_id=post["id"]))
    assert "он будет опубликован прямо сейчас" in h.edits()[-1].text
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    target = (await h.db.get_post(post["id"]))["targets"][0]
    assert target["status"] == "sent" and target["scheduled_at"] is None and not target["queued"]


# ------------------------------------------------------------------ finding #7: lost edit sessions

async def test_stale_save_after_section_switch_is_refused(h, one_channel):
    post = await publish(h)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "Новый текст")
    await h.send(OWNER, BTN_POSTS)
    assert any(f"Несохранённые изменения поста #{post['id']} отменены" in t for t in h.session.texts_to(OWNER))

    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    answer = h.callback_answers()[-1]
    assert answer.show_alert and answer.text.startswith(LOST_CHANGES_TEXT[:40])
    assert channel_edits(h) == []
    assert "текущий опубликованный вариант" in panel(h).text
    assert (await h.db.get_post(post["id"]))["text_md"] == "Старый текст"
    await h.press(OWNER, ComposeCb(action="save", post_id=post["id"]))
    assert "Изменений нет" in h.callback_answers()[-1].text


async def test_stale_save_after_restart_is_refused(h, one_channel):
    from aiogram.fsm.storage.base import StorageKey

    post = await publish(h)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "Новый текст")
    key = StorageKey(bot_id=h.bot.id, chat_id=OWNER, user_id=OWNER)
    await h.dp.storage.set_state(key, None)
    await h.dp.storage.set_data(key, {})  # MemoryStorage after a restart
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    assert h.callback_answers()[-1].show_alert
    assert channel_edits(h) == []
    assert not [r for r in h.session.sent_to(OWNER) if "Обновлено" in (getattr(r, "text", "") or "")]


async def test_save_without_changes_retries_a_failed_channel(h, one_channel):
    post = await publish(h)
    await h.db.update_target(post["id"], CH_A, error="Сетевая ошибка")
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    assert len(channel_edits(h)) == 1
    assert h.last_to(OWNER).text.startswith("✅ Обновлено: 1 из 1")


# ------------------------------------------------------------------ findings #8 / #20

async def test_draft_composer_stops_when_the_post_was_published_meanwhile(h, one_channel, config):
    post = await new_draft(h, "Оригинал")
    await schedule_in_an_hour(h, post)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    assert not (await h.fsm(OWNER))[1]["edit"]
    # the scheduler (or the web panel) publishes the post while the composer is open
    await publish_post(h.bot, h.db, config, post["id"])
    h.session.clear()

    await h.send(OWNER, "Исправленный текст")
    saved = await h.db.get_post(post["id"])
    assert saved["status"] == POST_SENT and saved["text_md"] == "Оригинал"
    assert channel_edits(h) == []
    assert "уже опубликован" in h.last_to(OWNER).text
    assert (await h.fsm(OWNER))[0] is None


async def test_photo_into_a_post_published_meanwhile_is_refused(h, one_channel, config):
    post = await new_draft(h, "Оригинал")
    await publish_post(h.bot, h.db, config, post["id"])
    await h.send(OWNER, None, photo=photo_sizes("pic"))
    saved = await h.db.get_post(post["id"])
    assert saved["media"] == [] and saved["published_kind"] == "text"
    assert "уже опубликован" in h.last_to(OWNER).text
    # and the post is still editable as a text post
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "Правка")
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["text_md"] == "Правка"


async def test_buttons_and_options_into_a_post_published_meanwhile_are_refused(h, one_channel, config):
    post = await new_draft(h, "Оригинал")
    await h.press(OWNER, ComposeCb(action="buttons", post_id=post["id"]))
    await publish_post(h.bot, h.db, config, post["id"])
    await h.send(OWNER, "Купить - https://shop.com")
    assert (await h.db.get_post(post["id"]))["buttons"] == []
    assert "уже опубликован" in h.last_to(OWNER).text
    assert (await h.fsm(OWNER))[0] is None


async def test_album_into_a_post_published_meanwhile_is_refused(h, one_channel, config):
    post = await new_draft(h, "Оригинал")
    await publish_post(h.bot, h.db, config, post["id"])
    for fid in ("a", "b"):
        await h.send(OWNER, None, photo=photo_sizes(fid), media_group_id="late")
    await h.settle()
    assert (await h.db.get_post(post["id"]))["media"] == []
    assert "уже опубликован" in h.last_to(OWNER).text


async def test_post_being_sent_is_not_written(h, one_channel):
    post = await new_draft(h, "Оригинал")
    await h.db.update_post(post["id"], status=POST_SENDING)
    await h.send(OWNER, "Новый")
    assert (await h.db.get_post(post["id"]))["text_md"] == "Оригинал"
    assert "публикуется" in h.last_to(OWNER).text


async def test_update_draft_is_conditional(h, one_channel):
    post_id = await h.db.create_post(text_md="x", status=POST_DRAFT)
    await h.db.set_post_targets(post_id, [CH_A])
    assert await update_draft(h.db, post_id, text_md="y", media=[{"type": "photo", "file_id": "f"}])
    saved = await h.db.get_post(post_id)
    assert saved["text_md"] == "y" and saved["photo_file_id"] == "f"
    for status in (POST_SENDING, POST_SENT, "partial", "deleted"):
        await h.db.update_post(post_id, status=status)
        assert not await update_draft(h.db, post_id, text_md="z")
    # V2: a scheduled post already sent to one of its channels is no longer a draft
    await h.db.update_post(post_id, status=POST_SCHEDULED)
    await h.db.update_target(post_id, CH_A, status="sent", message_id=5)
    assert not await update_draft(h.db, post_id, text_md="z")
    assert (await h.db.get_post(post_id))["text_md"] == "y"


async def test_option_toggle_on_a_stale_draft_panel(h, one_channel, config):
    post = await new_draft(h)
    await publish_post(h.bot, h.db, config, post["id"])
    await h.press(OWNER, ComposeOptionCb(post_id=post["id"], option="silent"))
    assert "нельзя изменить" in h.callback_answers()[-1].text
    assert not (await h.db.get_post(post["id"]))["silent"]


# ------------------------------------------------------------------ finding #9

async def test_failed_save_keeps_db_equal_to_channels_and_can_be_retried(h, one_channel):
    post = await publish(h)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "Новый текст")
    h.session.fail_on[EditMessageText] = lambda m: (
        TelegramBadRequest(method=m, message="Bad Request: message can't be edited") if m.chat_id == CH_A else None)
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    saved = await h.db.get_post(post["id"])
    assert saved["text_md"] == "Старый текст" and saved["text_html"] == "Старый текст"
    state, data = await h.fsm(OWNER)
    assert state == ComposeStates.menu.state and data["overrides"]["text_md"] == "Новый текст"
    assert "Изменения не применены" in h.edits()[-1].text and "❌ Обновлено: 0 из 1" in h.edits()[-1].text

    del h.session.fail_on[EditMessageText]
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["text_md"] == "Новый текст"
    assert h.last_to(OWNER).text.startswith("✅ Обновлено: 1 из 1")


async def test_failed_media_save_is_retried_with_the_media(h, one_channel):
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo_sizes("old_pic"), caption="Подпись")
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, None, photo=photo_sizes("new_pic"))
    h.session.fail_on[EditMessageMedia] = lambda m: TelegramNetworkError(method=m, message="timeout")
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["media"][0]["file_id"] == "old_pic"

    del h.session.fail_on[EditMessageMedia]
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    (edit,) = channel_edits(h, EditMessageMedia)
    assert edit.media.media == "new_pic"
    assert (await h.db.get_post(post["id"]))["media"][0]["file_id"] == "new_pic"


# ------------------------------------------------------------------ findings #11 / #22

@pytest.mark.parametrize("how", ["removed", "left"])
async def test_unavailable_target_can_be_unticked(h, two_channels, how):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="ch_all", post_id=post["id"]))
    if how == "removed":
        await h.db.delete_channel(CH_B)
    else:
        await h.db.set_channel_status(CH_B, CH_LEFT)
    await h.press(OWNER, ComposeCb(action="channels", post_id=post["id"]))
    picker = h.edits()[-1]
    stale = ComposeChannelCb(post_id=post["id"], channel_id=CH_B).pack()
    assert stale in button_data(picker.reply_markup) and "недоступен" in str(picker.reply_markup)
    assert ComposeCb(action="ch_none", post_id=post["id"]).pack() in button_data(picker.reply_markup)
    await h.press(OWNER, stale)
    assert [t["channel_id"] for t in (await h.db.get_post(post["id"]))["targets"]] == [CH_A]
    # it cannot be ticked again
    await h.press(OWNER, stale)
    assert "больше не подключён" in h.callback_answers()[-1].text


# ------------------------------------------------------------------ finding #13

async def test_cancel_and_new_post_do_not_call_a_scheduled_post_a_draft(h, one_channel):
    post = await new_draft(h)
    await schedule_in_an_hour(h, post)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "/cancel")
    text = h.last_to(OWNER).text
    assert "остаётся запланированным" in text and "Запланированные" in text and "Черновик" not in text

    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, BTN_NEW)
    text = h.last_to(OWNER).text
    assert "остаётся запланированным" in text and "Черновик" not in text
    assert (await h.db.get_post(post["id"]))["status"] == POST_SCHEDULED


# ------------------------------------------------------------------ finding #14

async def test_close_does_not_promise_publication_of_a_broken_scheduled_post(h, one_channel):
    post = await new_draft(h)
    await schedule_in_an_hour(h, post)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, "x" * 5000)
    await h.press(OWNER, ComposeCb(action="close", post_id=post["id"]))
    text = h.edits()[-1].text
    assert "не опубликуется" in text and "слишком длинный" in text and "будет опубликован" not in text


async def test_unticking_every_channel_of_a_scheduled_post_makes_it_a_draft(h, one_channel):
    post = await new_draft(h)
    await schedule_in_an_hour(h, post)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.press(OWNER, ComposeChannelCb(post_id=post["id"], channel_id=CH_A))
    assert (await h.db.get_post(post["id"]))["status"] == POST_DRAFT
    await h.press(OWNER, ComposeCb(action="close", post_id=post["id"]))
    assert "Черновик" in h.edits()[-1].text and "будет опубликован" not in h.edits()[-1].text


async def test_close_of_a_good_scheduled_post_promises_publication(h, one_channel):
    post = await new_draft(h)
    await schedule_in_an_hour(h, post)
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="close", post_id=post["id"]))
    assert "будет опубликован" in h.edits()[-1].text


# ------------------------------------------------------------------ finding #13: one tap — one publish

def timeout_in(chat_id: int):
    """SendMessage to `chat_id` times out (the message may have been delivered anyway)."""
    def fail(method):
        if method.chat_id == chat_id:
            return TelegramNetworkError(method=method, message="Request timeout error")
        return None
    return fail


async def test_second_confirm_tap_after_a_failure_does_not_resend(h, one_channel):
    """A second tap on «✅ Да, опубликовать» waits until the first publish ends; when that one failed
    (a timeout — maybe delivered after all) it must not send again without a new confirmation."""
    post = await new_draft(h)
    h.session.fail_on[SendMessage] = timeout_in(CH_A)
    await h.press(OWNER, ComposeCb(action="now", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    assert len(h.session.sent_to(CH_A)) == 1
    assert (await h.db.get_post(post["id"]))["status"] != POST_SENT

    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))  # the queued second tap
    answer = h.callback_answers()[-1]
    assert answer.show_alert and "уже нажата" in answer.text
    assert len(h.session.sent_to(CH_A)) == 1
    assert (await h.fsm(OWNER))[0] is None  # no composer session re-opened by the old button

    # a deliberate retry goes through a new confirmation and works
    del h.session.fail_on[SendMessage]
    await h.press(OWNER, ComposeCb(action="now", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    assert len(h.session.sent_to(CH_A)) == 2
    assert (await h.db.get_post(post["id"]))["status"] == POST_SENT


async def test_second_pub_ok_tap_on_a_card_does_not_resend_failed_channels(h, two_channels):
    post = await new_draft(h)
    await h.press(OWNER, ComposeCb(action="ch_all", post_id=post["id"]))
    await schedule_in_an_hour(h, post)
    h.session.fail_on[SendMessage] = timeout_in(CH_B)
    await h.press(OWNER, PostCb(action="now", post_id=post["id"], tab="s"))
    await h.press(OWNER, PostCb(action="pub_ok", post_id=post["id"], tab="s"))
    assert len(h.session.sent_to(CH_A)) == 1 and len(h.session.sent_to(CH_B)) == 1
    report = h.edits()[-1]
    assert "Канал Б" in report.text

    await h.press(OWNER, PostCb(action="pub_ok", post_id=post["id"], tab="s"))  # the queued second tap
    assert "уже нажата" in h.callback_answers()[-1].text
    assert len(h.session.sent_to(CH_B)) == 1
    assert h.edits()[-1] is report  # the first report stays on screen

    # «🔁 Повторить для неудачных» asks again, then publishes
    del h.session.fail_on[SendMessage]
    await h.press(OWNER, PostCb(action="retry", post_id=post["id"], tab="s"))
    await h.press(OWNER, PostCb(action="pub_ok", post_id=post["id"], tab="s"))
    assert len(h.session.sent_to(CH_B)) == 2 and len(h.session.sent_to(CH_A)) == 1


# ------------------------------------------------------------------ finding #15

async def test_delete_from_channels_says_the_waiting_channels_are_cancelled(h, two_channels, config):
    """A post out in А and still queued to Б: «🗑 Удалить из каналов» also cancels Б — the confirmation,
    the report and the card say so."""
    for cid in (CH_A, CH_B):
        await h.db.set_channel_slots(cid, plan_service.parse_slots(EVERY_HOUR))
    post_id = await h.db.create_post(text_md="Пост", text_html="Пост", status=POST_DRAFT)
    await h.db.set_post_targets(post_id, [CH_A, CH_B])
    await plan_service.queue_post(h.db, post_id, [CH_A, CH_B], config.tz)
    await publish_post(h.bot, h.db, config, post_id, channel_ids=[CH_A])
    assert (await h.db.get_post(post_id))["status"] == POST_SCHEDULED

    await h.press(OWNER, PostCb(action="delch", post_id=post_id, tab="s"))
    confirm = h.edits()[-1].text
    assert "из 1 канала" in confirm
    assert "Публикация в «Канал Б» по расписанию тоже будет отменена" in confirm
    await h.press(OWNER, PostCb(action="delch_ok", post_id=post_id, tab="s"))
    report = h.edits()[-1].text
    assert report.startswith("✅ Удалено: 1 из 1") and "⏰ Публикация в «Канал Б» отменена." in report
    await h.press(OWNER, PostCb(action="open", post_id=post_id, tab="s"))
    assert "• Канал Б — отменён" in h.edits()[-1].text

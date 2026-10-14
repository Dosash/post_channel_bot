"""Tests for app.services.scheduler (time parsing, V2 per-target publishing loop)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import CH_ACTIVE, now
from app.services import posting
from app.services import scheduler as scheduler_module
from app.services.posting import PostError
from app.services.scheduler import HELP_ERROR, PAST_ERROR, Scheduler, format_dt, parse_when
from tests.fakes import bad_request

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 14, 12, 0, 20, tzinfo=TZ)
CH = -1001000000001


@pytest.fixture
async def db(db):
    await db.migrate()
    return db


def at(month, day, hour, minute, second=0, year=2026) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ)


# ------------------------------------------------------------------ parse_when

@pytest.mark.parametrize("text, expected", [
    ("18:30", at(10, 14, 18, 30)),
    ("  18:30 ", at(10, 14, 18, 30)),
    ("9:05", at(10, 15, 9, 5)),            # already passed today → tomorrow
    ("12:00", at(10, 15, 12, 0)),          # the current minute has started → tomorrow
    ("в 18:30", at(10, 14, 18, 30)),
    ("25.12 10:00", at(12, 25, 10, 0)),
    ("25/12 10:00", at(12, 25, 10, 0)),
    ("10:00 25.12", at(12, 25, 10, 0)),
    ("25.12.2026 10:00", at(12, 25, 10, 0)),
    ("01.02.2027 07:45", at(2, 1, 7, 45, year=2027)),
    ("25.12.26 10:00", at(12, 25, 10, 0)),
    ("01.01 00:00", at(1, 1, 0, 0, year=2027)),  # a date long gone this year → next year
    ("2026-12-25 10:00", at(12, 25, 10, 0)),
    ("2026-12-25T10:00", at(12, 25, 10, 0)),
    ("2026-12-25T10:00:30", at(12, 25, 10, 0)),
    ("+30m", at(10, 14, 12, 30, 20)),
    ("+2h", at(10, 14, 14, 0, 20)),
    ("+1d", at(10, 15, 12, 0, 20)),
    ("+1h30m", at(10, 14, 13, 30, 20)),
    ("+1ч30м", at(10, 14, 13, 30, 20)),
    ("+ 45 мин", at(10, 14, 12, 45, 20)),
    ("+90", at(10, 14, 13, 30, 20)),
    ("через 2 часа", at(10, 14, 14, 0, 20)),
    ("через 15 минут", at(10, 14, 12, 15, 20)),
    ("Через час", at(10, 14, 13, 0, 20)),
    ("через полчаса", at(10, 14, 12, 30, 20)),
    ("через 1 час 30 минут", at(10, 14, 13, 30, 20)),
    ("через 1 час и 5 минут", at(10, 14, 13, 5, 20)),
    ("через 2 дня", at(10, 16, 12, 0, 20)),
    ("через сутки", at(10, 15, 12, 0, 20)),
    ("завтра 10:00", at(10, 15, 10, 0)),
    ("Завтра в 9:30", at(10, 15, 9, 30)),
    ("завтра 10.00", at(10, 15, 10, 0)),
    ("послезавтра 23:59", at(10, 16, 23, 59)),
    ("сегодня 20:00", at(10, 14, 20, 0)),
])
def test_parse_when(text, expected):
    result = parse_when(text, TZ, NOW)
    assert result == expected
    assert result.tzinfo is not None and result.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize("text", ["", "   ", "abc", "завтра", "через", "через 5 лет", "+0m", "+", "18.30",
                                  "10:00 завтра утром", "25.12", "1 2 3"])
def test_parse_when_unparsable(text):
    with pytest.raises(ValueError) as err:
        parse_when(text, TZ, NOW)
    assert str(err.value) == HELP_ERROR


@pytest.mark.parametrize("text", ["13.10 10:00", "сегодня 10:00", "2025-01-01 10:00", "14.10.2026 12:00"])
def test_parse_when_past(text):
    with pytest.raises(ValueError) as err:
        parse_when(text, TZ, NOW)
    assert str(err.value) == PAST_ERROR


@pytest.mark.parametrize("text", ["25:00", "12:60", "31.02 10:00", "2026-13-01 10:00", "+99999999d"])
def test_parse_when_invalid(text):
    with pytest.raises(ValueError, match="Некорректная дата или время"):
        parse_when(text, TZ, NOW)


def test_parse_when_now_variants():
    utc_now = NOW.astimezone(timezone.utc)
    assert parse_when("18:30", TZ, utc_now) == at(10, 14, 18, 30)
    assert parse_when("18:30", TZ, NOW.replace(tzinfo=None)) == at(10, 14, 18, 30)
    assert parse_when("+1m", TZ) > datetime.now(TZ)


def test_format_dt():
    assert format_dt(int(at(12, 25, 9, 5).timestamp()), TZ) == "25.12.2026 09:05"


# ------------------------------------------------------------------ Scheduler.run_due_posts

CH2 = -1001000000002


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    async def fake_pause(seconds: float) -> None:
        return None

    monkeypatch.setattr(posting, "_pause", fake_pause)


async def setup_channel(db, bot, chat_id=CH, title="Канал", username="chan_name"):
    bot.add_chat(chat_id, title, username)
    await db.upsert_channel(chat_id, title, username, status=CH_ACTIVE, rights={"can_post": True})


async def scheduled_post(db, when: int, text_md="Отложенный", created_by=1, channels=(CH,)) -> int:
    """A post queued to `channels` at `when` (per-target times, as the V2 content plan stores them)."""
    post_id = await db.create_post(text_md=text_md, created_by=created_by)
    await db.set_post_targets(post_id, channels)
    for channel_id in channels:
        await db.set_target_schedule(post_id, channel_id, when, queued=True)
    await db.refresh_post_schedule(post_id)
    return post_id


def channel_texts(bot, chat_id=CH):
    return [c["text"] for c in bot.calls_of("send_message") if c["chat_id"] == chat_id]


def notices(bot, admin_id=1):
    return [c["text"] for c in bot.calls_of("send_message") if c["chat_id"] == admin_id]


async def test_run_due_posts_publishes_only_due(db, bot, config):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    due = await scheduled_post(db, now() - 10)
    future = await scheduled_post(db, now() + 3600)
    draft = await db.create_post(text_md="Черновик")
    await db.set_post_targets(draft, [CH])

    processed = await Scheduler(bot, db, config).run_due_posts()

    assert processed == 1
    assert (await db.get_post(due))["status"] == "sent"
    assert (await db.get_post(future))["status"] == "scheduled"
    assert (await db.get_post(draft))["status"] == "draft"
    assert channel_texts(bot) == ["Отложенный"]
    (notice,) = [c for c in bot.calls_of("send_message") if c["chat_id"] == 1]
    assert notice["text"].startswith("⏰ Отложенный пост опубликован\n✅ Опубликовано: 1 из 1")
    assert "https://t.me/chan_name/" in notice["text"] and notice["parse_mode"] == "HTML"

    assert await Scheduler(bot, db, config).run_due_posts() == 0


async def test_run_due_posts_notifies_all_admins_when_creator_unknown(db, bot, config):
    await db.add_admin(1, is_owner=True)
    await db.add_admin(2)
    await setup_channel(db, bot)
    await scheduled_post(db, now() - 1, created_by=555)

    await Scheduler(bot, db, config).run_due_posts()

    notified = sorted(c["chat_id"] for c in bot.calls_of("send_message") if c["chat_id"] > 0)
    assert notified == [1, 2]


async def test_post_queued_to_two_channels_at_different_times(db, bot, config, monkeypatch):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    await setup_channel(db, bot, CH2, "Второй", None)
    t0 = now()
    post_id = await db.create_post(text_md="Очередь", created_by=1)
    await db.set_post_targets(post_id, [CH, CH2])
    await db.set_target_schedule(post_id, CH, t0 - 5, queued=True)
    await db.set_target_schedule(post_id, CH2, t0 + 3600, queued=True)
    await db.refresh_post_schedule(post_id)
    scheduler = Scheduler(bot, db, config)

    assert await scheduler.run_due_posts() == 1
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["scheduled_at"] == t0 + 3600
    assert channel_texts(bot, CH) == ["Очередь"] and channel_texts(bot, CH2) == []
    (notice,) = notices(bot)
    assert "Остальные каналы — по расписанию, следующая публикация " in notice
    assert format_dt(t0 + 3600, config.tz) in notice

    monkeypatch.setattr(scheduler_module, "now", lambda: t0 + 3600)
    assert await scheduler.run_due_posts() == 1
    post = await db.get_post(post_id)
    assert post["status"] == "sent" and post["scheduled_at"] is None
    assert channel_texts(bot, CH) == ["Очередь"] and channel_texts(bot, CH2) == ["Очередь"]
    assert all(t["status"] == "sent" for t in post["targets"])
    assert await scheduler.run_due_posts() == 0


async def test_run_due_posts_render_error_marks_only_due_targets_failed(db, bot, config):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    await setup_channel(db, bot, CH2, "Второй", None)
    post_id = await db.create_post(text_md="x" * 5000, created_by=1)
    await db.set_post_targets(post_id, [CH, CH2])
    await db.set_target_schedule(post_id, CH, now() - 1)
    await db.set_target_schedule(post_id, CH2, now() + 3600)
    await db.refresh_post_schedule(post_id)

    assert await Scheduler(bot, db, config).run_due_posts() == 1

    post = await db.get_post(post_id)
    assert post["status"] == "scheduled"  # the other channel is still queued
    assert post["error"].startswith("Текст слишком длинный")
    targets = {t["channel_id"]: t for t in post["targets"]}
    assert targets[CH]["status"] == "failed" and targets[CH]["scheduled_at"] is None
    assert targets[CH2]["status"] == "pending" and targets[CH2]["scheduled_at"]
    (notice,) = bot.calls_of("send_message")
    assert notice["chat_id"] == 1
    assert notice["text"].startswith(f"❌ Не удалось опубликовать отложенный пост #{post_id}\nТекст слишком длинный")
    assert notice["text"].endswith("Каналы: «Канал»")
    # not retried on the next poll
    assert await Scheduler(bot, db, config).run_due_posts() == 0


async def test_run_due_posts_render_error_single_channel_marks_post_failed(db, bot, config):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1, text_md="x" * 5000)
    await Scheduler(bot, db, config).run_due_posts()
    post = await db.get_post(post_id)
    assert post["status"] == "failed" and post["error"].startswith("Текст слишком длинный")


async def test_run_due_posts_no_channels_marks_failed(db, bot, config):
    await db.add_admin(1, is_owner=True)
    post_id = await db.create_post(text_md="Без каналов", status="scheduled", scheduled_at=now() - 1, created_by=1)
    assert await Scheduler(bot, db, config).run_due_posts() == 1
    post = await db.get_post(post_id)
    assert post["status"] == "failed" and post["error"] == "Не выбраны каналы для публикации"
    (notice,) = notices(bot)
    assert notice == f"❌ Не удалось опубликовать отложенный пост #{post_id}\nНе выбраны каналы для публикации"
    assert await Scheduler(bot, db, config).run_due_posts() == 0


async def test_run_due_posts_never_adopts_post_level_times(db, bot, config):
    """Regression (review2 #4): the scheduler no longer copies posts.scheduled_at into targets
    without a time on every tick. Such a state exists only for a moment — between the two writes
    of «Снять с расписания» — and adopting it then published the post the owner just cancelled.
    (V1 data is converted once by the migration: test_migrate_v1_database_file.)"""
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    await db.set_target_schedule(post_id, CH, None)  # plan.unschedule_post: 1st write done…
    # …the scheduler tick lands here, before refresh_post_schedule
    assert (await db.get_post(post_id))["status"] == "scheduled"

    await Scheduler(bot, db, config).run_due_posts()

    assert (await db.get_post(post_id))["targets"][0]["scheduled_at"] is None
    await db.refresh_post_schedule(post_id)  # …2nd write of the unschedule
    assert (await db.get_post(post_id))["status"] == "draft"
    await Scheduler(bot, db, config).run_due_posts()
    assert channel_texts(bot) == []


async def test_due_post_level_time_adopted_explicitly_is_published(db, bot, config):
    """db.adopt_legacy_schedules (the migration's conversion) still makes a V1 post publishable."""
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    due = await db.create_post(text_md="Старый способ", status="scheduled", scheduled_at=now() - 1, created_by=1)
    await db.set_post_targets(due, [CH])
    assert await db.adopt_legacy_schedules() == 1
    await Scheduler(bot, db, config).run_due_posts()
    assert (await db.get_post(due))["status"] == "sent"
    assert channel_texts(bot) == ["Старый способ"]


async def test_flood_control_longer_than_cap_replans_the_channel(db, bot, config, monkeypatch):
    """Regression (review2 #5): Telegram asks for 40 s — the scheduler does not wait 30 s, retry
    inside the window and lose the slot; the channel stays pending and goes out after the wait."""
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    await setup_channel(db, bot, CH2, "Второй", None)
    t0 = now()
    post_id = await scheduled_post(db, t0 - 1, channels=(CH, CH2))
    flood = TelegramRetryAfter(method=SendMessage(chat_id=CH, text="x"), message="Too Many Requests",
                               retry_after=40)
    bot.fail_on["send_message"] = lambda kw: flood if kw["chat_id"] == CH else None
    sleeps = []

    async def fake_pause(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(posting, "_pause", fake_pause)
    monkeypatch.setattr(posting, "now", lambda: t0)
    scheduler = Scheduler(bot, db, config)
    monkeypatch.setattr(scheduler_module, "now", lambda: t0)
    assert await scheduler.run_due_posts() == 1

    assert 30 not in sleeps and len(channel_texts(bot, CH)) == 1  # no retry inside the window
    assert channel_texts(bot, CH2) == ["Отложенный"]
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["scheduled_at"] == t0 + 41
    target = await db.get_target(post_id, CH)
    assert (target["status"], target["scheduled_at"], target["queued"]) == ("pending", t0 + 41, True)
    assert "повторю автоматически через 40 с" in target["error"]
    assert "повторю автоматически через 40 с" in notices(bot)[0]

    bot.fail_on.clear()
    monkeypatch.setattr(scheduler_module, "now", lambda: t0 + 41)
    monkeypatch.setattr(posting, "now", lambda: t0 + 41)
    assert await scheduler.run_due_posts() == 1
    post = await db.get_post(post_id)
    assert post["status"] == "sent" and post["error"] is None
    assert channel_texts(bot, CH) == ["Отложенный", "Отложенный"]  # the refused call + the send
    assert all(t["status"] == "sent" and t["error"] is None for t in post["targets"])


async def test_flood_control_within_cap_is_waited_out(db, bot, config, monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    attempts = []

    def flaky(kwargs):
        if kwargs["chat_id"] != CH:
            return None
        attempts.append(1)
        return TelegramRetryAfter(method=SendMessage(chat_id=CH, text="x"), message="Too Many Requests",
                                  retry_after=12) if len(attempts) == 1 else None

    bot.fail_on["send_message"] = flaky
    sleeps = []

    async def fake_pause(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(posting, "_pause", fake_pause)
    await Scheduler(bot, db, config).run_due_posts()
    assert sleeps == [12] and len(attempts) == 2
    assert (await db.get_post(post_id))["status"] == "sent"


async def test_run_due_posts_heals_published_post_with_due_target(db, bot, config):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    await setup_channel(db, bot, CH2, "Второй", None)
    post_id = await db.create_post(text_md="Допубликация", created_by=1)
    await db.set_post_targets(post_id, [CH])
    await posting.publish_post(bot, db, config, post_id)
    await db.set_post_targets(post_id, [CH2])  # queued to one more channel later
    await db.set_target_schedule(post_id, CH2, now() - 1, queued=True)
    await db.update_post(post_id, status="partial")  # state written by code not using refresh
    bot.calls.clear()

    assert await Scheduler(bot, db, config).run_due_posts() == 1
    assert channel_texts(bot, CH2) == ["Допубликация"]
    assert (await db.get_post(post_id))["status"] == "sent"


async def test_run_due_posts_all_channels_failing(db, bot, config):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    bot.fail_on["send_message"] = lambda kw: bad_request("Bad Request: chat not found") if kw["chat_id"] == CH else None
    await Scheduler(bot, db, config).run_due_posts()
    assert (await db.get_post(post_id))["status"] == "failed"
    (notice,) = notices(bot)
    assert notice.startswith("⏰ Отложенный пост не удалось опубликовать\n❌ Опубликовано: 0 из 1")


async def test_run_due_posts_skips_post_taken_by_someone_else(db, bot, config, monkeypatch):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    original = db.get_due_targets

    async def due_then_taken(*args, **kwargs):
        rows = await original(*args, **kwargs)
        await db.claim_post_for_sending(post_id)  # e.g. the admin pressed «🚀 Сейчас» meanwhile
        return rows

    monkeypatch.setattr(db, "get_due_targets", due_then_taken)
    assert await Scheduler(bot, db, config).run_due_posts() == 1
    assert (await db.get_post(post_id))["status"] == "sending"  # owned by the other publisher
    assert bot.calls_of("send_message") == []


async def test_run_due_posts_unexpected_error_marks_failed(db, bot, config, monkeypatch):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)

    async def boom(*args, **kwargs):
        raise RuntimeError("db is locked")

    monkeypatch.setattr(scheduler_module, "publish_post", boom)
    await Scheduler(bot, db, config).run_due_posts()
    post = await db.get_post(post_id)
    assert post["status"] == "failed" and post["error"] == "Внутренняя ошибка: db is locked"
    assert notices(bot)[0].startswith(f"❌ Не удалось опубликовать отложенный пост #{post_id}\nВнутренняя ошибка")


async def test_due_posts_wait_while_telegram_is_unreachable(db, bot, config):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetMe

    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    bot.fail_on["get_me"] = TelegramNetworkError(method=GetMe(), message="Cannot connect to host")
    scheduler = Scheduler(bot, db, config)

    assert await scheduler.run_due_posts() == 0
    assert await scheduler.run_due_posts() == 0
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["targets"][0]["status"] == "pending"
    assert bot.calls_of("send_message") == []

    bot.fail_on.clear()
    assert await scheduler.run_due_posts() == 1
    assert (await db.get_post(post_id))["status"] == "sent"


async def test_no_connectivity_check_without_due_posts(db, bot, config):
    await scheduled_post(db, now() + 3600)
    assert await Scheduler(bot, db, config).run_due_posts() == 0
    assert bot.calls_of("get_me") == []


# ------------------------------------------------------- race regressions (review #0 / #19)

async def two_due_posts_with_hook(db, bot, hook):
    """Two posts due now; `hook` runs while the first one is being sent to the channel."""
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    first = await scheduled_post(db, now() - 20, text_md="first")
    second = await scheduled_post(db, now() - 10, text_md="second")
    original = bot.send_message
    done = []

    async def send_message(**kwargs):
        if kwargs.get("text") == "first" and not done:
            done.append(1)
            await hook(second)
        return await original(**kwargs)

    bot.send_message = send_message
    return first, second


async def test_post_unscheduled_during_batch_is_not_published(db, bot, config):
    async def unschedule(post_id):  # plan.unschedule_post
        for target in await db.get_targets(post_id):
            await db.set_target_schedule(post_id, target["channel_id"], None)
        await db.refresh_post_schedule(post_id)

    first, second = await two_due_posts_with_hook(db, bot, unschedule)
    await Scheduler(bot, db, config).run_due_posts()
    assert channel_texts(bot) == ["first"]
    post = await db.get_post(second)
    assert post["status"] == "draft" and post["error"] is None
    assert len(notices(bot)) == 1  # only the report of the first post, no failure notice


async def test_post_rescheduled_during_batch_is_not_published(db, bot, config):
    later = now() + 5 * 3600

    async def reschedule(post_id):  # plan.schedule_post_at / move_target
        for target in await db.get_targets(post_id):
            await db.set_target_schedule(post_id, target["channel_id"], later)
        await db.refresh_post_schedule(post_id)

    first, second = await two_due_posts_with_hook(db, bot, reschedule)
    await Scheduler(bot, db, config).run_due_posts()
    assert channel_texts(bot) == ["first"]
    post = await db.get_post(second)
    assert post["status"] == "scheduled" and post["scheduled_at"] == later
    assert len(notices(bot)) == 1


async def test_post_cancelled_v1_style_during_batch_is_not_published(db, bot, config):
    async def cancel(post_id):  # V1 handlers: status → draft, target times untouched
        await db.update_post(post_id, status="draft", scheduled_at=None)

    first, second = await two_due_posts_with_hook(db, bot, cancel)
    await Scheduler(bot, db, config).run_due_posts()
    assert channel_texts(bot) == ["first"]
    assert (await db.get_post(second))["status"] == "draft"


async def test_post_moved_v1_style_during_batch_is_not_published(db, bot, config):
    later = now() + 3600

    async def move(post_id):  # V1 reschedule: only posts.scheduled_at changes
        await db.update_post(post_id, scheduled_at=later)

    first, second = await two_due_posts_with_hook(db, bot, move)
    await Scheduler(bot, db, config).run_due_posts()
    assert channel_texts(bot) == ["first"]
    post = await db.get_post(second)
    assert post["status"] == "scheduled" and post["scheduled_at"] == later
    assert post["targets"][0]["status"] == "pending"


async def test_rescheduled_post_with_error_is_not_marked_failed(db, bot, config, monkeypatch):
    """A post moved to later while its publish failed validation must not be marked failed."""
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    later = now() + 3600

    async def moved_then_error(bot_, db_, config_, pid, **kwargs):
        await db.set_target_schedule(pid, CH, later)
        await db.refresh_post_schedule(pid)
        raise PostError("Текст слишком длинный")

    monkeypatch.setattr(scheduler_module, "publish_post", moved_then_error)
    await Scheduler(bot, db, config).run_due_posts()
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["error"] is None
    assert post["targets"][0]["status"] == "pending"
    assert bot.calls_of("send_message") == []


# ------------------------------------------------------------------ Scheduler loops

async def test_scheduler_start_stop_and_survives_errors(db, bot, config, monkeypatch):
    await setup_channel(db, bot)
    scheduler = Scheduler(bot, db, config, poll_interval=0.01, snapshot_interval=60)
    calls = []

    async def flaky_run():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first iteration fails")
        return 0

    monkeypatch.setattr(scheduler, "run_due_posts", flaky_run)
    scheduler.start()
    scheduler.start()  # idempotent
    assert scheduler.running
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    tasks = list(scheduler._tasks)
    await scheduler.stop()

    assert len(calls) >= 3
    assert not scheduler.running and all(t.done() for t in tasks)
    assert bot.calls_of("get_chat_member_count") == [{"chat_id": CH}]  # snapshot taken at start
    assert [s["count"] for s in await db.get_member_snapshots(CH)] == [10]
    await scheduler.stop()  # stopping twice is harmless


async def test_scheduler_stop_lets_running_publish_finish(db, bot, config):
    """Regression (review #3): stopping does not cut a scheduled publish mid-send."""
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    first = await scheduled_post(db, now() - 20, text_md="first")
    second = await scheduled_post(db, now() - 10, text_md="second")
    started, release = asyncio.Event(), asyncio.Event()
    original = bot.send_message

    async def slow_send(**kwargs):
        if kwargs.get("text") == "first":
            started.set()
            await release.wait()
        return await original(**kwargs)

    bot.send_message = slow_send
    scheduler = Scheduler(bot, db, config, poll_interval=60, snapshot_interval=60)
    scheduler.start()
    await asyncio.wait_for(started.wait(), 5)
    asyncio.get_running_loop().call_later(0.05, release.set)
    await scheduler.stop(timeout=5)

    assert (await db.get_post(first))["status"] == "sent"  # finished, not left 'sending'
    assert (await db.get_post(second))["status"] == "scheduled"  # not picked after the stop request
    assert channel_texts(bot) == ["first"]
    assert not scheduler.running


async def test_scheduler_stop_timeout_cancels(db, bot, config):
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    started = asyncio.Event()

    async def stuck_send(**kwargs):
        started.set()
        await asyncio.sleep(3600)

    bot.send_message = stuck_send
    scheduler = Scheduler(bot, db, config, poll_interval=60, snapshot_interval=60, stop_timeout=0.05)
    scheduler.start()
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(scheduler.stop(), 5)
    assert not scheduler.running
    post = await db.get_post(post_id)
    assert post["status"] == "failed"  # the finish step still ran, never left 'sending'
    # the cut send may have reached Telegram: the target is not left due for an automatic re-send
    assert post["targets"][0]["status"] == "failed"
    assert post["targets"][0]["error"] == posting.INTERRUPTED_ERROR
    assert await db.get_due_targets(now() + 60) == []


async def test_request_stop_picks_no_new_post(db, bot, config):
    """Regression (review2 #7): once the shutdown began, the scheduler starts no publish."""
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    post_id = await scheduled_post(db, now() - 1)
    scheduler = Scheduler(bot, db, config)
    scheduler.request_stop()
    assert await scheduler.run_due_posts() == 0
    assert (await db.get_post(post_id))["status"] == "scheduled"
    assert bot.calls == []


async def test_request_stop_during_a_run_finishes_only_the_current_post(db, bot, config):
    await db.add_admin(1, is_owner=True)
    await setup_channel(db, bot)
    first = await scheduled_post(db, now() - 20, text_md="first")
    second = await scheduled_post(db, now() - 10, text_md="second")
    scheduler = Scheduler(bot, db, config)
    original = bot.send_message

    async def send_message(**kwargs):
        if kwargs.get("text") == "first":
            scheduler.request_stop()  # SIGTERM while the first post is being sent
        return await original(**kwargs)

    bot.send_message = send_message
    assert await scheduler.run_due_posts() == 1
    assert (await db.get_post(first))["status"] == "sent"
    assert (await db.get_post(second))["status"] == "scheduled"
    assert channel_texts(bot) == ["first"]


async def test_notices_about_a_post_in_many_channels_fit_one_message(db, bot, config, monkeypatch):
    """The scheduler's report / failure notice lists every channel: with many channels it must stay
    within Telegram's 4096 characters, or the admins get nothing at all."""
    monkeypatch.setattr(posting, "_pause", lambda seconds: asyncio.sleep(0))
    await db.add_admin(1, is_owner=True)
    channels = [-1009000000000 - n for n in range(150)]
    for n, chat_id in enumerate(channels):
        await setup_channel(db, bot, chat_id, f"Очень длинное название канала номер {n:03d}", None)
    await scheduled_post(db, now() - 5, channels=channels)
    await scheduled_post(db, now() - 4, text_md="   ", channels=channels)  # cannot be rendered → fails
    await Scheduler(bot, db, config).run_due_posts()
    published, failed = notices(bot)
    assert published.startswith("⏰ Отложенный пост опубликован\n✅ Опубликовано: 150 из 150")
    assert "…и ещё" in published and len(published) <= 4096
    assert failed.startswith("❌ Не удалось опубликовать отложенный пост") and "(всего 150)" in failed
    assert len(failed) <= 4096

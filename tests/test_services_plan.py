"""Tests for app.services.plan (slots parsing, slot arithmetic, queue and plan views/editing)."""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import CH_ACTIVE, CH_LEFT, POST_DRAFT, POST_SCHEDULED
from app.services import plan
from app.services.plan import (
    WEEKDAYS,
    PlanError,
    channel_plan,
    compact_queue,
    describe_slots,
    iter_slot_times,
    move_target,
    next_free_slots,
    normalize_slots,
    parse_slots,
    post_summary,
    preview_queue,
    queue_post,
    refresh_post_schedule,
    schedule_post_at,
    shift_target,
    slot_label,
    slots_to_text,
    timeline,
    unqueue_target,
    unschedule_post,
)

TZ = ZoneInfo("Asia/Tashkent")
BERLIN = ZoneInfo("Europe/Berlin")
CH_A = -1001000000001
CH_B = -1001000000002
CH_C = -1001000000003
ALL_DAYS = {d: ["09:00", "18:00"] for d in range(7)}


def at(month: int, day: int, hour: int, minute: int = 0, *, year: int = 2026, tz=TZ) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp())


NOW = at(10, 14, 12, 0)  # Wednesday


@pytest.fixture
def frozen(monkeypatch):
    """Freeze plan's clock at NOW (Wednesday 14.10.2026 12:00 Tashkent)."""
    monkeypatch.setattr(plan, "now", lambda: NOW)
    return NOW


async def add_channel(db, chat_id, title, *, status=CH_ACTIVE, slots=None, folder_id=None):
    await db.upsert_channel(chat_id, title, None, status=status)
    if slots is not None:
        await db.set_channel_slots(chat_id, slots)
    if folder_id is not None:
        await db.set_channel_folder(chat_id, folder_id)


async def new_post(db, text="Пост", channels=(), **fields) -> int:
    status = fields.pop("status", None)  # set after the targets: none can be set on a sending post
    post_id = await db.create_post(text_md=text, text_html=text, **fields)
    if channels:
        await db.set_post_targets(post_id, channels)
    if status is not None:
        await db.update_post(post_id, status=status)
    return post_id


async def target(db, post_id, channel_id) -> dict:
    return await db.get_target(post_id, channel_id)


async def status(db, post_id) -> tuple[str, int | None]:
    post = await db.get_post(post_id)
    return post["status"], post["scheduled_at"]


# ================================================================== parse_slots

WEEK = list(range(7))


@pytest.mark.parametrize("text, expected", [
    ("пн-пт 09:00, 18:00\nсб, вс 12:00",
     {**{d: ["09:00", "18:00"] for d in range(5)}, 5: ["12:00"], 6: ["12:00"]}),
    ("пн 9:00", {0: ["09:00"]}),
    ("вт 9.30", {1: ["09:30"]}),
    ("ср 09:05 7:00", {2: ["07:00", "09:05"]}),
    ("Пн.-Пт.: 9:00", {d: ["09:00"] for d in range(5)}),
    ("понедельник, среда 10:00", {0: ["10:00"], 2: ["10:00"]}),
    ("пон, втор, четв 10:00", {0: ["10:00"], 1: ["10:00"], 3: ["10:00"]}),
    ("Mon-Fri 9:00", {d: ["09:00"] for d in range(5)}),
    ("sat, sun 12:00", {5: ["12:00"], 6: ["12:00"]}),
    ("пт-пн 07:00", {4: ["07:00"], 5: ["07:00"], 6: ["07:00"], 0: ["07:00"]}),
    ("сб-вт 07:00", {5: ["07:00"], 6: ["07:00"], 0: ["07:00"], 1: ["07:00"]}),
    ("пн – ср 8:00", {0: ["08:00"], 1: ["08:00"], 2: ["08:00"]}),
    ("ежедневно 21:00", {d: ["21:00"] for d in WEEK}),
    ("каждый день 21:00", {d: ["21:00"] for d in WEEK}),
    ("Все дни 6:00", {d: ["06:00"] for d in WEEK}),
    ("будни 8:00", {d: ["08:00"] for d in range(5)}),
    ("по будним дням 8:00", {d: ["08:00"] for d in range(5)}),
    ("выходные 11:00", {5: ["11:00"], 6: ["11:00"]}),
    ("по выходным 11:00", {5: ["11:00"], 6: ["11:00"]}),
    ("weekends 11:00", {5: ["11:00"], 6: ["11:00"]}),
    ("9:00, 18:00", {d: ["09:00", "18:00"] for d in WEEK}),
    ("с пн по ср в 10:00", {0: ["10:00"], 1: ["10:00"], 2: ["10:00"]}),
    ("mon to wed 10:00", {0: ["10:00"], 1: ["10:00"], 2: ["10:00"]}),
    ("по средам и пятницам 10:00", {2: ["10:00"], 4: ["10:00"]}),
    ("пн 9:00 вт 10:00, 18:00", {0: ["09:00"], 1: ["10:00", "18:00"]}),
    ("09:00 пн-ср", {0: ["09:00"], 1: ["09:00"], 2: ["09:00"]}),
    ("будни 8:00; выходные 11:00", {**{d: ["08:00"] for d in range(5)}, 5: ["11:00"], 6: ["11:00"]}),
    ("пн 18:00 9:00 09:00 18.00", {0: ["09:00", "18:00"]}),
    ("пн 9:00\nпн 10:00\nпн-вт 9:00", {0: ["09:00", "10:00"], 1: ["09:00"]}),
    ("- пн 9:00\n• вт 10:00", {0: ["09:00"], 1: ["10:00"]}),
    ("\n\n  пн 0:00 23:59  \n\n", {0: ["00:00", "23:59"]}),
    ("ВС 12:00", {6: ["12:00"]}),
    ("воскресенье 12:00", {6: ["12:00"]}),
    ("", {}),
    ("   \n  ", {}),
])
def test_parse_slots(text, expected):
    assert parse_slots(text) == expected


def test_parse_slots_result_is_sorted_and_canonical():
    result = parse_slots("вс 18:00 9:00\nпн 12:00")
    assert list(result) == [0, 6]
    assert result[6] == ["09:00", "18:00"]


@pytest.mark.parametrize("text, message", [
    ("пн 25:00", "Строка 1: не понял время «25:00»"),
    ("пн 9:00\nвт 25:00", "Строка 2: не понял время «25:00»"),
    ("пн 24:00", "Строка 1: не понял время «24:00»"),
    ("пн 9:60", "Строка 1: не понял время «9:60»"),
    ("пн 9:5", "Строка 1: не понял время «9:5»"),
    ("пн 9", "Строка 1: не понял время «9»"),
    ("пн 9:00\n\nxyz 9:00", "Строка 3: не понял «xyz»"),
    ("пн 9:00-18:00", "Строка 1: интервалы времени («9:00-18:00») не поддерживаются"),
    ("\nпн", "Строка 2: не указано время"),
    ("пн 9:00 вт", "Строка 1: после «вт» не указано время"),
    ("9:00 пн 10:00", "Строка 1: непонятно, к каким дням относится время «9:00»"),
    ("пн до 9:00", "Строка 1: не понял «до»"),
    ("пн-xx 9:00", "Строка 1: не понял «пн-xx»"),
])
def test_parse_slots_errors(text, message):
    with pytest.raises(PlanError) as exc:
        parse_slots(text)
    assert str(exc.value).startswith(message)


# ================================================================== normalize / text

@pytest.mark.parametrize("obj, expected", [
    (None, {}),
    ({}, {}),
    ({"0": ["9:00", "09:00", "8:30"]}, {0: ["08:30", "09:00"]}),
    ({3: ["10:00"], "1": []}, {3: ["10:00"]}),
    ({"пт": "9:00, 18:00"}, {4: ["09:00", "18:00"]}),
    ({"6": None}, {}),
    ([["09:00"], [], ["10:00"]], {0: ["09:00"], 2: ["10:00"]}),
    ('{"5": ["12:00"]}', {5: ["12:00"]}),
    ("null", {}),
])
def test_normalize_slots(obj, expected):
    assert normalize_slots(obj) == expected


@pytest.mark.parametrize("obj", [
    {"7": ["09:00"]}, {"-1": ["09:00"]}, {"xx": ["09:00"]}, {True: ["09:00"]},
    {"0": ["25:00"]}, {"0": [900]}, {"0": 5}, 42, "не json", [[]] * 8,
])
def test_normalize_slots_rejects(obj):
    with pytest.raises(PlanError):
        normalize_slots(obj)


@pytest.mark.parametrize("slots, text", [
    ({**{d: ["09:00", "18:00"] for d in range(5)}, 5: ["12:00"], 6: ["12:00"]},
     "пн-пт 09:00, 18:00\nсб, вс 12:00"),
    ({d: ["10:00"] for d in WEEK}, "ежедневно 10:00"),
    ({0: ["10:00"], 2: ["10:00"], 3: ["10:00"], 4: ["10:00"]}, "пн, ср-пт 10:00"),
    ({0: ["09:00"], 1: ["10:00"], 2: ["09:00"]}, "пн, ср 09:00\nвт 10:00"),
    ({"6": ["7:00"]}, "вс 07:00"),
    ({}, ""),
])
def test_slots_to_text(slots, text):
    assert slots_to_text(slots) == text


def test_describe_slots():
    slots = {**{d: ["09:00", "18:00"] for d in range(5)}, 5: ["12:00"], 6: ["12:00"]}
    assert describe_slots(slots) == "пн-пт 09:00, 18:00 · сб, вс 12:00"
    assert describe_slots({}) == "не настроено"
    assert describe_slots(None) == "не настроено"
    assert describe_slots({"9": ["x"]}) == "не настроено"


def _random_slots(rng: random.Random) -> dict[int, list[str]]:
    pool = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 5, 15, 30, 45, 59)]
    shared = [rng.sample(pool, rng.randint(1, 4)) for _ in range(3)]
    slots = {}
    for day in WEEK:
        roll = rng.random()
        if roll < 0.25:
            continue
        times = rng.choice(shared) if roll < 0.7 else rng.sample(pool, rng.randint(1, 5))
        slots[day] = sorted(set(times))
    return slots


@pytest.mark.parametrize("seed", range(10))
def test_slots_text_round_trip_property(seed):
    rng = random.Random(seed)
    for _ in range(100):
        slots = _random_slots(rng)
        text = slots_to_text(slots)
        assert parse_slots(text) == slots, text
        assert slots_to_text(parse_slots(text)) == text
        assert normalize_slots({str(k): v for k, v in slots.items()}) == slots


@pytest.mark.parametrize("seed", range(3))
def test_parse_slots_random_formatting_property(seed):
    """Random spellings of the same rules parse to the same slots."""
    rng = random.Random(1000 + seed)
    spellings = [
        ["пн", "пон", "понедельник", "Mon", "monday", "пн."],
        ["вт", "вторник", "tue", "Вт."],
        ["ср", "среда", "wed", "СР"],
        ["чт", "четверг", "thu", "чт."],
        ["пт", "пятница", "fri", "Пт"],
        ["сб", "суббота", "sat", "сб."],
        ["вс", "воскресенье", "sun", "Вс"],
    ]
    for _ in range(50):
        slots = _random_slots(rng)
        lines = []
        for day, times in slots.items():
            spelled = [t[1:] if t[0] == "0" and rng.random() < 0.5 else t for t in times]
            spelled = [t.replace(":", ".") if rng.random() < 0.3 else t for t in spelled]
            sep = rng.choice([", ", " ", ",", " , "])
            lines.append(f"{rng.choice(spellings[day])} {sep.join(spelled)}")
        rng.shuffle(lines)
        assert parse_slots("\n".join(lines)) == slots


# ================================================================== iter_slot_times

def test_iter_slot_times_basic_order_and_strictly_after():
    slots = {2: ["09:00", "18:00"], 3: ["12:00"]}
    after = at(10, 14, 9, 0)  # Wednesday 09:00 exactly → not included
    got = list(iter_slot_times(slots, TZ, after, horizon_days=8))
    assert got[:3] == [at(10, 14, 18), at(10, 15, 12), at(10, 21, 9)]
    assert got == sorted(got)
    assert all(ts > after for ts in got)


def test_iter_slot_times_empty_and_horizon():
    assert list(iter_slot_times({}, TZ, NOW)) == []
    assert list(iter_slot_times(None, TZ, NOW)) == []
    daily = list(iter_slot_times({d: ["10:00"] for d in WEEK}, TZ, NOW, horizon_days=3))
    assert daily == [at(10, 15, 10), at(10, 16, 10), at(10, 17, 10)]
    assert len(list(iter_slot_times({0: ["10:00"]}, TZ, NOW))) in (57, 58)


def test_iter_slot_times_accepts_tz_name_and_json_keys():
    got = list(iter_slot_times({"3": ["12:00"]}, "Asia/Tashkent", NOW, horizon_days=1))
    assert got == [at(10, 15, 12)]


def test_iter_slot_times_dst_spring_gap_skipped():
    # 2026-03-29 (Sunday): Berlin clocks jump 02:00 → 03:00; 02:30 does not exist that day.
    slots = {6: ["02:30", "03:00", "09:00"]}
    after = at(3, 28, 0, 0, tz=BERLIN)
    got = list(iter_slot_times(slots, BERLIN, after, horizon_days=8))
    local = [datetime.fromtimestamp(ts, BERLIN) for ts in got]
    assert [(d.month, d.day, d.hour, d.minute) for d in local] == [
        (3, 29, 3, 0), (3, 29, 9, 0), (4, 5, 2, 30), (4, 5, 3, 0), (4, 5, 9, 0),
    ]
    assert got == sorted(got)


def test_iter_slot_times_dst_fall_ambiguous_first_occurrence():
    # 2026-10-25 (Sunday): 03:00 CEST → 02:00 CET; 02:30 happens twice, the first one is taken.
    slots = {6: ["02:30"]}
    after = at(10, 24, 12, 0, tz=BERLIN)
    first = next(iter_slot_times(slots, BERLIN, after))
    assert first == int(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp())
    assert len(list(iter_slot_times(slots, BERLIN, after, horizon_days=1))) == 1


def test_iter_slot_times_wall_clock_kept_across_dst():
    slots = {d: ["09:00"] for d in WEEK}
    after = at(3, 27, 12, 0, tz=BERLIN)
    got = list(iter_slot_times(slots, BERLIN, after, horizon_days=3))
    utc_hours = [datetime.fromtimestamp(ts, timezone.utc).hour for ts in got]
    assert utc_hours == [8, 7, 7]  # 09:00 CET, then 09:00 CEST
    assert all(datetime.fromtimestamp(ts, BERLIN).hour == 9 for ts in got)


@pytest.mark.parametrize("seed", range(4))
def test_iter_slot_times_property_berlin_year(seed):
    rng = random.Random(seed)
    slots = _random_slots(rng) or {0: ["02:30"]}
    slots.setdefault(6, []).append("02:30")
    slots = normalize_slots(slots)
    after = at(1, 1, 0, 0, tz=BERLIN) + rng.randint(0, 86400)
    got = list(iter_slot_times(slots, BERLIN, after, horizon_days=366))
    assert got == sorted(set(got))
    assert all(ts > after for ts in got)
    for ts in got:
        local = datetime.fromtimestamp(ts, BERLIN)
        assert f"{local:%H:%M}" in slots[local.weekday()]
    # every wall-clock occurrence that exists is produced exactly once
    expected = 0
    day = datetime.fromtimestamp(after, BERLIN).date()
    for offset in range(367):
        d = day + timedelta(days=offset)
        for t in slots.get(d.weekday(), []):
            ts = plan._local_ts(d, int(t[:2]), int(t[3:]), BERLIN)
            if ts is not None and ts > after:
                expected += 1
    assert len(got) == expected


def test_slot_label():
    assert slot_label(at(10, 14, 9, 5), TZ) == "Ср 14.10 09:05"
    assert WEEKDAYS[0] == "Пн" and len(WEEKDAYS) == 7


# ================================================================== next_free_slots / preview

async def test_next_free_slots_without_schedule(db, frozen):
    await add_channel(db, CH_A, "A")
    assert await next_free_slots(db, CH_A, TZ, 3) == []
    assert await next_free_slots(db, CH_A, TZ, 0) == []


async def test_next_free_slots_skips_occupied_and_counts(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db, channels=[CH_A])
    await db.set_target_schedule(post, CH_A, at(10, 14, 18), queued=True)
    got = await next_free_slots(db, CH_A, TZ, 3)
    assert got == [at(10, 15, 9), at(10, 15, 18), at(10, 16, 9)]
    # the post's own time is free for itself
    assert await next_free_slots(db, CH_A, TZ, 1, exclude=(post, CH_A)) == [at(10, 14, 18)]
    assert await next_free_slots(db, CH_A, TZ, 1, after_ts=at(10, 20, 0)) == [at(10, 20, 9)]


async def test_next_free_slots_requires_a_minute_of_lead(db, monkeypatch):
    monkeypatch.setattr(plan, "now", lambda: at(10, 14, 17, 59, ) + 30)
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    assert await next_free_slots(db, CH_A, TZ) == [at(10, 15, 9)]


async def test_preview_queue(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B")
    post = await new_post(db, channels=[CH_A])
    await db.set_target_schedule(post, CH_A, at(10, 14, 18), queued=True)
    before = await db.get_post(post)
    assert await preview_queue(db, [CH_A, CH_B, CH_A], TZ) == {CH_A: at(10, 15, 9), CH_B: None}
    assert await preview_queue(db, [CH_A], TZ, post_id=post) == {CH_A: at(10, 14, 18)}
    assert await db.get_post(post) == before


# ================================================================== queue_post

async def test_queue_post_single_channel(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db)
    result = await queue_post(db, post, [CH_A], TZ)
    assert result == {CH_A: at(10, 14, 18)}
    t = await target(db, post, CH_A)
    assert (t["status"], t["scheduled_at"], t["queued"]) == ("pending", at(10, 14, 18), True)
    assert await status(db, post) == (POST_SCHEDULED, at(10, 14, 18))


async def test_queue_post_consecutive_posts_take_consecutive_slots(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    posts = [await new_post(db, f"Пост {i}") for i in range(4)]
    times = [(await queue_post(db, p, [CH_A], TZ))[CH_A] for p in posts]
    assert times == [at(10, 14, 18), at(10, 15, 9), at(10, 15, 18), at(10, 16, 9)]


async def test_queue_post_channels_are_independent(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B", slots={3: ["07:00"]})
    first = await new_post(db)
    await queue_post(db, first, [CH_A], TZ)
    post = await new_post(db)
    result = await queue_post(db, post, [CH_A, CH_B], TZ)
    assert result == {CH_A: at(10, 15, 9), CH_B: at(10, 15, 7)}
    assert await status(db, post) == (POST_SCHEDULED, at(10, 15, 7))


async def test_queue_post_requeue_keeps_own_slot(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db)
    first = await queue_post(db, post, [CH_A], TZ)
    assert await queue_post(db, post, [CH_A], TZ) == first


async def test_queue_post_without_schedule_changes_nothing(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "Бэ")
    await add_channel(db, CH_C, "Цэ")
    post = await new_post(db, channels=[CH_A])
    with pytest.raises(PlanError) as exc:
        await queue_post(db, post, [CH_A, CH_B, CH_C], TZ)
    assert str(exc.value) == "У каналов «Бэ», «Цэ» не настроено расписание публикаций"
    with pytest.raises(PlanError, match="^У канала «Бэ» не настроено расписание публикаций$"):
        await queue_post(db, post, [CH_B], TZ)
    assert (await target(db, post, CH_A))["scheduled_at"] is None
    assert await target(db, post, CH_B) is None
    assert await status(db, post) == (POST_DRAFT, None)


async def test_queue_post_refuses_sending_and_deleted_posts(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    sending = await new_post(db, channels=[CH_A], status="sending")
    deleted = await new_post(db, channels=[CH_A], status="deleted")
    with pytest.raises(PlanError, match="Пост сейчас публикуется"):
        await queue_post(db, sending, [CH_A], TZ)
    with pytest.raises(PlanError, match="удалён"):
        await queue_post(db, deleted, [CH_A], TZ)
    with pytest.raises(PlanError, match="Пост не найден"):
        await queue_post(db, 999, [CH_A], TZ)
    assert (await target(db, sending, CH_A))["scheduled_at"] is None


async def test_queue_post_resets_failed_and_creates_missing_targets(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B", slots=ALL_DAYS)
    await add_channel(db, CH_C, "C", slots=ALL_DAYS)
    post = await new_post(db, channels=[CH_A, CH_C], status="failed")
    await db.update_target(post, CH_A, status="failed", error="Нет прав")
    await queue_post(db, post, [CH_A, CH_B], TZ)
    a, b, c = [await target(db, post, ch) for ch in (CH_A, CH_B, CH_C)]
    assert (a["status"], a["error"], a["queued"]) == ("pending", None, True)
    assert b["status"] == "pending" and b["scheduled_at"] == at(10, 14, 18)
    assert c["scheduled_at"] is None  # not selected → untouched
    assert (await db.get_post(post))["status"] == POST_SCHEDULED


async def test_queue_post_skips_channels_where_already_sent(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B", slots=ALL_DAYS)
    post = await new_post(db, channels=[CH_A, CH_B], status="partial")
    await db.update_target(post, CH_A, status="sent", message_id=5, sent_at=NOW - 100)
    await db.update_target(post, CH_B, status="failed", error="x")
    assert await queue_post(db, post, [CH_A, CH_B], TZ) == {CH_B: at(10, 14, 18)}
    assert (await target(db, post, CH_A))["status"] == "sent"
    assert (await db.get_post(post))["status"] == POST_SCHEDULED
    with pytest.raises(PlanError, match="уже опубликован"):
        await queue_post(db, post, [CH_A], TZ)


async def test_queue_post_validates_channels(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "Ушёл", slots=ALL_DAYS, status=CH_LEFT)
    post = await new_post(db)
    with pytest.raises(PlanError, match="Выберите хотя бы один канал"):
        await queue_post(db, post, [], TZ)
    with pytest.raises(PlanError, match="Канал не найден"):
        await queue_post(db, post, [CH_A, CH_C], TZ)
    with pytest.raises(PlanError, match="«Ушёл» недоступен"):
        await queue_post(db, post, [CH_A, CH_B], TZ)
    assert await db.get_targets(post) == []


async def test_queue_post_no_free_slot_in_horizon(db, frozen, monkeypatch):
    await add_channel(db, CH_A, "A", slots={0: ["10:00"]})
    monkeypatch.setattr(plan, "DEFAULT_HORIZON_DAYS", 6)
    post = await new_post(db)
    first = await new_post(db)
    await queue_post(db, first, [CH_A], TZ)
    with pytest.raises(PlanError, match="нет свободного времени"):
        await queue_post(db, post, [CH_A], TZ)


async def test_queue_post_guard_when_post_claimed_meanwhile(db, frozen, monkeypatch):
    """The scheduler claims the post between the check and the write → nothing is written."""
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db, channels=[CH_A])
    real_slots = db.get_channel_slots

    async def slots_and_claim(chat_id):
        await db.claim_post_for_sending(post)
        return await real_slots(chat_id)

    monkeypatch.setattr(db, "get_channel_slots", slots_and_claim)
    with pytest.raises(PlanError, match="Пост сейчас публикуется"):
        await queue_post(db, post, [CH_A], TZ)
    assert (await target(db, post, CH_A))["scheduled_at"] is None
    assert (await db.get_post(post))["status"] == "sending"


# ================================================================== schedule / unschedule

async def test_schedule_post_at(db, frozen):
    await add_channel(db, CH_A, "A")
    await add_channel(db, CH_B, "B")
    post = await new_post(db)
    when = at(10, 20, 10, 30)
    await schedule_post_at(db, post, [CH_A, CH_B], when)
    for ch in (CH_A, CH_B):
        t = await target(db, post, ch)
        assert (t["scheduled_at"], t["queued"]) == (when, False)
    assert await status(db, post) == (POST_SCHEDULED, when)


async def test_schedule_post_at_rejects_past_and_sending(db, frozen):
    await add_channel(db, CH_A, "A")
    post = await new_post(db)
    with pytest.raises(PlanError, match="^Это время уже прошло$"):
        await schedule_post_at(db, post, [CH_A], NOW)
    sending = await new_post(db, status="sending")
    with pytest.raises(PlanError, match="Пост сейчас публикуется"):
        await schedule_post_at(db, sending, [CH_A], NOW + 3600)


async def test_schedule_post_at_overrides_queue_slot(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    await schedule_post_at(db, post, [CH_A], at(10, 14, 13))
    t = await target(db, post, CH_A)
    assert (t["scheduled_at"], t["queued"]) == (at(10, 14, 13), False)


async def test_unschedule_post(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B", slots=ALL_DAYS)
    post = await new_post(db)
    await queue_post(db, post, [CH_A, CH_B], TZ)
    await unschedule_post(db, post)
    assert await status(db, post) == (POST_DRAFT, None)
    for ch in (CH_A, CH_B):
        t = await target(db, post, ch)
        assert (t["status"], t["scheduled_at"], t["queued"]) == ("pending", None, False)


async def test_unschedule_partially_sent_post_becomes_partial(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B", slots=ALL_DAYS)
    post = await new_post(db, channels=[CH_A, CH_B])
    await db.update_target(post, CH_A, status="sent", message_id=1, sent_at=NOW)
    await queue_post(db, post, [CH_B], TZ)
    assert (await db.get_post(post))["status"] == POST_SCHEDULED
    await unschedule_post(db, post)
    assert await status(db, post) == ("partial", None)


async def test_unschedule_refuses_sending(db, frozen):
    post = await new_post(db, status="sending")
    with pytest.raises(PlanError, match="Пост сейчас публикуется"):
        await unschedule_post(db, post)


async def test_refresh_post_schedule_delegates(db, frozen):
    await add_channel(db, CH_A, "A")
    post = await new_post(db, channels=[CH_A])
    await db.set_target_schedule(post, CH_A, NOW + 500)
    fresh = await refresh_post_schedule(db, post)
    assert (fresh["status"], fresh["scheduled_at"]) == (POST_SCHEDULED, NOW + 500)
    with pytest.raises(PlanError, match="Пост не найден"):
        await refresh_post_schedule(db, 12345)


# ================================================================== channel_plan / timeline

async def test_channel_plan_structure_and_merge(db, frozen):
    await add_channel(db, CH_A, "Канал", slots=ALL_DAYS)
    queued = await new_post(db, "**Первый** пост", buttons=[[{"text": "x", "url": "https://x.y"}]])
    await queue_post(db, queued, [CH_A], TZ)  # 14.10 18:00
    explicit = await new_post(db, "", media=[{"type": "video", "file_id": "v1", "path": None}])
    await schedule_post_at(db, explicit, [CH_A], at(10, 15, 13, 30))
    result = await channel_plan(db, CH_A, TZ, days=3)
    assert result["channel"]["id"] == CH_A
    assert result["slots_text"] == "ежедневно 09:00, 18:00"
    assert result["slots_summary"] == "ежедневно 09:00, 18:00"
    assert [d["date"] for d in result["days"]] == ["2026-10-14", "2026-10-15", "2026-10-16"]
    assert [d["weekday"] for d in result["days"]] == ["Ср", "Чт", "Пт"]
    today, tomorrow, _ = result["days"]
    # today's 09:00 has passed → only 18:00, taken by the queued post
    assert [(i["time"], i["slot"], i["post"]["id"] if i["post"] else None) for i in today["items"]] == [
        ("18:00", True, queued),
    ]
    assert [(i["time"], i["slot"], i["post"]["id"] if i["post"] else None) for i in tomorrow["items"]] == [
        ("09:00", True, None), ("13:30", False, explicit), ("18:00", True, None),
    ]
    summary = today["items"][0]["post"]
    assert summary == {
        "id": queued, "excerpt": "Первый пост", "media_kind": "text", "status": POST_SCHEDULED,
        "queued": True, "has_buttons": True, "media_count": 0, "thumb_type": None,
    }
    video = tomorrow["items"][1]["post"]
    assert (video["media_kind"], video["queued"], video["has_buttons"]) == ("video", False, False)
    assert video["excerpt"] == "🎬 Видео"


async def test_channel_plan_start_and_unknown_channel(db, frozen):
    await add_channel(db, CH_A, "A")
    result = await channel_plan(db, CH_A, TZ, days=2, start_ts=at(10, 20, 15))
    assert [d["date"] for d in result["days"]] == ["2026-10-20", "2026-10-21"]
    assert all(d["items"] == [] for d in result["days"])
    assert result["slots"] == {} and result["slots_summary"] == "не настроено"
    assert len((await channel_plan(db, CH_A, TZ, days=0))["days"]) == 1
    with pytest.raises(PlanError, match="Канал не найден"):
        await channel_plan(db, CH_B, TZ)


async def test_channel_plan_two_posts_on_one_slot(db, frozen):
    await add_channel(db, CH_A, "A", slots={3: ["09:00"]})
    p1, p2 = await new_post(db, "один"), await new_post(db, "два")
    await schedule_post_at(db, p1, [CH_A], at(10, 15, 9))
    await schedule_post_at(db, p2, [CH_A], at(10, 15, 9))
    day = (await channel_plan(db, CH_A, TZ, days=2))["days"][1]
    assert [(i["slot"], i["post"]["id"]) for i in day["items"]] == [(True, p1), (True, p2)]


def test_post_summary_without_media_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.media", None)  # import fails → local fallbacks
    album = {"id": 1, "status": "draft", "text_md": "", "buttons": [[]],
             "media": [{"type": "photo"}, {"type": "video"}]}
    summary = post_summary(album)
    assert (summary["excerpt"], summary["media_kind"], summary["has_buttons"]) == ("🖼 Медиа", "album", False)
    single = post_summary({**album, "media": [{"type": "animation"}]})
    assert single["media_kind"] == "animation"
    assert post_summary({**album, "media": []})["excerpt"] == "(без текста)"


def test_post_summary_long_text_excerpt():
    summary = post_summary({"post_id": 3, "post_status": "scheduled", "text_md": "слово " * 30, "media": []},
                           queued=True)
    assert summary["id"] == 3 and summary["status"] == "scheduled" and summary["queued"] is True
    assert len(summary["excerpt"]) <= 50 and summary["excerpt"].endswith("…")


async def test_timeline_across_channels_and_filters(db, frozen):
    folder = await db.create_folder("Новости")
    await add_channel(db, CH_A, "Альфа", slots=ALL_DAYS, folder_id=folder)
    await add_channel(db, CH_B, "бета", slots=ALL_DAYS)
    p1 = await new_post(db, "первый")
    p2 = await new_post(db, "второй")
    await queue_post(db, p1, [CH_A, CH_B], TZ)       # both 14.10 18:00
    await schedule_post_at(db, p2, [CH_B], at(10, 14, 13))
    far = await new_post(db, "далеко")
    await schedule_post_at(db, far, [CH_A], at(10, 30, 10))
    items = await timeline(db, TZ, days=7)
    assert [(i["time"], i["channel"]["title"], i["post"]["id"]) for i in items] == [
        ("13:00", "бета", p2), ("18:00", "Альфа", p1), ("18:00", "бета", p1),
    ]
    assert items[1]["channel"] == {"id": CH_A, "title": "Альфа", "username": None, "folder_id": folder}
    assert (items[0]["date"], items[0]["weekday"]) == ("2026-10-14", "Ср")
    assert [i["channel"]["id"] for i in await timeline(db, TZ, folder_id=folder)] == [CH_A]
    assert [i["channel"]["id"] for i in await timeline(db, TZ, folder_id=0)] == [CH_B, CH_B]
    assert [i["post"]["id"] for i in await timeline(db, TZ, channel_ids=[CH_B])] == [p2, p1]
    assert await timeline(db, TZ, channel_ids=[]) == []
    assert [i["post"]["id"] for i in await timeline(db, TZ, days=30)][-1] == far


# ================================================================== move / shift / unqueue

async def test_move_target_to_free_time(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    await move_target(db, post, CH_A, at(10, 16, 9))
    t = await target(db, post, CH_A)
    assert (t["scheduled_at"], t["queued"]) == (at(10, 16, 9), True)
    assert await status(db, post) == (POST_SCHEDULED, at(10, 16, 9))
    await move_target(db, post, CH_A, at(10, 16, 11, 15), queued=False)
    t = await target(db, post, CH_A)
    assert (t["scheduled_at"], t["queued"]) == (at(10, 16, 11, 15), False)


async def test_move_target_to_occupied_time_swaps(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    first, second = await new_post(db, "1"), await new_post(db, "2")
    await queue_post(db, first, [CH_A], TZ)                    # 14.10 18:00 queued
    await schedule_post_at(db, second, [CH_A], at(10, 15, 11))  # explicit
    await move_target(db, first, CH_A, at(10, 15, 11))
    a, b = await target(db, first, CH_A), await target(db, second, CH_A)
    assert (a["scheduled_at"], a["queued"]) == (at(10, 15, 11), False)
    assert (b["scheduled_at"], b["queued"]) == (at(10, 14, 18), True)
    assert await status(db, first) == (POST_SCHEDULED, at(10, 15, 11))
    assert await status(db, second) == (POST_SCHEDULED, at(10, 14, 18))


async def test_move_target_errors(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B")
    post, other = await new_post(db), await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    with pytest.raises(PlanError, match="^Это время уже прошло$"):
        await move_target(db, post, CH_A, NOW - 1)
    with pytest.raises(PlanError, match="не запланирован в этом канале"):
        await move_target(db, post, CH_B, NOW + 3600)
    # an unscheduled target cannot take an occupied time (that would unschedule the other post)
    await db.set_post_targets(other, [CH_A])
    with pytest.raises(PlanError, match="занято другим постом"):
        await move_target(db, other, CH_A, at(10, 14, 18))
    await db.update_target(post, CH_A, status="sent")
    with pytest.raises(PlanError, match="уже опубликован в этом канале"):
        await move_target(db, post, CH_A, NOW + 3600)


async def test_move_target_occupant_being_sent(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post, busy = await new_post(db), await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    await schedule_post_at(db, busy, [CH_A], at(10, 15, 11))
    await db.update_post(busy, status="sending")
    with pytest.raises(PlanError, match="сейчас публикуется"):
        await move_target(db, post, CH_A, at(10, 15, 11))
    assert (await target(db, post, CH_A))["scheduled_at"] == at(10, 14, 18)


async def test_move_target_stale_plan_detected(db, frozen, monkeypatch):
    """The occupant is moved by someone else between the read and the swap → nothing changes."""
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post, other = await new_post(db), await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    await queue_post(db, other, [CH_A], TZ)   # 15.10 09:00
    real_fetchone = db.fetchone

    async def fetch_then_change(sql, params=()):
        row = await real_fetchone(sql, params)
        if "t.scheduled_at = ?" in sql:
            await db.set_target_schedule(other, CH_A, at(10, 20, 9), queued=True)
        return row

    monkeypatch.setattr(db, "fetchone", fetch_then_change)
    with pytest.raises(PlanError, match="План изменился"):
        await move_target(db, post, CH_A, at(10, 15, 9))
    assert (await target(db, post, CH_A))["scheduled_at"] == at(10, 14, 18)


async def test_shift_target_swaps_with_neighbours(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    posts = [await new_post(db, str(i)) for i in range(3)]
    for p in posts:
        await queue_post(db, p, [CH_A], TZ)
    t0, t1, t2 = at(10, 14, 18), at(10, 15, 9), at(10, 15, 18)
    assert await shift_target(db, posts[2], CH_A, -1) is True
    assert [(await target(db, p, CH_A))["scheduled_at"] for p in posts] == [t0, t2, t1]
    assert await shift_target(db, posts[0], CH_A, +1) is True
    assert [(await target(db, p, CH_A))["scheduled_at"] for p in posts] == [t1, t2, t0]
    assert await shift_target(db, posts[2], CH_A, -1) is False   # already first
    assert await shift_target(db, posts[1], CH_A, 1) is False    # already last
    assert await status(db, posts[2]) == (POST_SCHEDULED, t0)


async def test_shift_target_neighbour_flags_follow_time(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    queued, explicit = await new_post(db), await new_post(db)
    await queue_post(db, queued, [CH_A], TZ)                       # 18:00 queued
    await schedule_post_at(db, explicit, [CH_A], at(10, 15, 12))  # explicit
    assert await shift_target(db, explicit, CH_A, -1)
    q, e = await target(db, queued, CH_A), await target(db, explicit, CH_A)
    assert (e["scheduled_at"], e["queued"]) == (at(10, 14, 18), True)
    assert (q["scheduled_at"], q["queued"]) == (at(10, 15, 12), False)


async def test_shift_target_ignores_past_neighbours_and_validates(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    overdue, post, loose = await new_post(db), await new_post(db), await new_post(db, channels=[CH_A])
    await db.set_post_targets(overdue, [CH_A])
    await db.set_target_schedule(overdue, CH_A, NOW - 30)
    await db.refresh_post_schedule(overdue)
    await queue_post(db, post, [CH_A], TZ)
    assert await shift_target(db, post, CH_A, -1) is False
    with pytest.raises(PlanError, match="Время публикации уже наступило"):
        await shift_target(db, overdue, CH_A, 1)
    with pytest.raises(PlanError, match="не запланирован"):
        await shift_target(db, loose, CH_A, 1)
    with pytest.raises(PlanError, match="Неверное направление"):
        await shift_target(db, post, CH_A, 0)


async def test_shift_target_neighbour_being_sent(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post, busy = await new_post(db), await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    await queue_post(db, busy, [CH_A], TZ)
    await db.update_post(busy, status="sending")
    with pytest.raises(PlanError, match="Соседний пост сейчас публикуется"):
        await shift_target(db, post, CH_A, 1)


async def test_unqueue_target(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B", slots=ALL_DAYS)
    post = await new_post(db)
    await queue_post(db, post, [CH_A, CH_B], TZ)
    await unqueue_target(db, post, CH_A)
    a = await target(db, post, CH_A)
    assert (a["status"], a["scheduled_at"], a["queued"]) == ("pending", None, False)
    assert (await db.get_post(post))["status"] == POST_SCHEDULED
    await unqueue_target(db, post, CH_B)
    assert await status(db, post) == (POST_DRAFT, None)
    await unqueue_target(db, post, CH_B)  # nothing to do — no error


async def test_unqueue_target_guards(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    await db.update_post(post, status="sending")
    with pytest.raises(PlanError, match="Пост сейчас публикуется"):
        await unqueue_target(db, post, CH_A)
    await db.update_post(post, status="scheduled")
    await db.update_target(post, CH_A, status="failed")
    with pytest.raises(PlanError, match="не удалась"):
        await unqueue_target(db, post, CH_A)
    with pytest.raises(PlanError, match="не запланирован"):
        await unqueue_target(db, post, CH_B)


# ================================================================== compact_queue

async def test_compact_queue_fills_gaps_preserving_order(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    posts = [await new_post(db, str(i)) for i in range(4)]
    for p in posts:
        await queue_post(db, p, [CH_A], TZ)   # 14 18:00, 15 09:00, 15 18:00, 16 09:00
    explicit = await new_post(db, "явный")
    await schedule_post_at(db, explicit, [CH_A], at(10, 15, 9))   # same time as posts[1]
    await unqueue_target(db, posts[0], CH_A)
    await unqueue_target(db, posts[1], CH_A)
    moved = await compact_queue(db, CH_A, TZ)
    assert moved == 2
    assert (await target(db, posts[2], CH_A))["scheduled_at"] == at(10, 14, 18)
    assert (await target(db, posts[3], CH_A))["scheduled_at"] == at(10, 15, 18)  # 15 09:00 is explicit
    assert (await target(db, explicit, CH_A))["scheduled_at"] == at(10, 15, 9)
    assert await status(db, posts[3]) == (POST_SCHEDULED, at(10, 15, 18))
    assert await compact_queue(db, CH_A, TZ) == 0


async def test_compact_queue_after_schedule_change(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    posts = [await new_post(db, str(i)) for i in range(3)]
    for p in posts:
        await queue_post(db, p, [CH_A], TZ)
    await db.set_channel_slots(CH_A, {4: ["10:00"]})   # only Fridays now
    assert await compact_queue(db, CH_A, TZ) == 3
    assert [(await target(db, p, CH_A))["scheduled_at"] for p in posts] == [
        at(10, 16, 10), at(10, 23, 10), at(10, 30, 10),
    ]


async def test_compact_queue_skips_posts_being_sent_and_errors(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "Без расписания")
    gap, busy, last = await new_post(db), await new_post(db), await new_post(db)
    for p in (gap, busy, last):
        await queue_post(db, p, [CH_A], TZ)
    await unqueue_target(db, gap, CH_A)
    await db.update_post(busy, status="sending")
    assert await compact_queue(db, CH_A, TZ) == 1
    assert (await target(db, busy, CH_A))["scheduled_at"] == at(10, 15, 9)
    assert (await target(db, last, CH_A))["scheduled_at"] == at(10, 14, 18)
    with pytest.raises(PlanError, match="У канала «Без расписания» не настроено расписание"):
        await compact_queue(db, CH_B, TZ)
    with pytest.raises(PlanError, match="Канал не найден"):
        await compact_queue(db, CH_C, TZ)


async def test_compact_queue_sparse_schedule_keeps_tail(db, frozen, monkeypatch):
    """Fewer free slots than queued posts: the head is compacted, the tail keeps its times."""
    await add_channel(db, CH_A, "A", slots={0: ["10:00"], 2: ["18:00"]})
    first, second = await new_post(db, channels=[CH_A]), await new_post(db, channels=[CH_A])
    await db.set_target_schedule(first, CH_A, at(10, 15, 11), queued=True)
    await db.set_target_schedule(second, CH_A, at(10, 16, 11), queued=True)
    monkeypatch.setattr(plan, "DEFAULT_HORIZON_DAYS", 0)  # the scan covers only 14.10–15.10
    assert await compact_queue(db, CH_A, TZ) == 1
    assert (await target(db, first, CH_A))["scheduled_at"] == at(10, 14, 18)
    assert (await target(db, second, CH_A))["scheduled_at"] == at(10, 16, 11)


async def test_compact_preview_is_exactly_what_compact_queue_does(db, frozen, monkeypatch):
    """«🧹 Уплотнить очередь» in the bot asks first with the count of posts that would move: the
    preview shares the compaction rule (here the sparse case, where a naive one-pass count would
    promise a move onto a slot the tail keeps)."""
    await add_channel(db, CH_A, "A", slots={2: ["18:00"]})
    head, tail = await new_post(db, channels=[CH_A]), await new_post(db, channels=[CH_A])
    await db.set_target_schedule(head, CH_A, at(10, 14, 17), queued=True)  # not a slot
    await db.set_target_schedule(tail, CH_A, at(10, 14, 18), queued=True)  # the only slot in reach
    monkeypatch.setattr(plan, "DEFAULT_HORIZON_DAYS", 0)
    assert await plan.compact_preview(db, CH_A, TZ) == (0, None)
    assert await compact_queue(db, CH_A, TZ) == 0

    gap = await new_post(db, channels=[CH_A])
    monkeypatch.setattr(plan, "DEFAULT_HORIZON_DAYS", 30)
    await db.set_target_schedule(gap, CH_A, at(10, 28, 18), queued=True)
    await db.set_target_schedule(head, CH_A, None)
    before = {p: (await target(db, p, CH_A))["scheduled_at"] for p in (tail, gap)}
    assert await plan.compact_preview(db, CH_A, TZ) == (1, at(10, 21, 18))
    assert {p: (await target(db, p, CH_A))["scheduled_at"] for p in (tail, gap)} == before  # unchanged
    assert await compact_queue(db, CH_A, TZ) == 1
    assert (await target(db, gap, CH_A))["scheduled_at"] == at(10, 21, 18)


async def test_many_queued_posts_large_backlog(db, frozen):
    """A big backlog: 250 posts queued one after another get distinct ascending slots."""
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    times = []
    for i in range(250):
        post = await new_post(db, f"Пост {i}")
        times.append((await queue_post(db, post, [CH_A], TZ))[CH_A])
    assert times == sorted(set(times))
    first = await db.fetchone("SELECT MIN(id) AS id FROM posts")
    await unqueue_target(db, first["id"], CH_A)
    assert await compact_queue(db, CH_A, TZ) == 249


# ================================================================== review2: lead time (#8), sending posts (#9)

async def test_move_swap_never_hands_an_imminent_time_to_another_post(db, monkeypatch):
    """Regression (review2 #8): at 17:59:30 X (today 18:00) is moved onto Y's time (tomorrow
    18:00) — the swap would give Y today 18:00 and the scheduler would publish Y a day early."""
    clock = [NOW]
    monkeypatch.setattr(plan, "now", lambda: clock[0])
    await add_channel(db, CH_A, "A", slots={d: ["18:00"] for d in range(7)})
    x, y = await new_post(db, "X"), await new_post(db, "Y")
    await queue_post(db, x, [CH_A], TZ)  # today 18:00
    await queue_post(db, y, [CH_A], TZ)  # tomorrow 18:00
    assert (await target(db, x, CH_A))["scheduled_at"] == at(10, 14, 18)
    current = clock[0] = at(10, 14, 17, 59) + 30
    with pytest.raises(PlanError, match="поменять его местами с другим нельзя"):
        await move_target(db, x, CH_A, at(10, 15, 18), queued=False)
    with pytest.raises(PlanError, match="меньше чем через минуту"):
        await shift_target(db, x, CH_A, +1)
    assert await shift_target(db, y, CH_A, -1) is False  # X is no neighbour any more
    assert (await target(db, x, CH_A))["scheduled_at"] == at(10, 14, 18)
    assert (await target(db, y, CH_A))["scheduled_at"] == at(10, 15, 18)
    assert [r["post_id"] for r in await db.get_due_targets(current + 30)] == [x]  # only X, not Y
    # a free time is still fine for X, and Y may move anywhere ≥ 60 s ahead
    await move_target(db, x, CH_A, at(10, 16, 12), queued=False)
    assert (await target(db, x, CH_A))["scheduled_at"] == at(10, 16, 12)


async def test_move_target_refuses_a_time_within_the_lead(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    post = await new_post(db)
    await queue_post(db, post, [CH_A], TZ)
    with pytest.raises(PlanError, match="слишком близко"):
        await move_target(db, post, CH_A, NOW + 30)
    await move_target(db, post, CH_A, NOW + plan.MIN_LEAD, queued=False)  # exactly a minute: fine
    assert (await target(db, post, CH_A))["scheduled_at"] == NOW + plan.MIN_LEAD


async def test_shift_target_skips_an_imminent_neighbour(db, frozen):
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    soon, early, post = await new_post(db, "soon"), await new_post(db, "early"), await new_post(db, "post")
    await schedule_post_at(db, early, [CH_A], NOW + 3600)
    await schedule_post_at(db, post, [CH_A], NOW + 7200)
    await db.set_post_targets(soon, [CH_A])
    await db.set_target_schedule(soon, CH_A, NOW + 30)
    await db.refresh_post_schedule(soon)
    assert await shift_target(db, post, CH_A, -1) is True  # swaps with `early`, not with `soon`
    assert (await target(db, post, CH_A))["scheduled_at"] == NOW + 3600
    assert (await target(db, early, CH_A))["scheduled_at"] == NOW + 7200
    assert await shift_target(db, post, CH_A, -1) is False
    assert (await target(db, soon, CH_A))["scheduled_at"] == NOW + 30


async def test_plan_views_keep_a_post_being_sent_elsewhere(db, frozen):
    """Regression (review2 #9): while a queued post is being sent to channel A, its slot in
    channel B was shown as free (and the timeline dropped it)."""
    await add_channel(db, CH_A, "A", slots=ALL_DAYS)
    await add_channel(db, CH_B, "B", slots=ALL_DAYS)
    post = await new_post(db, "P")
    await queue_post(db, post, [CH_A, CH_B], TZ)
    await db.set_target_schedule(post, CH_A, NOW - 5, queued=True)
    await db.refresh_post_schedule(post)
    assert await db.claim_post_for_sending(post, ("scheduled",), due_before=NOW)

    day = (await channel_plan(db, CH_B, TZ, days=1))["days"][0]
    (item,) = [i for i in day["items"] if i["ts"] == at(10, 14, 18)]
    assert item["post"]["id"] == post and item["post"]["status"] == "sending"
    assert [(i["channel"]["id"], i["post"]["status"]) for i in await timeline(db, TZ, days=1)] == [
        (CH_A, "sending"), (CH_B, "sending")]

"""Tests for app.services.stats."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.db import CH_ACTIVE, CH_PENDING
from app.services.channels import ChannelError
from app.services.stats import channel_stats, format_stats_html, overview, sparkline

TZ = ZoneInfo("Asia/Tashkent")  # UTC+5, no DST
CH = -1001000000001
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)
NOW_TS = int(NOW.timestamp())


@pytest.fixture
async def db(db):
    await db.migrate()
    return db


def ts(day: int, hour: int = 12, minute: int = 0) -> int:
    """Unix ts of 2026-10-<day> hour:minute in TZ."""
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp())


async def make_channel(db, username="chan_name", members=None):
    await db.upsert_channel(CH, "Мой <канал>", username, status=CH_ACTIVE, members_count=members)


# ------------------------------------------------------------------ sparkline

def test_sparkline():
    assert sparkline([]) == ""
    assert sparkline([None, None]) == ""
    assert sparkline([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█"
    assert sparkline([10, 0, 10]) == "█▁█"
    assert sparkline([5, 5, 5]) == "▅▅▅"
    assert sparkline([None, 1, None, 3]) == "▁█"


# ------------------------------------------------------------------ channel_stats

async def test_members_series_carry_forward_and_deltas(db):
    await make_channel(db)
    await db.add_member_snapshot(CH, 100, ts(4))   # before the 7-day period: seed
    await db.add_member_snapshot(CH, 110, ts(9, 8))
    await db.add_member_snapshot(CH, 120, ts(9, 20))  # last snapshot of 09.10 wins
    await db.add_member_snapshot(CH, 130, ts(12))
    await db.add_member_snapshot(CH, 135, ts(14, 11))
    await db.add_member_snapshot(CH, 999, ts(14, 13))  # after "now": ignored

    stats = await channel_stats(db, CH, 7, TZ, now_ts=NOW_TS)

    assert stats["days"] == 7
    assert [d["date"] for d in stats["series"]] == [f"2026-10-{d:02d}" for d in range(8, 15)]
    assert [d["members"] for d in stats["series"]] == [100, 120, 120, 120, 130, 130, 135]
    assert stats["members_now"] == 135
    assert stats["delta"] == {"1d": 5, "7d": 35, "30d": None}
    assert stats["tracking_since"] == ts(4)
    assert stats["period_start"] == ts(8, 0)


async def test_members_unknown_before_tracking(db):
    await make_channel(db, members=50)
    stats = await channel_stats(db, CH, 3, TZ, now_ts=NOW_TS)
    assert [d["members"] for d in stats["series"]] == [None, None, None]
    assert stats["members_now"] == 50  # falls back to channels.members_count
    assert stats["delta"] == {"1d": None, "7d": None, "30d": None}
    assert stats["tracking_since"] is None

    await db.add_member_snapshot(CH, 60, ts(13, 9))
    stats = await channel_stats(db, CH, 3, TZ, now_ts=NOW_TS)
    assert [d["members"] for d in stats["series"]] == [None, 60, 60]
    assert stats["members_now"] == 60


async def test_events_and_posts_bucketed_by_local_day(db):
    await make_channel(db)
    await db.add_member_event(CH, 1, "join", ts(13, 23, 30))  # 18:30 UTC → still 13.10 locally
    await db.add_member_event(CH, 2, "join", ts(14, 0, 10))   # 19:10 UTC of 13.10 → 14.10 locally
    await db.add_member_event(CH, 3, "leave", ts(14, 9))
    await db.add_member_event(CH, 4, "join", ts(7))            # before the period
    await db.add_member_event(CH, 5, "leave", ts(14, 18))      # after now
    await db.add_channel_post(CH, 10, via_bot=True, ts=ts(12))
    await db.add_channel_post(CH, 11, via_bot=False, ts=ts(12, 15))
    await db.add_channel_post(CH, 12, via_bot=False, ts=ts(14, 10))
    await db.add_channel_post(CH, 9, via_bot=True, ts=ts(1))   # before the period

    stats = await channel_stats(db, CH, 3, TZ, now_ts=NOW_TS)

    by_date = {d["date"]: d for d in stats["series"]}
    assert (by_date["2026-10-13"]["joins"], by_date["2026-10-14"]["joins"]) == (1, 1)
    assert by_date["2026-10-14"]["leaves"] == 1
    assert (by_date["2026-10-12"]["posts"], by_date["2026-10-12"]["posts_bot"]) == (2, 1)
    assert (by_date["2026-10-14"]["posts"], by_date["2026-10-14"]["posts_bot"]) == (1, 0)
    assert (stats["joins"], stats["leaves"]) == (2, 1)
    assert (stats["posts"], stats["posts_bot"]) == (3, 1)


async def test_reactions_and_top_posts(db):
    await make_channel(db)
    old = await db.create_post(text_md="Старый пост", status="sent")
    await db.set_post_targets(old, [CH])
    await db.update_target(old, CH, status="sent", message_id=5, sent_at=ts(1))
    await db.set_target_reactions(CH, 5, 100, [{"emoji": "👍", "count": 100}])
    for message_id, text, reactions in ((20, "**Первый** пост", 3), (21, "", 9)):
        post_id = await db.create_post(text_md=text, photo_file_id="F" if not text else None, status="sent")
        await db.set_post_targets(post_id, [CH])
        await db.update_target(post_id, CH, status="sent", message_id=message_id, sent_at=ts(13))
        await db.set_target_reactions(CH, message_id, reactions, [{"emoji": "🔥", "count": reactions}])

    stats = await channel_stats(db, CH, 7, TZ, now_ts=NOW_TS)

    assert stats["reactions"] == 12
    top = stats["top_posts"]
    assert [p["message_id"] for p in top] == [21, 20]
    assert top[0]["excerpt"] == "🖼 Фото" and top[1]["excerpt"] == "Первый пост"
    assert (top[0]["media_kind"], top[1]["media_kind"]) == ("photo", "text")
    assert top[1]["link"] == "https://t.me/chan_name/20"
    assert top[0]["reactions"] == [{"emoji": "🔥", "count": 9}] and top[0]["reactions_total"] == 9
    assert set(top[0]) == {"post_id", "message_id", "link", "excerpt", "media_kind", "reactions_total",
                           "reactions", "sent_at"}


async def test_album_posts_count_once(db):
    """Finding #5: every album item is a channel message, but the album is one post."""
    await make_channel(db)
    for message_id in (30, 31, 32):  # an album posted from a Telegram client
        await db.add_channel_post(CH, message_id, via_bot=False, ts=ts(13), media_group_id="g1")
    await db.add_channel_post(CH, 33, via_bot=False, ts=ts(13))  # a plain post
    await db.add_channel_post(CH, 40, via_bot=True, ts=ts(14, 9), media_group_id="g2")  # a bot album
    await db.add_channel_post(CH, 50, via_bot=False, ts=ts(14, 10), media_group_id="g3")
    await db.add_channel_post(CH, 51, via_bot=False, ts=ts(14, 10), media_group_id="g3")

    stats = await channel_stats(db, CH, 3, TZ, now_ts=NOW_TS)

    by_date = {d["date"]: d for d in stats["series"]}
    assert by_date["2026-10-13"]["posts"] == 2
    assert (by_date["2026-10-14"]["posts"], by_date["2026-10-14"]["posts_bot"]) == (2, 1)
    assert (stats["posts"], stats["posts_bot"]) == (4, 1)


async def test_top_album_and_video_posts_are_described(db):
    await make_channel(db)
    media = [{"type": "photo", "file_id": "A"}, {"type": "video", "file_id": "B"}]
    for message_id, items, reactions in ((60, media, 5), (61, [{"type": "video", "file_id": "V"}], 4)):
        post_id = await db.create_post(text_md="", media=items, status="sent")
        await db.set_post_targets(post_id, [CH])
        await db.update_target(post_id, CH, status="sent", message_id=message_id, sent_at=ts(13))
        await db.set_target_reactions(CH, message_id, reactions, [])
    text_post = await db.create_post(text_md="Видео с подписью", media=[{"type": "video", "file_id": "W"}],
                                     status="sent")
    await db.set_post_targets(text_post, [CH])
    await db.update_target(text_post, CH, status="sent", message_id=62, sent_at=ts(13))
    await db.set_target_reactions(CH, 62, 3, [])

    stats = await channel_stats(db, CH, 7, TZ, now_ts=NOW_TS)
    top = stats["top_posts"]
    assert [(p["excerpt"], p["media_kind"]) for p in top] == [
        ("🗂 Альбом: 1 фото, 1 видео", "album"), ("🎬 Видео", "video"), ("Видео с подписью", "video")]
    text = format_stats_html(stats, TZ)
    assert "🗂 Альбом: 1 фото, 1 видео</a> — ❤️ 5" in text
    assert "🎬 Видео с подписью</a> — ❤️ 3" in text


async def test_days_are_clamped_and_default_now(db):
    await make_channel(db)
    assert len((await channel_stats(db, CH, 0, TZ, now_ts=NOW_TS))["series"]) == 1
    assert len((await channel_stats(db, CH, 1000, TZ, now_ts=NOW_TS))["series"]) == 366
    stats = await channel_stats(db, CH, 2, TZ)
    assert stats["series"][-1]["date"] == datetime.now(TZ).date().isoformat()


async def test_unknown_channel(db):
    with pytest.raises(ChannelError, match="Канал не найден"):
        await channel_stats(db, -100404, 7, TZ)


# ------------------------------------------------------------------ overview

async def test_overview(db):
    await db.upsert_channel(-1001, "A", None, status=CH_ACTIVE, members_count=10)
    await db.upsert_channel(-1002, "B", None, status=CH_ACTIVE, members_count=None)
    await db.upsert_channel(-1003, "C", None, status=CH_PENDING, members_count=1000)
    for status in ("sent", "partial", "scheduled", "draft", "draft", "failed"):
        await db.create_post(text_md="x", status=status)
    assert await overview(db) == {
        "channels_active": 2, "channels_pending": 1, "members_total": 10,
        "posts_sent": 2, "posts_scheduled": 1, "posts_draft": 2,
    }


# ------------------------------------------------------------------ format_stats_html

async def test_format_stats_html(db):
    await make_channel(db)
    for day, count in ((4, 1000), (9, 1200), (12, 1230), (14, 1234)):
        await db.add_member_snapshot(CH, count, ts(day))
    await db.add_member_event(CH, 1, "join", ts(13))
    await db.add_channel_post(CH, 20, via_bot=True, ts=ts(13))
    post_id = await db.create_post(text_md="Лучший <пост>", status="sent")
    await db.set_post_targets(post_id, [CH])
    await db.update_target(post_id, CH, status="sent", message_id=20, sent_at=ts(13))
    await db.set_target_reactions(CH, 20, 7, [])

    text = format_stats_html(await channel_stats(db, CH, 7, TZ, now_ts=NOW_TS), TZ)

    assert text.startswith("📊 <b>Мой &lt;канал&gt;</b> (@chan_name)")
    assert "👥 Подписчики: 1 234 (+234 за 7 дн.)" in text
    assert "➕ Вступили: 1 · ➖ Отписались: 0" in text
    assert "📝 Постов: 1 (через бота: 1)" in text
    assert "❤️ Реакций: 7" in text
    assert '1. <a href="https://t.me/chan_name/20">Лучший &lt;пост&gt;</a> — ❤️ 7' in text
    assert "Статистика вступлений/отписок собирается с момента подключения канала (данные с 04.10.2026)" in text
    assert any(ch in text for ch in "▁▂▃▄▅▆▇█")


async def test_format_stats_html_without_data(db):
    await make_channel(db, username=None)
    text = format_stats_html(await channel_stats(db, CH, 30, TZ, now_ts=NOW_TS), TZ)
    assert text.startswith("📊 <b>Мой &lt;канал&gt;</b>\n")
    assert "👥 Подписчики: нет данных" in text
    assert "🏆" not in text
    assert not any(ch in text for ch in "▁▂▃▄▅▆▇█")


def test_format_stats_html_negative_delta_and_long_sparkline():
    series = [{"date": f"d{i}", "members": 1000 - i, "joins": 0, "leaves": 0, "posts": 0, "posts_bot": 0}
              for i in range(90)]
    stats = {"channel": {"id": CH, "title": "T", "username": None}, "days": 90, "members_now": 911,
             "delta": {"1d": -1, "7d": -7, "30d": -30}, "tracking_since": None, "series": series,
             "joins": 0, "leaves": 89, "posts": 0, "posts_bot": 0, "reactions": 0, "top_posts": []}
    text = format_stats_html(stats, TZ)
    assert "👥 Подписчики: 911 (−7 за 7 дн.)" in text
    spark_line = next(line for line in text.splitlines() if line and line[0] in "▁▂▃▄▅▆▇█")
    assert len(spark_line) == 30
    assert spark_line[0] == "█" and spark_line[-1] == "▁"


async def test_fresh_posts_slightly_ahead_of_the_local_clock_are_counted(db):
    await make_channel(db)
    await db.add_channel_post(CH, 70, via_bot=False, ts=NOW_TS + 2)  # Telegram's clock is a bit ahead
    await db.add_member_event(CH, 7, "join", NOW_TS + 2)
    await db.add_channel_post(CH, 71, via_bot=False, ts=NOW_TS + 3600)  # really in the future
    stats = await channel_stats(db, CH, 1, TZ, now_ts=NOW_TS)
    assert (stats["posts"], stats["joins"]) == (1, 1)

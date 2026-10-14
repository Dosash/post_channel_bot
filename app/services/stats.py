"""Channel statistics: subscriber dynamics, joins/leaves, posts and reactions.

Telegram Bot API exposes no view counts, so statistics are built from what the bot collects
itself: periodic subscriber-count snapshots, `chat_member` join/leave events, `channel_post`
updates and reaction counts of bot-published posts.
"""
from __future__ import annotations

import html
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from app.db import CH_ACTIVE, CH_PENDING, POST_DRAFT, POST_PARTIAL, POST_SCHEDULED, POST_SENT
from app.markdown import excerpt
from app.media import MEDIA_ICONS, describe_media, media_kind, post_media
from app.services.channels import ChannelError
from app.services.posting import message_link

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
MAX_DAYS = 366
SPARK_MAX_POINTS = 30  # keep the bot's sparkline readable on a phone
TOP_POSTS_LIMIT = 5
DELTA_PERIODS = {"1d": 1, "7d": 7, "30d": 30}
DAY = 86400
# Telegram stamps channel events with its own clock: an event a little "in the future" of the local
# clock is a fresh event, not one to skip.
CLOCK_SKEW = 300


def sparkline(values: list[int | None]) -> str:
    """Unicode block sparkline ("▁▂▃▅▇"); None values are skipped, '' for no data."""
    data = [v for v in values if v is not None]
    if not data:
        return ""
    lo, hi = min(data), max(data)
    if lo == hi:
        return SPARK_BLOCKS[len(SPARK_BLOCKS) // 2] * len(data)
    top = len(SPARK_BLOCKS) - 1
    return "".join(SPARK_BLOCKS[round((v - lo) / (hi - lo) * top)] for v in data)


def _downsample(values: list[int], max_points: int) -> list[int]:
    """Keep at most `max_points` values, taking the last value of each equal-sized chunk."""
    if len(values) <= max_points:
        return values
    step = len(values) / max_points
    return [values[min(len(values) - 1, int((i + 1) * step) - 1)] for i in range(max_points)]


def _day_start(day: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, dtime.min, tzinfo=tz).timestamp())


def _day_index(starts: list[int], ts: int) -> int:
    """Index of the day bucket containing ts (starts are sorted day-start timestamps)."""
    return bisect_right(starts, ts) - 1


async def channel_stats(db: Any, chat_id: int, days: int, tz: ZoneInfo, *, now_ts: int | None = None) -> dict:
    """Statistics of one channel for the last `days` days (today included) in timezone `tz`.

    `now_ts` overrides the current time (tests). Raises ChannelError for an unknown channel.
    """
    channel = await db.get_channel(chat_id)
    if channel is None:
        raise ChannelError("Канал не найден")
    days = max(1, min(int(days), MAX_DAYS))
    current = int(now_ts if now_ts is not None else time.time())
    today = datetime.fromtimestamp(current, tz).date()
    day_list = [today - timedelta(days=days - 1 - i) for i in range(days)]
    starts = [_day_start(d, tz) for d in day_list]
    period_start = starts[0]

    series = [
        {"date": d.isoformat(), "members": None, "joins": 0, "leaves": 0, "posts": 0, "posts_bot": 0}
        for d in day_list
    ]
    await _fill_members(db, chat_id, series, starts, current)
    joins, leaves = await _fill_events(db, chat_id, series, starts, current)
    posts, posts_bot = await _fill_posts(db, chat_id, series, starts, current)

    latest = await db.get_member_count_at(chat_id, current)
    members_now = latest if latest is not None else channel.get("members_count")
    delta: dict[str, int | None] = {}
    for key, n_days in DELTA_PERIODS.items():
        past = await db.get_member_count_at(chat_id, current - n_days * DAY)
        delta[key] = members_now - past if members_now is not None and past is not None else None
    first = await db.get_first_snapshot(chat_id)

    return {
        "channel": channel,
        "days": days,
        "period_start": period_start,
        "members_now": members_now,
        "delta": delta,
        "tracking_since": first["ts"] if first else None,
        "series": series,
        "joins": joins,
        "leaves": leaves,
        "posts": posts,
        "posts_bot": posts_bot,
        "reactions": await db.reactions_total(chat_id, period_start),
        "top_posts": await _top_posts(db, channel, period_start),
    }


async def _fill_members(db: Any, chat_id: int, series: list[dict], starts: list[int], current: int) -> None:
    """members per day = last snapshot of the day, carried forward; None before tracking began."""
    last_by_day: dict[int, int] = {}
    for snap in await db.get_member_snapshots(chat_id, since_ts=starts[0]):
        if snap["ts"] <= current:
            last_by_day[_day_index(starts, snap["ts"])] = snap["count"]  # ordered by ts → last wins
    carry = await db.get_member_count_at(chat_id, starts[0] - 1)
    for index, item in enumerate(series):
        carry = last_by_day.get(index, carry)
        item["members"] = carry


async def _fill_events(db: Any, chat_id: int, series: list[dict], starts: list[int], current: int) -> tuple[int, int]:
    joins = leaves = 0
    for event in await db.get_member_events(chat_id, since_ts=starts[0]):
        if event["ts"] > current + CLOCK_SKEW:
            continue
        item = series[_day_index(starts, event["ts"])]
        if event["kind"] == "join":
            item["joins"] += 1
            joins += 1
        elif event["kind"] == "leave":
            item["leaves"] += 1
            leaves += 1
    return joins, leaves


async def _fill_posts(db: Any, chat_id: int, series: list[dict], starts: list[int], current: int) -> tuple[int, int]:
    """Posts per day; the messages of one album (same media_group_id) count as one post."""
    posts = posts_bot = 0
    albums: set[str] = set()
    for row in await db.get_channel_posts(chat_id, since_ts=starts[0]):
        if row["ts"] > current + CLOCK_SKEW:
            continue
        group = row.get("media_group_id")
        if group:
            if group in albums:
                continue
            albums.add(group)
        item = series[_day_index(starts, row["ts"])]
        item["posts"] += 1
        posts += 1
        if row["via_bot"]:
            item["posts_bot"] += 1
            posts_bot += 1
    return posts, posts_bot


async def _top_posts(db: Any, channel: dict, since_ts: int) -> list[dict]:
    top = []
    for row in await db.top_posts_by_reactions(channel["id"], since_ts, TOP_POSTS_LIMIT):
        post = await db.get_post(row["post_id"])
        media = post_media(post) if post is not None else post_media(row)
        text = excerpt(row.get("text_md") or "")
        if not text:
            text = describe_media(media) or "—"
        top.append({
            "post_id": row["post_id"],
            "message_id": row["message_id"],
            "link": message_link(channel, row["message_id"]) if row["message_id"] else None,
            "excerpt": text,
            "media_kind": media_kind(media),
            "reactions_total": row["reactions_total"],
            "reactions": row["reactions"],
            "sent_at": row["sent_at"],
        })
    return top


async def overview(db: Any) -> dict:
    """Headline numbers for the dashboard."""
    active = await db.list_channels([CH_ACTIVE])
    pending = await db.list_channels([CH_PENDING])
    return {
        "channels_active": len(active),
        "channels_pending": len(pending),
        "members_total": sum(ch["members_count"] or 0 for ch in active),
        "posts_sent": await db.count_posts([POST_SENT, POST_PARTIAL]),
        "posts_scheduled": await db.count_posts(POST_SCHEDULED),
        "posts_draft": await db.count_posts(POST_DRAFT),
    }


# ------------------------------------------------------------------ formatting

def _num(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _signed(value: int) -> str:
    return f"+{_num(value)}" if value >= 0 else f"−{_num(-value)}"


def _members_line(stats: dict) -> str:
    members = stats.get("members_now")
    if members is None:
        return "👥 Подписчики: нет данных"
    line = f"👥 Подписчики: {_num(members)}"
    delta = stats.get("delta") or {}
    for key, label in (("7d", "за 7 дн."), ("1d", "за сутки")):
        if delta.get(key) is not None:
            return f"{line} ({_signed(delta[key])} {label})"
    return line


def format_stats_html(stats: dict, tz: ZoneInfo) -> str:
    """Russian HTML block for the bot (parse_mode=HTML)."""
    channel = stats["channel"]
    title = html.escape(channel.get("title") or str(channel.get("id")))
    header = f"📊 <b>{title}</b>"
    if channel.get("username"):
        header += f" (@{html.escape(channel['username'])})"
    lines = [header, f"Период: {stats['days']} дн.", "", _members_line(stats)]

    members = [item["members"] for item in stats.get("series", []) if item["members"] is not None]
    spark = sparkline(_downsample(members, SPARK_MAX_POINTS))
    if len(members) > 1 and spark:
        lines.append(spark)
    lines.append(f"➕ Вступили: {_num(stats['joins'])} · ➖ Отписались: {_num(stats['leaves'])}")
    lines.append(f"📝 Постов: {_num(stats['posts'])} (через бота: {_num(stats['posts_bot'])})")
    lines.append(f"❤️ Реакций: {_num(stats['reactions'])}")

    top = [p for p in stats.get("top_posts", []) if p.get("reactions_total")][:3]
    if top:
        lines += ["", "🏆 Топ постов:"]
        for index, post in enumerate(top, 1):
            text = html.escape(post["excerpt"])
            kind = post.get("media_kind") or "text"
            if kind != "text" and not post["excerpt"].startswith(MEDIA_ICONS.get(kind, "")):
                text = f"{MEDIA_ICONS.get(kind, '')} {text}"
            if post.get("link"):
                text = f'<a href="{html.escape(post["link"], quote=True)}">{text}</a>'
            lines.append(f"{index}. {text} — ❤️ {_num(post['reactions_total'])}")

    note = "Статистика вступлений/отписок собирается с момента подключения канала"
    if stats.get("tracking_since"):
        since = datetime.fromtimestamp(stats["tracking_since"], tz).strftime("%d.%m.%Y")
        note += f" (данные с {since})"
    lines += ["", f"<i>{note}.</i>"]
    return "\n".join(lines)

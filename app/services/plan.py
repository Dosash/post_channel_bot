"""Content plan: weekly publishing slots of channels and the per-channel publication queue.

A channel may have a weekly schedule of *slots* — local wall-clock times per weekday in the bot's
timezone (``channels.slots_json``, ``{weekday 0=Mon..6: ["HH:MM", ...]}``). A post is put into a
channel's queue by giving its target (``post_targets`` row) the next free slot of that channel
(``queued = 1``) or an explicit time (``queued = 0``). The scheduler publishes pending targets whose
``scheduled_at`` has come. Every function that changes target times recomputes the post status
through ``db.refresh_post_schedule``.

Slots text format (bot and web text mode), one rule per line — days, then times::

    пн-пт 09:00, 18:00
    сб, вс 12:00
    ежедневно 21:00

Days: ``пн вт ср чт пт сб вс`` (also ``пон``, ``понедельник``…, English ``mon``…``sun``,
abbreviations with or without a dot), ranges ``пн-пт`` (wrap-around ``пт-пн`` too), lists
``сб, вс``, words ``ежедневно`` / ``каждый день`` / ``все дни``, ``будни``, ``выходные``. A line
with only times means every day. Several rules may share a line (``пн 09:00 вт 10:00``).
Times: ``09:00``, ``9:00``, ``9.00``.

Concurrency: plan changes never touch a post that is being published (``sending``) — the guard is
part of each UPDATE statement, so a post claimed by the scheduler between the check and the write
is left alone. Swaps and queue compaction are written by one statement that applies only if every
row is still in the state it was read in (otherwise "План изменился…").

Lead time: plan edits never give a post a time less than MIN_LEAD (60 s) from now — the scheduler
polls every 15 s, so such a time is due at once. In particular a swap never hands an imminent
time to another post (it would be published right away, maybe days early).
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.db import (
    CH_ACTIVE,
    POST_DELETED,
    POST_SENDING,
    TARGET_DELETED,
    TARGET_FAILED,
    TARGET_SENT,
    now,
)
from app.markdown import excerpt

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

MIN_LEAD = 60  # a queue slot must be at least this many seconds in the future
DEFAULT_HORIZON_DAYS = 400
MAX_PLAN_DAYS = 366
EXCERPT_LIMIT = 50
_CHUNK = 200  # rows per guarded UPDATE (keeps the statement far below SQLite's variable limit)

SLOTS_HELP = (
    "Расписание — по строке на правило: дни и время.\n"
    "пн-пт 09:00, 18:00\n"
    "сб, вс 12:00\n"
    "ежедневно 21:00\n"
    "Дни: пн вт ср чт пт сб вс, диапазоны (пн-пт, пт-пн), списки (сб, вс), "
    "«будни», «выходные», «ежедневно». Строка только со временем — каждый день."
)

POST_NOT_FOUND = "Пост не найден"
SENDING_ERROR = "Пост сейчас публикуется"
DELETED_ERROR = "Пост удалён из каналов"
PAST_ERROR = "Это время уже прошло"
NO_CHANNELS_ERROR = "Выберите хотя бы один канал"
ALL_SENT_ERROR = "Пост уже опубликован в выбранных каналах"
CHANNEL_NOT_FOUND = "Канал не найден"
TARGET_MISSING_ERROR = "Пост не запланирован в этом канале"
TARGET_SENT_ERROR = "Пост уже опубликован в этом канале"
TARGET_DELETED_ERROR = "Пост удалён из этого канала"
TARGET_FAILED_ERROR = "Публикация в этот канал не удалась — запланируйте пост заново"
STALE_ERROR = "План изменился — обновите его и попробуйте ещё раз"
OCCUPIED_ERROR = "Это время уже занято другим постом — выберите другое"
OCCUPANT_SENDING_ERROR = "Пост, стоящий на это время, сейчас публикуется — выберите другое время"
NEIGHBOUR_SENDING_ERROR = "Соседний пост сейчас публикуется — попробуйте чуть позже"
DUE_ERROR = "Время публикации уже наступило — пост вот-вот выйдет"
TOO_SOON_ERROR = "Это время слишком близко — выберите время хотя бы через минуту"
IMMINENT_ERROR = "Пост выйдет меньше чем через минуту — переставлять его уже поздно"
SWAP_SOON_ERROR = ("Пост вот-вот выйдет — поменять его местами с другим нельзя: тот вышел бы сразу. "
                   "Выберите свободное время")
DIRECTION_ERROR = "Неверное направление"
BAD_SLOTS_ERROR = "Неверный формат расписания"
MEDIA_FALLBACK = "🖼 Медиа"
EMPTY_POST = "(без текста)"


class PlanError(Exception):
    """Content plan operation failed. The message is Russian and user-presentable."""


# ------------------------------------------------------------------ slots: parsing

_DAY_NAMES: tuple[tuple[str, ...], ...] = (
    ("пн", "пон", "понед", "понедельник", "понедельника", "понедельникам",
     "mon", "monday", "mondays", "mo"),
    ("вт", "вто", "втор", "вторник", "вторника", "вторникам", "tue", "tues", "tuesday", "tuesdays", "tu"),
    ("ср", "сре", "сред", "среда", "среду", "среды", "средам", "wed", "wednesday", "wednesdays", "we"),
    ("чт", "чет", "четв", "четверг", "четверга", "четвергам",
     "thu", "thur", "thurs", "thursday", "thursdays", "th"),
    ("пт", "пят", "пятн", "пятница", "пятницу", "пятницы", "пятницам", "fri", "friday", "fridays", "fr"),
    ("сб", "суб", "субб", "суббота", "субботу", "субботы", "субботам",
     "sat", "saturday", "saturdays", "sa"),
    ("вс", "вос", "воск", "воскр", "воскресенье", "воскресенья", "воскресеньям",
     "sun", "sunday", "sundays", "su"),
)
_DAY_ALIASES: dict[str, int] = {name: idx for idx, names in enumerate(_DAY_NAMES) for name in names}
_GROUP_ALIASES: dict[str, tuple[int, ...]] = {
    **dict.fromkeys(("ежедневно", "ежедн", "daily", "everyday", "всегда"), tuple(range(7))),
    **dict.fromkeys(("будни", "будням", "будние", "weekdays", "workdays"), (0, 1, 2, 3, 4)),
    **dict.fromkeys(("выходные", "выходным", "weekend", "weekends"), (5, 6)),
}
_FILLERS = frozenset({"в", "во", "и", "по", "с", "со", "at", "on", "and", "from"})
_RANGE_WORDS = frozenset({"по", "до", "to", "through", "till", "until"})
_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), repl)
    for pattern, repl in (
        (r"\b(?:каждый|кажд\.?)\s+день\b", " ежедневно "),
        (r"\bвсе\s+дни\b", " ежедневно "),
        (r"\bevery\s*day\b", " ежедневно "),
        (r"\b(?:по\s+)?будн(?:ие|им)\s+дн(?:и|ям)\b", " будни "),
        (r"\b(?:по\s+)?выходн(?:ые|ым)\s+дн(?:и|ям)\b", " выходные "),
    )
)
_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")
_TIME_LIKE_RE = re.compile(r"\d+[:.]\d+")
_TOKEN_SPLIT_RE = re.compile(r"[\s,/+&]+")
_DASHES_RE = re.compile(r"\s*[-–—−]\s*")


def _parse_time(token: str) -> str | None:
    """'9:00' / '09:00' / '9.00' → '09:00'; None when the token is not a valid time."""
    match = _TIME_RE.fullmatch(token.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _word(token: str) -> str:
    return token.lower().replace("ё", "е").rstrip(".:")


def _single_day(token: str) -> int | None:
    """'пн' / 'Пн.' / 'понедельник' / 'mon' → 0; None if not a day name."""
    return _DAY_ALIASES.get(_word(token))


def _day_word(token: str) -> tuple[int, ...] | None:
    """A single day word or group word → weekday numbers; None if unknown."""
    day = _single_day(token)
    return (day,) if day is not None else _GROUP_ALIASES.get(_word(token))


def _day_range(first: int, last: int) -> tuple[int, ...]:
    """Inclusive weekday range, wrapping over Sunday ('пт-пн' → Fri, Sat, Sun, Mon)."""
    return tuple((first + i) % 7 for i in range((last - first) % 7 + 1))


def _classify(token: str, lineno: int) -> tuple[str, Any]:
    """Token → ('time', 'HH:MM') | ('days', (weekdays…)) | ('range', None) — a range word that
    must stand between two days | ('range-or-filler', None) — 'по' | ('filler', None).
    """
    raw = token.strip()
    time_value = _parse_time(raw) or _parse_time(raw.rstrip(".:"))
    if time_value:
        return "time", time_value
    if _TIME_LIKE_RE.fullmatch(raw.rstrip(".:")):
        raise PlanError(f"Строка {lineno}: не понял время «{raw}»")
    lowered = raw.lower().replace("ё", "е")
    if lowered in _RANGE_WORDS and lowered not in _FILLERS:
        return "range", None
    if lowered in _RANGE_WORDS:
        return "range-or-filler", None
    if lowered in _FILLERS:
        return "filler", None
    days = _day_word(raw)
    if days is not None:
        return "days", days
    if "-" in raw:
        parts = raw.split("-")
        if len(parts) == 2:
            first, last = _single_day(parts[0]), _single_day(parts[1])
            if first is not None and last is not None:
                return "days", _day_range(first, last)
            if _parse_time(parts[0]) and _parse_time(parts[1].rstrip(".:")):
                raise PlanError(
                    f"Строка {lineno}: интервалы времени («{raw}») не поддерживаются — "
                    "перечислите время через запятую, например 09:00, 18:00"
                )
        raise PlanError(f"Строка {lineno}: не понял «{raw}»")
    if raw.isdigit():
        raise PlanError(f"Строка {lineno}: не понял время «{raw}» — пишите ЧЧ:ММ, например 09:00")
    raise PlanError(
        f"Строка {lineno}: не понял «{raw}» — нужны дни недели (пн-пт, сб, вс…) и время (09:00)"
    )


def _tokens(segment: str, lineno: int) -> list[tuple[str, Any, str]]:
    """Classified tokens of one rule segment: (kind, value, source text)."""
    text = segment.replace("ё", "е").replace("Ё", "Е").strip().lstrip("-–—•*·").strip()
    for pattern, repl in _PHRASES:
        text = pattern.sub(repl, text)
    text = _DASHES_RE.sub("-", text)
    raw_tokens = [t for t in _TOKEN_SPLIT_RE.split(text) if t and t not in (".", ":", "-")]
    classified = [(*_classify(tok, lineno), tok) for tok in raw_tokens]
    # 'с пн по пт' / 'mon to fri' → a range; 'по средам' → filler
    result: list[tuple[str, Any, str]] = []
    i = 0
    while i < len(classified):
        kind, value, src = classified[i]
        if kind in ("range", "range-or-filler"):
            prev = result[-1] if result else None
            nxt = classified[i + 1] if i + 1 < len(classified) else None
            if (
                prev is not None and prev[0] == "days" and len(prev[1]) == 1
                and nxt is not None and nxt[0] == "days" and len(nxt[1]) == 1
            ):
                result[-1] = ("days", _day_range(prev[1][0], nxt[1][0]), f"{prev[2]}-{nxt[2]}")
                i += 2
                continue
            if kind == "range":
                raise PlanError(f"Строка {lineno}: не понял «{src}» — пример диапазона: пн-пт")
            i += 1
            continue
        if kind != "filler":
            result.append((kind, value, src))
        i += 1
    return result


def _parse_segment(segment: str, lineno: int) -> list[tuple[set[int], set[str]]]:
    """One rule segment → [(days, times)]. Runs alternate between day tokens and time tokens."""
    tokens = _tokens(segment, lineno)
    if not tokens:
        return []
    runs: list[tuple[str, list[tuple[Any, str]]]] = []
    for kind, value, src in tokens:
        if runs and runs[-1][0] == kind:
            runs[-1][1].append((value, src))
        else:
            runs.append((kind, [(value, src)]))

    def days_of(run: list[tuple[Any, str]]) -> set[int]:
        return {day for value, _ in run for day in value}

    def times_of(run: list[tuple[Any, str]]) -> set[str]:
        return {value for value, _ in run}

    if len(runs) == 1:
        kind, run = runs[0]
        if kind == "time":
            return [(set(range(7)), times_of(run))]
        raise PlanError(f"Строка {lineno}: не указано время — например «{run[0][1]} 09:00»")
    if len(runs) % 2:
        if runs[0][0] == "days":
            last = runs[-1][1][-1][1]
            raise PlanError(f"Строка {lineno}: после «{last}» не указано время")
        raise PlanError(
            f"Строка {lineno}: непонятно, к каким дням относится время «{runs[0][1][0][1]}» — "
            "пишите сначала дни, потом время"
        )
    rules = []
    for idx in range(0, len(runs), 2):
        (kind_a, run_a), (_, run_b) = runs[idx], runs[idx + 1]
        days_run, times_run = (run_a, run_b) if kind_a == "days" else (run_b, run_a)
        rules.append((days_of(days_run), times_of(times_run)))
    return rules


def parse_slots(text: str) -> dict[int, list[str]]:
    """Parse the slots text format (see the module docstring) into normalized slots.

    Later lines add to earlier ones; empty text → {} (no schedule). Errors → PlanError with the
    line number, e.g. 'Строка 2: не понял время «25:00»'.
    """
    result: dict[int, set[str]] = {}
    for lineno, line in enumerate((text or "").splitlines(), 1):
        for segment in line.split(";"):
            for days, times in _parse_segment(segment, lineno):
                for day in days:
                    result.setdefault(day, set()).update(times)
    return {day: sorted(result[day]) for day in sorted(result) if result[day]}


# ------------------------------------------------------------------ slots: normalizing / text

def _norm_day(key: object) -> int:
    if isinstance(key, bool):
        raise PlanError(f"Неверный день недели: «{key}»")
    if isinstance(key, int):
        day = key
    else:
        text = str(key).strip()
        if text.lstrip("-").isdigit():
            day = int(text)
        else:
            single = _single_day(text)
            if single is None:
                raise PlanError(f"Неверный день недели: «{key}»")
            day = single
    if not 0 <= day <= 6:
        raise PlanError(f"Неверный день недели: «{key}»")
    return day


def _norm_times(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        items: Iterable[object] = [t for t in re.split(r"[\s,;]+", value) if t]
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        raise PlanError(BAD_SLOTS_ERROR)
    times = set()
    for item in items:
        parsed = _parse_time(item) if isinstance(item, str) else None
        if parsed is None:
            raise PlanError(f"Неверное время «{item}» — нужно ЧЧ:ММ, например 09:00")
        times.add(parsed)
    return times


def normalize_slots(obj: object) -> dict[int, list[str]]:
    """Validate slots from JSON/DB: ``{"0": ["9:00"], …}`` (keys str or int, also day names; a
    list of 7 time lists is accepted too) → ``{0: ["09:00"], …}`` with only non-empty days,
    times sorted and unique. None/empty → {}. Invalid data → PlanError.
    """
    if obj is None or obj == "":
        return {}
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            raise PlanError(BAD_SLOTS_ERROR) from None
        if obj is None:
            return {}
    if isinstance(obj, (list, tuple)):
        if len(obj) > 7:
            raise PlanError(BAD_SLOTS_ERROR)
        pairs: Iterable[tuple[object, object]] = enumerate(obj)
    elif isinstance(obj, dict):
        pairs = obj.items()
    else:
        raise PlanError(BAD_SLOTS_ERROR)
    result: dict[int, set[str]] = {}
    for key, value in pairs:
        result.setdefault(_norm_day(key), set()).update(_norm_times(value))
    return {day: sorted(result[day]) for day in sorted(result) if result[day]}


def _days_text(days: list[int]) -> str:
    """Sorted weekday numbers → 'ежедневно' | 'пн-пт' | 'сб, вс' | 'пн, ср-пт'."""
    if len(days) == 7:
        return "ежедневно"
    runs: list[list[int]] = []
    for day in days:
        if runs and runs[-1][-1] == day - 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    parts = []
    for run in runs:
        names = [WEEKDAYS[d].lower() for d in run]
        parts.append(f"{names[0]}-{names[-1]}" if len(run) >= 3 else ", ".join(names))
    return ", ".join(parts)


def slots_to_text(slots: object) -> str:
    """Canonical compact text of slots ('пн-пт 09:00, 18:00\\nсб, вс 12:00'); '' if empty.

    Days with identical time lists share a line; ``parse_slots(slots_to_text(x)) == x``.
    """
    norm = normalize_slots(slots)
    groups: dict[tuple[str, ...], list[int]] = {}
    for day in sorted(norm):
        groups.setdefault(tuple(norm[day]), []).append(day)
    return "\n".join(f"{_days_text(days)} {', '.join(times)}" for times, days in groups.items())


def describe_slots(slots: object) -> str:
    """One-line Russian summary: 'пн-пт 09:00, 18:00 · сб, вс 12:00' or 'не настроено'."""
    try:
        text = slots_to_text(slots)
    except PlanError:
        text = ""
    return " · ".join(text.splitlines()) if text else "не настроено"


# ------------------------------------------------------------------ slots: time arithmetic

def _zone(tz: tzinfo | str | None) -> tzinfo:
    if tz is None:
        return ZoneInfo("UTC")
    return ZoneInfo(tz) if isinstance(tz, str) else tz


def _local_ts(day: date, hour: int, minute: int, zone: tzinfo) -> int | None:
    """Unix time of a local wall-clock time; None when it does not exist (DST gap).

    Ambiguous times (DST fall-back) resolve to their first occurrence (fold=0).
    """
    naive = datetime(day.year, day.month, day.day, hour, minute)
    ts = naive.replace(tzinfo=zone, fold=0).timestamp()
    if datetime.fromtimestamp(ts, zone).replace(tzinfo=None) != naive:
        return None
    return int(ts)


def iter_slot_times(
    slots: object, tz: tzinfo | str, after_ts: int, *, horizon_days: int = DEFAULT_HORIZON_DAYS
) -> Iterator[int]:
    """Ascending unix timestamps of slot occurrences strictly after `after_ts`.

    Local dates from the date of `after_ts` up to `horizon_days` days later are scanned; slot
    times that do not exist on a day (DST gap) are skipped, ambiguous ones taken once (first).
    """
    norm = normalize_slots(slots)
    if not norm:
        return
    zone = _zone(tz)
    parsed = {day: [(int(t[:2]), int(t[3:])) for t in times] for day, times in norm.items()}
    start = datetime.fromtimestamp(after_ts, zone).date()
    for offset in range(max(0, int(horizon_days)) + 1):
        day = start + timedelta(days=offset)
        times = parsed.get(day.weekday())
        if not times:
            continue
        stamps = {_local_ts(day, hour, minute, zone) for hour, minute in times}
        yield from sorted(ts for ts in stamps if ts is not None and ts > after_ts)


def slot_label(ts: int, tz: tzinfo | str) -> str:
    """Short label of a plan time for buttons: 'Пн 14.10 09:00'."""
    moment = datetime.fromtimestamp(ts, _zone(tz))
    return f"{WEEKDAYS[moment.weekday()]} {moment:%d.%m %H:%M}"


def _day_start(day: date, zone: tzinfo) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=zone).timestamp())


def _clamp_days(days: object) -> int:
    try:
        value = int(days)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        value = 7
    return max(1, min(value, MAX_PLAN_DAYS))


# ------------------------------------------------------------------ helpers: posts / targets

def _title(channel: dict | None, channel_id: int | None = None) -> str:
    if channel:
        if channel.get("title"):
            return channel["title"]
        if channel.get("username"):
            return f"@{channel['username']}"
        channel_id = channel.get("id", channel_id)
    return str(channel_id)


def _names_error(channels: list[dict], one: str, many: str) -> PlanError:
    names = ", ".join(f"«{_title(ch)}»" for ch in channels)
    return PlanError((one if len(channels) == 1 else many).format(names))


def _media_kind(media: list[dict]) -> str:
    try:
        from app.media import media_kind
    except ImportError:
        media_kind = None
    if media_kind is not None:
        try:
            return media_kind(media)
        except ValueError:
            pass
    if not media:
        return "text"
    return "album" if len(media) > 1 else (media[0].get("type") or "photo")


def _describe_media(media: list[dict]) -> str:
    if not media:
        return ""
    try:
        from app.media import describe_media
    except ImportError:
        return MEDIA_FALLBACK
    try:
        return describe_media(media) or MEDIA_FALLBACK
    except ValueError:
        return MEDIA_FALLBACK


def post_summary(post: dict, *, queued: bool = False) -> dict:
    """Compact post info for plan items.

    Accepts a post (``db.get_post``) or a row of ``db.get_scheduled_targets`` (post_id,
    post_status). Returns {id, excerpt, media_kind, status, queued, has_buttons, media_count,
    thumb_type}; excerpt falls back to the media description when the text is empty.
    """
    media = post.get("media") or []
    buttons = post.get("buttons") or []
    text = excerpt(post.get("text_md") or "", EXCERPT_LIMIT)
    return {
        "id": post.get("post_id", post.get("id")),
        "excerpt": text or _describe_media(media) or EMPTY_POST,
        "media_kind": _media_kind(media),
        "status": post.get("post_status", post.get("status")),
        "queued": bool(queued),
        "has_buttons": any(bool(row) for row in buttons),
        "media_count": len(media),
        "thumb_type": media[0].get("type") if media else None,
    }


async def _editable_post(db: Any, post_id: int) -> dict:
    """The post if its schedule may be changed; PlanError otherwise."""
    post = await db.get_post(int(post_id))
    if post is None:
        raise PlanError(POST_NOT_FOUND)
    if post["status"] == POST_SENDING:
        raise PlanError(SENDING_ERROR)
    if post["status"] == POST_DELETED:
        raise PlanError(DELETED_ERROR)
    return post


def _pending_target(post: dict, channel_id: int) -> dict:
    target = next((t for t in post["targets"] if t["channel_id"] == int(channel_id)), None)
    if target is None:
        raise PlanError(TARGET_MISSING_ERROR)
    if target["status"] == TARGET_SENT:
        raise PlanError(TARGET_SENT_ERROR)
    if target["status"] == TARGET_DELETED:
        raise PlanError(TARGET_DELETED_ERROR)
    if target["status"] == TARGET_FAILED:
        raise PlanError(TARGET_FAILED_ERROR)
    return target


async def _raise_for_failed_write(db: Any, post_id: int) -> None:
    """A guarded write changed nothing: explain why."""
    post = await db.get_post(post_id)
    if post is None:
        raise PlanError(POST_NOT_FOUND)
    if post["status"] == POST_SENDING:
        raise PlanError(SENDING_ERROR)
    if post["status"] == POST_DELETED:
        raise PlanError(DELETED_ERROR)
    raise PlanError(STALE_ERROR)


def _clean_ids(channel_ids: Iterable[object] | None) -> list[int]:
    try:
        return list(dict.fromkeys(int(c) for c in (channel_ids or [])))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise PlanError(CHANNEL_NOT_FOUND) from None


async def _channels_to_schedule(db: Any, post: dict, channel_ids: Iterable[object]) -> list[dict]:
    """Active channel rows of `channel_ids` the post can still be published to.

    Channels where the post is already sent/deleted are skipped; unknown or inactive channels →
    PlanError (nothing is changed then).
    """
    ids = _clean_ids(channel_ids)
    if not ids:
        raise PlanError(NO_CHANNELS_ERROR)
    done = {t["channel_id"] for t in post["targets"] if t["status"] in (TARGET_SENT, TARGET_DELETED)}
    todo = [cid for cid in ids if cid not in done]
    if not todo:
        raise PlanError(ALL_SENT_ERROR)
    channels, inactive = [], []
    for cid in todo:
        channel = await db.get_channel(cid)
        if channel is None:
            raise PlanError(CHANNEL_NOT_FOUND)
        (channels if channel["status"] == CH_ACTIVE else inactive).append(channel)
    if inactive:
        raise _names_error(
            inactive, "Канал {} недоступен для публикации", "Каналы {} недоступны для публикации"
        )
    return channels


async def _upsert_targets(db: Any, post_id: int, times: dict[int, int], *, queued: bool) -> None:
    """Make the post's targets in these channels pending with the given times — in one statement
    that does nothing if the post is being sent or deleted. Sent/deleted targets stay untouched.
    """
    values = ", ".join("(?, ?)" for _ in times)
    params: list[Any] = [post_id, int(queued)]
    for cid, ts in times.items():
        params += [cid, ts]
    params.append(post_id)
    cur = await db.execute(
        "INSERT INTO post_targets(post_id, channel_id, status, scheduled_at, queued) "
        f"SELECT ?, v.column1, 'pending', v.column2, ? FROM (VALUES {values}) AS v "
        "WHERE EXISTS (SELECT 1 FROM posts p WHERE p.id = ? AND p.status NOT IN ('sending', 'deleted')) "
        "ON CONFLICT(post_id, channel_id) DO UPDATE SET status = 'pending', error = NULL, "
        "scheduled_at = excluded.scheduled_at, queued = excluded.queued "
        "WHERE post_targets.status IN ('pending', 'failed')",
        params,
    )
    if cur.rowcount == 0:
        await _raise_for_failed_write(db, post_id)


async def _apply_times(
    db: Any, channel_id: int, changes: list[tuple[int, int | None, int | None, bool]]
) -> None:
    """Set times of several targets of one channel: (post_id, expected_old_ts, new_ts, queued).

    Written by one UPDATE per chunk that applies only when every row is still pending, still has
    the expected time and its post is not being sent/deleted — all or nothing (PlanError otherwise).
    """
    for start in range(0, len(changes), _CHUNK):
        chunk = changes[start:start + _CHUNK]
        when = " ".join("WHEN ? THEN ?" for _ in chunk)
        params: list[Any] = []
        for post_id, _, new_ts, _ in chunk:
            params += [post_id, new_ts]
        for post_id, _, new_ts, queued in chunk:
            params += [post_id, int(bool(queued) and new_ts is not None)]
        params.append(channel_id)
        params += [post_id for post_id, *_ in chunk]
        params.append(channel_id)
        for post_id, old_ts, _, _ in chunk:
            params += [post_id, old_ts]
        params.append(len(chunk))
        cur = await db.execute(
            f"UPDATE post_targets SET scheduled_at = CASE post_id {when} END, "
            f"queued = CASE post_id {when} END "
            f"WHERE channel_id = ? AND post_id IN ({','.join('?' * len(chunk))}) AND ("
            "SELECT COUNT(*) FROM post_targets t JOIN posts p ON p.id = t.post_id "
            "WHERE t.channel_id = ? AND t.status = 'pending' AND p.status NOT IN ('sending', 'deleted') "
            f"AND ({' OR '.join('(t.post_id = ? AND t.scheduled_at IS ?)' for _ in chunk)})) = ?",
            params,
        )
        if cur.rowcount != len(chunk):
            if len(chunk) == 1:
                await _raise_for_failed_write(db, chunk[0][0])
            raise PlanError(STALE_ERROR)


async def _refresh(db: Any, post_ids: Iterable[int]) -> None:
    for post_id in dict.fromkeys(post_ids):
        await db.refresh_post_schedule(post_id)


async def _occupied(db: Any, channel_id: int, from_ts: int, exclude: tuple[int, int] | None) -> set[int]:
    if exclude is None or int(exclude[1]) != int(channel_id):
        return await db.occupied_times(channel_id, from_ts)
    rows = await db.fetchall(
        "SELECT scheduled_at FROM post_targets WHERE channel_id = ? AND status = 'pending' "
        "AND scheduled_at IS NOT NULL AND scheduled_at >= ? AND post_id != ?",
        (channel_id, from_ts, int(exclude[0])),
    )
    return {int(r["scheduled_at"]) for r in rows}


# ------------------------------------------------------------------ queue

async def next_free_slots(
    db: Any,
    channel_id: int,
    tz: tzinfo | str,
    count: int = 1,
    *,
    after_ts: int | None = None,
    exclude: tuple[int, int] | None = None,
) -> list[int]:
    """The channel's next `count` free slot times (ascending; fewer if the horizon runs out,
    [] without a schedule).

    Free = no pending target of this channel has exactly that time. `after_ts` defaults to
    now + 60 s. `exclude` = (post_id, channel_id): that target's own time does not count as taken
    (used when a post that is already queued is queued again).
    """
    if count <= 0:
        return []
    slots = await db.get_channel_slots(channel_id)
    if not slots:
        return []
    after = int(after_ts) if after_ts is not None else now() + MIN_LEAD
    occupied = await _occupied(db, channel_id, after, exclude)
    found: list[int] = []
    for ts in iter_slot_times(slots, tz, after, horizon_days=DEFAULT_HORIZON_DAYS):
        if ts not in occupied:
            found.append(ts)
            if len(found) >= count:
                break
    return found


async def queue_post(db: Any, post_id: int, channel_ids: list[int], tz: tzinfo | str) -> dict[int, int]:
    """Queue the post in every given channel at that channel's next free slot.

    Targets become pending (failed ones too, missing ones are created) with ``queued = 1``; other
    targets of the post are not touched; channels where the post is already sent are skipped.
    Returns {channel_id: ts}. PlanError (nothing changed) when the post is being sent / deleted,
    a channel is unknown / inactive, has no schedule («У канала «X» не настроено расписание
    публикаций») or no free slot within the horizon.
    """
    post = await _editable_post(db, post_id)
    channels = await _channels_to_schedule(db, post, channel_ids)
    no_slots = [ch for ch in channels if not await db.get_channel_slots(ch["id"])]
    if no_slots:
        raise _names_error(
            no_slots,
            "У канала {} не настроено расписание публикаций",
            "У каналов {} не настроено расписание публикаций",
        )
    times: dict[int, int] = {}
    full = []
    for channel in channels:
        found = await next_free_slots(db, channel["id"], tz, 1, exclude=(post["id"], channel["id"]))
        if found:
            times[channel["id"]] = found[0]
        else:
            full.append(channel)
    if full:
        raise _names_error(
            full,
            "В расписании канала {} нет свободного времени на ближайший год",
            "В расписании каналов {} нет свободного времени на ближайший год",
        )
    await _upsert_targets(db, post["id"], times, queued=True)
    await db.refresh_post_schedule(post["id"])
    return times


async def schedule_post_at(db: Any, post_id: int, channel_ids: list[int], ts: int) -> None:
    """Schedule the post at an explicit time `ts` in every given channel (``queued = 0``).

    Same rules as queue_post; ts must be in the future (PlanError «Это время уже прошло»).
    """
    post = await _editable_post(db, post_id)
    ts = int(ts)
    if ts <= now():
        raise PlanError(PAST_ERROR)
    channels = await _channels_to_schedule(db, post, channel_ids)
    await _upsert_targets(db, post["id"], {ch["id"]: ts for ch in channels}, queued=False)
    await db.refresh_post_schedule(post["id"])


async def unschedule_post(db: Any, post_id: int) -> None:
    """Remove the time of every pending target; the post becomes a draft (or stays partial/sent)."""
    post = await _editable_post(db, post_id)
    cur = await db.execute(
        "UPDATE post_targets SET scheduled_at = NULL, queued = 0 "
        "WHERE post_id = ? AND status = 'pending' AND (scheduled_at IS NOT NULL OR queued != 0) "
        "AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.id = post_targets.post_id "
        "AND p.status IN ('sending', 'deleted'))",
        (post["id"],),
    )
    if cur.rowcount == 0:
        fresh = await db.get_post(post["id"])
        if fresh is not None and fresh["status"] == POST_SENDING:
            raise PlanError(SENDING_ERROR)
    await db.refresh_post_schedule(post["id"])


async def refresh_post_schedule(db: Any, post_id: int) -> dict:
    """Recompute posts.status / scheduled_at from the targets (see db.refresh_post_schedule)."""
    post = await db.refresh_post_schedule(int(post_id))
    if post is None:
        raise PlanError(POST_NOT_FOUND)
    return post


async def preview_queue(
    db: Any, channel_ids: Iterable[int], tz: tzinfo | str, *, post_id: int | None = None
) -> dict[int, int | None]:
    """Next free slot per channel without changing anything (None = no schedule / no free slot).

    With `post_id` the post's own current times do not count as taken (what queue_post would do).
    """
    result: dict[int, int | None] = {}
    for cid in _clean_ids(channel_ids):
        exclude = (int(post_id), cid) if post_id is not None else None
        found = await next_free_slots(db, cid, tz, 1, exclude=exclude)
        result[cid] = found[0] if found else None
    return result


# ------------------------------------------------------------------ plan views

def _item_time(ts: int, zone: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, zone)


async def channel_plan(
    db: Any, channel_id: int, tz: tzinfo | str, *, days: int = 14, start_ts: int | None = None
) -> dict:
    """The channel's plan for `days` local days starting with the day of `start_ts` (default today).

    {"channel", "slots", "slots_text", "slots_summary", "days": [{"date": 'YYYY-MM-DD',
    "weekday": 'Пн', "items": [{"ts", "time": 'HH:MM', "slot": bool, "post": None | summary}]}]}.
    Items are the union of slot occurrences (free ones only when still usable: > now + 60 s) and
    pending scheduled targets; a target at a slot time is one item with slot=True and its post.
    """
    zone = _zone(tz)
    channel = await db.get_channel(int(channel_id))
    if channel is None:
        raise PlanError(CHANNEL_NOT_FOUND)
    days = _clamp_days(days)
    current = now()
    first = _item_time(int(start_ts) if start_ts is not None else current, zone).date()
    dates = [first + timedelta(days=i) for i in range(days)]
    lo = _day_start(first, zone) - 86400
    hi = _day_start(first + timedelta(days=days), zone) + 86400
    slots = normalize_slots(await db.get_channel_slots(channel["id"]))
    slot_times = set()
    for ts in iter_slot_times(slots, zone, lo - 1, horizon_days=days + 3):
        if ts >= hi:
            break
        slot_times.add(ts)

    items: dict[date, list[dict]] = {d: [] for d in dates}
    taken: set[int] = set()
    for row in await db.get_scheduled_targets(channel_ids=[channel["id"]], from_ts=lo, until_ts=hi):
        ts = int(row["scheduled_at"])
        moment = _item_time(ts, zone)
        if moment.date() not in items:
            continue
        taken.add(ts)
        items[moment.date()].append({
            "ts": ts,
            "time": f"{moment:%H:%M}",
            "slot": ts in slot_times,
            "post": post_summary(row, queued=row["queued"]),
        })
    for ts in slot_times:
        if ts in taken or ts <= current + MIN_LEAD:
            continue
        moment = _item_time(ts, zone)
        if moment.date() in items:
            items[moment.date()].append({"ts": ts, "time": f"{moment:%H:%M}", "slot": True, "post": None})
    for day_items in items.values():
        day_items.sort(key=lambda it: (it["ts"], it["post"] is None, (it["post"] or {}).get("id") or 0))
    return {
        "channel": channel,
        "slots": slots,
        "slots_text": slots_to_text(slots),
        "slots_summary": describe_slots(slots),
        "days": [
            {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "items": items[d]}
            for d in dates
        ],
    }


async def timeline(
    db: Any,
    tz: tzinfo | str,
    *,
    days: int = 7,
    folder_id: int | None = None,
    channel_ids: Iterable[int] | None = None,
    start_ts: int | None = None,
) -> list[dict]:
    """All pending scheduled targets in `days` local days from the day of `start_ts` (default
    today) across channels, sorted by time: [{"ts", "time", "date", "weekday", "channel": {id,
    title, username, folder_id}, "post": summary}].

    `folder_id` limits to the channels of a folder (0 = channels without a folder); `channel_ids`
    to the given channels.
    """
    zone = _zone(tz)
    days = _clamp_days(days)
    first = _item_time(int(start_ts) if start_ts is not None else now(), zone).date()
    last = first + timedelta(days=days)
    ids = _clean_ids(channel_ids) if channel_ids is not None else None
    rows = await db.get_scheduled_targets(
        channel_ids=ids, from_ts=_day_start(first, zone) - 86400, until_ts=_day_start(last, zone) + 86400
    )
    wanted_folder = None if folder_id is None or int(folder_id) == 0 else int(folder_id)
    result = []
    for row in rows:
        if folder_id is not None and row["channel_folder_id"] != wanted_folder:
            continue
        ts = int(row["scheduled_at"])
        moment = _item_time(ts, zone)
        if not first <= moment.date() < last:
            continue
        result.append({
            "ts": ts,
            "time": f"{moment:%H:%M}",
            "date": moment.date().isoformat(),
            "weekday": WEEKDAYS[moment.weekday()],
            "channel": {
                "id": row["channel_id"],
                "title": row["channel_title"] or "",
                "username": row["channel_username"],
                "folder_id": row["channel_folder_id"],
            },
            "post": post_summary(row, queued=row["queued"]),
        })
    result.sort(key=lambda it: (
        it["ts"], it["channel"]["title"].casefold(), it["channel"]["id"], it["post"]["id"],
    ))
    return result


# ------------------------------------------------------------------ plan editing

async def move_target(
    db: Any, post_id: int, channel_id: int, new_ts: int, *, queued: bool | None = None
) -> None:
    """Move the post's pending target in a channel to `new_ts` (at least MIN_LEAD s from now).

    If another pending target of the channel stands at `new_ts`, the two swap times (the queued
    flag travels with the time) — refused when the moved post's own time is less than MIN_LEAD
    away (the other post would go out at once). `queued` sets the flag of the moved target
    explicitly (e.g. False for «Перенести» to an arbitrary time, True for a drop onto a slot);
    None keeps / swaps it.
    """
    post = await _editable_post(db, post_id)
    channel_id = int(channel_id)
    target = _pending_target(post, channel_id)
    new_ts = int(new_ts)
    current = now()
    if new_ts <= current:
        raise PlanError(PAST_ERROR)
    if new_ts < current + MIN_LEAD:
        raise PlanError(TOO_SOON_ERROR)
    old_ts, own_queued = target["scheduled_at"], bool(target["queued"])
    if old_ts == new_ts:
        if queued is not None and bool(queued) != own_queued:
            await _apply_times(db, channel_id, [(post["id"], old_ts, old_ts, bool(queued))])
            await _refresh(db, [post["id"]])
        return
    occupant = await db.fetchone(
        "SELECT t.post_id, t.queued, p.status AS post_status FROM post_targets t "
        "JOIN posts p ON p.id = t.post_id WHERE t.channel_id = ? AND t.status = 'pending' "
        "AND t.scheduled_at = ? AND t.post_id != ? ORDER BY t.post_id LIMIT 1",
        (channel_id, new_ts, post["id"]),
    )
    if occupant is None:
        flag = own_queued if queued is None else bool(queued)
        await _apply_times(db, channel_id, [(post["id"], old_ts, new_ts, flag)])
        await _refresh(db, [post["id"]])
        return
    if occupant["post_status"] in (POST_SENDING, POST_DELETED):
        raise PlanError(OCCUPANT_SENDING_ERROR)
    if old_ts is None or old_ts <= current:
        raise PlanError(OCCUPIED_ERROR)
    if old_ts < current + MIN_LEAD:
        raise PlanError(SWAP_SOON_ERROR)
    flag = bool(occupant["queued"]) if queued is None else bool(queued)
    await _apply_times(db, channel_id, [
        (post["id"], old_ts, new_ts, flag),
        (occupant["post_id"], new_ts, old_ts, own_queued),
    ])
    await _refresh(db, [post["id"], occupant["post_id"]])


async def shift_target(db: Any, post_id: int, channel_id: int, direction: int) -> bool:
    """Swap the target's time with the previous (-1) / next (+1) future pending scheduled target
    of the same channel (any post). False when there is no such neighbour. Times less than
    MIN_LEAD s away take no part: such a post cannot be shifted (DUE_ERROR / IMMINENT_ERROR) and
    is no neighbour.
    """
    if not direction:
        raise PlanError(DIRECTION_ERROR)
    post = await _editable_post(db, post_id)
    channel_id = int(channel_id)
    target = _pending_target(post, channel_id)
    own_ts = target["scheduled_at"]
    if own_ts is None:
        raise PlanError(TARGET_MISSING_ERROR)
    current = now()
    if own_ts <= current:
        raise PlanError(DUE_ERROR)
    if own_ts < current + MIN_LEAD:
        raise PlanError(IMMINENT_ERROR)
    if direction < 0:
        condition, order = "t.scheduled_at < ? AND t.scheduled_at >= ?", "t.scheduled_at DESC, t.post_id DESC"
        params: tuple[Any, ...] = (own_ts, current + MIN_LEAD)
    else:
        condition, order = "t.scheduled_at > ?", "t.scheduled_at, t.post_id"
        params = (own_ts,)
    neighbour = await db.fetchone(
        "SELECT t.post_id, t.scheduled_at, t.queued, p.status AS post_status FROM post_targets t "
        "JOIN posts p ON p.id = t.post_id WHERE t.channel_id = ? AND t.status = 'pending' "
        f"AND t.post_id != ? AND {condition} ORDER BY {order} LIMIT 1",
        (channel_id, post["id"], *params),
    )
    if neighbour is None:
        return False
    if neighbour["post_status"] in (POST_SENDING, POST_DELETED):
        raise PlanError(NEIGHBOUR_SENDING_ERROR)
    their_ts = int(neighbour["scheduled_at"])
    await _apply_times(db, channel_id, [
        (post["id"], own_ts, their_ts, bool(neighbour["queued"])),
        (neighbour["post_id"], their_ts, own_ts, bool(target["queued"])),
    ])
    await _refresh(db, [post["id"], neighbour["post_id"]])
    return True


async def compact_queue(db: Any, channel_id: int, tz: tzinfo | str) -> int:
    """Re-assign the channel's queued (queued=1) future pending targets to the earliest free slots
    from now + 60 s, preserving their order. Explicit-time targets stay and keep their times
    occupied. Returns how many targets got a new time.
    """
    channel_id = int(channel_id)
    changes = await _compact_changes(db, channel_id, tz)
    if not changes:
        return 0
    await _apply_times(db, channel_id, changes)
    await _refresh(db, [c[0] for c in changes])
    return len(changes)


async def compact_preview(db: Any, channel_id: int, tz: tzinfo | str) -> tuple[int, int | None]:
    """What `compact_queue` would do, without changing anything: (how many posts get a new time,
    the earliest new time or None). Same PlanError as compact_queue."""
    changes = await _compact_changes(db, int(channel_id), tz)
    return len(changes), (min(new for _, _, new, _ in changes) if changes else None)


async def _compact_changes(db: Any, channel_id: int, tz: tzinfo | str) -> list[tuple[int, int, int, bool]]:
    """The (post_id, old_ts, new_ts, queued) moves of a queue compaction (see compact_queue)."""
    channel = await db.get_channel(channel_id)
    if channel is None:
        raise PlanError(CHANNEL_NOT_FOUND)
    slots = await db.get_channel_slots(channel_id)
    if not slots:
        raise _names_error([channel], "У канала {} не настроено расписание публикаций", "")
    after = now() + MIN_LEAD
    rows = await db.fetchall(
        "SELECT t.post_id, t.scheduled_at, t.queued, p.status AS post_status FROM post_targets t "
        "JOIN posts p ON p.id = t.post_id WHERE t.channel_id = ? AND t.status = 'pending' "
        "AND t.scheduled_at IS NOT NULL AND t.scheduled_at > ? ORDER BY t.scheduled_at, t.post_id",
        (channel_id, after),
    )
    movable = [r for r in rows if r["queued"] and r["post_status"] not in (POST_SENDING, POST_DELETED)]
    if not movable:
        return []
    movable_ids = {r["post_id"] for r in movable}
    taken = {int(r["scheduled_at"]) for r in rows if r["post_id"] not in movable_ids}
    horizon = (int(movable[-1]["scheduled_at"]) - after) // 86400 + DEFAULT_HORIZON_DAYS
    head = movable
    while True:
        free: list[int] = []
        for ts in iter_slot_times(slots, tz, after, horizon_days=horizon):
            if ts not in taken:
                free.append(ts)
                if len(free) == len(head):
                    break
        if len(free) == len(head):
            break
        # Too few slots (very sparse schedule): the tail keeps its times, nobody may land on them.
        taken |= {int(r["scheduled_at"]) for r in head[len(free):]}
        head = head[:len(free)]
    return [
        (row["post_id"], int(row["scheduled_at"]), ts, True)
        for row, ts in zip(head, free)
        if ts != row["scheduled_at"]
    ]


async def unqueue_target(db: Any, post_id: int, channel_id: int) -> None:
    """Take the post out of one channel's plan: the target stays selected but loses its time."""
    post = await _editable_post(db, post_id)
    channel_id = int(channel_id)
    target = _pending_target(post, channel_id)
    if target["scheduled_at"] is not None or target["queued"]:
        await _apply_times(db, channel_id, [(post["id"], target["scheduled_at"], None, False)])
    await _refresh(db, [post["id"]])

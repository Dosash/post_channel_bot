"""Scheduled posts: parsing human time input and the background loops.

`Scheduler` runs two asyncio tasks: one publishes due post targets every `poll_interval` seconds
(each channel of a post may have its own time — V2 content plan), the other stores
subscriber-count snapshots every `snapshot_interval` seconds (immediately at start, then
periodically). A failing iteration is logged and the loop goes on. Stopping lets a publishing run
in progress finish instead of cutting it mid-send; `request_stop()` (at the start of a graceful
shutdown) makes the loops pick no new post right away. `Scheduler.status()` reports the loops'
health (for ``/api/health``).

Only targets with their own time are published: a post whose time lives on the post alone (V1)
is converted once by the database migration, never per tick — a per-tick conversion could undo
«Снять с расписания» landing between its two writes and publish the post.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import LinkPreviewOptions

from app.db import POST_FAILED, POST_PARTIAL, POST_SCHEDULED, POST_SENT, TARGET_PENDING, now
from app.services import avatars, channels
from app.services.posting import (
    NO_TARGETS_ERROR,
    PostError,
    PostNotDue,
    PublishReport,
    notify_admins,
    publish_post,
)

log = logging.getLogger(__name__)

HELP_ERROR = "Не понял время. Примеры: 18:30, 25.12 10:00, +2h, через 30 минут"
PAST_ERROR = "Это время уже прошло"
INVALID_ERROR = "Некорректная дата или время"

# How far in the past a date without a year may be before it is read as next year's date
# ("01.01 10:00" typed on 31.12 means the coming January).
_NEXT_YEAR_THRESHOLD = timedelta(days=31)

_UNIT_MINUTES = {"d": 1440, "h": 60, "m": 1}
_UNIT_ALIASES = {
    "d": "d", "д": "d", "дн": "d", "день": "d", "дня": "d", "дней": "d", "сутки": "d", "суток": "d",
    "h": "h", "ч": "h", "час": "h", "часа": "h", "часов": "h",
    "m": "m", "м": "m", "мин": "m", "минута": "m", "минуту": "m", "минуты": "m", "минут": "m",
}
_UNIT_RE = "|".join(sorted(map(re.escape, _UNIT_ALIASES), key=len, reverse=True))
_REL_PART = re.compile(rf"(\d+)?\s*({_UNIT_RE})(?![а-яa-z])")
_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")
_HHMM = re.compile(r"(\d{1,2}):(\d{2})")
_DMY = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ t](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?")
_DAY_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def parse_when(text: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Parse a publication time typed by a user into a tz-aware datetime in the future.

    Accepts 'HH:MM' (today, or tomorrow if already passed), 'DD.MM HH:MM', 'DD.MM.YYYY HH:MM',
    'YYYY-MM-DD HH:MM', '+30m', '+2h', '+1d', '+1h30m', 'через 2 часа', 'через 15 минут',
    'завтра HH:MM' (also 'сегодня' / 'послезавтра'). Raises ValueError with a Russian message.
    """
    if now is None:
        current = datetime.now(tz)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=tz)
    else:
        current = now.astimezone(tz)
    s =" ".join((text or "").strip().lower().replace("ё", "е").split())
    s = re.sub(r"\bв\s+(?=\d)", "", s)  # "завтра в 10:00" → "завтра 10:00"
    if not s:
        raise ValueError(HELP_ERROR)
    try:
        result = _parse(s, tz, current)
    except (ValueError, OverflowError) as exc:
        if str(exc) in (HELP_ERROR, PAST_ERROR, INVALID_ERROR):
            raise
        raise ValueError(INVALID_ERROR) from exc
    if result is None:
        raise ValueError(HELP_ERROR)
    if result <= current:
        raise ValueError(PAST_ERROR)
    return result


def _parse(s: str, tz: ZoneInfo, current: datetime) -> datetime | None:
    if s.startswith("+"):
        return _relative(s[1:], current, allow_bare_number=True)
    if s.startswith("через "):
        return _relative(s[len("через "):], current, allow_bare_number=False)
    if m := _ISO.fullmatch(s):
        y, mo, d, hh, mm = map(int, m.groups())
        return _at(date(y, mo, d), hh, mm, tz)
    words = s.split(" ")
    if words[0] in _DAY_WORDS and len(words) == 2 and (m := _TIME_RE.fullmatch(words[1])):
        day = current.date() + timedelta(days=_DAY_WORDS[words[0]])
        return _at(day, int(m.group(1)), int(m.group(2)), tz)
    if len(words) == 1 and (m := _HHMM.fullmatch(s)):
        result = _at(current.date(), int(m.group(1)), int(m.group(2)), tz)
        return result if result > current else _at(current.date() + timedelta(days=1), *_hm(m), tz)
    if len(words) == 2:
        return _date_and_time(words, tz, current)
    return None


def _hm(m: re.Match[str]) -> tuple[int, int]:
    return int(m.group(1)), int(m.group(2))


def _date_and_time(words: list[str], tz: ZoneInfo, current: datetime) -> datetime | None:
    """'DD.MM[.YYYY] HH:MM' or 'HH:MM DD.MM[.YYYY]'."""
    for date_part, time_part in ((words[0], words[1]), (words[1], words[0])):
        dm, tm = _DMY.fullmatch(date_part), _HHMM.fullmatch(time_part)
        if not (dm and tm):
            continue
        day, month, year = int(dm.group(1)), int(dm.group(2)), dm.group(3)
        if year is not None:
            full_year = int(year) + (2000 if len(year) == 2 else 0)
            return _at(date(full_year, month, day), *_hm(tm), tz)
        result = _at(date(current.year, month, day), *_hm(tm), tz)
        if current - result > _NEXT_YEAR_THRESHOLD:
            result = _at(date(current.year + 1, month, day), *_hm(tm), tz)
        return result
    return None


def _at(day: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(INVALID_ERROR)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def _relative(s: str, current: datetime, *, allow_bare_number: bool) -> datetime | None:
    s = s.strip().replace(" и ", " ")
    if allow_bare_number and s.isdigit():
        return (current + timedelta(minutes=int(s))).replace(microsecond=0)
    if s == "полчаса":
        return (current + timedelta(minutes=30)).replace(microsecond=0)
    minutes = 0
    pos = 0
    for m in _REL_PART.finditer(s):
        if s[pos:m.start()].strip():
            return None
        amount = int(m.group(1)) if m.group(1) else 1
        minutes += amount * _UNIT_MINUTES[_UNIT_ALIASES[m.group(2)]]
        pos = m.end()
    if pos == 0 or s[pos:].strip() or minutes <= 0:
        return None
    return (current + timedelta(minutes=minutes)).replace(microsecond=0)


def format_dt(ts: int, tz: ZoneInfo) -> str:
    """Unix seconds → 'DD.MM.YYYY HH:MM' in tz."""
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M")


@dataclass(frozen=True)
class SchedulerStatus:
    running: bool
    last_ok_ts: int | None  # end of the last publishing iteration that completed without an exception
    last_error: str | None  # "<loop>: <Exception>: <message>" of a loop whose last iteration raised
    last_error_ts: int | None
    telegram_down: bool  # the last publishing run found Telegram unreachable (due posts are waiting)


_ERROR_TEXT_LIMIT = 300
REPORT_BUDGET = 3500  # characters of a publish report in a notice (a Telegram message holds 4096)


class Scheduler:
    """Background publishing of scheduled posts and periodic subscriber snapshots."""

    def __init__(self, bot: Any, db: Any, config: Any, *, poll_interval: float = 15,
                 snapshot_interval: float = 3600, stop_timeout: float = 60):
        self.bot = bot
        self.db = db
        self.config = config
        self.poll_interval = poll_interval
        self.snapshot_interval = snapshot_interval
        self.stop_timeout = stop_timeout
        self._tasks: list[asyncio.Task] = []
        self._busy: set[str] = set()  # loops whose job is running right now
        self._closing = False
        self._telegram_down = False
        self._last_ok_ts: int | None = None
        self._errors: dict[str, tuple[str, int]] = {}  # loop name -> (error text, ts) of its last iteration

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    def status(self) -> SchedulerStatus:
        error = self._errors.get("posts") or self._errors.get("snapshots")
        return SchedulerStatus(
            running=self.running and not self._closing,
            last_ok_ts=self._last_ok_ts,
            last_error=error[0] if error else None,
            last_error_ts=error[1] if error else None,
            telegram_down=self._telegram_down,
        )

    def start(self) -> None:
        """Spawn the loops (no-op if already running). Must be called inside a running event loop."""
        if self.running:
            return
        self._closing = False
        self._tasks = [
            asyncio.create_task(self._loop("posts", self.run_due_posts, self.poll_interval),
                                name="scheduler-posts"),
            asyncio.create_task(self._loop("snapshots", self._take_snapshots, self.snapshot_interval),
                                name="scheduler-snapshots"),
        ]

    def request_stop(self) -> None:
        """Begin stopping without waiting: no new post is picked (a post being published finishes);
        `stop()` then waits for the current run and cancels the loops."""
        self._closing = True

    async def stop(self, timeout: float | None = None) -> None:
        """Stop the loops. A publishing run in progress is not cut mid-send: no new post is picked,
        and the current one may finish (up to `timeout`, default `stop_timeout` seconds)."""
        self._closing = True
        tasks, self._tasks = self._tasks, []
        posts_task = next((t for t in tasks if t.get_name() == "scheduler-posts"), None)
        if posts_task is not None and "posts" in self._busy and not posts_task.done():
            limit = self.stop_timeout if timeout is None else timeout
            log.info("Жду завершения публикации отложенных постов (до %s с)…", limit)
            await asyncio.wait({posts_task}, timeout=limit)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self, name: str, job: Callable[[], Awaitable[Any]], interval: float) -> None:
        while not self._closing:
            self._busy.add(name)
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Scheduler job %r failed", name)
                self._errors[name] = (f"{name}: {type(exc).__name__}: {exc}"[:_ERROR_TEXT_LIMIT], now())
            else:
                self._errors.pop(name, None)
                if name == "posts":
                    self._last_ok_ts = now()
            finally:
                self._busy.discard(name)
            if self._closing:
                return
            await asyncio.sleep(interval)

    async def _take_snapshots(self) -> None:
        # Half an interval: a restart right after a pass does not ask Telegram for every channel again
        # (dozens of getChatMemberCount at once hit flood control), the regular hourly pass always runs.
        await channels.take_snapshots(self.bot, self.db, min_age=self.snapshot_interval / 2)
        # Avatars for the panel: each channel once a day, never in the way of the snapshots.
        try:
            await avatars.sync_all(self.bot, self.db, self.config.uploads_dir)
        except Exception:
            log.warning("Avatar sync failed", exc_info=True)

    async def run_due_posts(self) -> int:
        """Publish the targets whose time has come, post by post. Returns the number of due posts
        handled (a post cancelled or moved meanwhile is skipped silently)."""
        ts = now()
        handled = 0
        for post in await self.db.fail_orphan_scheduled(NO_TARGETS_ERROR, ts):
            await self._notify(post, f"❌ Не удалось опубликовать отложенный пост #{post['id']}\n"
                                     f"{html.escape(NO_TARGETS_ERROR)}")
            handled += 1
        due = await self.db.get_due_targets(ts)
        post_ids = list(dict.fromkeys(row["post_id"] for row in due))
        if self._closing:
            return handled
        if post_ids and not await self._telegram_reachable():
            return handled  # the posts stay due and go out once the connection is back
        for post_id in post_ids:
            if self._closing:
                break
            await self._publish_due(post_id)
            handled += 1
        return handled

    async def _telegram_reachable(self) -> bool:
        """One getMe before a publishing run: during a network / Telegram outage due posts must
        wait instead of being sent into errors and marked failed."""
        try:
            await self.bot.get_me()
        except Exception as exc:
            if not self._telegram_down:
                log.warning("Telegram недоступен (%s) — отложенные посты выйдут, когда связь восстановится", exc)
            self._telegram_down = True
            return False
        if self._telegram_down:
            log.info("Связь с Telegram восстановлена — публикую отложенные посты")
        self._telegram_down = False
        return True

    async def _publish_due(self, post_id: int) -> None:
        post = await self.db.get_post(post_id)
        if post is None:
            return
        if post["status"] in (POST_PARTIAL, POST_FAILED, POST_SENT):
            # A due pending target on a post that is not 'scheduled' (e.g. a channel queued after
            # the post was published elsewhere): the V2 status rules make it 'scheduled' again.
            post = await self.db.refresh_post_schedule(post_id) or post
        ts = now()
        try:
            report = await publish_post(self.bot, self.db, self.config, post_id, only_due=True, now_ts=ts)
        except PostNotDue as exc:
            log.info("Scheduled post %s skipped: %s", post_id, exc)
            return
        except PostError as exc:
            await self._fail_due(post, str(exc), ts)
            return
        except Exception as exc:
            log.exception("Publishing scheduled post %s failed", post_id)
            await self._fail_due(post, f"Внутренняя ошибка: {exc}", ts)
            return
        text = _report_text(report)
        fresh = await self.db.get_post(post_id)
        if fresh and fresh["status"] == POST_SCHEDULED and fresh.get("scheduled_at"):
            text += (f"\n⏰ Остальные каналы — по расписанию, следующая публикация "
                     f"{format_dt(fresh['scheduled_at'], self.config.tz)}")
        await self._notify(post, text)

    async def _fail_due(self, post: dict, error: str, ts: int) -> None:
        """The due part of a post cannot be published (render error, missing file…): mark only its
        due targets failed (other channels stay queued) and tell the admins. Atomic and conditional:
        a post cancelled, rescheduled or taken by someone else meanwhile is left alone."""
        due_titles = [t.get("channel_title") or str(t["channel_id"]) for t in post["targets"]
                      if t["status"] == TARGET_PENDING and t.get("scheduled_at") and t["scheduled_at"] <= ts]
        if not await self.db.fail_due_targets(post["id"], error, ts):
            log.info("Scheduled post %s skipped: %s", post["id"], error)
            return
        await self.db.refresh_post_schedule(post["id"])
        await self.db.update_post(post["id"], error=error)
        text = f"❌ Не удалось опубликовать отложенный пост #{post['id']}\n{html.escape(error)}"
        if due_titles:
            text += "\nКаналы: " + _titles_text(due_titles)
        await self._notify(post, text)

    async def _notify(self, post: dict, text: str) -> None:
        creator = post.get("created_by")
        if creator and await self.db.is_admin(creator):
            try:
                await self.bot.send_message(chat_id=creator, text=text, parse_mode="HTML",
                                            link_preview_options=LinkPreviewOptions(is_disabled=True))
                return
            except Exception:
                log.warning("Could not notify post creator %s, falling back to all admins", creator,
                            exc_info=True)
        await notify_admins(self.bot, self.db, text)


def _report_text(report: PublishReport) -> str:
    header = "⏰ Отложенный пост опубликован" if report.ok_count else "⏰ Отложенный пост не удалось опубликовать"
    return f"{header}\n{report.summary_html(REPORT_BUDGET)}"


def _titles_text(titles: list[str], limit: int = 1500) -> str:
    """«A», «B», … (escaped) cut to about `limit` characters, so a notice fits one message."""
    text = ", ".join(f"«{title}»" for title in titles)
    if len(text) > limit:
        text = text[: limit - 1].rsplit(", ", 1)[0] + f" … (всего {len(titles)})"
    return html.escape(text, quote=False)

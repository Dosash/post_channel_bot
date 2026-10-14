"""Entry point robustness (`app/__main__.py`): start-up while Telegram is unreachable (review #2),
graceful shutdown that lets running publishes finish (review #3), interrupted-publish notices."""
from __future__ import annotations

import asyncio
import time

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.methods import GetMe
from aiogram.types import User
from aiohttp import web

from app.db import CH_ACTIVE, Database
from app.services import posting
from tests.fakes import FakeBot

main_module = pytest.importorskip("app.__main__", reason="app.bot / app.web not importable", exc_type=ImportError)

CH = -1001000000001


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=GetMe(), message="Cannot connect to host api.telegram.org")


class FakeSession:
    def __init__(self, events: list[str]):
        self.events = events
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.events.append("session_close")


class StartupBot(FakeBot):
    """FakeBot with aiogram's cached `me()` scripted by `outcomes` (exception or None = success)."""

    def __init__(self, events: list[str], outcomes: list | None = None):
        super().__init__()
        self.events = events
        self.outcomes = list(outcomes or [])
        self.session = FakeSession(events)
        self._me: User | None = None

    async def me(self) -> User:
        if self._me is not None:
            return self._me
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            self.events.append("me_fail")
            raise outcome
        if callable(outcome):
            return await outcome()
        self.events.append("me_ok")
        self._me = User(id=self.id, is_bot=True, first_name="Test", username=self.username)
        return self._me


class StubDispatcher:
    def __init__(self, db, events: list[str], polling=None):
        self.db = db
        self.events = events
        self.polling = polling
        self.data: dict = {}  # workflow data (dp["token_state"] …)
        self._handle_update_tasks: set[asyncio.Task] = set()

    def __setitem__(self, key, value) -> None:
        self.data[key] = value

    def __getitem__(self, key):
        return self.data[key]

    def resolve_used_update_types(self) -> list[str]:
        return ["message"]

    async def start_polling(self, bot, **kwargs) -> None:
        self.events.append("polling")
        self.polling_kwargs = kwargs
        if self.polling is not None:
            await self.polling(self, bot)


@pytest.fixture
def harness(monkeypatch, config):
    """Patch the outside world of `run()`: no real Bot, no TCP port, no aiogram polling."""
    events: list[str] = []
    state: dict = {"outcomes": [], "polling": None}

    def make_bot(token, default=None):
        state["bot"] = StartupBot(events, state["outcomes"])
        return state["bot"]

    def make_dispatcher(db, config_, scheduler=None):
        state["dp"] = StubDispatcher(db, events, state["polling"])
        state["scheduler"] = scheduler
        return state["dp"]

    class StubSite:
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            events.append("site_start")

    original_start = main_module.Scheduler.start

    def scheduler_start(self):
        events.append("scheduler_start")
        original_start(self)

    monkeypatch.setattr(main_module, "Bot", make_bot)
    monkeypatch.setattr(main_module, "create_dispatcher", make_dispatcher)
    monkeypatch.setattr(main_module, "create_web_app", lambda bot, db, config_, scheduler=None: web.Application())
    monkeypatch.setattr(main_module.web, "TCPSite", StubSite)
    monkeypatch.setattr(main_module.Scheduler, "start", scheduler_start)
    monkeypatch.setattr(main_module, "TELEGRAM_RETRY_START", 0.01)
    monkeypatch.setattr(main_module, "COMMANDS_RETRY_DELAY", 0.01)
    monkeypatch.setattr(main_module, "SHUTDOWN_TIMEOUT", 5.0)
    state["events"] = events
    state["config"] = config
    return state


async def test_startup_survives_unreachable_telegram(harness):
    """Regression (review #2): getMe failing at boot is retried; the panel and scheduler run meanwhile."""
    harness["outcomes"] = [network_error(), network_error()]

    assert await main_module.run(harness["config"]) == 0

    events = harness["events"]
    assert events[:6] == ["scheduler_start", "site_start", "me_fail", "me_fail", "me_ok", "polling"]
    assert harness["dp"].polling_kwargs["close_bot_session"] is False
    assert harness["bot"].session.closed
    assert not harness["scheduler"].running  # stopped on exit


async def test_startup_wrong_token_exits_with_error(harness):
    harness["outcomes"] = [TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")]
    assert await main_module.run(harness["config"]) == 1
    assert "polling" not in harness["events"]
    assert harness["bot"].session.closed


async def test_stop_requested_while_waiting_for_telegram(harness, monkeypatch):
    captured = {}
    original = main_module._install_stop_signals

    def capture(stop, on_signal=None):
        captured["signal"] = on_signal or stop.set
        original(stop, on_signal)

    async def fail_and_stop():
        captured["signal"]()  # SIGTERM while Telegram is still unreachable
        raise network_error()

    monkeypatch.setattr(main_module, "_install_stop_signals", capture)
    harness["outcomes"] = [fail_and_stop]
    assert await main_module.run(harness["config"]) == 0
    assert "polling" not in harness["events"]


async def test_shutdown_waits_for_publish_started_from_bot(harness, monkeypatch):
    """Regression (review #3): a publish still running when polling stops completes before the DB
    and the bot session are closed — no post left 'sending', no target left 'pending'."""
    config = harness["config"]
    setup = Database(config.db_path)
    await setup.connect()
    await setup.upsert_channel(CH, "Канал", "chan", status=CH_ACTIVE, rights={"can_post": True})
    post_id = await setup.create_post(text_md="Пост", created_by=1)
    await setup.set_post_targets(post_id, [CH])
    await setup.close()

    async def polling(dp, bot):
        bot.add_chat(CH, "Канал", "chan")
        original = bot.send_message

        async def slow_send(**kwargs):
            await asyncio.sleep(0.2)
            return await original(**kwargs)

        bot.send_message = slow_send
        task = asyncio.create_task(posting.publish_post(bot, dp.db, config, post_id))  # «✅ Да, опубликовать»
        dp._handle_update_tasks.add(task)
        await asyncio.sleep(0.01)  # SIGTERM arrives right after the click

    harness["polling"] = polling
    assert await main_module.run(config) == 0

    check = Database(config.db_path)
    await check.connect()
    try:
        post = await check.get_post(post_id)
        assert post["status"] == "sent" and post["targets"][0]["status"] == "sent"
    finally:
        await check.close()
    events = harness["events"]
    assert events.index("polling") < events.index("session_close")


async def test_interrupted_publications_are_reported_after_start(harness):
    config = harness["config"]
    setup = Database(config.db_path)
    await setup.connect()
    post_id = await setup.create_post(text_md="Прерван", created_by=1)
    await setup.set_post_targets(post_id, [CH])
    await setup.update_target(post_id, CH, sending_at=1)  # was in flight when the process died
    await setup.update_post(post_id, status="sending")
    await setup.close()

    async def polling(dp, bot):
        for _ in range(200):
            if bot.calls_of("send_message"):
                break
            await asyncio.sleep(0.01)

    harness["polling"] = polling
    assert await main_module.run(config) == 0
    bot = harness["bot"]
    assert bot.calls_of("set_my_commands")
    (notice,) = bot.calls_of("send_message")
    assert notice["chat_id"] == 1
    assert "Публикация была прервана перезапуском бота" in notice["text"]
    assert f"Пост #{post_id} — не опубликован" in notice["text"]
    check = Database(config.db_path)
    await check.connect()
    try:
        target = (await check.get_targets(post_id))[0]
        assert target["status"] == "failed" and target["error"] == posting.INTERRUPTED_ERROR
    finally:
        await check.close()


# ------------------------------------------------------------------ review2 #7: shutdown

async def test_scheduler_starts_no_publish_while_draining(harness, monkeypatch):
    """Regression (review2 #7): while drain() waited for a publish started from the bot, the
    scheduler kept polling and published a post that became due meanwhile."""
    config = harness["config"]
    setup = Database(config.db_path)
    await setup.connect()
    await setup.add_admin(1, is_owner=True)
    await setup.upsert_channel(CH, "Канал", "chan", status=CH_ACTIVE, rights={"can_post": True})
    clicked = await setup.create_post(text_md="Из бота", created_by=1)
    await setup.set_post_targets(clicked, [CH])
    due_soon = await setup.create_post(text_md="По расписанию", created_by=1)
    await setup.set_post_targets(due_soon, [CH])
    # Due 1–2 s from now: always after SIGTERM (0.05 s), always before the slow publish ends (2.2 s).
    await setup.set_target_schedule(due_soon, CH, int(time.time()) + 2, queued=True)
    await setup.refresh_post_schedule(due_soon)
    await setup.close()

    original_init = main_module.Scheduler.__init__

    def fast_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.poll_interval = 0.02

    monkeypatch.setattr(main_module.Scheduler, "__init__", fast_init)

    async def polling(dp, bot):
        bot.add_chat(CH, "Канал", "chan")
        original = bot.send_message

        async def slow_send(**kwargs):
            if kwargs.get("text") == "Из бота":
                await asyncio.sleep(2.2)  # the scheduled post becomes due meanwhile
            return await original(**kwargs)

        bot.send_message = slow_send
        task = asyncio.create_task(posting.publish_post(bot, dp.db, config, clicked))
        dp._handle_update_tasks.add(task)
        await asyncio.sleep(0.05)  # SIGTERM

    harness["polling"] = polling
    assert await main_module.run(config) == 0

    check = Database(config.db_path)
    await check.connect()
    try:
        assert (await check.get_post(clicked))["status"] == "sent"
        post = await check.get_post(due_soon)
        assert post["status"] == "scheduled" and post["targets"][0]["status"] == "pending"
    finally:
        await check.close()
    assert [c["text"] for c in harness["bot"].calls_of("send_message") if c["chat_id"] == CH] == ["Из бота"]


async def test_shutdown_budget_cancels_a_stuck_publish_while_the_db_is_open(harness, monkeypatch):
    """The whole graceful stop shares one budget; a publish still stuck when it is spent is
    cancelled before the DB closes, so it is recorded as interrupted (not left 'sending')."""
    monkeypatch.setattr(main_module, "SHUTDOWN_TIMEOUT", 0.3)
    monkeypatch.setattr(main_module, "CANCEL_GRACE", 0.5)
    monkeypatch.setattr(main_module, "FINAL_GRACE", 0.1)
    config = harness["config"]
    setup = Database(config.db_path)
    await setup.connect()
    await setup.upsert_channel(CH, "Канал", "chan", status=CH_ACTIVE, rights={"can_post": True})
    post_id = await setup.create_post(text_md="Завис", created_by=1)
    await setup.set_post_targets(post_id, [CH])
    await setup.close()

    async def polling(dp, bot):
        bot.add_chat(CH, "Канал", "chan")

        async def stuck_send(**kwargs):
            await asyncio.sleep(3600)

        bot.send_message = stuck_send
        dp._handle_update_tasks.add(asyncio.create_task(posting.publish_post(bot, dp.db, config, post_id)))
        await asyncio.sleep(0.05)

    harness["polling"] = polling
    started = time.monotonic()
    assert await main_module.run(config) == 0
    assert time.monotonic() - started < 3

    check = Database(config.db_path)
    await check.connect()
    try:
        post = await check.get_post(post_id)
        assert post["status"] == "failed"
        assert post["targets"][0]["error"] == posting.INTERRUPTED_ERROR
    finally:
        await check.close()


def test_shutdown_budget_is_shared():
    budget = main_module.ShutdownBudget(10)
    first = budget.remaining()
    assert 9.5 < first <= 10
    budget.start()  # already running: not restarted
    assert budget.remaining() <= first
    assert main_module.ShutdownBudget(0).remaining() == 0


def test_compose_grace_period_covers_the_shutdown_budget():
    import re
    from pathlib import Path

    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r"stop_grace_period:\s*(\d+)s", compose)
    assert match, "docker-compose.yml: post-bot needs stop_grace_period"
    budget = main_module.SHUTDOWN_TIMEOUT + 2 * main_module.CANCEL_GRACE + main_module.FINAL_GRACE
    assert int(match.group(1)) >= budget + 10


def test_interrupted_text_for_a_still_scheduled_post():
    text = main_module._interrupted_text([{"id": 3, "status": "scheduled", "created_by": 1, "interrupted": True}])
    assert "Пост #3 — публикация в один из каналов прервана — проверьте его" in text


# ------------------------------------------------------------------ helpers

async def test_wait_for_telegram_backoff_and_retry_after(monkeypatch):
    events: list[str] = []
    retry = TelegramRetryAfter(method=GetMe(), message="Too Many Requests", retry_after=0)
    bot = StartupBot(events, [network_error(), retry, network_error()])
    pauses = []
    original_sleep = asyncio.sleep

    async def fake_sleep(seconds, *args, **kwargs):
        pauses.append(seconds)
        await original_sleep(0)

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    me = await main_module.wait_for_telegram(bot, initial_delay=1, max_delay=3)
    assert me.username == "test_bot"
    assert pauses == [1, 0.0, 3]  # 1 s, flood-control pause from Telegram, then backoff capped at 3 s
    assert await bot.me() is me  # cached for aiogram's polling


async def test_wait_for_telegram_stops_while_request_hangs():
    stop = asyncio.Event()

    async def hang():
        await asyncio.sleep(3600)

    bot = StartupBot([], [hang])
    asyncio.get_running_loop().call_later(0.02, stop.set)
    assert await asyncio.wait_for(main_module.wait_for_telegram(bot, stop), 5) is None


async def test_wait_for_telegram_wrong_token():
    bot = StartupBot([], [TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")])
    with pytest.raises(TelegramUnauthorizedError):
        await main_module.wait_for_telegram(bot)


async def test_after_connect_retries_commands(db):
    await db.add_admin(1, is_owner=True)
    bot = StartupBot([])
    attempts = []

    def flaky(kwargs):
        attempts.append(1)
        return network_error() if len(attempts) < 3 else None

    bot.fail_on["set_my_commands"] = flaky
    await main_module.after_connect(bot, db, [{"id": 5, "status": "partial", "created_by": 1, "interrupted": True}],
                                    retry_delay=0.001)
    assert len(attempts) == 3
    (notice,) = bot.calls_of("send_message")
    assert "Пост #5 — опубликован не во все каналы" in notice["text"]


async def test_drain_waits_for_handlers_and_operations():
    dp = StubDispatcher(None, [])
    done = []

    async def handler():
        await asyncio.sleep(0.05)
        done.append(1)

    task = asyncio.create_task(handler())
    dp._handle_update_tasks.add(task)
    assert await main_module.drain(dp, 5) is True
    assert done == [1]

    slow = asyncio.create_task(asyncio.sleep(3600))
    dp._handle_update_tasks = {slow}
    assert await main_module.drain(dp, 0.02) is False
    slow.cancel()


# ------------------------------------------------------------------ V2.1: password warnings, version

async def test_open_panel_warning_respects_web_password(db, config, caplog):
    """The «open without password» warning knows about WEB_PASSWORD from .env."""
    await db.migrate()
    config.web_host = "0.0.0.0"
    with caplog.at_level("WARNING", logger="app"):
        await main_module.warn_about_password(db, config)
        assert "открыта без пароля" in caplog.text
        caplog.clear()

        config.web_password = "env-secret-1"
        await main_module.warn_about_password(db, config)
        assert caplog.text == ""

        config.web_password = "x" * 300
        await main_module.warn_about_password(db, config)
        assert "WEB_PASSWORD длиннее" in caplog.text and "открыта без пароля" not in caplog.text
        caplog.clear()

        config.web_password = "abc"
        await main_module.warn_about_password(db, config)
        assert "WEB_PASSWORD короче" in caplog.text
        caplog.clear()

        config.web_password = None
        config.web_host = "127.0.0.1"  # local only: no warning
        await main_module.warn_about_password(db, config)
        assert caplog.text == ""


async def test_startup_logs_panel_url_with_prefix(harness, caplog):
    config = harness["config"]
    config.web_prefix = "/posts"
    with caplog.at_level("INFO", logger="app"):
        assert await main_module.run(config) == 0
    assert "Веб-панель: http://127.0.0.1:8080/posts/" in caplog.text

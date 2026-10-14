"""SPEC_V22 — changing the publishing bot by its token from the web panel.

Token resolution at start-up (panel token → .env fallback), the in-process restart loop, the
settings API (``/api/settings``, ``/api/settings/bot-token``), masking, the channel re-check after
a bot change, the per-bot file_id rules (`file_bot`) in publishing / previews / the composer, and
the bot's «⚙️ Настройки» text. Telegram is never called: bots are FakeBot-like stubs or aiogram
Bots over tests.mocked_session, the token checker is stubbed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import socket

import aiohttp
import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe, SendMessage, SendPhoto
from aiogram.types import FSInputFile, User
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.bot.handlers import compose as compose_handlers
from app.bot.handlers import settings as settings_handlers
from app.config import (
    BOT_ID_SETTING,
    BOT_TOKEN_SETTING,
    TokenState,
    is_bot_token,
    mask_token,
    redact_tokens,
)
from app.db import CH_ACTIVE, CH_LEFT, CH_PENDING, Database
from app.services import posting
from app.services.posting import FOREIGN_MEDIA_ERROR, PostError, edit_published_post, publish_post, send_post
from app.web import api_bot, api_media, create_web_app
from app.web.keys import DB_KEY, RESTART_KEY, TOKEN_CHECKER_KEY, TOKEN_STATE_KEY
from tests.fakes import BOT_ID, FakeBot, bad_request
from tests.mocked_session import Harness, MockedSession, photo_sizes, private_message

main_module = pytest.importorskip("app.__main__", reason="app.bot / app.web not importable", exc_type=ImportError)

H = {"X-Requested-With": "pcb"}
SECRET = "Zq7RkW2xYv9LmN4pT8sJ3dF6gH1cB5nQ0aE"  # 35 chars, fake
ENV_TOKEN = f"111111:{SECRET}"
PANEL_TOKEN = f"222222:{SECRET[::-1]}"
OTHER_TOKEN = f"333333:{SECRET.lower()}"
SAME_TOKEN = f"{BOT_ID}:{SECRET.upper()}"  # the FakeBot the web app runs with
CH_A = -1001000000001
CH_B = -1001000000002
CH_C = -1001000000003
HEX = "0123456789abcdef" * 2
FREE_PORT = 8099  # scratch servers of this project: 127.0.0.1:8098-8099 only


def unauthorized() -> TelegramUnauthorizedError:
    return TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")


def network_error(message: str = "Cannot connect to host api.telegram.org") -> TelegramNetworkError:
    return TelegramNetworkError(method=GetMe(), message=message)


def upload(config, ext: str = "jpg", n: int = 0) -> str:
    name = f"{HEX[:-1]}{n:x}.{ext}"
    (config.uploads_dir / name).write_bytes(b"\x00fake-media")
    return name


def no_tokens_in(text: str, *tokens: str) -> bool:
    return not any(token in text for token in tokens) and SECRET not in text and SECRET[::-1] not in text


def assert_no_token_logged(caplog, *tokens: str) -> None:
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        if record.exc_info and record.exc_info[1] is not None:
            text += str(record.exc_info[1])
        assert no_tokens_in(text, *tokens), f"token in log record: {record.name}: {text}"


# ================================================================== helpers / config

def test_token_format_and_masking():
    assert is_bot_token(ENV_TOKEN) and is_bot_token(PANEL_TOKEN)
    for bad in ("", "123:TEST", "abc:" + SECRET, "1234:" + SECRET, "123456:short", "123456:" + SECRET + " x",
                "123456:" + SECRET + "!", None, 123456, "123456:" + "A" * 300):
        assert not is_bot_token(bad), bad
    assert mask_token(ENV_TOKEN) == "…" + SECRET[-4:] and mask_token("") == "" and mask_token(None) == ""
    text = f"ClientResponseError url='https://api.telegram.org/bot{ENV_TOKEN}/getMe'"
    assert redact_tokens(text) == "ClientResponseError url='https://api.telegram.org/bot…" + SECRET[-4:] + "/getMe'"
    assert TokenState() == TokenState(source="env", masked="", panel_token_rejected=False)


def test_log_filter_masks_tokens_in_messages_and_tracebacks():
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = Collect()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(main_module.RedactTokensFilter())
    logger = logging.getLogger("tests.redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("Failed: %s", f"url=https://api.telegram.org/bot{ENV_TOKEN}/getUpdates")
        try:
            raise RuntimeError(f"boom {PANEL_TOKEN}")
        except RuntimeError:
            logger.exception("Unhandled")
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    assert len(records) == 2
    assert all(no_tokens_in(text, ENV_TOKEN, PANEL_TOKEN) for text in records)
    assert "bot…" + SECRET[-4:] + "/getUpdates" in records[0] and "RuntimeError: boom …" in records[1]


def test_setup_logging_installs_the_filter(monkeypatch):
    root = logging.getLogger()
    handler = logging.StreamHandler()
    monkeypatch.setattr(root, "handlers", [handler])
    access = logging.getLogger("aiohttp.access")
    monkeypatch.setattr(access, "level", access.level)  # restored after the test
    main_module.setup_logging("INFO")
    main_module.setup_logging("INFO")  # idempotent
    assert sum(isinstance(f, main_module.RedactTokensFilter) for f in handler.filters) == 1
    aiosqlite_filters = logging.getLogger("aiosqlite").filters
    assert sum(isinstance(f, main_module.RedactTokensFilter) for f in aiosqlite_filters) == 1


async def test_default_bot_factory_makes_an_html_bot():
    bot = main_module.default_bot_factory(ENV_TOKEN)
    try:
        assert bot.id == 111111 and bot.default.parse_mode == "HTML"
    finally:
        await bot.session.close()


# ================================================================== start-up: which token

class FakeSession:
    def __init__(self, events: list[str], label: str):
        self.events = events
        self.label = label
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.events.append(f"session_close:{self.label}")


class TokenBot(FakeBot):
    """A FakeBot built from a token (id = the token's number) with aiogram's cached `me()`;
    `outcomes` script the getMe answers (an exception, or None = success)."""

    def __init__(self, token: str, events: list[str], outcomes: list | None = None):
        bot_id = int(token.split(":")[0])
        super().__init__(bot_id=bot_id, username=f"bot{bot_id}")
        self.token = token
        self.events = events
        self.outcomes = list(outcomes or [])
        self.session = FakeSession(events, str(bot_id))
        self._me: User | None = None
        events.append(f"bot:{bot_id}")

    async def me(self) -> User:
        if self._me is not None:
            return self._me
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            self.events.append(f"me_fail:{self.id}")
            raise outcome
        self.events.append(f"me_ok:{self.id}")
        self._me = User(id=self.id, is_bot=True, first_name="Bot", username=self.username)
        return self._me


class PollingStub:
    """Dispatcher double: `script(dp, bot)` runs as polling; `stop_polling` behaves like aiogram's
    (stops a running polling and waits for it, RuntimeError when none runs)."""

    def __init__(self, db, events: list[str], script=None):
        self.db = db
        self.events = events
        self.script = script
        self.data: dict = {}
        self._handle_update_tasks: set[asyncio.Task] = set()
        self.running = False
        self._stop = asyncio.Event()
        self._stopped = asyncio.Event()

    def __setitem__(self, key, value) -> None:
        self.data[key] = value

    def __getitem__(self, key):
        return self.data[key]

    def resolve_used_update_types(self) -> list[str]:
        return ["message"]

    async def start_polling(self, bot, **kwargs) -> None:
        self.events.append(f"polling:{bot.id}")
        self.kwargs = kwargs
        self.running = True
        try:
            if self.script is not None:
                await self.script(self, bot)
        finally:
            self.running = False
            self._stopped.set()

    async def stop_polling(self) -> None:
        if not self.running:
            raise RuntimeError("Polling is not started")
        self.events.append("stop_polling")
        self._stop.set()
        await self._stopped.wait()

    async def wait_stopped(self, timeout: float = 10) -> None:
        await asyncio.wait_for(self._stop.wait(), timeout)


@pytest.fixture
def startup(monkeypatch, config):
    """`run()` with stubbed outside world: bots from `factory`, no TCP port, stub polling."""
    config.bot_token = ENV_TOKEN
    state: dict = {"events": [], "bots": [], "outcomes": {}, "scripts": [], "dps": [], "config": config}

    def factory(token: str) -> TokenBot:
        bot = TokenBot(token, state["events"], state["outcomes"].get(token))
        state["bots"].append(bot)
        return bot

    def make_dispatcher(db, config_, scheduler=None):
        script = state["scripts"].pop(0) if state["scripts"] else None
        dp = PollingStub(db, state["events"], script)
        state["dps"].append(dp)
        return dp

    class StubSite:
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            state["events"].append("site_start")

    def make_web_app(bot, db, config_, scheduler=None):
        app = web.Application()
        state.setdefault("apps", []).append(app)
        return app

    monkeypatch.setattr(main_module, "create_dispatcher", make_dispatcher)
    monkeypatch.setattr(main_module, "create_web_app", make_web_app)
    monkeypatch.setattr(main_module.web, "TCPSite", StubSite)
    monkeypatch.setattr(main_module, "TELEGRAM_RETRY_START", 0.01)
    monkeypatch.setattr(main_module, "COMMANDS_RETRY_DELAY", 0.01)
    monkeypatch.setattr(main_module, "SHUTDOWN_TIMEOUT", 5.0)
    state["factory"] = factory
    return state


async def set_panel_token(config, token: str | None) -> None:
    database = Database(config.db_path)
    await database.connect()
    try:
        await database.set_setting(BOT_TOKEN_SETTING, token)
    finally:
        await database.close()


def tokens_used(state) -> list[str]:
    return [bot.token for bot in state["bots"]]


async def test_start_with_env_token_only(startup, caplog):
    runtime = main_module.Runtime()
    with caplog.at_level(logging.DEBUG):
        assert await main_module.run(startup["config"], runtime, bot_factory=startup["factory"]) == 0
    assert tokens_used(startup) == [ENV_TOKEN]
    token_state = startup["dps"][0]["token_state"]
    assert token_state == TokenState(source="env", masked=mask_token(ENV_TOKEN), panel_token_rejected=False)
    assert startup["apps"][0][TOKEN_STATE_KEY] is token_state
    assert callable(startup["apps"][0][RESTART_KEY])
    assert startup["dps"][0].kwargs["handle_signals"] is False
    assert "Токен бота: из .env (…" in caplog.text
    assert_no_token_logged(caplog, ENV_TOKEN)


async def test_start_with_panel_token(startup, caplog):
    await set_panel_token(startup["config"], PANEL_TOKEN)
    with caplog.at_level(logging.DEBUG):
        assert await main_module.run(startup["config"], main_module.Runtime(), bot_factory=startup["factory"]) == 0
    assert tokens_used(startup) == [PANEL_TOKEN]
    assert startup["dps"][0]["token_state"] == TokenState(source="panel", masked=mask_token(PANEL_TOKEN))
    assert "Токен бота: задан в веб-панели" in caplog.text
    assert_no_token_logged(caplog, ENV_TOKEN, PANEL_TOKEN)


async def test_rejected_panel_token_falls_back_to_env(startup, caplog):
    await set_panel_token(startup["config"], PANEL_TOKEN)
    startup["outcomes"][PANEL_TOKEN] = [unauthorized()]
    runtime = main_module.Runtime()
    with caplog.at_level(logging.DEBUG):
        first = await main_module.run(startup["config"], runtime, bot_factory=startup["factory"])
        assert first == main_module.RESTART and runtime.panel_token_rejected
        second = await main_module.run(startup["config"], runtime, bot_factory=startup["factory"])
    assert second == 0
    assert tokens_used(startup) == [PANEL_TOKEN, ENV_TOKEN]
    events = startup["events"]
    assert events.index("session_close:222222") < events.index("bot:111111")  # fully shut down first
    assert "polling:222222" not in events and "polling:111111" in events
    state = startup["dps"][1]["token_state"]
    assert state.source == "env" and state.panel_token_rejected is True
    assert "Telegram не принял токен бота, заданный в веб-панели (…" in caplog.text
    assert_no_token_logged(caplog, ENV_TOKEN, PANEL_TOKEN)


async def test_corrupted_panel_token_is_not_used(startup):
    await set_panel_token(startup["config"], "not-a-token")
    runtime = main_module.Runtime()
    assert await main_module.run(startup["config"], runtime, bot_factory=startup["factory"]) == 0
    assert tokens_used(startup) == [ENV_TOKEN]
    assert startup["dps"][0]["token_state"].panel_token_rejected is True


async def test_both_tokens_rejected_exit_1(startup):
    await set_panel_token(startup["config"], PANEL_TOKEN)
    startup["outcomes"] = {PANEL_TOKEN: [unauthorized()], ENV_TOKEN: [unauthorized()]}
    runtime = main_module.Runtime()
    assert await main_module.run(startup["config"], runtime, bot_factory=startup["factory"]) == main_module.RESTART
    assert await main_module.run(startup["config"], runtime, bot_factory=startup["factory"]) == 1
    assert tokens_used(startup) == [PANEL_TOKEN, ENV_TOKEN]


async def test_network_error_retries_the_panel_token_without_fallback(startup):
    await set_panel_token(startup["config"], PANEL_TOKEN)
    startup["outcomes"][PANEL_TOKEN] = [network_error(), network_error()]
    runtime = main_module.Runtime()
    assert await main_module.run(startup["config"], runtime, bot_factory=startup["factory"]) == 0
    assert tokens_used(startup) == [PANEL_TOKEN] and not runtime.panel_token_rejected
    events = startup["events"]
    assert events.count("me_fail:222222") == 2 and "me_ok:222222" in events and "polling:222222" in events


def test_serve_restarts_in_a_fresh_loop_until_a_final_result(startup):
    asyncio.run(set_panel_token(startup["config"], PANEL_TOKEN))
    startup["outcomes"] = {PANEL_TOKEN: [unauthorized()], ENV_TOKEN: [unauthorized()]}
    runtime = main_module.Runtime()
    assert main_module.serve(startup["config"], bot_factory=startup["factory"], runtime=runtime) == 1
    assert tokens_used(startup) == [PANEL_TOKEN, ENV_TOKEN] and runtime.restarts == 1


async def test_restart_requested_from_the_panel(startup):
    """The restart hook stops polling; the run shuts down and returns RESTART; a web restart
    lets the next run try the panel token again."""

    async def polling(dp, bot):
        startup["apps"][0][RESTART_KEY]()  # what the web panel calls after a new token was saved
        await dp.wait_stopped()

    startup["scripts"] = [polling]
    runtime = main_module.Runtime(panel_token_rejected=True)
    result = await main_module.run(startup["config"], runtime, bot_factory=startup["factory"])
    assert result == main_module.RESTART and runtime.panel_token_rejected is False
    events = startup["events"]
    assert "stop_polling" in events and events.index("stop_polling") < events.index("session_close:111111")


async def test_restart_requested_while_waiting_for_telegram(startup):
    class UnreachableBot(TokenBot):
        async def me(self):  # Telegram is down; meanwhile a new token is saved in the panel
            startup["apps"][0][RESTART_KEY]()
            raise network_error()

    def factory(token):
        bot = UnreachableBot(token, startup["events"])
        startup["bots"].append(bot)
        return bot

    result = await main_module.run(startup["config"], main_module.Runtime(), bot_factory=factory)
    assert result == main_module.RESTART
    assert not any(e.startswith("polling") for e in startup["events"])
    assert startup["bots"][0].session.closed


def test_signal_wins_over_a_restart_request():
    control = main_module._RunControl()
    control.request_restart()
    assert control.restarting and control.stop.is_set()
    control.on_signal()  # SIGTERM during the restart: stop for good
    assert not control.restarting
    runtime = main_module.Runtime(panel_token_rejected=True)
    assert main_module._finish(control, runtime) == 0 and runtime.panel_token_rejected


# ================================================================== the restart loop, end to end

def _port_free(port: int) -> bool:
    """Nothing listens on `port`. SO_REUSEADDR like aiohttp's TCPSite: connections of an earlier
    server in TIME_WAIT do not keep the port busy, a listening socket does."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def test_restart_loop_switches_the_bot_end_to_end(config, monkeypatch):
    """Two real runs on a real port: the panel saves a new token over HTTP, the first run shuts
    down completely (polling of the old bot stopped, port released), the second one runs the new
    bot on the same port, re-checks the channels and notifies the admins."""
    if not _port_free(FREE_PORT):
        pytest.skip(f"127.0.0.1:{FREE_PORT} is busy")
    config.bot_token = ENV_TOKEN
    config.web_host = "127.0.0.1"
    config.web_port = FREE_PORT
    base = f"http://127.0.0.1:{FREE_PORT}"

    async def prepare() -> None:
        database = Database(config.db_path)
        await database.connect()
        await database.upsert_channel(CH_A, "Канал", "chan_a", status=CH_ACTIVE,
                                      rights={"can_post": True, "can_edit": True, "can_delete": True})
        await database.close()

    asyncio.run(prepare())
    events: list[str] = []
    bots: list[TokenBot] = []
    checked: list[str] = []

    def factory(token: str) -> TokenBot:
        bot = TokenBot(token, events)
        bot.add_chat(CH_A, "Канал", "chan_a", bot_status="administrator" if bot.id == 111111 else "left")
        bots.append(bot)
        return bot

    async def checker(token: str) -> dict:
        checked.append(token)
        bot_id = int(token.split(":")[0])
        return {"id": bot_id, "username": f"bot{bot_id}", "name": "Бот"}

    original_create = main_module.create_web_app

    def create(bot, db, config_, scheduler=None):
        app = original_create(bot, db, config_, scheduler)
        app[TOKEN_CHECKER_KEY] = checker
        return app

    results: dict = {}

    async def first_run(dp, bot):
        for _ in range(200):  # the identity of the first bot is remembered in the background
            if await dp.db.get_setting(BOT_ID_SETTING) == "111111":
                break
            await asyncio.sleep(0.01)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/api/settings") as resp:
                results["settings_1"] = await resp.json()
            async with session.post(f"{base}/api/settings/bot-token", json={"token": PANEL_TOKEN},
                                    headers=H) as resp:
                results["post"] = (resp.status, await resp.text())
        await dp.wait_stopped()

    async def second_run(dp, bot):
        for _ in range(500):
            if any("сменён" in call["text"] for call in bot.calls_of("send_message")):
                break
            await asyncio.sleep(0.01)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/api/health") as resp:
                results["health"] = await resp.json()
            async with session.get(f"{base}/api/settings") as resp:
                results["settings_2"] = await resp.json()

    scripts = [first_run, second_run]
    runs: list[tuple] = []

    def make_dispatcher(db, config_, scheduler=None):
        dp = PollingStub(db, events, scripts.pop(0))
        runs.append((dp, scheduler))
        return dp

    monkeypatch.setattr(main_module, "create_dispatcher", make_dispatcher)
    monkeypatch.setattr(main_module, "create_web_app", create)
    monkeypatch.setattr(main_module, "SHUTDOWN_TIMEOUT", 5.0)
    monkeypatch.setattr(api_bot, "RESTART_DELAY", 0.05)

    runtime = main_module.Runtime()
    assert main_module.serve(config, bot_factory=factory, runtime=runtime) == 0

    assert [bot.token for bot in bots] == [ENV_TOKEN, PANEL_TOKEN] and runtime.restarts == 1
    assert checked == [PANEL_TOKEN]
    status, body = results["post"]
    assert status == 200 and no_tokens_in(body, PANEL_TOKEN)
    assert '"restarting": true' in body and '"id": 222222' in body
    assert results["settings_1"]["bot_token_source"] == "env"
    assert results["settings_2"]["bot_token_source"] == "panel"
    assert results["settings_2"]["bot_token_masked"] == mask_token(PANEL_TOKEN)
    assert results["settings_2"]["bot"]["id"] == 222222
    assert results["health"]["bot"] == "@bot222222"
    # The old bot stopped polling and was closed before the new one was created.
    assert events.index("stop_polling") < events.index("session_close:111111") < events.index("bot:222222")
    assert events.count("polling:111111") == 1 and events.count("polling:222222") == 1
    # The new bot re-checked the channel (not an admin there → left) and told the admins.
    (notice,) = [c for c in bots[1].calls_of("send_message") if "сменён" in c["text"]]
    assert notice["chat_id"] == 1
    assert "Бот публикаций сменён на @bot222222" in notice["text"] and "в 1 из 1 каналов" in notice["text"]

    async def check() -> None:
        database = Database(config.db_path)
        await database.connect()
        try:
            assert (await database.get_channel(CH_A))["status"] == CH_LEFT
            assert await database.get_setting(BOT_ID_SETTING) == "222222"
            assert await database.get_setting(BOT_TOKEN_SETTING) == PANEL_TOKEN
        finally:
            await database.close()

    asyncio.run(check())
    assert _port_free(FREE_PORT)  # released at the end, too
    for dp, scheduler in runs:  # every run closed its database and stopped its scheduler
        assert dp.db._conn is None and not scheduler.running


async def test_web_port_is_bound_again_after_a_restart(monkeypatch, config, caplog):
    attempts: list[int] = []

    class BusySite:
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError(98, "Address already in use")

    monkeypatch.setattr(main_module.web, "TCPSite", BusySite)
    monkeypatch.setattr(main_module, "SITE_BIND_RETRY", 0)
    assert await main_module._start_site(None, config, attempts=5) is True and len(attempts) == 3
    attempts.clear()
    with caplog.at_level(logging.ERROR, logger="app"):
        assert await main_module._start_site(None, config, attempts=1) is False
    assert len(attempts) == 1 and "Не удалось запустить веб-панель" in caplog.text


# ================================================================== the bot was changed

async def _channels(db, bot: FakeBot) -> None:
    rights = {"can_post": True, "can_edit": True, "can_delete": True}
    for chat_id, title, status in ((CH_A, "Альфа", "administrator"), (CH_B, "Бета", "left")):
        await db.upsert_channel(chat_id, title, None, status=CH_ACTIVE, rights=rights)
        bot.add_chat(chat_id, title, None, bot_status=status)
    await db.upsert_channel(CH_C, "Пропал", None, status=CH_ACTIVE, rights=rights)  # unknown to Telegram
    await db.upsert_channel(-1009, "Ждёт", None, status=CH_PENDING)


def new_me(bot: FakeBot) -> User:
    return User(id=bot.id, is_bot=True, first_name="New", username=bot.username)


async def test_bot_change_rechecks_channels_and_notifies_admins(db):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    await db.add_admin(2)
    bot = FakeBot(bot_id=444444, username="new_bot")
    await _channels(db, bot)
    await db.set_setting(BOT_ID_SETTING, "111111")

    await main_module.track_bot_identity(bot, db, new_me(bot))

    statuses = {ch["id"]: ch["status"] for ch in await db.list_channels()}
    assert statuses == {CH_A: CH_ACTIVE, CH_B: CH_LEFT, CH_C: CH_LEFT, -1009: CH_PENDING}
    notices = bot.calls_of("send_message")
    assert [n["chat_id"] for n in notices] == [1, 2]
    text = notices[0]["text"]
    assert text.startswith("🤖 Бот публикаций сменён на @new_bot. Проверьте каналы: в 2 из 3 каналов новый бот "
                           "не администратор — добавьте его администратором с правом публикации.")
    assert "загрузите заново" in text
    assert await db.get_setting(BOT_ID_SETTING) == "444444"


async def test_bot_change_notice_failure_is_ignored(db):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    bot = FakeBot(bot_id=444444, username="new_bot")
    await _channels(db, bot)
    await db.set_setting(BOT_ID_SETTING, "111111")
    bot.fail_on["send_message"] = bad_request("Bad Request: chat not found")  # no /start in the new bot yet
    await main_module.track_bot_identity(bot, db, new_me(bot))
    assert await db.get_setting(BOT_ID_SETTING) == "444444"
    assert (await db.get_channel(CH_B))["status"] == CH_LEFT


async def test_bot_change_with_network_trouble_and_all_channels_fine(db):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    bot = FakeBot(bot_id=444444, username="new_bot")
    rights = {"can_post": True}
    for chat_id in (CH_A, CH_B):
        await db.upsert_channel(chat_id, "К", None, status=CH_ACTIVE, rights=rights)
        bot.add_chat(chat_id, "К")
    bot.fail_on["get_chat"] = lambda kw: network_error() if kw["chat_id"] == CH_B else None
    await db.set_setting(BOT_ID_SETTING, "111111")
    await main_module.track_bot_identity(bot, db, new_me(bot))
    assert {ch["id"]: ch["status"] for ch in await db.list_channels()} == {CH_A: CH_ACTIVE, CH_B: CH_ACTIVE}
    (notice,) = bot.calls_of("send_message")
    assert "Новый бот — администратор во всех каналах (2)." in notice["text"]
    assert "Не удалось проверить каналов: 1" in notice["text"]


async def test_bot_identity_first_start_and_same_bot(db):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    bot = FakeBot(bot_id=444444, username="new_bot")
    await _channels(db, bot)
    await main_module.track_bot_identity(bot, db, new_me(bot))  # nothing stored yet: just remember
    assert await db.get_setting(BOT_ID_SETTING) == "444444"
    await main_module.track_bot_identity(bot, db, new_me(bot))  # the same bot again
    assert bot.calls == []
    assert (await db.get_channel(CH_B))["status"] == CH_ACTIVE


def test_bot_changed_text_variants():
    assert "Подключённых каналов пока нет" in main_module.bot_changed_text("b<x>", 0, 0)
    assert "@b&lt;x&gt;" in main_module.bot_changed_text("b<x>", 0, 0)
    assert "нового бота" in main_module.bot_changed_text(None, 1, 1)
    assert "Снова подключены" not in main_module.bot_changed_text("b", 2, 0)
    assert "Снова подключены каналы, где новый бот — администратор: 1." in main_module.bot_changed_text(
        "b", 2, 0, restored=1)
    text = main_module.bot_changed_text("b", 1, 0, panel_token_rejected=True)
    assert text.startswith("🤖 Бот публикаций сменён на @b. Telegram не принял токен, заданный в веб-панели, — "
                           "бот работает с токеном из .env")


async def test_switching_back_restores_channels_left_by_the_other_bot(db):
    """A → B (B is not an admin of CH_B: left) → back to A: A is an admin there all along, Telegram
    sends no update — the re-check after the change brings the channel back."""
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    rights = {"can_post": True, "can_edit": True, "can_delete": True}
    bot = FakeBot(bot_id=111111, username="old_bot")
    for chat_id in (CH_A, CH_B):
        await db.upsert_channel(chat_id, "К", None, status=CH_ACTIVE, rights=rights)
        bot.add_chat(chat_id, "К")
    await db.upsert_channel(CH_B, "К", None, status=CH_LEFT, rights={})  # left while B ran
    await db.upsert_channel(CH_C, "Пропал", None, status=CH_LEFT, rights={})  # unknown to Telegram
    await db.upsert_channel(-1009, "Нет прав", None, status=CH_LEFT, rights={})
    bot.add_chat(-1009, "Нет прав", None, bot_status="left")
    await db.set_setting(BOT_ID_SETTING, "222222")

    await main_module.track_bot_identity(bot, db, new_me(bot), TokenState(panel_token_rejected=True))

    statuses = {ch["id"]: ch["status"] for ch in await db.list_channels()}
    assert statuses == {CH_A: CH_ACTIVE, CH_B: CH_ACTIVE, CH_C: CH_LEFT, -1009: CH_LEFT}
    (notice,) = bot.calls_of("send_message")
    assert "Telegram не принял токен, заданный в веб-панели" in notice["text"]
    assert "Новый бот — администратор во всех каналах (2)." in notice["text"]
    assert "Снова подключены каналы, где новый бот — администратор: 1." in notice["text"]
    assert await db.get_setting(BOT_ID_SETTING) == "111111"


# ================================================================== web API

class Checker:
    def __init__(self):
        self.calls: list[str] = []
        self.error: BaseException | None = None

    async def __call__(self, token: str) -> dict:
        self.calls.append(token)
        if self.error is not None:
            raise self.error
        bot_id = int(token.split(":")[0])
        return {"id": bot_id, "username": f"bot{bot_id}", "name": "Новый бот"}


class Restarter:
    def __init__(self):
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
async def web_client(db, config, bot, monkeypatch):
    await db.migrate()
    config.bot_token = ENV_TOKEN
    monkeypatch.setattr(api_bot, "RESTART_DELAY", 0.01)
    app = create_web_app(bot, db, config)
    app[TOKEN_CHECKER_KEY] = Checker()
    app[RESTART_KEY] = Restarter()
    async with TestClient(TestServer(app)) as client:
        yield client


def _app(client: TestClient) -> web.Application:
    return client.server.app


async def _post_token(client: TestClient, token, *, headers=H) -> tuple[int, dict, str]:
    resp = await client.post("/api/settings/bot-token", json={"token": token}, headers=headers)
    text = await resp.text()
    return resp.status, (await resp.json()), text


async def test_settings_describe_the_running_bot(web_client, bot):
    data = await (await web_client.get("/api/settings")).json()
    assert data["bot"] == {"id": BOT_ID, "username": "test_bot", "name": "Test bot"}
    assert data["bot_token_source"] == "env" and data["bot_token_masked"] == mask_token(ENV_TOKEN)
    assert data["panel_token_rejected"] is False and data["panel_token_set"] is False

    _app(web_client)[TOKEN_STATE_KEY].source = "env"
    _app(web_client)[TOKEN_STATE_KEY].panel_token_rejected = True
    await _app(web_client)[DB_KEY].set_setting(BOT_TOKEN_SETTING, PANEL_TOKEN)
    data = await (await web_client.get("/api/settings")).json()
    assert data["panel_token_rejected"] is True and data["panel_token_set"] is True
    assert data["bot_token_source"] == "env"


async def test_settings_while_telegram_is_down(db, config, bot):
    await db.migrate()
    bot.fail_on["get_me"] = network_error()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as client:
        data = await (await client.get("/api/settings")).json()
    assert data["bot"] == {"id": BOT_ID, "username": None, "name": None}


async def test_set_token_validation(web_client):
    checker = _app(web_client)[TOKEN_CHECKER_KEY]
    for value in (None, "", 123, "abc", "123:TEST", f"12:{SECRET}", f"123456:{SECRET}!", ["x"],
                  f"123456:{'A' * 300}"):
        status, data, _ = await _post_token(web_client, value)
        assert status == 400 and data == {"error": "Это не похоже на токен бота (пример: 123456789:AA…)"}, value
    resp = await web_client.post("/api/settings/bot-token", data="not json", headers=H)
    assert resp.status == 400
    resp = await web_client.post("/api/settings/bot-token", json={"token": PANEL_TOKEN})  # no CSRF header
    assert resp.status == 403
    assert checker.calls == []


async def test_set_token_rejected_and_unreachable(web_client, caplog):
    app = _app(web_client)
    checker = app[TOKEN_CHECKER_KEY]
    with caplog.at_level(logging.DEBUG):
        checker.error = unauthorized()
        status, data, text = await _post_token(web_client, PANEL_TOKEN)
        assert status == 400 and data == {"error": "Telegram не принял токен"}
        checker.error = network_error(f"ClientResponseError url='https://api.telegram.org/bot{PANEL_TOKEN}/getMe'")
        status, data, text2 = await _post_token(web_client, f"  {PANEL_TOKEN}\n")  # trimmed
        assert status == 502 and data == {"error": "Не удалось связаться с Telegram, попробуйте ещё раз"}
        checker.error = asyncio.TimeoutError()
        status, data, _ = await _post_token(web_client, PANEL_TOKEN)
        assert status == 502
        checker.error = bad_request("Bad Request: Not Found")
        status, data, _ = await _post_token(web_client, PANEL_TOKEN)
        assert status == 400
    assert checker.calls == [PANEL_TOKEN] * 4
    assert await app[DB_KEY].get_setting(BOT_TOKEN_SETTING) is None
    assert app[RESTART_KEY].calls == 0
    assert no_tokens_in(text + text2, PANEL_TOKEN)
    assert_no_token_logged(caplog, PANEL_TOKEN)
    assert "…" + PANEL_TOKEN[-4:] in caplog.text


async def test_set_token_same_bot_is_refused(web_client):
    status, data, _ = await _post_token(web_client, SAME_TOKEN)
    assert status == 400 and data == {"error": "Это тот же бот, что работает сейчас"}
    assert await _app(web_client)[DB_KEY].get_setting(BOT_TOKEN_SETTING) is None


async def test_set_token_saves_and_restarts(web_client, caplog):
    app = _app(web_client)
    restart = app[RESTART_KEY]
    with caplog.at_level(logging.DEBUG):
        status, data, text = await _post_token(web_client, PANEL_TOKEN)
        assert status == 200
        assert data == {"ok": True, "bot": {"id": 222222, "username": "bot222222", "name": "Новый бот"},
                        "restarting": True}
        assert restart.calls == 0  # only after the answer was sent
        await asyncio.sleep(0.1)
        assert restart.calls == 1
        assert await app[DB_KEY].get_setting(BOT_TOKEN_SETTING) == PANEL_TOKEN
        settings = await (await web_client.get("/api/settings")).text()
    assert no_tokens_in(text + settings, PANEL_TOKEN)
    assert_no_token_logged(caplog, PANEL_TOKEN)


async def test_set_token_of_the_env_bot_clears_the_panel_token(web_client):
    app = _app(web_client)
    await app[DB_KEY].set_setting(BOT_TOKEN_SETTING, OTHER_TOKEN)
    status, data, _ = await _post_token(web_client, ENV_TOKEN)
    assert status == 200 and data["restarting"] is True
    assert await app[DB_KEY].get_setting(BOT_TOKEN_SETTING) is None


async def test_set_token_without_restart_hook(db, config, bot):
    await db.migrate()
    app = create_web_app(bot, db, config)
    app[TOKEN_CHECKER_KEY] = Checker()
    async with TestClient(TestServer(app)) as client:
        status, data, _ = await _post_token(client, PANEL_TOKEN)
    assert status == 200 and data["restarting"] is False
    assert await db.get_setting(BOT_TOKEN_SETTING) == PANEL_TOKEN


async def test_delete_panel_token(web_client):
    app = _app(web_client)
    db = app[DB_KEY]
    resp = await web_client.delete("/api/settings/bot-token", headers=H)
    assert resp.status == 400 and await resp.json() == {"error": "Токен из панели не задан"}

    await db.set_setting(BOT_TOKEN_SETTING, PANEL_TOKEN)
    assert (await web_client.delete("/api/settings/bot-token")).status == 403  # CSRF
    app[TOKEN_STATE_KEY].source = "panel"
    resp = await web_client.delete("/api/settings/bot-token", headers=H)
    text = await resp.text()
    # The .env token was checked first; the answer names its bot (the panel waits for it).
    assert resp.status == 200 and await resp.json() == {
        "ok": True, "restarting": True, "bot": {"id": 111111, "username": "bot111111", "name": "Новый бот"}}
    assert app[TOKEN_CHECKER_KEY].calls == [ENV_TOKEN] and no_tokens_in(text, ENV_TOKEN, PANEL_TOKEN)
    assert await db.get_setting(BOT_TOKEN_SETTING) is None
    await asyncio.sleep(0.05)
    assert app[RESTART_KEY].calls == 1


@pytest.mark.parametrize(("error", "status", "message"), [
    (unauthorized(), 400, api_bot.ENV_TOKEN_REJECTED_ERROR),
    (network_error(), 502, api_bot.TOKEN_NETWORK_ERROR),
])
async def test_delete_keeps_the_panel_token_when_the_env_token_does_not_work(web_client, caplog, error, status,
                                                                            message):
    """Back to .env with a revoked BOT_TOKEN would stop the bot for good (exit 1): refused."""
    app = _app(web_client)
    db = app[DB_KEY]
    await db.set_setting(BOT_TOKEN_SETTING, PANEL_TOKEN)
    app[TOKEN_STATE_KEY].source = "panel"
    app[TOKEN_CHECKER_KEY].error = error
    with caplog.at_level(logging.DEBUG):
        resp = await web_client.delete("/api/settings/bot-token", headers=H)
        assert resp.status == status and await resp.json() == {"error": message}
    assert await db.get_setting(BOT_TOKEN_SETTING) == PANEL_TOKEN
    await asyncio.sleep(0.05)
    assert app[RESTART_KEY].calls == 0
    assert_no_token_logged(caplog, ENV_TOKEN, PANEL_TOKEN)


async def test_delete_rejected_panel_token_needs_no_restart(web_client):
    app = _app(web_client)
    db = app[DB_KEY]
    await db.set_setting(BOT_TOKEN_SETTING, PANEL_TOKEN)
    app[TOKEN_STATE_KEY].panel_token_rejected = True  # the bot already runs on .env
    resp = await web_client.delete("/api/settings/bot-token", headers=H)
    assert resp.status == 200 and await resp.json() == {"ok": True, "restarting": False, "bot": None}
    assert await db.get_setting(BOT_TOKEN_SETTING) is None
    assert app[TOKEN_CHECKER_KEY].calls == []  # the .env bot is the one running
    await asyncio.sleep(0.05)
    assert app[RESTART_KEY].calls == 0
    # Nothing restarts, so the "rejected" note must go away right now (the panel and the bot show it).
    assert app[TOKEN_STATE_KEY].panel_token_rejected is False
    data = await (await web_client.get("/api/settings")).json()
    assert data["panel_token_rejected"] is False and data["panel_token_set"] is False
    resp = await web_client.delete("/api/settings/bot-token", headers=H)
    assert resp.status == 400


async def test_default_token_checker_uses_a_temporary_bot(monkeypatch):
    session = MockedSession()
    closed = []
    original_close = session.close

    async def close():
        closed.append(True)
        await original_close()

    session.close = close
    monkeypatch.setattr(api_bot, "AiohttpSession", lambda timeout: session)
    info = await api_bot.check_token(PANEL_TOKEN)
    assert info == {"id": 222222, "username": "post_test_bot", "name": "Test bot"}
    assert closed == [True] and isinstance(session.requests[0], GetMe)

    session.fail_on[GetMe] = unauthorized()
    with pytest.raises(TelegramUnauthorizedError):
        await api_bot.check_token(PANEL_TOKEN)
    assert closed == [True, True]


# ================================================================== file_id belongs to a bot

FOREIGN = 111111


async def _post(db, channels: list[int], media: list[dict], text_md: str = "Пост") -> int:
    post_id = await db.create_post(text_md=text_md, media=media, created_by=1)
    if channels:
        await db.set_post_targets(post_id, channels)
    return post_id


async def _channel(db, bot: FakeBot, chat_id: int = CH_A) -> None:
    await db.upsert_channel(chat_id, f"К{chat_id}", None, status=CH_ACTIVE,
                            rights={"can_post": True, "can_edit": True, "can_delete": True})
    bot.add_chat(chat_id, f"К{chat_id}")


async def test_foreign_file_id_is_replaced_by_the_upload(db, bot, config):
    await db.migrate()
    await _channel(db, bot, CH_A)
    await _channel(db, bot, CH_B)
    name = upload(config)
    post_id = await _post(db, [CH_A, CH_B], [{"type": "photo", "file_id": "OLD", "file_bot": FOREIGN, "path": name}])
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 2
    first, second = bot.calls_of("send_photo")
    assert isinstance(first["photo"], FSInputFile) and second["photo"] == "fake_file_1"
    media = (await db.get_post(post_id))["media"]
    assert media == [{"type": "photo", "file_id": "fake_file_1", "file_bot": BOT_ID, "path": name}]


async def test_foreign_file_id_without_upload_is_an_error(db, bot, config):
    await db.migrate()
    await _channel(db, bot)
    post_id = await _post(db, [CH_A], [{"type": "photo", "file_id": "OLD", "file_bot": FOREIGN}])
    with pytest.raises(PostError) as exc:
        await publish_post(bot, db, config, post_id)
    assert str(exc.value) == FOREIGN_MEDIA_ERROR == "Медиа загружено через другого бота — загрузите файл заново"
    assert (await db.get_post(post_id))["status"] == "draft" and not bot.calls_of("send_photo")
    with pytest.raises(PostError, match="другого бота"):
        await send_post(bot, await db.get_post(post_id), 1, config)
    with pytest.raises(PostError, match="другого бота"):
        await posting.send_preview(bot, db, config, post_id, 1)


async def test_foreign_media_edit_of_a_published_post(db, bot, config):
    await db.migrate()
    await _channel(db, bot)
    post_id = await _post(db, [CH_A], [{"type": "photo", "file_id": "MINE", "file_bot": BOT_ID}])
    await publish_post(bot, db, config, post_id)
    assert bot.calls_of("send_photo")[0]["photo"] == "MINE"  # own file_id: used as is
    await db.update_post(post_id, media=[{"type": "photo", "file_id": "OLD", "file_bot": FOREIGN}])
    with pytest.raises(PostError, match="другого бота"):
        await edit_published_post(bot, db, config, post_id, media_changed=True)


def wrong_file(kw: dict, field: str = "photo"):
    source = kw.get(field)
    return bad_request("Bad Request: wrong file identifier/HTTP URL specified") if isinstance(source, str) else None


async def test_legacy_file_id_unknown_to_telegram_is_resent_from_the_upload(db, bot, config, caplog):
    await db.migrate()
    await _channel(db, bot, CH_A)
    await _channel(db, bot, CH_B)
    name = upload(config)
    post_id = await _post(db, [CH_A, CH_B], [{"type": "photo", "file_id": "LEGACY", "path": name}])
    sent: list = []

    def fail_legacy(kw):
        sent.append(kw["photo"])
        return bad_request("Bad Request: wrong file identifier/HTTP URL specified") if kw["photo"] == "LEGACY" else None

    bot.fail_on["send_photo"] = fail_legacy
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 2 and report.fail_count == 0
    assert sent[0] == "LEGACY" and isinstance(sent[1], FSInputFile) and sent[2] == "fake_file_1"
    assert len(sent) == 3  # the second channel reuses the new file_id, no retry
    media = (await db.get_post(post_id))["media"]
    assert media == [{"type": "photo", "file_id": "fake_file_1", "path": name, "file_bot": BOT_ID}]


async def test_legacy_album_retry_and_no_upload(db, bot, config):
    await db.migrate()
    await _channel(db, bot)
    names = [upload(config, "jpg", 1), upload(config, "jpg", 2)]
    post_id = await _post(db, [CH_A], [{"type": "photo", "file_id": "L1", "path": names[0]},
                                       {"type": "photo", "file_id": "MINE", "file_bot": BOT_ID, "path": names[1]}])
    bot.fail_on["send_media_group"] = lambda kw: (
        bad_request("Bad Request: wrong remote file identifier specified")
        if any(m.media == "L1" for m in kw["media"]) else None)
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 1
    first, second = bot.calls_of("send_media_group")
    assert [m.media for m in first["media"]] == ["L1", "MINE"]
    assert isinstance(second["media"][0].media, FSInputFile) and second["media"][1].media == "MINE"
    media = (await db.get_post(post_id))["media"]
    assert media[0]["file_bot"] == BOT_ID and media[0]["file_id"].startswith("fake_file_")
    assert media[1] == {"type": "photo", "file_id": "MINE", "file_bot": BOT_ID, "path": names[1]}

    # A legacy file_id without an upload: the channel fails with an explanation.
    other = await _post(db, [CH_A], [{"type": "photo", "file_id": "LEGACY"}])
    bot.fail_on["send_photo"] = wrong_file
    report = await publish_post(bot, db, config, other)
    assert report.fail_count == 1 and report.results[0].error == "Файл недоступен в Telegram — загрузите его заново"


async def test_legacy_media_edit_retries_from_the_upload(db, bot, config):
    await db.migrate()
    await _channel(db, bot)
    post_id = await _post(db, [CH_A], [{"type": "photo", "file_id": "FIRST", "file_bot": BOT_ID}])
    await publish_post(bot, db, config, post_id)
    name = upload(config)
    await db.update_post(post_id, media=[{"type": "photo", "file_id": "LEGACY", "path": name}])
    bot.edit_media_returns_message = True
    bot.fail_on["edit_message_media"] = lambda kw: (
        bad_request("Bad Request: FILE_REFERENCE_EXPIRED") if isinstance(kw["media"].media, str) else None)
    report = await edit_published_post(bot, db, config, post_id, media_changed=True)
    assert report.ok_count == 1
    first, second = bot.calls_of("edit_message_media")
    assert first["media"].media == "LEGACY" and isinstance(second["media"].media, FSInputFile)
    media = (await db.get_post(post_id))["media"]
    assert media[0]["file_bot"] == BOT_ID and media[0]["file_id"].startswith("fake_file_")


async def test_preview_of_a_foreign_upload_stores_the_new_file_id(db, bot, config):
    await db.migrate()
    name = upload(config)
    post_id = await _post(db, [], [{"type": "photo", "file_id": "OLD", "file_bot": FOREIGN, "path": name}])
    await posting.send_preview(bot, db, config, post_id, 1)
    assert isinstance(bot.calls_of("send_photo")[0]["photo"], FSInputFile)
    media = (await db.get_post(post_id))["media"]
    assert media == [{"type": "photo", "file_id": "fake_file_1", "file_bot": BOT_ID, "path": name}]


def test_uses_file_id_rules():
    uses = posting.uses_file_id
    assert uses({"file_id": "A", "file_bot": 5}, 5) and not uses({"file_id": "A", "file_bot": 6}, 5)
    assert uses({"file_id": "A"}, 5) and uses({"file_id": "A", "path": "x"}, 5)
    assert not uses({"file_id": "A", "path": "x"}, 5, legacy_path=True) and uses({"file_id": "A"}, 5, legacy_path=True)
    assert uses({"file_id": "A", "file_bot": 6}, None) and not uses({"path": "x"}, 5)
    assert posting.bot_id_of(FakeBot()) == BOT_ID and posting.bot_id_of(object()) is None


# ================================================================== web: media of another bot

@pytest.fixture
async def media_client(db, config, bot):
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as client:
        yield client


async def test_preview_endpoint_of_another_bots_file(media_client, db, bot, config):
    post_id = await db.create_post(text_md="x", source="bot",
                                   media=[{"type": "photo", "file_id": "OLDFILE", "file_bot": FOREIGN}])
    resp = await media_client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 404
    assert await resp.json() == {"error": "Файл загружен через другого бота — предпросмотр недоступен"}
    assert not bot.calls_of("get_file")

    # a copy downloaded while the old bot was running still shows
    stem = api_media.CACHE_PREFIX + hashlib.sha1(b"OLDFILE").hexdigest()[:16]
    (config.uploads_dir / f"{stem}.jpg").write_bytes(b"\xff\xd8\xffcached")
    resp = await media_client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 200 and await resp.read() == b"\xff\xd8\xffcached"

    # the upload wins
    name = upload(config, "png", 3)
    other = await db.create_post(text_md="x", source="web",
                                 media=[{"type": "photo", "file_id": "OLD2", "file_bot": FOREIGN, "path": name}])
    resp = await media_client.get(f"/api/posts/{other}/media/0")
    assert resp.status == 200 and await resp.read() == b"\x00fake-media"

    # this bot's own file is downloaded as before
    own = await db.create_post(text_md="x", source="bot",
                               media=[{"type": "photo", "file_id": "MINEFILE", "file_bot": BOT_ID}])
    assert (await media_client.get(f"/api/posts/{own}/media/0")).status == 200
    assert bot.calls_of("get_file") == [{"file_id": "MINEFILE"}]


async def test_preview_endpoint_of_a_legacy_unknown_file(media_client, db, bot):
    bot.fail_on["get_file"] = bad_request("Bad Request: wrong file identifier/HTTP URL specified")
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "video", "file_id": "LEGACYVID"}])
    resp = await media_client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 404 and "другого бота" in (await resp.json())["error"]


async def test_web_keeps_file_bot_and_never_takes_it_from_the_request(media_client, db, config):
    post_id = await db.create_post(text_md="x", source="bot",
                                   media=[{"type": "photo", "file_id": "TGFILE", "file_bot": FOREIGN}])
    data = (await (await media_client.get(f"/api/posts/{post_id}")).json())["post"]
    assert data["media"][0]["file_bot"] == FOREIGN and data["media"][0]["file_id"] == "TGFILE"

    # an echo of the item (the client may send any file_bot) keeps the stored owner
    resp = await media_client.put(f"/api/posts/{post_id}", headers=H, json={
        "text_md": "y", "media": [{"type": "photo", "file_id": "TGFILE", "file_bot": 999}], "action": "draft"})
    assert resp.status == 200, await resp.text()
    assert (await db.get_post(post_id))["media"][0]["file_bot"] == FOREIGN

    # a duplicate (copy_from) keeps it too
    resp = await media_client.post("/api/posts", headers=H, json={
        "text_md": "copy", "copy_from": post_id, "media": [{"type": "photo", "file_id": "TGFILE"}], "action": "draft"})
    assert resp.status == 200, await resp.text()
    copy = await db.get_post((await resp.json())["post"]["id"])
    assert copy["media"][0]["file_bot"] == FOREIGN

    # a new upload never gets a file_bot from the request
    name = upload(config, "png", 4)
    resp = await media_client.post("/api/posts", headers=H, json={
        "text_md": "new", "media": [{"type": "photo", "path": name, "file_bot": 999}], "action": "draft"})
    assert resp.status == 200, await resp.text()
    item = (await db.get_post((await resp.json())["post"]["id"]))["media"][0]
    assert "file_bot" not in item and item["path"] == name


# ================================================================== bot: composer and settings

OWNER = 111


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    harness.albums.delay = 0.05
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    await harness.add_channel(CH_A, "Канал А", "kanal_a")
    yield harness
    await harness.close()


async def latest_post(h: Harness) -> dict:
    row = await h.db.fetchone("SELECT id FROM posts ORDER BY id DESC LIMIT 1")
    return await h.db.get_post(row["id"])


async def test_composer_media_belong_to_the_bot(h):
    await h.send(OWNER, "/new")
    for index, file_id in enumerate(("p1", "p2")):
        await h.send(OWNER, None, media_group_id="alb", photo=photo_sizes(file_id),
                     caption="Альбом" if index == 0 else None)
    await h.settle()
    post = await latest_post(h)
    assert [(m["file_id"], m["file_bot"]) for m in post["media"]] == [("p1", h.bot.id), ("p2", h.bot.id)]
    message = private_message(OWNER, None, photo=photo_sizes("solo"))
    assert compose_handlers.message_media(message, 5)["file_bot"] == 5
    assert "file_bot" not in compose_handlers.message_media(message)  # unknown bot: no owner recorded


async def test_composer_preview_stores_file_ids_with_the_bot(h, config):
    name = upload(config)
    post_id = await h.db.create_post(text_md="x", source="web", created_by=OWNER,
                                     media=[{"type": "photo", "file_id": "OLD", "file_bot": FOREIGN, "path": name}])
    post = await h.db.get_post(post_id)
    ids, error = await compose_handlers._send_preview(h.bot, h.db, config, post, OWNER, edit=False)
    assert error is None and len(ids) == 1
    (request,) = h.session.of(SendPhoto)
    assert isinstance(request.photo, FSInputFile)
    media = (await h.db.get_post(post_id))["media"]
    assert media[0]["file_bot"] == h.bot.id and media[0]["file_id"].startswith("uploaded_")

    # A file_id of another bot without an upload: the composer explains instead of sending.
    other = await h.db.create_post(text_md="x", source="bot", created_by=OWNER,
                                   media=[{"type": "photo", "file_id": "OLD", "file_bot": FOREIGN}])
    ids, error = await compose_handlers._send_preview(h.bot, h.db, config, await h.db.get_post(other), OWNER,
                                                      edit=False)
    assert ids == [] and error == FOREIGN_MEDIA_ERROR


async def test_bot_settings_show_the_token_source(h):
    await h.send(OWNER, "/settings")
    text = [r.text for r in h.session.of(SendMessage) if "Настройки" in (r.text or "")][-1]
    assert "🤖 Токен бота: из .env" in text and "Сменить бота — в веб-панели: Настройки → Бот" in text

    await h.db.set_setting(BOT_TOKEN_SETTING, PANEL_TOKEN)  # no state from __main__: read from the DB
    await h.send(OWNER, "/settings")
    text = [r.text for r in h.session.of(SendMessage) if "Настройки" in (r.text or "")][-1]
    assert "🤖 Токен бота: задан в панели" in text and no_tokens_in(text, PANEL_TOKEN)

    h.dp["token_state"] = TokenState(source="env", masked=mask_token(ENV_TOKEN), panel_token_rejected=True)
    await h.send(OWNER, "/settings")
    text = [r.text for r in h.session.of(SendMessage) if "Настройки" in (r.text or "")][-1]
    assert "🤖 Токен бота: из .env" in text
    assert "Токен из панели не принят Telegram — бот работает с токеном из .env" in text
    assert no_tokens_in(text, ENV_TOKEN, PANEL_TOKEN)


async def test_settings_screen_helper(db, config):
    await db.migrate()
    text, _ = await settings_handlers.settings_screen(db, config, 1, TokenState(source="panel"))
    assert "🤖 Токен бота: задан в панели" in text and "не принят" not in text

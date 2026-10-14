"""Entry point: ``python -m app`` starts the bot (long polling), the web panel and the scheduler.

Startup: config → logging → database (connect, migrate, recover posts stuck in 'sending', seed
admins from ADMIN_IDS) → bot token (the one set in the web panel, else BOT_TOKEN from .env) → Bot
→ dispatcher → scheduler → web panel → wait until Telegram answers getMe (retried with backoff, so
a network outage at boot does not kill the panel and the scheduler) → bot commands / notices and,
when the bot was changed, a re-check of the channels in the background → polling.
Ctrl+C / SIGTERM stops polling and the scheduler's picking of new posts; running publishes are
allowed to finish within one shutdown budget (SHUTDOWN_TIMEOUT for everything — keep Docker's
stop_grace_period above it, see docker-compose.yml), then everything is closed in reverse order
(web panel, scheduler, bot session, database). What is still running when the budget is spent is
cancelled: a cut publish marks its channel «Публикация прервана — проверьте, вышел ли пост».

In-process restart (SPEC_V22): `main()` → `serve()` runs `run()` in a loop, each time in a fresh
event loop. `run()` returns RESTART when the web panel changed the bot token (the restart hook
``app[RESTART_KEY]`` stops polling, then the normal graceful shutdown runs — the web port is
released before the next run binds it again) or when Telegram rejected the token set in the
panel (the next run falls back to BOT_TOKEN from .env). The token is never logged — only masked.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import Any, NamedTuple, TypeVar

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import BotCommandScopeAllPrivateChats, User
from aiohttp import web

from app.bot import BOT_COMMANDS, create_dispatcher
from app.config import (
    BOT_ID_SETTING,
    BOT_TOKEN_SETTING,
    TOKEN_SOURCE_ENV,
    TOKEN_SOURCE_PANEL,
    Config,
    RedactTokensFilter,  # noqa: F401 — re-exported (tests, log configuration)
    TokenState,
    install_log_redaction,
    is_bot_token,
    load_config,
    mask_token,
)
from app.db import CH_ACTIVE, CH_LEFT, POST_PARTIAL, POST_SCHEDULED, Database
from app.services import channels as channels_service
from app.services import posting
from app.services.scheduler import Scheduler
from app.web import create_web_app
from app.web.auth import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, fixed_password, get_password_hash
from app.web.keys import RESTART_KEY, TOKEN_STATE_KEY

log = logging.getLogger("app")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Update types the bot relies on; requested even if no router registered a handler for one of them
# (chat_member and message_reaction_count are only delivered when listed explicitly).
REQUIRED_UPDATES = frozenset({
    "message", "callback_query", "my_chat_member", "chat_member", "channel_post", "message_reaction_count",
})

SHUTDOWN_TIMEOUT = 60.0  # budget for running publishes to finish on stop (flood wait ≤ 30 s)
CANCEL_GRACE = 5.0  # cancelled handlers / web requests get this long to record what happened
FINAL_GRACE = 2.0  # last wait for publish operations before the bot session and the DB close
# Whole graceful shutdown ≈ SHUTDOWN_TIMEOUT + 2 × CANCEL_GRACE + FINAL_GRACE (≈ 72 s): Docker's
# stop_grace_period (docker-compose.yml, 90s) must be longer, or SIGKILL cuts a publish.
COMMANDS_RETRY_DELAY = 5.0  # set_my_commands retries: 5, 10, 20, 40 s
TELEGRAM_RETRY_START = 1.0  # first pause between getMe attempts while Telegram is unreachable
TELEGRAM_RETRY_MAX = 60.0  # max pause (exponential backoff)
SITE_BIND_ATTEMPTS = 20  # after a restart: tries to bind the web port again…
SITE_BIND_RETRY = 0.5  # …this many seconds apart
RECHECK_PAUSE = 0.05  # between channels while re-checking them after the bot was changed

# `run()` result: start again (the bot token was changed in the panel, or the panel token was
# rejected by Telegram and the next run uses BOT_TOKEN from .env).
RESTART = "restart"

T = TypeVar("T")
BotFactory = Callable[[str], Any]


def setup_logging(level_name: str) -> None:
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
    install_log_redaction()
    if level > logging.DEBUG:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


@dataclass
class Runtime:
    """What survives an in-process restart (`serve` → `run` → RESTART → `run` …)."""

    panel_token_rejected: bool = False  # Telegram rejected the panel token: run on BOT_TOKEN from .env
    restarts: int = 0


def default_bot_factory(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def resolve_token(db: Database, config: Config, runtime: Runtime) -> tuple[str, TokenState]:
    """The token to run with: the one set in the web panel (DB setting) unless Telegram rejected
    it during this process' life, else BOT_TOKEN from .env."""
    panel = await db.get_setting(BOT_TOKEN_SETTING)
    if panel and not runtime.panel_token_rejected:
        if is_bot_token(panel):
            return panel, TokenState(source=TOKEN_SOURCE_PANEL, masked=mask_token(panel))
        log.error("Токен бота из веб-панели повреждён — работаю с BOT_TOKEN из .env")
        runtime.panel_token_rejected = True
    return config.bot_token, TokenState(source=TOKEN_SOURCE_ENV, masked=mask_token(config.bot_token),
                                        panel_token_rejected=bool(panel) and runtime.panel_token_rejected)


# ------------------------------------------------------------------ the bot was changed

class Recheck(NamedTuple):
    """Result of `recheck_channels`."""

    total: int  # active channels checked + 'left' channels that became active again
    missing: int  # active channels where the new bot is not an admin (now 'left')
    failed: int  # active channels that could not be checked (network trouble)
    restored: int  # 'left' channels where the new bot is an admin with the right to post (now active)


async def recheck_channels(bot: Any, db: Database, *, pause: float = RECHECK_PAUSE) -> Recheck:
    """Re-check the channels with the (new) bot: active channels where it is not an admin become
    'left'; 'left' channels where it is an admin with the posting right become active again (e.g.
    the owner switched back to the previous bot — nothing else would bring them back, Telegram sends
    no update for a membership that did not change)."""
    active = await db.list_channels([CH_ACTIVE])
    left = await db.list_channels([CH_LEFT])
    missing = failed = restored = 0
    for index, channel in enumerate(active + left):
        if index:
            await asyncio.sleep(pause)
        was_active = index < len(active)
        try:
            row = await channels_service.refresh_channel(bot, db, channel["id"])
        except Exception as exc:  # network trouble: the channel stays as it is
            if was_active:
                failed += 1
                log.warning("Не удалось проверить канал %s: %s", channel["id"], exc)
            else:
                log.info("Не удалось проверить неактивный канал %s: %s", channel["id"], exc)
            continue
        if was_active and row.get("status") == CH_LEFT:
            missing += 1
        elif not was_active and row.get("status") == CH_ACTIVE:
            restored += 1
    return Recheck(len(active) + restored, missing, failed, restored)


def bot_changed_text(username: str | None, total: int, missing: int, failed: int = 0, *,
                     restored: int = 0, panel_token_rejected: bool = False) -> str:
    name = f"@{html.escape(username)}" if username else "нового бота"
    first = f"🤖 Бот публикаций сменён на {name}."
    if panel_token_rejected:
        first += (" Telegram не принял токен, заданный в веб-панели, — бот работает с токеном из .env "
                  "(веб-панель → Настройки → Бот).")
    if not total:
        lines = [first + " Подключённых каналов пока нет — добавьте нового бота администратором "
                         "с правом публикации в свои каналы."]
    elif missing:
        lines = [first + f" Проверьте каналы: в {missing} из {total} каналов новый бот не администратор — "
                         "добавьте его администратором с правом публикации."]
    else:
        lines = [first + f" Новый бот — администратор во всех каналах ({total})."]
    if restored:
        lines.append(f"Снова подключены каналы, где новый бот — администратор: {restored}.")
    if failed:
        lines.append(f"Не удалось проверить каналов: {failed} — обновите их в разделе «📢 Каналы».")
    lines.append("Фото и видео, присланные в Telegram прежнему боту, загрузите заново — новый бот "
                 "не может их отправить.")
    return "\n".join(lines)


async def track_bot_identity(bot: Any, db: Database, me: User, token_state: TokenState | None = None) -> None:
    """Remember which bot runs; when it is another one than last time, re-check the channels and
    tell the admins (through the new bot — failures ignored, an admin may not have pressed /start
    in it yet; the notice says when the panel token was rejected), then store the new id."""
    try:
        stored = await db.get_setting(BOT_ID_SETTING)
        if stored == str(me.id):
            return
        if stored is not None:
            log.warning("Бот публикаций сменён: был id %s, теперь @%s (id %s) — проверяю каналы",
                        stored, me.username, me.id)
            result = await recheck_channels(bot, db)
            log.info("Каналы проверены: %d, новый бот не администратор в %d, не удалось проверить %d, "
                     "снова подключено %d", result.total, result.missing, result.failed, result.restored)
            rejected = bool(token_state and token_state.panel_token_rejected)
            await posting.notify_admins(bot, db, bot_changed_text(
                me.username, result.total, result.missing, result.failed,
                restored=result.restored, panel_token_rejected=rejected))
        await db.set_setting(BOT_ID_SETTING, str(me.id))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Не удалось проверить каналы после смены бота")


async def seed_admins(db: Database, admin_ids: Iterable[int]) -> None:
    """Insert ADMIN_IDS; the first one becomes the owner unless an owner already exists."""
    has_owner = await db.fetchone("SELECT 1 FROM admins WHERE is_owner = 1") is not None
    for index, user_id in enumerate(admin_ids):
        await db.add_admin(user_id, is_owner=(index == 0 and not has_owner))


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def warn_about_password(db: Database, config: Config) -> None:
    """Start-up warnings about the panel password: none at all on a network address (WEB_PASSWORD
    in .env counts), or a WEB_PASSWORD that is too short / too long to log in with."""
    fixed = fixed_password(config)
    if fixed and len(fixed) > MAX_PASSWORD_LENGTH:
        log.warning("WEB_PASSWORD длиннее %d символов — войти в веб-панель с ним не получится.",
                    MAX_PASSWORD_LENGTH)
    elif fixed and len(fixed) < MIN_PASSWORD_LENGTH:
        log.warning("WEB_PASSWORD короче %d символов — задайте пароль подлиннее.", MIN_PASSWORD_LENGTH)
    if not is_loopback(config.web_host) and await get_password_hash(db, config) is None:
        log.warning("Веб-панель открыта без пароля для сети! Задайте WEB_PASSWORD в .env "
                    "или пароль в разделе «Настройки» панели.")


def allowed_updates(dp: Dispatcher) -> list[str]:
    return sorted(set(dp.resolve_used_update_types()) | REQUIRED_UPDATES)


async def _until_stopped(awaitable: Awaitable[T], stop: asyncio.Event) -> tuple[bool, T | None]:
    """Run `awaitable` unless `stop` is set first: (True, result) or (False, None) when stopped."""
    task = asyncio.ensure_future(awaitable)
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
    if not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        return False, None
    return True, task.result()


async def wait_for_telegram(bot: Bot, stop: asyncio.Event | None = None, *,
                            initial_delay: float = TELEGRAM_RETRY_START,
                            max_delay: float = TELEGRAM_RETRY_MAX) -> User | None:
    """getMe until Telegram answers, with exponential backoff (`initial_delay` … `max_delay`).

    Uses the cached `bot.me()`, so aiogram's polling does not call getMe again. Returns None when
    `stop` was set while waiting; raises TelegramUnauthorizedError for a wrong token.
    """
    stop = stop or asyncio.Event()
    delay = initial_delay
    while not stop.is_set():
        try:
            done, me = await _until_stopped(bot.me(), stop)
        except TelegramUnauthorizedError:
            raise
        except Exception as exc:  # network down, DNS not ready, Telegram 5xx, flood control…
            pause = float(exc.retry_after) if isinstance(exc, TelegramRetryAfter) else delay
            log.warning("Telegram недоступен (%s) — повтор через %.0f с. Веб-панель и планировщик работают.",
                        exc, pause)
            finished, _ = await _until_stopped(asyncio.sleep(pause), stop)
            if not finished:
                return None
            delay = min(delay * 2, max_delay)
            continue
        return me if done else None
    return None


def _interrupted_text(posts: list[dict]) -> str:
    lines = ["⚠️ <b>Публикация была прервана перезапуском бота</b>"]
    for item in posts:
        if item["status"] == POST_PARTIAL:
            state = "опубликован не во все каналы"
        elif item["status"] == POST_SCHEDULED:
            state = ("публикация в один из каналов прервана — проверьте его, остальные каналы получат "
                     "пост по расписанию")
        else:
            state = "не опубликован"
        lines.append(f"• Пост #{item['id']} — {html.escape(state)}")
    lines.append("Проверьте каналы и при необходимости повторите публикацию в «🗂 Посты».")
    return "\n".join(lines)


async def after_connect(bot: Bot, db: Database, interrupted: list[dict], *, attempts: int = 5,
                        retry_delay: float = 5.0) -> None:
    """Background start-up chores once Telegram is reachable: the command menu (retried) and a
    notice to admins about publications interrupted by the previous shutdown."""
    for attempt in range(attempts):
        try:
            await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
            break
        except Exception as exc:
            log.warning("Не удалось установить список команд бота: %s", exc)
            if attempt + 1 < attempts:
                await asyncio.sleep(retry_delay * 2 ** attempt)
    if interrupted:
        await posting.notify_admins(bot, db, _interrupted_text(interrupted))


async def drain(dp: Dispatcher, timeout: float = SHUTDOWN_TIMEOUT) -> bool:
    """After polling stopped: let update handlers still running (e.g. a publish started from the
    bot) and any publish/edit/delete operation finish before the session and DB are closed.

    Returns False when `timeout` passed first; handlers still running then are cancelled (and get
    CANCEL_GRACE seconds to record it) while the database is still open.
    """
    deadline = time.monotonic() + timeout
    tasks = [t for t in getattr(dp, "_handle_update_tasks", ()) if not t.done()]
    if tasks:
        log.info("Жду завершения %d обработчиков…", len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            log.warning("Не дождался %d обработчиков — прерываю их", len(pending))
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=CANCEL_GRACE)
            return False
    if posting.active_operations():
        log.info("Жду завершения публикаций…")
    return await posting.wait_idle(max(0.0, deadline - time.monotonic()))


class ShutdownBudget:
    """One time budget for the whole graceful shutdown: draining handlers, the scheduler's last
    run and the remaining publish operations share it. The clock starts at `start()` (when the
    stop begins) or at the first `remaining()`."""

    def __init__(self, total: float) -> None:
        self.total = total
        self._deadline: float | None = None

    def start(self) -> None:
        if self._deadline is None:
            self._deadline = time.monotonic() + self.total

    def remaining(self) -> float:
        self.start()
        assert self._deadline is not None
        return max(0.0, self._deadline - time.monotonic())


async def _stop_scheduler(scheduler: Scheduler, budget: ShutdownBudget) -> None:
    scheduler.request_stop()
    await scheduler.stop(budget.remaining())


async def _wait_operations(budget: ShutdownBudget) -> None:
    """Last step before the bot session and the DB close: publish operations still running
    (e.g. one cut by the cancellation of its web request) record their outcome."""
    if not await posting.wait_idle(max(budget.remaining(), FINAL_GRACE)):
        log.warning("Публикации не завершились за отведённое время — при запуске они будут отмечены прерванными")


class _RunControl:
    """How one `run()` ends: `stop` is set by SIGINT/SIGTERM (`on_signal`) or by the web panel's
    restart hook (`request_restart`); a signal always wins over a restart."""

    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.signalled = False
        self.restart_requested = False

    def on_signal(self) -> None:
        self.signalled = True
        self.stop.set()

    def request_restart(self) -> None:
        if not self.restart_requested:
            log.info("Перезапуск бота по запросу из веб-панели…")
        self.restart_requested = True
        self.stop.set()

    @property
    def restarting(self) -> bool:
        return self.restart_requested and not self.signalled


async def _stop_polling_on(stop: asyncio.Event, dp: Any) -> None:
    """Stop polling once `stop` is set (a signal or a restart request)."""
    await stop.wait()
    stop_polling = getattr(dp, "stop_polling", None)
    if stop_polling is not None:
        with suppress(RuntimeError):  # polling already finished
            await stop_polling()


async def _start_site(runner: web.AppRunner, config: Config, *, attempts: int = 1) -> bool:
    """Bind the web panel's port; after a restart the old socket may need a moment to go away."""
    for attempt in range(attempts):
        try:
            await web.TCPSite(runner, config.web_host, config.web_port).start()
            return True
        except OSError as exc:
            if attempt + 1 >= attempts:
                log.error("Не удалось запустить веб-панель на %s:%s: %s", config.web_host, config.web_port, exc)
                return False
            await asyncio.sleep(SITE_BIND_RETRY)
    return False


def _install_stop_signals(stop: asyncio.Event, on_signal: Callable[[], None] | None = None) -> None:
    """Graceful stop on SIGINT/SIGTERM (`on_signal`, default: set `stop`), also while polling."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, on_signal or stop.set)


def _restore_default_signals() -> None:
    """While shutting down, a second Ctrl+C / SIGTERM aborts at once instead of being ignored."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


def _finish(control: _RunControl, runtime: Runtime) -> int | str:
    """Result of a run that stopped normally: RESTART when the panel asked for it, else 0."""
    if control.restarting:
        runtime.panel_token_rejected = False  # a new / removed panel token: try the panel token again
        return RESTART
    return 0


async def run(config: Config, runtime: Runtime | None = None, *,
              bot_factory: BotFactory | None = None) -> int | str:
    """Run until polling stops. Returns the process exit code, or RESTART when the bot has to
    start again (see the module docstring). `bot_factory(token)` makes the Bot (tests stub it)."""
    runtime = runtime if runtime is not None else Runtime()
    make_bot = bot_factory or default_bot_factory
    control = _RunControl()
    async with AsyncExitStack() as stack:
        db = Database(config.db_path)
        await db.connect()
        stack.push_async_callback(db.close)
        await db.migrate()
        recovered = await db.recover_stuck_sending()
        interrupted = [item for item in recovered if item["interrupted"]]
        if recovered:
            log.warning("Публикация %d пост(ов) была прервана перезапуском — статусы восстановлены",
                        len(recovered))
        await seed_admins(db, config.admin_ids)

        token, token_state = await resolve_token(db, config, runtime)
        log.info("Токен бота: %s (%s)", "задан в веб-панели" if token_state.source == TOKEN_SOURCE_PANEL
                 else "из .env", token_state.masked)
        bot = make_bot(token)
        stack.push_async_callback(bot.session.close)
        budget = ShutdownBudget(SHUTDOWN_TIMEOUT)
        # Runs after the web panel and the scheduler are stopped, before the session / DB close.
        stack.push_async_callback(_wait_operations, budget)

        scheduler = Scheduler(bot, db, config, stop_timeout=SHUTDOWN_TIMEOUT)
        dp = create_dispatcher(db, config, scheduler)
        dp["token_state"] = token_state  # «⚙️ Настройки» in the bot
        scheduler.start()
        stack.push_async_callback(_stop_scheduler, scheduler, budget)

        webapp = create_web_app(bot, db, config, scheduler)
        webapp[TOKEN_STATE_KEY] = token_state
        webapp[RESTART_KEY] = control.request_restart
        access_log = logging.getLogger("aiohttp.access")
        # Requests still running at shutdown were waited for by drain() (publishes are tracked);
        # anything else (e.g. a file upload to the panel) gets CANCEL_GRACE seconds.
        runner = web.AppRunner(webapp, access_log=access_log, shutdown_timeout=CANCEL_GRACE)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        if not await _start_site(runner, config, attempts=SITE_BIND_ATTEMPTS if runtime.restarts else 1):
            return 1
        log.info("Веб-панель: %s", config.web_url)
        await warn_about_password(db, config)

        _install_stop_signals(control.stop, control.on_signal)
        try:
            me = await wait_for_telegram(bot, control.stop, initial_delay=TELEGRAM_RETRY_START,
                                         max_delay=TELEGRAM_RETRY_MAX)
        except TelegramUnauthorizedError:
            _restore_default_signals()
            if token_state.source == TOKEN_SOURCE_PANEL:
                log.error("Telegram не принял токен бота, заданный в веб-панели (%s) — перезапускаюсь "
                          "с BOT_TOKEN из .env", token_state.masked)
                runtime.panel_token_rejected = True
                return RESTART
            log.error("Telegram не принял BOT_TOKEN — проверьте токен в файле .env")
            return 1
        if me is None:
            _restore_default_signals()
            log.info("Перезапуск…" if control.restarting else "Остановка…")
            return _finish(control, runtime)
        log.info("Бот @%s запущен. Остановка — Ctrl+C", me.username)
        background = (
            ("startup-chores", after_connect(bot, db, interrupted, retry_delay=COMMANDS_RETRY_DELAY)),
            ("bot-identity", track_bot_identity(bot, db, me, token_state)),
            ("polling-stopper", _stop_polling_on(control.stop, dp)),
        )
        for name, coro in background:
            stack.push_async_callback(_cancel, asyncio.create_task(coro, name=name))

        await dp.start_polling(
            bot,
            allowed_updates=allowed_updates(dp),
            handle_signals=False,  # our handlers stay installed: they stop polling via `control.stop`
            close_bot_session=False,  # closed by the exit stack after the scheduler is stopped
        )
        _restore_default_signals()
        log.info("Перезапуск…" if control.restarting else "Остановка…")
        scheduler.request_stop()  # no new scheduled post is picked while the running ones finish
        budget.start()
        await drain(dp, budget.remaining())
    # Everything is closed here: web runner (port released), scheduler, bot session, database.
    return _finish(control, runtime)


def serve(config: Config, *, bot_factory: BotFactory | None = None, runtime: Runtime | None = None) -> int:
    """Run the app until it stops for good. Every RESTART starts `run()` again in a fresh event
    loop, so nothing of the previous run (tasks, sessions, sockets) can leak into the next one."""
    runtime = runtime if runtime is not None else Runtime()
    while True:
        result = asyncio.run(run(config, runtime, bot_factory=bot_factory))
        if result != RESTART:
            return int(result)
        runtime.restarts += 1
        log.info("Запускаю бота заново…")


def main() -> int:
    try:
        config = load_config()
    except (RuntimeError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level)
    log.info("Бот публикаций, версия %s", config.version)
    try:
        return serve(config)
    except KeyboardInterrupt:  # Ctrl+C before the stop handlers were installed / between runs
        return 0


if __name__ == "__main__":
    sys.exit(main())

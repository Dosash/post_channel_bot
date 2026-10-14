"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app import __version__

BASE_DIR = Path(__file__).resolve().parent.parent

# One segment of WEB_PREFIX: characters that need no escaping in a URL path.
_PREFIX_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_HTTP_URL = re.compile(r"https?://[^\s/?#]+", re.IGNORECASE)

# ------------------------------------------------------------------ the bot token (SPEC_V22)
#
# `BOT_TOKEN` from .env is required and is the fallback. The owner may set another bot's token in
# the web panel («Настройки» → «Бот»): it is stored in the DB setting BOT_TOKEN_SETTING and wins
# over .env while Telegram accepts it. BOT_ID_SETTING remembers which bot ran last (a change
# triggers a re-check of the channels). A token is a secret: never log or return it — mask it.

BOT_TOKEN_SETTING = "bot_token"
BOT_ID_SETTING = "bot_id"
TOKEN_SOURCE_ENV = "env"
TOKEN_SOURCE_PANEL = "panel"
MAX_TOKEN_LENGTH = 256
BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
# A token inside a longer text (e.g. an API URL ".../bot<token>/getMe" in an error message).
_TOKEN_IN_TEXT = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


def is_bot_token(value: object) -> bool:
    """`value` looks like a token from @BotFather: ``<bot id>:<secret>``."""
    return isinstance(value, str) and len(value) <= MAX_TOKEN_LENGTH and BOT_TOKEN_RE.fullmatch(value) is not None


def mask_token(token: str | None) -> str:
    """What may be shown or logged instead of a token: ``'…'`` + its last 4 characters."""
    return "…" + token[-4:] if token else ""


def redact_tokens(text: str) -> str:
    """`text` with every bot token in it masked (for log records and error messages)."""
    return _TOKEN_IN_TEXT.sub(lambda match: mask_token(match.group(0)), text)


class RedactTokensFilter(logging.Filter):
    """Masks bot tokens in log records (an aiohttp error may quote an API URL with the token,
    aiosqlite logs SQL parameters at DEBUG level) — on top of never logging a token on purpose."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken record is the formatter's business
            return True
        redacted = redact_tokens(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and record.exc_info[1] is not None and not record.exc_text:
            record.exc_text = redact_tokens(logging.Formatter().formatException(record.exc_info))
        return True


# Libraries that log raw data which may contain the token: aiosqlite logs every SQL call with its
# parameters at DEBUG level (the panel token is stored in the settings table).
_RAW_DATA_LOGGERS = ("aiosqlite",)


def install_log_redaction(*, root_handlers: bool = True) -> None:
    """Mask bot tokens in log output: on the loggers that quote raw data (records are masked when
    they are made) and, with `root_handlers`, on every handler of the root logger (all records
    pass them). Idempotent."""
    targets: list[logging.Filterer] = [logging.getLogger(name) for name in _RAW_DATA_LOGGERS]
    if root_handlers:
        targets += logging.getLogger().handlers
    for target in targets:
        if not any(isinstance(f, RedactTokensFilter) for f in target.filters):
            target.addFilter(RedactTokensFilter())


@dataclass
class TokenState:
    """Which token the running bot uses (no secret in here): `source` "env" | "panel", the masked
    token and whether a token set in the panel was rejected by Telegram (→ running on .env)."""

    source: str = TOKEN_SOURCE_ENV
    masked: str = ""
    panel_token_rejected: bool = False


def normalize_prefix(value: str | None) -> str:
    """URL prefix of the web panel: ``'posts'``, ``'/posts/'`` → ``'/posts'``; ``''``, ``'/'`` → ``''``.

    Nested prefixes are allowed (``'tools/posts'`` → ``'/tools/posts'``). Raises ValueError for
    characters that do not belong in a URL path and for ``.`` / ``..`` segments.
    """
    text = (value or "").strip()
    segments = [part for part in text.split("/") if part]
    for part in segments:
        if part in (".", "..") or not _PREFIX_SEGMENT.fullmatch(part):
            raise ValueError(
                f"WEB_PREFIX «{text}»: допустимы латинские буквы, цифры и символы - _ . ~ "
                "(например, /posts)"
            )
    return "".join(f"/{part}" for part in segments)


def normalize_hub_url(value: str | None) -> str | None:
    """HUB_URL: a path on the same site (``/``) or an http(s) URL; empty → None (no hub link)."""
    text = (value or "").strip()
    if not text:
        return None
    if (text.startswith("/") and not text.startswith("//")) or _HTTP_URL.match(text):
        return text
    raise ValueError(f"HUB_URL «{text}»: укажите путь (например, /) или адрес http(s)://…")


@dataclass
class Config:
    bot_token: str
    admin_ids: list[int] = field(default_factory=list)
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_public_url: str | None = None
    web_password: str | None = None  # WEB_PASSWORD: fixed panel password from .env (overrides the panel setting)
    web_prefix: str = ""  # WEB_PREFIX: the panel is served under this path ('' = at the site root)
    hub_url: str | None = None  # HUB_URL: «← Все панели» link in the panel sidebar (None = no link)
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    db_path: Path = BASE_DIR / "data" / "bot.db"
    uploads_dir: Path = BASE_DIR / "data" / "uploads"
    log_level: str = "INFO"
    version: str = __version__

    def __post_init__(self) -> None:
        self.web_prefix = normalize_prefix(self.web_prefix)
        self.hub_url = normalize_hub_url(self.hub_url)

    @property
    def web_url(self) -> str:
        """URL of the web panel to show in the bot: WEB_PUBLIC_URL as given, else the local address."""
        if self.web_public_url:
            return self.web_public_url
        host = "127.0.0.1" if self.web_host in ("0.0.0.0", "::", "") else self.web_host
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        return f"http://{host}:{self.web_port}{normalize_prefix(self.web_prefix)}/"


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.append(int(part))
    return ids


def _port(raw: str) -> int:
    try:
        port = int(raw.strip() or "8080")
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise ValueError(f"WEB_PORT «{raw}»: укажите номер порта от 1 до 65535")
    return port


def _timezone(raw: str) -> ZoneInfo:
    name = raw.strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError) as exc:  # ZoneInfoNotFoundError is a KeyError
        raise ValueError(f"TIMEZONE «{name}»: неизвестный часовой пояс (пример: Europe/Moscow)") from exc


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан (см. .env.example)")
    cfg = Config(
        bot_token=token,
        admin_ids=_parse_ids(os.getenv("ADMIN_IDS", "")),
        web_host=os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        web_port=_port(os.getenv("WEB_PORT", "8080")),
        web_public_url=os.getenv("WEB_PUBLIC_URL", "").strip() or None,
        web_password=os.getenv("WEB_PASSWORD", "") or None,
        web_prefix=os.getenv("WEB_PREFIX", ""),
        hub_url=os.getenv("HUB_URL", ""),
        tz=_timezone(os.getenv("TIMEZONE", "UTC")),
        db_path=_path(os.getenv("DB_PATH"), BASE_DIR / "data" / "bot.db"),
        uploads_dir=_path(os.getenv("UPLOADS_DIR"), BASE_DIR / "data" / "uploads"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
    return cfg

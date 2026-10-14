"""Web panel authentication.

* No password (the default) → the panel is open.
* A password set in the panel is stored in the setting ``web_password_hash`` as
  ``scrypt$<salt_hex>$<hash_hex>`` (``hashlib.scrypt``, n=2**14, r=8, p=1).
* ``WEB_PASSWORD`` in ``.env`` (``config.web_password``) is a fixed password that overrides the
  panel setting; it cannot be set, changed or removed from the panel (400, `FIXED_PASSWORD_ERROR`).
  ``password_source`` in ``/api/auth/status`` and ``/api/settings``: ``"env"`` | ``"panel"`` | null.
* Session cookie ``pcb_session`` = ``<issued_ts>.<hmac_sha256(key, f"{issued}:{password_hash}")>``,
  path ``<prefix>/``. Sessions live 30 days; changing or removing the password (also WEB_PASSWORD
  in .env) invalidates all of them (the hash is part of the signed message).
* The signing key (`session_key`) mixes two secrets: the random setting ``web_secret`` (in the DB)
  and BOT_TOKEN from .env (``config.bot_token`` — never the token set in the panel, which lives in
  the DB). Both the panel password hash and ``web_secret`` are in ``data/bot.db``, so with the DB
  alone a copy of it (a backup) would be enough to mint a valid cookie; with the .env secret mixed
  in it is not. Trade-off: replacing BOT_TOKEN in .env logs every panel session out (once, like
  the deploy of this change); whoever has .env has the bot token anyway, which is worth more than
  a panel session. Changing the bot in the panel (SPEC_V22) does not touch sessions.
* Several ``pcb_session`` cookies may reach the server: a stale one with ``Path=/`` from a panel
  served without a prefix on the same host (cookies are per host, not per port) is sent *after*
  the current ``<prefix>/`` one. A request is authenticated when any of them is valid, and login /
  logout / password changes also expire the cookie on the legacy paths (``/`` and ``<prefix>``).
* Every mutating ``/api`` request (POST/PUT/PATCH/DELETE) must carry ``X-Requested-With: pcb``
  — a custom header cannot be sent cross-site without a CORS preflight, which is never granted.
* ``/api/health`` is always open: no host check, no CSRF header, no session.
* All paths are relative to the panel prefix (``WEB_PREFIX``, see `app.web.urls`).
* Login brute-force guard: after 5 wrong passwords from one IP the IP is locked out for 30 s.
  An attempt is counted *before* the slow password check, so parallel requests cannot slip
  past the limit.
* While the panel is open, ``/api`` answers only when the browser addressed it by an IP address,
  ``localhost`` or a configured host name. This blocks DNS-rebinding attacks from web pages.
* Reverse proxy (opt-in, env ``TRUST_PROXY``): ``1`` trusts ``X-Forwarded-For`` /
  ``X-Forwarded-Proto`` from proxies on this machine (loopback peers); a comma-separated list of
  IPs / networks (``127.0.0.1, 172.17.0.0/16``) trusts those peers instead. The client IP is the
  last ``X-Forwarded-For`` entry — the one appended by the trusted proxy. Off by default: a
  forged header is never trusted from an untrusted peer.

Also contains the small JSON helpers (``json_error``, ``read_json``) shared with ``app.web.api``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.http_exceptions import HttpProcessingError

from app.config import Config
from app.db import Database, now
from app.web.keys import CONFIG_KEY, DB_KEY, LOGIN_GUARD_KEY, PREFIX_KEY, TRUSTED_PROXIES_KEY
from app.web.urls import cookie_path, local_path, prefixed_router

log = logging.getLogger(__name__)

PASSWORD_HASH_SETTING = "web_password_hash"
SECRET_SETTING = "web_secret"
COOKIE_NAME = "pcb_session"
SESSION_TTL = 30 * 24 * 3600
CLOCK_SKEW = 300  # a cookie "issued in the future" by more than this is rejected
SESSION_KEY_CONTEXT = b"pcb-session-key:"  # domain separation of the derived signing key
MAX_SESSION_COOKIES = 5  # pcb_session values of one request that are checked
MAX_SESSION_TOKEN_LENGTH = 128  # longer values cannot be a session token
EXPIRED_COOKIE = 'Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0'

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "pcb"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 256

MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 30.0

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

# Pseudo-hash of the fixed WEB_PASSWORD (it is never stored): "env$<sha256 hex>".
ENV_HASH_PREFIX = "env$"
TRUST_PROXY_ENV = "TRUST_PROXY"

JSON_BODY_LIMIT = 2 * 1024 * 1024  # JSON requests are small; uploads use multipart
_READ_CHUNK = 64 * 1024

AUTH_REQUIRED = "auth_required"  # machine-readable error the SPA reacts to by showing the login form
FIXED_PASSWORD_ERROR = "Пароль задан в .env (WEB_PASSWORD) — изменить его можно только там"
HEALTH_PATH = "/api/health"
PASSWORD_SOURCE_ENV = "env"
PASSWORD_SOURCE_PANEL = "panel"

_ISSUED_RE = re.compile(r"[0-9]{1,12}")
_HEX_RE = re.compile(r"[0-9a-f]+")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


# ------------------------------------------------------------------ JSON helpers

class JsonError(web.HTTPException):
    """HTTP error with a JSON body ``{"error": message, **extra}``. Raise it from handlers."""

    def __init__(self, status: int, message: str, *, headers: dict[str, str] | None = None,
                 **extra: Any) -> None:
        self.status_code = status
        payload = {"error": message, **extra}
        super().__init__(
            text=json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
            headers=headers,
        )


def json_error(status: int, message: str, *, headers: dict[str, str] | None = None,
               **extra: Any) -> JsonError:
    return JsonError(status, message, headers=headers, **extra)


async def _read_body(request: web.Request, limit: int) -> bytes:
    if request.content_length is not None and request.content_length > limit:
        raise json_error(413, "Запрос слишком большой")
    data = bytearray()
    try:
        while chunk := await request.content.read(_READ_CHUNK):
            data += chunk
            if len(data) > limit:
                raise json_error(413, "Запрос слишком большой")
    except HttpProcessingError as exc:  # broken chunked encoding, length mismatch…
        raise json_error(400, "Некорректный запрос") from exc
    return bytes(data)


async def read_json(request: web.Request, *, limit: int = JSON_BODY_LIMIT) -> dict[str, Any]:
    """Request body as a JSON object; an empty body is ``{}``.

    Malformed bodies (bad charset or encoding, invalid or absurdly nested JSON, not an object)
    → 400; a body over `limit` bytes → 413.
    """
    if not request.body_exists:
        return {}
    data = await _read_body(request, limit)
    try:
        raw = data.decode(request.charset or "utf-8")
    except (UnicodeDecodeError, LookupError) as exc:  # LookupError: unknown charset name
        raise json_error(400, "Некорректная кодировка запроса") from exc
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise json_error(400, "Некорректный JSON в запросе") from exc
    if not isinstance(parsed, dict):
        raise json_error(400, "Ожидается JSON-объект")
    return parsed


# ------------------------------------------------------------------ password hashing

def hash_password(password: str, salt: bytes | None = None) -> str:
    """scrypt hash in the storage format ``scrypt$<salt_hex>$<hash_hex>``."""
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def env_password_hash(password: str) -> str:
    """Stable pseudo-hash of the fixed WEB_PASSWORD (part of the session signature)."""
    return ENV_HASH_PREFIX + hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash. Malformed hashes never match."""
    if stored.startswith(ENV_HASH_PREFIX):
        return hmac.compare_digest(env_password_hash(password), stored)
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    salt_hex, hash_hex = parts[1], parts[2]
    if not (_HEX_RE.fullmatch(salt_hex) and _HEX_RE.fullmatch(hash_hex)) or len(salt_hex) % 2 or len(hash_hex) % 2:
        return False
    expected = bytes.fromhex(hash_hex)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=len(expected),
    )
    return hmac.compare_digest(digest, expected)


async def _hash_password_async(password: str) -> str:
    # scrypt takes tens of milliseconds of CPU — keep the event loop responsive.
    return await asyncio.to_thread(hash_password, password)


async def _verify_password_async(password: str, stored: str) -> bool:
    if stored.startswith(ENV_HASH_PREFIX):
        return verify_password(password, stored)
    return await asyncio.to_thread(verify_password, password, stored)


# ------------------------------------------------------------------ settings storage

def fixed_password(config: Any) -> str | None:
    """The WEB_PASSWORD from .env, if configured."""
    value = getattr(config, "web_password", None) if config is not None else None
    return value if isinstance(value, str) and value else None


async def get_password_hash(db: Database, config: Any = None) -> str | None:
    """Hash every login is checked against: the fixed WEB_PASSWORD (when `config` has one),
    else the password set in the panel; None = no password, the panel is open."""
    fixed = fixed_password(config)
    if fixed:
        return env_password_hash(fixed)
    return await db.get_setting(PASSWORD_HASH_SETTING) or None


async def password_source(db: Database, config: Any = None) -> str | None:
    """Where the password every login is checked against comes from: ``"env"`` (WEB_PASSWORD),
    ``"panel"`` (set in the panel) or None (no password, the panel is open)."""
    if fixed_password(config):
        return PASSWORD_SOURCE_ENV
    if await db.get_setting(PASSWORD_HASH_SETTING):
        return PASSWORD_SOURCE_PANEL
    return None


async def set_password(db: Database, password: str) -> str:
    """Store a new password (invalidates every session). Returns the new hash."""
    password_hash = await _hash_password_async(password)
    await db.set_setting(PASSWORD_HASH_SETTING, password_hash)
    return password_hash


async def reset_password(db: Database) -> None:
    """Remove the password: the panel becomes open. Used by the bot settings (owner only).

    A fixed WEB_PASSWORD from .env is not affected.
    """
    await db.set_setting(PASSWORD_HASH_SETTING, None)


async def get_secret(db: Database) -> str:
    """HMAC secret for session cookies, generated once and kept in the settings table."""
    secret = await db.get_setting(SECRET_SETTING)
    if secret:
        return secret
    # INSERT ... DO NOTHING + re-read: concurrent first requests agree on one secret.
    await db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
        (SECRET_SETTING, secrets.token_hex(32)),
    )
    secret = await db.get_setting(SECRET_SETTING)
    assert secret
    return secret


async def _password_hash(request: web.Request) -> str | None:
    return await get_password_hash(request.app[DB_KEY], request.app[CONFIG_KEY])


# ------------------------------------------------------------------ sessions

def session_key(config: Any, db_secret: str) -> str:
    """Signing key of the session cookies: ``web_secret`` from the DB keyed with BOT_TOKEN from
    .env, so a copy of the DB alone cannot mint a session (see the module docstring)."""
    env_secret = getattr(config, "bot_token", None) or ""
    return hmac.new(env_secret.encode("utf-8"), SESSION_KEY_CONTEXT + db_secret.encode("utf-8"),
                    hashlib.sha256).hexdigest()


async def _session_key(request: web.Request) -> str:
    return session_key(request.app[CONFIG_KEY], await get_secret(request.app[DB_KEY]))


def make_session_token(secret: str, password_hash: str, issued: int) -> str:
    signature = hmac.new(
        secret.encode("utf-8"), f"{issued}:{password_hash}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{issued}.{signature}"


def verify_session_token(token: str, secret: str, password_hash: str, now_ts: int | None = None) -> bool:
    issued_raw, sep, _ = token.partition(".")
    if not sep or not _ISSUED_RE.fullmatch(issued_raw):
        return False
    issued = int(issued_raw)
    current = now_ts if now_ts is not None else now()
    if issued > current + CLOCK_SKEW or current - issued > SESSION_TTL:
        return False
    expected = make_session_token(secret, password_hash, issued)
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def session_cookies(request: web.Request) -> list[str]:
    """Every ``pcb_session`` value of the request (at most MAX_SESSION_COOKIES), in the order the
    browser sent them. ``request.cookies`` keeps only the last value of a repeated name — the one
    with the shortest path, i.e. a stale cookie of a panel without a prefix."""
    values: list[str] = []
    for header in request.headers.getall("Cookie", []):
        for part in header.split(";"):
            name, sep, value = part.strip().partition("=")
            if not sep or name.strip() != COOKIE_NAME:
                continue
            value = value.strip().strip('"')
            if value and len(value) <= MAX_SESSION_TOKEN_LENGTH:
                values.append(value)
                if len(values) >= MAX_SESSION_COOKIES:
                    return values
    return values


async def is_authenticated(request: web.Request) -> bool:
    """True when no password is set or the request carries a valid session cookie (any of the
    ``pcb_session`` cookies it sent)."""
    password_hash = await _password_hash(request)
    if password_hash is None:
        return True
    tokens = session_cookies(request)
    if not tokens:
        return False
    key = await _session_key(request)
    return any(verify_session_token(token, key, password_hash) for token in tokens)


def legacy_cookie_paths(request: web.Request) -> list[str]:
    """Paths where an old panel may have left a ``pcb_session`` cookie that shadows the current
    one: ``/`` (before WEB_PREFIX) and ``<prefix>`` without the slash. None without a prefix."""
    prefix = request.app.get(PREFIX_KEY, "")
    return ["/", prefix] if prefix else []


def _expire_legacy_cookies(request: web.Request, response: web.StreamResponse) -> None:
    # Raw headers: aiohttp keeps response cookies by name, so del_cookie() here would replace the
    # session cookie set for <prefix>/ instead of adding a second Set-Cookie.
    for path in legacy_cookie_paths(request):
        response.headers.add("Set-Cookie", f'{COOKIE_NAME}=""; {EXPIRED_COOKIE}; Path={path}')


async def issue_session(request: web.Request, response: web.StreamResponse, password_hash: str) -> None:
    token = make_session_token(await _session_key(request), password_hash, now())
    _expire_legacy_cookies(request, response)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL,
        path=cookie_path(request),
        httponly=True,
        samesite="Strict",
        secure=True if request.secure else None,
    )


def clear_session(request: web.Request, response: web.StreamResponse) -> None:
    _expire_legacy_cookies(request, response)
    response.del_cookie(COOKIE_NAME, path=cookie_path(request))


# ------------------------------------------------------------------ brute-force guard

class LoginGuard:
    """Counts wrong passwords per client key (IP) and locks the key out for a while.

    `begin()` checks the lockout and counts the attempt as a failure in one synchronous step,
    *before* the slow password check; `success()` then forgets the key. So requests that run in
    parallel are limited exactly like sequential ones.
    """

    MAX_ENTRIES = 10_000  # guard against unbounded growth; expired entries are pruned first

    def __init__(self, max_failures: int = MAX_LOGIN_FAILURES, lockout: float = LOCKOUT_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures = max_failures
        self.lockout = lockout
        self._clock = clock
        # key -> [failures since the last lockout, locked_until (clock value)]
        self._state: dict[str, list[float]] = {}

    def retry_after(self, key: str) -> int:
        """Seconds left of the lockout for `key` (0 when it may try)."""
        entry = self._state.get(key)
        if entry is None:
            return 0
        left = entry[1] - self._clock()
        return max(1, int(left + 0.999)) if left > 0 else 0

    def begin(self, key: str) -> int:
        """Reserve one password check for `key`: 0 when allowed (the attempt is counted as a
        failure until `success()`), else the seconds left of the lockout."""
        wait = self.retry_after(key)
        if not wait:
            self.failure(key)
        return wait

    def failure(self, key: str) -> None:
        if len(self._state) >= self.MAX_ENTRIES:
            self._prune()
        entry = self._state.setdefault(key, [0, 0.0])
        entry[0] += 1
        if entry[0] >= self.max_failures:
            entry[0] = 0
            entry[1] = self._clock() + self.lockout

    def success(self, key: str) -> None:
        self._state.pop(key, None)

    def _prune(self) -> None:
        current = self._clock()
        for key in [k for k, (count, until) in self._state.items() if until <= current and count == 0]:
            del self._state[key]
        if len(self._state) >= self.MAX_ENTRIES:  # still full: forget the oldest half
            for key in list(self._state)[: self.MAX_ENTRIES // 2]:
                del self._state[key]


def _client_key(request: web.Request) -> str:
    return request.remote or "unknown"


def _lockout_error(wait: int) -> JsonError:
    return json_error(429, f"Слишком много неудачных попыток. Подождите {wait} с",
                      headers={"Retry-After": str(wait)})


def _check_lockout(request: web.Request) -> None:
    wait = request.app[LOGIN_GUARD_KEY].retry_after(_client_key(request))
    if wait:
        raise _lockout_error(wait)


async def _check_current_password(request: web.Request, password: Any, stored: str, *,
                                  wrong_status: int, wrong_message: str) -> None:
    """Verify a typed password with the brute-force guard applied."""
    _check_lockout(request)
    if not isinstance(password, str) or not password:
        raise json_error(400, "Введите пароль" if wrong_status == 401 else "Введите текущий пароль")
    guard = request.app[LOGIN_GUARD_KEY]
    key = _client_key(request)
    # No await between the lockout check and counting the attempt (see LoginGuard).
    wait = guard.begin(key)
    if wait:
        raise _lockout_error(wait)
    if len(password) > MAX_PASSWORD_LENGTH or not await _verify_password_async(password, stored):
        log.warning("Wrong web panel password from %s", key)
        raise json_error(wrong_status, wrong_message)
    guard.success(key)


def _validate_new_password(value: Any) -> str:
    if not isinstance(value, str) or len(value) < MIN_PASSWORD_LENGTH:
        raise json_error(400, f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    if len(value) > MAX_PASSWORD_LENGTH:
        raise json_error(400, f"Пароль слишком длинный (максимум {MAX_PASSWORD_LENGTH} символов)")
    return value


# ------------------------------------------------------------------ host check (DNS rebinding)

def host_allowed(host: str | None, config: Config) -> bool:
    """IP literals, localhost, WEB_HOST and the WEB_PUBLIC_URL host are accepted."""
    if not host:
        return True  # no Host header: not a browser, cannot be a rebinding attack
    try:
        hostname = (urlsplit(f"//{host}").hostname or "").rstrip(".")
    except ValueError:
        return False
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    if hostname == (config.web_host or "").lower():
        return True
    if config.web_public_url:
        public = (urlsplit(config.web_public_url).hostname or "").rstrip(".")
        if hostname == public:
            return True
    return False


# ------------------------------------------------------------------ reverse proxy (opt-in)

_LOOPBACK: tuple[Network, ...] = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "да"})
_FALSE_WORDS = frozenset({"", "0", "false", "no", "off", "нет"})


def trusted_proxies(config: Any = None, env: dict[str, str] | None = None) -> tuple[Network, ...]:
    """Peers whose X-Forwarded-* headers are trusted (empty = feature off).

    Taken from `config.trust_proxy` when the config has it, else from the env var TRUST_PROXY:
    ``1``/``true`` → loopback only; a comma-separated list of IPs / networks → those; ``0`` or
    empty → nothing. Invalid entries are logged and ignored.
    """
    raw = getattr(config, "trust_proxy", None) if config is not None else None
    if raw is None:
        raw = (env if env is not None else os.environ).get(TRUST_PROXY_ENV, "")
    if isinstance(raw, bool):
        return _LOOPBACK if raw else ()
    text = str(raw).strip()
    if text.lower() in _FALSE_WORDS:
        return ()
    if text.lower() in _TRUE_WORDS:
        return _LOOPBACK
    networks: list[Network] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("TRUST_PROXY: «%s» — не IP-адрес и не подсеть, пропускаю", part)
    return tuple(networks)


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _forwarded_client(request: web.Request) -> str | None:
    """Client IP appended by the proxy: the last X-Forwarded-For entry (else X-Real-IP)."""
    entries = [e.strip() for value in request.headers.getall("X-Forwarded-For", []) for e in value.split(",")]
    entries = [e for e in entries if e]
    candidate = entries[-1] if entries else request.headers.get("X-Real-IP")
    ip = _parse_ip(candidate)
    return str(ip) if ip is not None else None


@web.middleware
async def proxy_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Behind a trusted reverse proxy: take the client IP and scheme from X-Forwarded-*."""
    networks = request.app[TRUSTED_PROXIES_KEY]
    if networks:
        peer = _parse_ip(request.remote)
        if peer is not None and any(peer.version == net.version and peer in net for net in networks):
            changes: dict[str, str] = {}
            client = _forwarded_client(request)
            if client:
                changes["remote"] = client
            proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
            if proto in ("http", "https"):
                changes["scheme"] = proto
            if changes:
                request = request.clone(**changes)
    return await handler(request)


# ------------------------------------------------------------------ middleware

def is_api_path(path: str) -> bool:
    """`path` is relative to the panel prefix (see app.web.urls.local_path)."""
    return path == "/api" or path.startswith("/api/")


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Host check (open panel only), CSRF header check and session check for ``<prefix>/api``."""
    path = local_path(request)
    if path is None or not is_api_path(path) or path == HEALTH_PATH:
        return await handler(request)
    password_hash = await _password_hash(request)
    if password_hash is None and not host_allowed(request.host, request.app[CONFIG_KEY]):
        raise json_error(
            403,
            "Панель открыта без пароля, поэтому доступна только по IP-адресу или localhost. "
            "Задайте пароль (в панели или WEB_PASSWORD в .env) или укажите этот адрес в WEB_PUBLIC_URL.",
        )
    if request.method in MUTATING_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise json_error(403, "Запрос отклонён: нет заголовка X-Requested-With")
    if path.startswith("/api/auth/") or password_hash is None:
        return await handler(request)
    if not await is_authenticated(request):
        raise json_error(401, AUTH_REQUIRED)
    return await handler(request)


# ------------------------------------------------------------------ handlers

async def auth_status(request: web.Request) -> web.Response:
    password_hash = await _password_hash(request)
    config = request.app[CONFIG_KEY]
    return web.json_response({
        "password_set": password_hash is not None,
        "password_fixed": fixed_password(config) is not None,
        "password_source": await password_source(request.app[DB_KEY], config),
        "authenticated": await is_authenticated(request),
    })


async def login(request: web.Request) -> web.Response:
    _check_lockout(request)
    body = await read_json(request)
    stored = await _password_hash(request)
    if stored is None:
        return web.json_response({"ok": True})  # nothing to log into: the panel is open
    await _check_current_password(request, body.get("password"), stored,
                                  wrong_status=401, wrong_message="Неверный пароль")
    response = web.json_response({"ok": True})
    await issue_session(request, response, stored)
    return response


async def logout(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    clear_session(request, response)
    return response


def _refuse_if_fixed(request: web.Request) -> None:
    if fixed_password(request.app[CONFIG_KEY]):
        raise json_error(400, FIXED_PASSWORD_ERROR)


async def change_password(request: web.Request) -> web.Response:
    """Set the first password, or change it (the current one is required then)."""
    _refuse_if_fixed(request)
    db = request.app[DB_KEY]
    body = await read_json(request)
    new_password = _validate_new_password(body.get("new"))
    stored = await get_password_hash(db)
    if stored is not None:
        await _check_current_password(request, body.get("current"), stored,
                                      wrong_status=403, wrong_message="Неверный текущий пароль")
    password_hash = await set_password(db, new_password)
    log.info("Web panel password %s", "changed" if stored else "set")
    response = web.json_response({"ok": True, "password_set": True})
    # The author of the change stays logged in; every other session is invalidated.
    await issue_session(request, response, password_hash)
    return response


async def remove_password(request: web.Request) -> web.Response:
    _refuse_if_fixed(request)
    db = request.app[DB_KEY]
    body = await read_json(request)
    stored = await get_password_hash(db)
    if stored is None:
        raise json_error(400, "Пароль не установлен")
    await _check_current_password(request, body.get("current"), stored,
                                  wrong_status=403, wrong_message="Неверный текущий пароль")
    await reset_password(db)
    log.info("Web panel password removed")
    response = web.json_response({"ok": True, "password_set": False})
    clear_session(request, response)
    return response


def setup_routes(app: web.Application) -> None:
    r = prefixed_router(app)
    r.add_get("/api/auth/status", auth_status)
    r.add_post("/api/auth/login", login)
    r.add_post("/api/auth/logout", logout)
    r.add_post("/api/auth/password", change_password)
    r.add_delete("/api/auth/password", remove_password)

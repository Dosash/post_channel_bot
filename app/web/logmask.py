"""Bot tokens never reach the log — the safety net under "never log a token on purpose".

Library errors may quote a token: aiohttp's ``ClientResponseError`` carries the request URL —
``…/file/bot<token>/<path>`` for a file download, ``…/bot<token>/<method>`` for an API call — and
aiosqlite logs SQL parameters (the token set in the panel is a setting) at DEBUG level.

`install_log_mask(*tokens)` (idempotent; `app.web.create_web_app` calls it with BOT_TOKEN from
.env and the running bot's token, `app.__main__` should call it at startup too) masks every log
record made from then on — of every logger, for every handler, also the ones added later:

* anything shaped like a bot token (``<bot id>:<secret>``, also URL-encoded ``%3A``),
* the registered tokens verbatim (and URL-encoded), whatever their shape,

in the message, the formatted traceback and the stack info. It wraps the logging record factory,
so a record is masked once, when it is made. `TokenMaskFilter` does the same as a
``logging.Filter`` for handlers set up elsewhere. A token is replaced by ``…`` + its last 4
characters (`app.config.mask_token`), so log lines stay recognisable.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any
from urllib.parse import quote

from app.config import mask_token

__all__ = ["TokenMaskFilter", "install_log_mask", "mask_record", "mask_secrets", "remember_token"]

# A token inside a longer text: "<bot id>:<secret>", the colon possibly URL-encoded.
TOKEN_SHAPE = re.compile(r"\d{5,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}")
# Tokens shorter than this are not masked verbatim (that would mask ordinary words).
MIN_KNOWN_TOKEN = 16

_lock = threading.Lock()
_known: tuple[str, ...] = ()  # registered tokens and their URL-encoded forms, longest first
_installed = False


def remember_token(token: Any) -> None:
    """Mask `token` verbatim from now on (also its URL-encoded form)."""
    global _known
    if not isinstance(token, str) or len(token) < MIN_KNOWN_TOKEN:
        return
    with _lock:
        forms = set(_known) | {token, quote(token, safe="")}
        _known = tuple(sorted(forms, key=len, reverse=True))


def mask_secrets(text: str, *tokens: Any) -> str:
    """`text` with every bot token masked: the registered ones, `tokens` and anything shaped like
    a token."""
    if not text:
        return text
    extra = tuple(t for t in tokens if isinstance(t, str) and t)
    for token in (*extra, *(quote(t, safe="") for t in extra), *_known):
        if token in text:
            text = text.replace(token, mask_token(token))
    return TOKEN_SHAPE.sub(lambda match: mask_token(match.group(0)), text)


def mask_record(record: logging.LogRecord) -> None:
    """Mask the tokens of a log record in place (message, traceback, stack info)."""
    try:
        message = record.getMessage()
    except Exception:  # a broken record is the formatter's business
        return
    masked = mask_secrets(message)
    if masked != message:
        record.msg, record.args = masked, None
    if record.exc_info and record.exc_info[1] is not None and not record.exc_text:
        try:
            text = logging.Formatter().formatException(record.exc_info)
        except Exception:
            text = ""
        masked = mask_secrets(text)
        if masked != text:
            record.exc_text = masked  # formatters use the cached text instead of formatting again
    if record.stack_info:
        record.stack_info = mask_secrets(record.stack_info)


class TokenMaskFilter(logging.Filter):
    """``logging.Filter`` that masks bot tokens in the records passing it (never drops one)."""

    def filter(self, record: logging.LogRecord) -> bool:
        mask_record(record)
        return True


def _masking_factory(previous: Any) -> Any:
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        mask_record(record)
        return record

    factory._pcb_token_mask = True  # type: ignore[attr-defined]
    return factory


def install_log_mask(*tokens: Any) -> None:
    """Mask bot tokens in every log record from now on; `tokens` are also masked verbatim.
    Idempotent: calling it again only registers more tokens."""
    global _installed
    for token in tokens:
        remember_token(token)
    with _lock:
        if _installed and getattr(logging.getLogRecordFactory(), "_pcb_token_mask", False):
            return
        current = logging.getLogRecordFactory()
        if not getattr(current, "_pcb_token_mask", False):
            logging.setLogRecordFactory(_masking_factory(current))
        _installed = True

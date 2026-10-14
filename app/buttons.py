"""Inline URL buttons of a post: text format, validation and keyboard building.

Buttons are stored as rows::

    [[{"text": str, "url": str, "style": "primary" | "success" | "danger" | None}]]

``style`` is the Bot API button color: ``primary`` — синяя, ``success`` — зелёная,
``danger`` — красная, ``None`` — обычная (the app default look).

Text format (what the admin types in the bot)::

    Текст кнопки - https://link.com
    Купить - https://shop.com - зелёная | Сайт - example.com - синяя
    🔴 Отписаться - https://t.me/x

* Every non-empty line is one row. Buttons in a row are separated by ``|`` with whitespace (or the
  line edge) on both sides, so a ``|`` inside a URL never splits the row.
* Fields of a button are separated by a dash (``-``, ``–`` or ``—``) with whitespace on both
  sides. The URL is the last field, optionally followed by a color field (any alias accepted by
  :func:`normalize_style`). Everything before the URL is the button text, so the text itself may
  contain " - ".
* Without a trailing color field, a leading color emoji (🔴🟢🔵🟥🟩🟦) of the text sets the color
  and is stripped from the text. When an explicit color field is present it wins and the text is
  taken literally (the emoji stays) — this is what lets :func:`buttons_to_text` round-trip texts
  that really start with such an emoji.
* Runs of whitespace inside a button text are collapsed to a single space.

Round trip: ``parse_buttons(buttons_to_text(rows)) == rows`` for any ``rows`` produced by
:func:`parse_buttons` or :func:`validate_buttons`, with one documented exception: a ``|`` standing
between spaces inside a button text (possible only for buttons made in the web panel) is written
as the fullwidth bar ``｜`` because a plain one would split the row.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

STYLES = ("primary", "success", "danger")  # blue, green, red
STYLE_LABELS: dict[str | None, str] = {
    None: "Обычная",
    "primary": "Синяя",
    "success": "Зелёная",
    "danger": "Красная",
}
STYLE_EMOJI: dict[str | None, str] = {None: "⚪️", "primary": "🔵", "success": "🟢", "danger": "🔴"}
# Canonical trailing color words written by buttons_to_text().
STYLE_WORDS: dict[str | None, str] = {
    None: "обычная",
    "primary": "синяя",
    "success": "зелёная",
    "danger": "красная",
}
MAX_ROWS = 10
MAX_PER_ROW = 8
MAX_TEXT = 64

# Plain text without HTML special characters: safe to embed into an HTML message as is.
FORMAT_HELP = (
    "Каждая строка — отдельный ряд кнопок, кнопки в ряду разделяются « | ».\n"
    "Кнопка: Текст - ссылка - цвет (цвет можно не указывать).\n"
    "Цвета: зелёная, синяя, красная — или эмодзи 🟢 🔵 🔴 в начале текста.\n"
    "Ссылки: https://…, t.me/…, @username, tg://…\n"
    "\n"
    "Пример:\n"
    "Текст кнопки - https://link.com\n"
    "Купить - https://shop.com - зелёная | Сайт - example.com - синяя\n"
    "🔴 Отписаться - https://t.me/x"
)


class ButtonsError(ValueError):
    """Invalid buttons. ``str(err)`` is a Russian, user-presentable message.

    ``line`` is the 1-based line number for errors raised by :func:`parse_buttons`.
    """

    def __init__(self, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


# ---------------------------------------------------------------------------- styles

def _style_key(value: str) -> str:
    """Lookup key for style aliases: case/ё-insensitive, emoji variation selector removed."""
    return value.strip().lower().replace("ё", "е").replace("️", "")


_STYLE_ALIASES_BY_STYLE: dict[str | None, tuple[str, ...]] = {
    None: ("", "none", "default", "обычная", "обычный", "обычное", "⚪"),
    "primary": ("primary", "blue", "синяя", "синий", "синее", "🔵", "🟦"),
    "success": ("success", "green", "зелёная", "зелёный", "зелёное", "🟢", "🟩"),
    "danger": ("danger", "red", "красная", "красный", "красное", "🔴", "🟥"),
}
_STYLE_ALIASES: dict[str, str | None] = {
    _style_key(alias): style
    for style, aliases in _STYLE_ALIASES_BY_STYLE.items()
    for alias in aliases
}
# Leading color emoji of a button text (optionally with the emoji variation selector).
_LEADING_EMOJI_RE = re.compile("^([🔴🟢🔵🟥🟩🟦])️?\\s*")
_EMOJI_STYLE = {"🔴": "danger", "🟥": "danger", "🟢": "success", "🟩": "success",
                "🔵": "primary", "🟦": "primary"}


def normalize_style(value: str | None) -> str | None:
    """Map a color alias (English/Russian word or color emoji, any case) to a Bot API style.

    ``None``, ``''``, ``default`` and ``обычная`` mean "no style" (``None``).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ButtonsError(f"Некорректный цвет кнопки: {value}")
    key = _style_key(value)
    if key not in _STYLE_ALIASES:
        raise ButtonsError(
            f"Неизвестный цвет «{_shorten(value.strip())}». "
            "Доступно: зелёная, синяя, красная или обычная"
        )
    return _STYLE_ALIASES[key]


def _is_style_word(value: str) -> bool:
    """True for a non-empty color alias (used to detect the optional trailing color field)."""
    return bool(value.strip()) and _style_key(value) in _STYLE_ALIASES


# ------------------------------------------------------------------------------ urls

_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_LABEL = r"[^\W_](?:[\w-]{0,61}[^\W_])?"
_TLD = r"(?:[^\W\d_]{2,63}|xn--[A-Za-z0-9-]{1,59})"
_HOSTNAME_RE = re.compile(rf"(?:{_LABEL}\.)+{_TLD}\.?")
# Scheme-less address: domain with a letter TLD, optional port and path/query/fragment.
_BARE_URL_RE = re.compile(rf"(?:{_LABEL}\.)+{_TLD}\.?(?::\d{{1,5}})?(?:[/?#]\S*)?")


def normalize_url(value: str) -> str:
    """Validate a button URL and bring it to a form Telegram accepts.

    ``http(s)://`` and ``tg://`` links are kept (scheme lowercased); ``t.me/x``, ``www.x.com``,
    ``x.com/path`` get ``https://``; ``@username`` becomes ``https://t.me/username``.
    Raises :class:`ButtonsError` for empty values, whitespace and unsupported schemes
    (``javascript:``, ``data:``, ``ftp:``, ``mailto:`` …).
    """
    if not isinstance(value, str):
        raise ButtonsError("Ссылка должна быть строкой")
    url = value.strip()
    if not url:
        raise ButtonsError("Пустая ссылка")
    if any(ch.isspace() for ch in url):
        raise ButtonsError(f"Ссылка не должна содержать пробелов: «{_shorten(url)}»")
    if not url.isprintable():
        raise ButtonsError(f"Ссылка содержит недопустимые символы: «{_shorten(url)}»")

    if url.startswith("@"):
        match = _USERNAME_RE.fullmatch(url)
        if not match:
            raise ButtonsError(
                f"Некорректный username «{_shorten(url)}»: "
                "от 4 до 32 символов — латиница, цифры и _"
            )
        return f"https://t.me/{match.group(1)}"

    lower = url.lower()
    if lower.startswith(("http://", "https://")):
        return _normalize_web_url(url)
    if lower.startswith("tg://"):
        if len(url) == len("tg://"):
            raise ButtonsError(f"Некорректная ссылка «{url}»")
        return "tg://" + url[len("tg://"):]
    # Checked before the scheme test: "example.com:8080/x" looks like a scheme otherwise.
    if _BARE_URL_RE.fullmatch(url):
        return "https://" + url
    if _SCHEME_RE.match(url):
        raise ButtonsError(
            f"Неподдерживаемая ссылка «{_shorten(url)}»: "
            "в кнопках можно использовать http://, https://, tg:// или @username"
        )
    raise ButtonsError(f"Некорректная ссылка «{_shorten(url)}»")


def _normalize_web_url(url: str) -> str:
    """Lowercase the scheme of an http(s) URL and check that it has a sensible host."""
    scheme, rest = url.split("://", 1)
    url = f"{scheme.lower()}://{rest}"
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 — raises ValueError for a malformed/out-of-range port
    except ValueError:
        raise ButtonsError(f"Некорректная ссылка «{_shorten(url)}»") from None
    if not _is_valid_host(host):
        raise ButtonsError(f"Некорректный адрес сайта в ссылке «{_shorten(url)}»")
    return url


def _is_valid_host(host: str) -> bool:
    if host == "localhost" or _HOSTNAME_RE.fullmatch(host):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_url(value: str) -> bool:
    try:
        normalize_url(value)
    except ButtonsError:
        return False
    return True


# ------------------------------------------------------------------- text parsing

# A dash with whitespace on both sides (or at the start of the segment) separates fields.
_FIELD_SEP_RE = re.compile(r"(?:(?<=\s)|^)[-–—](?=\s)")
# A bar with whitespace (or the line edge) on both sides separates buttons of a row.
_ROW_SEP_RE = re.compile(r"(?:(?<=\s)|^)\|(?=\s|$)")
_FULLWIDTH_BAR = "｜"


def parse_buttons(text: str) -> list[list[dict]]:
    """Parse the text format (see module docstring) into normalized button rows.

    Raises :class:`ButtonsError` with a message prefixed ``"Строка N: "``.
    """
    if text is None:
        return []
    if not isinstance(text, str):
        raise ButtonsError("Кнопки должны быть заданы текстом")
    rows: list[list[dict]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        segments = [seg.strip() for seg in _ROW_SEP_RE.split(raw_line.strip())]
        segments = [seg for seg in segments if seg]
        if not segments:
            continue
        if len(rows) >= MAX_ROWS:
            raise ButtonsError(
                f"Строка {line_no}: слишком много рядов кнопок (максимум {MAX_ROWS})",
                line=line_no,
            )
        if len(segments) > MAX_PER_ROW:
            raise ButtonsError(
                f"Строка {line_no}: слишком много кнопок в ряду: {len(segments)} "
                f"(максимум {MAX_PER_ROW})",
                line=line_no,
            )
        rows.append(_parse_row(segments, line_no))
    return rows


def _parse_row(segments: list[str], line_no: int) -> list[dict]:
    row = []
    for index, segment in enumerate(segments, start=1):
        try:
            row.append(_parse_button(segment))
        except ButtonsError as exc:
            where = f" (кнопка {index})" if len(segments) > 1 else ""
            raise ButtonsError(
                f"Строка {line_no}: {_lower_first(exc.message)}{where}", line=line_no
            ) from None
    return row


def _parse_button(segment: str) -> dict:
    """Parse one ``Текст - ссылка [- цвет]`` segment."""
    seps = list(_FIELD_SEP_RE.finditer(segment))
    if not seps:
        if _is_url(segment):
            raise ButtonsError("Не указан текст кнопки — формат «Текст - ссылка»")
        raise ButtonsError("Не найдена ссылка — формат «Текст - ссылка» (дефис с пробелами)")

    last = segment[seps[-1].end():].strip()
    url_sep = seps[-1]
    url_raw = last
    explicit_style = False
    style: str | None = None

    if len(seps) >= 2 and _is_style_word(last):
        candidate = segment[seps[-2].end():seps[-1].start()].strip()
        if _is_url(candidate):
            style = normalize_style(last)
            explicit_style = True
            url_sep, url_raw = seps[-2], candidate

    if not explicit_style:
        url = _url_or_explain(segment, seps, last)
    else:
        url = normalize_url(url_raw)

    text = segment[:url_sep.start()]
    if not explicit_style:
        emoji = _LEADING_EMOJI_RE.match(text.strip())
        if emoji:
            style = _EMOJI_STYLE[emoji.group(1)]
            text = text.strip()[emoji.end():]
    return {"text": _check_text(text), "url": url, "style": style}


def _url_or_explain(segment: str, seps: list[re.Match[str]], last: str) -> str:
    """Normalize the last field as the URL, or raise the most helpful error for it."""
    try:
        return normalize_url(last)
    except ButtonsError:
        # "Текст - ссылка - фиолетовая": a URL followed by something that is not a link
        # attempt is most likely a misspelled color.
        if len(seps) >= 2 and not any(ch in last for ch in "/:.@"):
            before_last = segment[seps[-2].end():seps[-1].start()].strip()
            if _is_url(before_last):
                normalize_style(last)  # raises "Неизвестный цвет …"
        if not last or _is_style_word(last):
            raise ButtonsError("Не найдена ссылка — формат «Текст - ссылка»") from None
        if not any(ch in last for ch in "/:.@"):
            raise ButtonsError(
                f"Не найдена ссылка: «{_shorten(last)}» не похоже на адрес"
            ) from None
        raise


def _clean_text(text: str) -> str:
    return " ".join(text.split())


def _check_text(text: str) -> str:
    text = _clean_text(text)
    if not text:
        raise ButtonsError("Пустой текст кнопки")
    if len(text) > MAX_TEXT:
        raise ButtonsError(f"Текст кнопки длиннее {MAX_TEXT} символов ({len(text)})")
    return text


# ----------------------------------------------------------------- serialization

def buttons_to_text(rows: list[list[dict]]) -> str:
    """Canonical text form of ``rows`` that :func:`parse_buttons` reads back to the same rows.

    The color is written as a trailing Russian word; a default-styled button whose text starts
    with a color emoji gets an explicit ``обычная`` so the emoji is kept as text. A ``|`` between
    spaces inside a text is written as ``｜`` (see module docstring).
    """
    lines = []
    for row in rows or []:
        parts = [_button_to_text(button) for button in row or [] if isinstance(button, dict)]
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def _button_to_text(button: dict) -> str:
    text = _ROW_SEP_RE.sub(_FULLWIDTH_BAR, _clean_text(str(button.get("text") or "")))
    url = str(button.get("url") or "").strip()
    try:
        style = normalize_style(button.get("style"))
    except ButtonsError:
        style = None  # display helper: an unknown stored style is shown as default
    if style is not None or _LEADING_EMOJI_RE.match(text):
        return f"{text} - {url} - {STYLE_WORDS[style]}"
    return f"{text} - {url}"


def count_buttons(rows: list[list[dict]] | None) -> int:
    """Total number of buttons in ``rows``."""
    return sum(len(row) for row in rows or [] if isinstance(row, (list, tuple)))


# -------------------------------------------------------------------- validation

def validate_buttons(rows: object) -> list[list[dict]]:
    """Validate rows coming as JSON (web panel) and return a normalized deep copy.

    ``None`` → ``[]``. Empty rows and completely blank buttons (no text and no url) are dropped;
    URLs and style aliases are normalized; unknown keys are dropped. Errors name the 1-based row
    and button numbers of the input.
    """
    if rows is None:
        return []
    if not isinstance(rows, (list, tuple)):
        raise ButtonsError("Кнопки должны быть списком рядов")
    result: list[list[dict]] = []
    for row_no, row in enumerate(rows, start=1):
        if row is None:
            continue
        if not isinstance(row, (list, tuple)):
            raise ButtonsError(f"Ряд {row_no}: ожидается список кнопок")
        buttons = _validate_row(row, row_no)
        if not buttons:
            continue
        if len(buttons) > MAX_PER_ROW:
            raise ButtonsError(
                f"Ряд {row_no}: слишком много кнопок: {len(buttons)} (максимум {MAX_PER_ROW})"
            )
        result.append(buttons)
    if len(result) > MAX_ROWS:
        raise ButtonsError(f"Слишком много рядов кнопок: {len(result)} (максимум {MAX_ROWS})")
    return result


def _validate_row(row: list | tuple, row_no: int) -> list[dict]:
    buttons = []
    for button_no, button in enumerate(row, start=1):
        try:
            normalized = _validate_button(button)
        except ButtonsError as exc:
            raise ButtonsError(
                f"Ряд {row_no}, кнопка {button_no}: {_lower_first(exc.message)}"
            ) from None
        if normalized is not None:
            buttons.append(normalized)
    return buttons


def _validate_button(button: Any) -> dict | None:
    """Normalized button dict, or ``None`` for a completely blank one."""
    if not isinstance(button, dict):
        raise ButtonsError("Кнопка должна быть объектом с полями text и url")
    text = button.get("text")
    url = button.get("url")
    if text is not None and not isinstance(text, str):
        raise ButtonsError("Текст кнопки должен быть строкой")
    if url is not None and not isinstance(url, str):
        raise ButtonsError("Ссылка должна быть строкой")
    text = _clean_text(text or "")
    url = (url or "").strip()
    if not text and not url:
        return None
    return {
        "text": _check_text(text),
        "url": normalize_url(url),
        "style": normalize_style(button.get("style")),
    }


# ---------------------------------------------------------------------- keyboard

def build_markup(rows: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    """Inline keyboard for a post, or ``None`` when there are no buttons.

    Rows are passed through :func:`validate_buttons` first, so malformed stored data raises
    :class:`ButtonsError` (a ``ValueError``) instead of a Telegram error. ``style`` is omitted
    for default buttons.
    """
    clean = validate_buttons(rows)
    if not clean:
        return None
    keyboard = [[_make_button(button) for button in row] for row in clean]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _make_button(button: dict) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {"text": button["text"], "url": button["url"]}
    if button["style"]:
        kwargs["style"] = button["style"]
    return InlineKeyboardButton(**kwargs)


# ----------------------------------------------------------------------- helpers

def _shorten(value: str, limit: int = 60) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _lower_first(message: str) -> str:
    return message[:1].lower() + message[1:]

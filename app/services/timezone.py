"""The bot's timezone: TIMEZONE from .env, overridden by the `timezone` setting (panel / bot).

Every time the bot shows or reads — slots, «ко времени», the plan, statistics days — is in this
zone. It changes on the fly: `config.tz` is replaced and read at call time everywhere. Posts already
scheduled keep their moment (timestamps); slot times («09:00») are then taken in the new zone.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TIMEZONE_SETTING = "timezone"
SOURCE_ENV = "env"
SOURCE_SETTING = "settings"
UNKNOWN_ERROR = "Неизвестный часовой пояс «{name}». Пример: Europe/Moscow или UTC+3"

# Quick choice in the bot and the panel (order is the bot's button order: keep it stable).
COMMON = [
    ("Europe/Kaliningrad", "Калининград"),
    ("Europe/Moscow", "Москва"),
    ("Europe/Samara", "Самара"),
    ("Asia/Yekaterinburg", "Екатеринбург"),
    ("Asia/Omsk", "Омск"),
    ("Asia/Novosibirsk", "Новосибирск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Irkutsk", "Иркутск"),
    ("Asia/Yakutsk", "Якутск"),
    ("Asia/Vladivostok", "Владивосток"),
    ("Asia/Magadan", "Магадан"),
    ("Asia/Kamchatka", "Камчатка"),
    ("Europe/Minsk", "Минск"),
    ("Europe/Kyiv", "Киев"),
    ("Asia/Almaty", "Алматы"),
    ("Asia/Tashkent", "Ташкент"),
    ("Asia/Tbilisi", "Тбилиси"),
    ("Asia/Yerevan", "Ереван"),
    ("Asia/Baku", "Баку"),
    ("Europe/Istanbul", "Стамбул"),
    ("Asia/Dubai", "Дубай"),
    ("Europe/Berlin", "Берлин"),
    ("Europe/London", "Лондон"),
    ("America/New_York", "Нью-Йорк"),
    ("UTC", "UTC"),
]

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-−])\s*(\d{1,2})$", re.IGNORECASE)


def parse_timezone(raw: str) -> ZoneInfo:
    """IANA name ("Europe/Moscow", any case) or a whole-hour offset ("UTC+3", "+5"). ValueError
    with a Russian message otherwise."""
    name = (raw or "").strip()
    match = _OFFSET_RE.match(name.replace(" ", ""))
    if match:
        hours = int(match.group(2))
        if hours > 14:
            raise ValueError(UNKNOWN_ERROR.format(name=name))
        if hours == 0:
            return ZoneInfo("UTC")
        # Etc/GMT signs are inverted: UTC+3 is Etc/GMT-3
        return ZoneInfo(f"Etc/GMT{'-' if match.group(1) == '+' else '+'}{hours}")
    if not name or len(name) > 64 or name.startswith(("/", ".")) or ".." in name:
        raise ValueError(UNKNOWN_ERROR.format(name=name))
    for candidate in (name, _canonical_case(name)):
        try:
            return ZoneInfo(candidate)
        except (ValueError, KeyError, OSError):  # ZoneInfoNotFoundError is a KeyError
            continue
    raise ValueError(UNKNOWN_ERROR.format(name=name))


def _canonical_case(name: str) -> str:
    """"europe/moscow" → "Europe/Moscow" (the tz database is case-sensitive on Linux)."""
    if name.upper() in ("UTC", "GMT"):
        return name.upper()
    return "/".join("_".join(w[:1].upper() + w[1:].lower() for w in part.split("_")) for part in name.split("/"))


def tz_key(tz: Any) -> str:
    return getattr(tz, "key", None) or str(tz)


def offset_text(tz: ZoneInfo, now_ts: float | None = None) -> str:
    """"UTC+5", "UTC−3:30", "UTC" — the zone's current offset."""
    offset = datetime.fromtimestamp(now_ts if now_ts is not None else time.time(), tz).utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    if not minutes:
        return "UTC"
    sign = "+" if minutes > 0 else "−"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


def describe(tz: ZoneInfo) -> str:
    """"Asia/Yekaterinburg (UTC+5)"; a bare offset ("UTC+3" → Etc/GMT-3) is shown as "UTC+3"."""
    key = tz_key(tz)
    if key == "UTC" or key.startswith("Etc/"):
        return offset_text(tz)
    return f"{key} ({offset_text(tz)})"


def common_choices() -> list[dict]:
    """[{name, label, offset}] of COMMON for the panel."""
    out = []
    for name, label in COMMON:
        tz = ZoneInfo(name)
        out.append({"name": name, "label": label, "offset": offset_text(tz)})
    return out


def source(config: Any) -> str:
    """"settings" when the zone was changed in the panel / bot, "env" when it is TIMEZONE from .env."""
    return SOURCE_SETTING if getattr(config, "tz_from_setting", False) else SOURCE_ENV


async def load(db: Any, config: Any) -> None:
    """At start: apply the stored setting over TIMEZONE from .env (a bad stored value is ignored)."""
    stored = await db.get_setting(TIMEZONE_SETTING)
    if not stored:
        return
    try:
        config.tz = parse_timezone(stored)
    except ValueError:
        log.warning("Часовой пояс из настроек «%s» не распознан — используется TIMEZONE из .env", stored)
        return
    config.tz_from_setting = True
    log.info("Часовой пояс: %s (задан в настройках, в .env — %s)", tz_key(config.tz), tz_key(config.tz_env))


async def set_timezone(db: Any, config: Any, raw: str | None) -> ZoneInfo:
    """Change the zone (None or "" — back to TIMEZONE from .env). Raises ValueError for an
    unknown zone; nothing is changed then."""
    if raw is None or not str(raw).strip():
        await db.set_setting(TIMEZONE_SETTING, None)
        config.tz = config.tz_env
        config.tz_from_setting = False
    else:
        tz = parse_timezone(str(raw))
        await db.set_setting(TIMEZONE_SETTING, tz_key(tz))
        config.tz = tz
        config.tz_from_setting = True
    log.info("Часовой пояс изменён: %s", tz_key(config.tz))
    return config.tz

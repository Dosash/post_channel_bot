"""Tests for app.services.timezone: the bot's timezone set in the panel / bot over TIMEZONE."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app.services import timezone as tzs


@pytest.mark.parametrize(("raw", "key"), [
    ("Europe/Moscow", "Europe/Moscow"),
    ("europe/moscow", "Europe/Moscow"),
    ("america/new_york", "America/New_York"),
    ("utc", "UTC"),
    ("UTC+3", "Etc/GMT-3"),
    ("+5", "Etc/GMT-5"),
    ("GMT-4", "Etc/GMT+4"),
    ("UTC+0", "UTC"),
])
def test_parse_timezone(raw, key):
    assert tzs.tz_key(tzs.parse_timezone(raw)) == key


@pytest.mark.parametrize("raw", ["", "Mars/Base", "../etc/passwd", "/etc/localtime", "UTC+15", "x" * 80])
def test_parse_timezone_rejects(raw):
    with pytest.raises(ValueError, match="Неизвестный часовой пояс"):
        tzs.parse_timezone(raw)


def test_describe_and_offset():
    assert tzs.describe(ZoneInfo("Asia/Yekaterinburg")) == "Asia/Yekaterinburg (UTC+5)"
    assert tzs.describe(tzs.parse_timezone("UTC-3")) == "UTC−3"
    assert tzs.offset_text(ZoneInfo("Asia/Kolkata")) == "UTC+5:30"
    assert tzs.describe(ZoneInfo("UTC")) == "UTC"
    assert all(c["offset"].startswith("UTC") for c in tzs.common_choices())


async def test_set_load_and_reset(db, config):
    await db.migrate()
    env = config.tz
    await tzs.set_timezone(db, config, "Europe/Moscow")
    assert tzs.tz_key(config.tz) == "Europe/Moscow" and tzs.source(config) == tzs.SOURCE_SETTING

    config.tz, config.tz_from_setting = env, False  # a restart: TIMEZONE from .env, then the setting
    await tzs.load(db, config)
    assert tzs.tz_key(config.tz) == "Europe/Moscow" and config.tz_env is env

    with pytest.raises(ValueError):
        await tzs.set_timezone(db, config, "nope")
    assert tzs.tz_key(config.tz) == "Europe/Moscow"

    await tzs.set_timezone(db, config, None)
    assert config.tz is env and tzs.source(config) == tzs.SOURCE_ENV
    assert await db.get_setting(tzs.TIMEZONE_SETTING) is None


async def test_load_ignores_bad_setting(db, config):
    await db.migrate()
    await db.set_setting(tzs.TIMEZONE_SETTING, "Mars/Base")
    env = config.tz
    await tzs.load(db, config)
    assert config.tz is env and tzs.source(config) == tzs.SOURCE_ENV

"""The bot token never reaches the log (review finding #21): masking helpers, the log mask
installed by create_web_app, and Telegram file downloads for media previews."""
from __future__ import annotations

import logging

import pytest
from aiohttp import ClientResponseError, RequestInfo
from aiohttp.test_utils import TestClient, TestServer
from aiogram.types import File
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from app.web import api_media, create_web_app, logmask
from app.web.logmask import TokenMaskFilter, install_log_mask, mask_secrets

SECRET = "AAHfakeSecretForTestsOnly_0123456789xyz"  # not a real token
TOKEN = f"123456:{SECRET}"
OTHER_SECRET = "BBHanotherFakeSecretForTests_9876543210"
OTHER = f"654321:{OTHER_SECRET}"


def _collect(logger_name: str) -> tuple[logging.Logger, list[str]]:
    """A logger that does not propagate, with its own handler and no filters at all."""
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = Collect()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, lines


def _http_error(url: str, status: int = 502, message: str = "Bad Gateway") -> ClientResponseError:
    target = URL(url)
    info = RequestInfo(target, "GET", CIMultiDictProxy(CIMultiDict()), target)
    return ClientResponseError(info, (), status=status, message=message)


# ------------------------------------------------------------------ helpers

def test_mask_secrets_shapes_and_known_tokens():
    url = f"https://api.telegram.org/file/bot{TOKEN}/photos/file_1.jpg"
    assert mask_secrets(url) == f"https://api.telegram.org/file/bot…{SECRET[-4:]}/photos/file_1.jpg"
    encoded = f"bot123456%3A{SECRET}/getMe"
    assert SECRET not in mask_secrets(encoded)
    # a token of an unusual shape is masked when it is given / registered
    odd = "short:tok-en-of-a-test-server"
    assert odd not in mask_secrets(f"x {odd} y", odd)
    logmask.remember_token(odd)
    assert odd not in mask_secrets(f"x {odd} y")
    assert mask_secrets("") == "" and mask_secrets("nothing secret here") == "nothing secret here"
    logmask.remember_token("abc")  # too short to be masked verbatim
    assert mask_secrets("abc") == "abc"


def test_log_mask_covers_every_logger_and_handler():
    install_log_mask(OTHER)
    install_log_mask(OTHER)  # idempotent
    logger, lines = _collect("tests.logmask.isolated")
    logger.warning("Failed: %s", f"url=https://api.telegram.org/bot{TOKEN}/getUpdates")
    logger.info("token as is: %s", OTHER)
    try:
        raise RuntimeError(f"boom https://api.telegram.org/file/bot{TOKEN}/x.jpg")
    except RuntimeError:
        logger.exception("Unhandled")
    text = "\n".join(lines)
    assert SECRET not in text and OTHER_SECRET not in text
    assert f"bot…{SECRET[-4:]}/getUpdates" in text and "RuntimeError: boom" in text


def test_token_mask_filter():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "token %s", (TOKEN,), None)
    assert TokenMaskFilter().filter(record) is True
    assert SECRET not in record.getMessage()


async def test_create_web_app_installs_the_mask(db, config, bot, monkeypatch):
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)  # as if never installed
    monkeypatch.setattr(logmask, "_installed", False)
    bot.token = OTHER  # the running bot's token (aiogram's Bot.token)
    create_web_app(bot, db, config)
    logger, lines = _collect("tests.logmask.after_app")
    logger.error("panel token %s and %s", OTHER, f"bot{TOKEN}/getMe")
    assert SECRET not in lines[0] and OTHER_SECRET not in lines[0]


# ------------------------------------------------------------------ media preview downloads

@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    bot.token = TOKEN
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


async def test_download_error_does_not_log_the_file_url(client, db, bot, caplog):
    """aiohttp's ClientResponseError quotes https://api.telegram.org/file/bot<token>/<path>."""
    calls = []

    async def download_file(file_path, destination=None, timeout=30, **kwargs):
        calls.append(file_path)
        raise _http_error(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}")

    bot.download_file = download_file
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "photo", "file_id": "AgACleak"}])
    with caplog.at_level(logging.DEBUG):
        resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 502 and (await resp.json())["error"] == "Не удалось получить картинку из Telegram"
    logged = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert "Could not download Telegram file AgACleak: ClientResponseError: HTTP 502 Bad Gateway" in logged
    assert SECRET not in logged and "api.telegram.org/file" not in logged


async def test_download_uses_the_known_file_path(client, db, bot, config):
    """One getFile per preview: the file is fetched by the path getFile returned."""
    calls = []

    async def download_file(file_path, destination=None, timeout=30, **kwargs):
        calls.append((file_path, timeout))
        destination.write_bytes(b"\xff\xd8\xff\xe0jpeg")

    bot.download_file = download_file
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "photo", "file_id": "AgACpath"}])
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 200 and await resp.read() == b"\xff\xd8\xff\xe0jpeg"
    assert calls == [("photos/AgACpath.jpg", api_media.DOWNLOAD_TIMEOUT)]
    assert len(bot.calls_of("get_file")) == 1 and not bot.calls_of("download")


async def test_download_without_file_path_fails_cleanly(client, db, bot, monkeypatch):
    async def get_file(file_id, **kwargs):
        return File(file_id=file_id, file_unique_id="u", file_size=10)

    monkeypatch.setattr(bot, "get_file", get_file)
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "video", "file_id": "BAACnopath"}])
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 502 and (await resp.json())["error"] == "Не удалось получить видео из Telegram"


async def test_get_file_errors_are_masked(client, db, bot, caplog):
    bot.fail_on["get_file"] = RuntimeError(f"proxy said: https://api.telegram.org/bot{TOKEN}/getFile")
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "photo", "file_id": "AgACget"}])
    with caplog.at_level(logging.DEBUG):
        resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 502
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Could not get Telegram file AgACget: RuntimeError" in logged and SECRET not in logged


def test_error_text():
    exc = _http_error(f"https://api.telegram.org/file/bot{TOKEN}/a.jpg", 404, "Not Found")
    assert api_media._error_text(exc) == "ClientResponseError: HTTP 404 Not Found"

    class Bot:
        token = "odd-token-of-the-running-bot-xyz"

    assert api_media._error_text(ValueError("odd-token-of-the-running-bot-xyz!"), Bot()) == "ValueError: …-xyz!"

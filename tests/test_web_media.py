"""Web API: markdown rendering, media uploads (photo / video / GIF) and post media files."""
from __future__ import annotations

import asyncio
import re

import aiohttp
import pytest
from aiogram.types import File
from aiohttp.test_utils import TestClient, TestServer

from app.web import CLIENT_MAX_SIZE, api_media, create_web_app
from tests.fakes import bad_request

H = {"X-Requested-With": "pcb"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 100
JPEG = b"\xff\xd8\xff\xe0" + b"\x02" * 100
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x03" * 100
GIF = b"GIF89a" + b"\x04" * 100
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 12 + b"\x05" * 100
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8 + b"\x06" * 100
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100
NAME_RE = re.compile(r"[0-9a-f]{32}\.(jpg|png|webp|mp4|mov|gif)")


@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        yield c


async def _upload(client: TestClient, data: bytes, *, field: str = "file", filename: str = "pic.bin"):
    form = aiohttp.FormData()
    form.add_field(field, data, filename=filename, content_type="application/octet-stream")
    return await client.post("/api/upload", data=form, headers=H)


def _visible_files(config) -> list[str]:
    return sorted(p.name for p in config.uploads_dir.iterdir() if not p.name.startswith("."))


# ------------------------------------------------------------------ render

async def test_render_ok(client):
    resp = await client.post("/api/render", json={"text_md": "**Жирный** и _курсив_", "has_photo": False}, headers=H)
    data = await resp.json()
    assert resp.status == 200
    assert data == {"html": "<b>Жирный</b> и <i>курсив</i>", "length": 15, "limit": 4096, "error": None,
                    "has_custom_emoji": False, "premium_emoji": False}


async def test_render_limits_and_errors(client):
    resp = await client.post("/api/render", json={"text_md": "a" * 1100, "has_photo": True}, headers=H)
    data = await resp.json()
    assert resp.status == 200
    assert data["limit"] == 1024 and data["length"] == 1100
    assert "слишком длинный" in data["error"]
    assert data["html"] == "a" * 1100  # the preview is still shown

    resp = await client.post("/api/render", json={"text_md": "", "has_photo": False}, headers=H)
    data = await resp.json()
    assert data["html"] == "" and data["length"] == 0 and "Пост пустой" in data["error"]

    resp = await client.post("/api/render", json={"text_md": "", "has_photo": True}, headers=H)
    data = await resp.json()
    assert data["error"] is None and data["limit"] == 1024

    resp = await client.post("/api/render", json={"text_md": "👍"}, headers=H)
    assert (await resp.json())["length"] == 2


async def test_render_has_media(client):
    data = await (await client.post("/api/render", json={"text_md": "x", "has_media": True}, headers=H)).json()
    assert data["limit"] == 1024
    data = await (await client.post("/api/render", json={"text_md": "", "has_media": True}, headers=H)).json()
    assert data["error"] is None
    # has_media wins over the legacy flag.
    data = await (await client.post("/api/render", json={"text_md": "x", "has_media": False, "has_photo": True},
                                    headers=H)).json()
    assert data["limit"] == 4096


# ------------------------------------------------------------------ upload

@pytest.mark.parametrize("payload, ext, kind, mime", [
    (PNG, "png", "photo", "image/png"),
    (JPEG, "jpg", "photo", "image/jpeg"),
    (WEBP, "webp", "photo", "image/webp"),
    (GIF, "gif", "animation", "image/gif"),
    (MP4, "mp4", "video", "video/mp4"),
    (MOV, "mov", "video", "video/quicktime"),
])
async def test_upload_valid_media(client, config, payload, ext, kind, mime):
    resp = await _upload(client, payload)
    data = await resp.json()
    assert resp.status == 200, data
    name = data["path"]
    assert NAME_RE.fullmatch(name) and name.endswith("." + ext)
    assert data["type"] == kind and data["size"] == len(payload) and data["mime"] == mime
    assert data["url"] == f"/api/uploads/{name}"
    assert data["photo_path"] == (name if kind == "photo" else None)
    assert (config.uploads_dir / name).read_bytes() == payload

    resp = await client.get(data["url"])
    assert resp.status == 200
    assert await resp.read() == payload
    assert resp.content_type == mime
    assert resp.headers["Cache-Control"] == "private, max-age=86400"


async def test_upload_streams_large_chunks(client, config):
    payload = MP4 + bytes(range(256)) * 12_000  # ~3 MB: several read chunks and buffered writes
    resp = await _upload(client, payload)
    data = await resp.json()
    assert resp.status == 200 and data["size"] == len(payload)
    assert (config.uploads_dir / data["path"]).read_bytes() == payload
    assert _visible_files(config) == [data["path"]]  # no temp files left


async def test_video_range_request(client):
    data = await (await _upload(client, MP4)).json()
    resp = await client.get(data["url"], headers={"Range": "bytes=0-7"})
    assert resp.status == 206
    assert await resp.read() == MP4[:8]


async def test_upload_rejects_other_types(client, config):
    for payload in (HEIC, b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"", b"RIFF\x00\x00\x00\x00WAVE",
                    b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 50, b"GIF8", b"PK\x03\x04zip"):
        resp = await _upload(client, payload)
        assert resp.status == 400, payload
        assert (await resp.json())["error"] == "Поддерживаются только JPEG, PNG, WEBP, GIF, MP4 и MOV"
    assert list(config.uploads_dir.iterdir()) == []


async def test_upload_photo_too_large(client, config):
    # Slightly above the limit: stopped while streaming.
    resp = await _upload(client, PNG + b"\x00" * (10 * 1024 * 1024 - len(PNG) + 1))
    assert resp.status == 413
    assert (await resp.json())["error"] == "Файл больше 10 МБ"
    resp = await _upload(client, PNG + b"\x00" * (11 * 1024 * 1024))
    assert resp.status == 413
    assert list(config.uploads_dir.iterdir()) == []  # the temp file is removed too


async def test_upload_video_and_gif_limits(client, config, monkeypatch):
    monkeypatch.setitem(api_media.SIZE_LIMITS, "video", 2000)
    monkeypatch.setitem(api_media.SIZE_LIMITS, "animation", 2000)
    for payload in (MP4, GIF):
        resp = await _upload(client, payload + b"\x00" * 2000)
        assert resp.status == 413
        assert (await resp.json())["error"] == "Файл больше 50 МБ"
        resp = await _upload(client, payload + b"\x00" * 100)
        assert resp.status == 200
    assert len(_visible_files(config)) == 2
    assert [p for p in config.uploads_dir.iterdir() if p.name.startswith(".")] == []


async def test_photo_limit_is_not_the_video_limit(client, monkeypatch):
    monkeypatch.setitem(api_media.SIZE_LIMITS, "photo", 500)
    assert (await _upload(client, PNG + b"\x00" * 600)).status == 413
    assert (await _upload(client, MP4 + b"\x00" * 600)).status == 200


def test_client_max_size_is_52_mb():
    assert CLIENT_MAX_SIZE == 52 * 1024 * 1024


async def test_upload_requires_file_field(client):
    resp = await _upload(client, PNG, field="image")
    assert resp.status == 400
    assert (await resp.json())["error"] == "Файл не передан"
    resp = await client.post("/api/upload", json={"file": "x"}, headers=H)
    assert resp.status == 400
    assert "multipart" in (await resp.json())["error"]


async def test_upload_filename_is_ignored(client, config):
    resp = await _upload(client, PNG, filename="../../evil.png")
    name = (await resp.json())["path"]
    assert NAME_RE.fullmatch(name)
    assert (config.uploads_dir / name).is_file()


async def test_upload_malformed_multipart_is_400(client, caplog):
    """Review finding #18: a broken part header is a client error, not a 500 with a traceback."""
    boundary = "XyZ"
    body = (f"--{boundary}\r\nContent-Disposition form-data name=file\r\n\r\n").encode() + PNG + \
        f"\r\n--{boundary}--\r\n".encode()
    resp = await client.post("/api/upload", data=body, headers={
        **H, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "Некорректные данные формы"
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_get_upload_rejects_traversal(client, config, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    (config.uploads_dir / "notes.txt").write_text("x")
    (config.uploads_dir / ("tg_" + "a" * 16 + ".jpg")).write_bytes(JPEG)  # cache files are not uploads
    for path in ("/api/uploads/..%2Fsecret.txt", "/api/uploads/notes.txt",
                 "/api/uploads/%2e%2e%2fsecret.txt", "/api/uploads/" + "0" * 32 + ".png",
                 "/api/uploads/tg_" + "a" * 16 + ".jpg"):
        resp = await client.get(path)
        assert resp.status == 404, path
        assert await resp.json() == {"error": "Файл не найден"}


# ------------------------------------------------------------------ post media files

async def _uploaded(client: TestClient, payload: bytes) -> str:
    return (await (await _upload(client, payload)).json())["path"]


async def test_media_from_local_uploads(client, db):
    photo, video = await _uploaded(client, PNG), await _uploaded(client, MP4)
    post_id = await db.create_post(text_md="x", source="web", media=[
        {"type": "photo", "path": photo, "file_id": None}, {"type": "video", "path": video, "file_id": None}])
    resp = await client.get(f"/api/posts/{post_id}/media/0?v=abc")
    assert resp.status == 200 and await resp.read() == PNG
    assert resp.content_type == "image/png"
    assert resp.headers["Cache-Control"] == "private, max-age=3600"
    resp = await client.get(f"/api/posts/{post_id}/media/1")
    assert resp.status == 200 and await resp.read() == MP4 and resp.content_type == "video/mp4"

    for index in (2, 99):
        resp = await client.get(f"/api/posts/{post_id}/media/{index}")
        assert resp.status == 404 and (await resp.json())["error"] == "Медиа не найдено"
    assert (await client.get(f"/api/posts/{post_id}/media/-1")).status == 404
    assert (await client.get("/api/posts/777/media/0")).status == 404


async def test_media_downloaded_by_file_id_and_cached(client, db, bot, config):
    post_id = await db.create_post(text_md="x", photo_file_id="AgACAgIAAxkBAAIB", source="bot")
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 200
    assert await resp.read() == b"\xff\xd8\xff\xe0fakejpeg"
    assert resp.content_type == "image/jpeg"
    assert bot.calls_of("get_file") == [{"file_id": "AgACAgIAAxkBAAIB"}]
    assert bot.calls_of("download")[0]["timeout"] == api_media.DOWNLOAD_TIMEOUT

    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 200
    assert len(bot.calls_of("download")) == 1  # served from the cache
    cached = _visible_files(config)
    assert len(cached) == 1 and cached[0].startswith("tg_") and cached[0].endswith(".jpg")


async def test_telegram_video_cached_with_its_extension(client, db, bot, config, monkeypatch):
    async def get_file(file_id, **kwargs):
        bot.calls.append(("get_file", {"file_id": file_id}))
        return File(file_id=file_id, file_unique_id="u", file_size=1000, file_path="videos/file_7.MP4")

    monkeypatch.setattr(bot, "get_file", get_file)
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "video", "file_id": "BAACvid"}])
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 200 and resp.content_type == "video/mp4"
    assert _visible_files(config)[0].endswith(".mp4")


async def test_parallel_requests_download_once(client, db, bot):
    original = bot.download

    async def slow_download(*args, **kwargs):
        await asyncio.sleep(0.05)
        return await original(*args, **kwargs)

    bot.download = slow_download
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "photo", "file_id": "AgACpar"}])
    responses = await asyncio.gather(*(client.get(f"/api/posts/{post_id}/media/0") for _ in range(4)))
    assert [r.status for r in responses] == [200] * 4
    assert len(bot.calls_of("get_file")) == 1
    assert client.server.app[api_media.DOWNLOAD_LOCKS_KEY] == {}  # lock entries are released


async def test_media_too_big_for_preview(client, db, bot, monkeypatch):
    async def big_file(file_id, **kwargs):
        return File(file_id=file_id, file_unique_id="u", file_size=25 * 1024 * 1024, file_path="videos/big.mp4")

    monkeypatch.setattr(bot, "get_file", big_file)
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "video", "file_id": "BAACbig"}])
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 404
    assert await resp.json() == {"error": "Файл больше 20 МБ — предпросмотр недоступен"}
    assert not bot.calls_of("download")


async def test_media_too_big_reported_by_telegram(client, db, bot):
    bot.fail_on["get_file"] = bad_request("Bad Request: file is too big")
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "animation", "file_id": "CgACbig"}])
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 404
    assert (await resp.json())["error"] == "Файл больше 20 МБ — предпросмотр недоступен"


async def test_media_falls_back_to_file_id_when_upload_is_gone(client, db, bot):
    post_id = await db.create_post(text_md="x", photo_path="f" * 32 + ".png", photo_file_id="FILEID", source="web")
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 200
    assert bot.calls_of("get_file")


async def test_media_download_errors(client, db, bot):
    bot.fail_on["get_file"] = bad_request("Bad Request: wrong file_id")
    post_id = await db.create_post(text_md="x", photo_file_id="BROKEN", source="bot")
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 502
    assert (await resp.json())["error"] == "Не удалось получить картинку из Telegram"

    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "video", "file_id": "BROKEN2"}])
    resp = await client.get(f"/api/posts/{post_id}/media/0")
    assert resp.status == 502
    assert (await resp.json())["error"] == "Не удалось получить видео из Telegram"


# ------------------------------------------------------------------ V1 photo route

async def test_post_photo_route(client, db, bot):
    name = await _uploaded(client, PNG)
    post_id = await db.create_post(text_md="x", photo_path=name, source="web")
    resp = await client.get(f"/api/posts/{post_id}/photo")
    assert resp.status == 200 and await resp.read() == PNG

    post_id = await db.create_post(text_md="x", source="web")
    resp = await client.get(f"/api/posts/{post_id}/photo")
    assert resp.status == 404 and (await resp.json())["error"] == "У поста нет картинки"
    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "video", "file_id": "BAAC"}])
    assert (await client.get(f"/api/posts/{post_id}/photo")).status == 404
    assert (await client.get("/api/posts/777/photo")).status == 404


def test_detect_media():
    assert api_media.detect_media(JPEG) == ("photo", "jpg")
    assert api_media.detect_media(MOV) == ("video", "mov")
    assert api_media.detect_media(b"\x00\x00\x00\x1cftypisom") == ("video", "mp4")
    assert api_media.detect_media(b"\x00\x00\x00\x1cftypavif") is None
    assert api_media.detect_media(b"GIF87a") == ("animation", "gif")
    assert api_media.detect_media(b"") is None

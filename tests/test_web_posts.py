"""Web API: creating, listing, editing, scheduling, publishing and deleting posts."""
from __future__ import annotations

import time

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiogram.types import FSInputFile

from app.db import CH_PENDING
from app.media import ALBUM_BUTTONS_ERROR, ALBUM_GIF_ERROR
from app.services import plan as plan_service
from app.services.plan import PlanError
from app.services.posting import KIND_MISMATCH_ERROR, PostError
from app.web import api, create_web_app
from tests.fakes import bad_request as fakes_bad_request

H = {"X-Requested-With": "pcb"}
CHANNEL = -1001111111111
CHANNEL2 = -1002222222222
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
EVERY_DAY = {str(day): ["09:00", "21:00"] for day in range(7)}


@pytest.fixture
async def client(db, config, bot):
    await db.migrate()
    await db.add_admin(1, is_owner=True)
    bot.add_chat(CHANNEL, "Первый", "first_chan")
    bot.add_chat(CHANNEL2, "Второй", None)
    async with TestClient(TestServer(create_web_app(bot, db, config))) as c:
        for ref in (CHANNEL, CHANNEL2):
            resp = await c.post("/api/channels", json={"ref": str(ref)}, headers=H)
            assert resp.status == 200, await resp.text()
        yield c


async def _create(client: TestClient, **body) -> tuple[int, dict]:
    resp = await client.post("/api/posts", json=body, headers=H)
    return resp.status, await resp.json()


async def _upload(client: TestClient, data: bytes = PNG, name: str = "a.png") -> str:
    form = aiohttp.FormData()
    form.add_field("file", data, filename=name, content_type="image/png")
    resp = await client.post("/api/upload", data=form, headers=H)
    assert resp.status == 200, await resp.text()
    return (await resp.json())["path"]


async def _put(client: TestClient, post_id: int, **body) -> tuple[int, dict]:
    resp = await client.put(f"/api/posts/{post_id}", json=body, headers=H)
    return resp.status, await resp.json()


async def _set_slots(client: TestClient, channel_id: int, slots: dict | None = None) -> None:
    resp = await client.put(f"/api/channels/{channel_id}/slots", json={"slots": slots or EVERY_DAY}, headers=H)
    assert resp.status == 200, await resp.text()


def _later(seconds: int = 3600) -> int:
    return int(time.time()) + seconds


# ------------------------------------------------------------------ create

async def test_create_draft(client):
    status, data = await _create(
        client,
        text_md="**Привет**, мир",
        buttons=[[{"text": "Сайт", "url": "example.com", "style": "green"}], []],
        channel_ids=[CHANNEL],
        options={"silent": True},
        action="draft",
    )
    assert status == 200, data
    post = data["post"]
    assert data["report"] is None
    assert post["status"] == "draft"
    assert post["source"] == "web"
    assert post["text_html"] == "<b>Привет</b>, мир"
    assert post["excerpt"] == "Привет, мир"
    assert post["buttons"] == [[{"text": "Сайт", "url": "https://example.com", "style": "success"}]]
    assert post["options"] == {"disable_preview": False, "silent": True, "protect": False, "caption_above": False}
    assert post["has_photo"] is False and post["photo_url"] is None and post["photo_path"] is None
    assert [(t["channel_id"], t["status"], t["title"], t["link"]) for t in post["targets"]] == [
        (CHANNEL, "pending", "Первый", None)
    ]
    for key in ("created_at", "updated_at", "scheduled_at", "sent_at", "error"):
        assert key in post


async def test_draft_may_be_empty_but_publish_may_not(client):
    status, data = await _create(client, text_md="", action="draft")
    assert status == 200 and data["post"]["text_html"] == ""
    status, data = await _create(client, text_md="   ", channel_ids=[CHANNEL], action="publish")
    assert status == 400
    assert "Пост пустой" in data["error"]


async def test_create_validation_errors(client, db):
    status, data = await _create(client, text_md="x", channel_ids=[-100999], action="draft")
    assert status == 400 and "не подключён" in data["error"]
    await db.upsert_channel(-100777, "Ждёт", None, status=CH_PENDING)
    status, data = await _create(client, text_md="x", channel_ids=[-100777], action="draft")
    assert status == 400 and "«Ждёт»" in data["error"]
    status, data = await _create(client, text_md="x", channel_ids=["abc"])
    assert status == 400
    status, data = await _create(client, text_md="x", buttons=[[{"text": "a", "url": "not a url"}]])
    assert status == 400 and "Ряд 1, кнопка 1" in data["error"]
    status, data = await _create(client, text_md="x", action="explode")
    assert status == 400
    status, data = await _create(client, text_md="x", action="publish")
    assert status == 400 and data["error"] == "Выберите хотя бы один канал"
    status, data = await _create(client, text_md="a" * 5000, channel_ids=[CHANNEL], action="publish")
    assert status == 400 and "слишком длинный" in data["error"]
    status, data = await _create(client, text_md=123)
    assert status == 400
    status, data = await _create(client, text_md="x", options="loud")
    assert status == 400
    assert (await (await client.get("/api/posts")).json())["total"] == 0


async def test_photo_path_validation(client):
    status, data = await _create(client, text_md="x", photo_path="../../etc/passwd")
    assert status == 400 and data["error"] == "Некорректное имя файла картинки"
    status, data = await _create(client, text_md="x", photo_path="0" * 32 + ".png")
    assert status == 400 and "загрузите её заново" in data["error"]
    name = await _upload(client)
    status, data = await _create(client, text_md="x", photo_path=name)
    assert status == 200
    assert data["post"]["photo_path"] == name
    assert data["post"]["photo_url"].startswith(f"/api/posts/{data['post']['id']}/media/0?v=")


async def test_schedule(client):
    when = _later()
    status, data = await _create(client, text_md="Позже", channel_ids=[CHANNEL], action="schedule",
                                 scheduled_at=when)
    assert status == 200, data
    assert data["post"]["status"] == "scheduled"
    assert data["post"]["scheduled_at"] == when

    status, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="schedule",
                                 scheduled_at=int(time.time()) + 5)
    assert status == 400 and data["error"] == "Время публикации уже прошло"
    status, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="schedule")
    assert status == 400
    status, data = await _create(client, text_md="x", action="schedule", scheduled_at=_later())
    assert status == 400 and data["error"] == "Выберите хотя бы один канал"


async def test_publish_text_post(client, bot):
    status, data = await _create(
        client, text_md="Новость", channel_ids=[CHANNEL, CHANNEL2], action="publish",
        buttons=[[{"text": "Купить", "url": "https://shop.example", "style": "danger"}]],
        options={"disable_preview": True},
    )
    assert status == 200, data
    report = data["report"]
    assert report["ok_count"] == 2 and report["fail_count"] == 0
    post = data["post"]
    assert post["status"] == "sent" and post["sent_at"]
    targets = {t["channel_id"]: t for t in post["targets"]}
    assert targets[CHANNEL]["link"] == f"https://t.me/first_chan/{targets[CHANNEL]['message_id']}"
    assert targets[CHANNEL2]["link"] == f"https://t.me/c/2222222222/{targets[CHANNEL2]['message_id']}"
    assert targets[CHANNEL]["status"] == "sent" and targets[CHANNEL]["error"] is None
    sent = {c["chat_id"]: c for c in bot.calls_of("send_message")}
    call = sent[CHANNEL]
    assert call["text"] == "Новость"
    assert call["reply_markup"].inline_keyboard[0][0].style == "danger"
    assert call["link_preview_options"].is_disabled is True


async def test_publish_photo_post_stores_file_id(client, bot, db):
    name = await _upload(client)
    status, data = await _create(client, text_md="С картинкой", photo_path=name, channel_ids=[CHANNEL],
                                 action="publish", options={"caption_above": True})
    assert status == 200, data
    call = bot.calls_of("send_photo")[0]
    assert isinstance(call["photo"], FSInputFile)
    assert call["caption"] == "С картинкой"
    assert call["show_caption_above_media"] is True
    stored = await db.get_post(data["post"]["id"])
    assert stored["photo_file_id"].startswith("fake_file_")
    assert data["post"]["has_photo"] is True


async def test_publish_failure_keeps_draft_and_reports(client, bot):
    from tests.fakes import bad_request

    bot.fail_on["send_message"] = bad_request("Bad Request: not enough rights to send text messages")
    status, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="publish")
    assert status == 200
    assert data["report"]["fail_count"] == 1
    assert data["post"]["status"] == "failed"
    assert data["post"]["targets"][0]["error"] == "Нет прав на публикацию"

    bot.fail_on.clear()
    resp = await client.post(f"/api/posts/{data['post']['id']}/publish", headers=H)
    body = await resp.json()
    assert resp.status == 200, body
    assert body["report"]["ok_count"] == 1
    assert body["post"]["status"] == "sent"


# ------------------------------------------------------------------ list / get

async def test_list_filters(client):
    await _create(client, text_md="черновик", action="draft")
    await _create(client, text_md="план", channel_ids=[CHANNEL], action="schedule", scheduled_at=_later())
    await _create(client, text_md="готово", channel_ids=[CHANNEL], action="publish")

    async def ids(status: str) -> tuple[list[str], int]:
        data = await (await client.get(f"/api/posts?status={status}")).json()
        return [p["text_md"] for p in data["posts"]], data["total"]

    assert await ids("draft") == (["черновик"], 1)
    assert await ids("scheduled") == (["план"], 1)
    assert await ids("sent") == (["готово"], 1)
    texts, total = await ids("all")
    assert total == 3 and sorted(texts) == ["готово", "план", "черновик"]
    data = await (await client.get("/api/posts")).json()
    assert data["total"] == 3

    data = await (await client.get("/api/posts?status=all&limit=2&offset=0")).json()
    assert len(data["posts"]) == 2 and data["total"] == 3
    data = await (await client.get("/api/posts?status=all&limit=2&offset=2")).json()
    assert len(data["posts"]) == 1
    data = await (await client.get("/api/posts?limit=abc&offset=-5")).json()
    assert len(data["posts"]) == 3

    resp = await client.get("/api/posts?status=weird")
    assert resp.status == 400


async def test_get_post_and_404(client):
    _, data = await _create(client, text_md="один")
    post_id = data["post"]["id"]
    resp = await client.get(f"/api/posts/{post_id}")
    assert resp.status == 200
    assert (await resp.json())["post"]["text_md"] == "один"
    resp = await client.get("/api/posts/99999")
    assert resp.status == 404
    assert (await resp.json())["error"] == "Пост не найден"
    resp = await client.get("/api/posts/abc")
    assert resp.status == 404


# ------------------------------------------------------------------ update

async def test_update_draft_then_schedule_then_back(client):
    _, data = await _create(client, text_md="v1", channel_ids=[CHANNEL])
    post_id = data["post"]["id"]

    resp = await client.put(f"/api/posts/{post_id}", json={
        "text_md": "v2", "channel_ids": [CHANNEL2], "buttons": [], "options": {"protect": True},
        "action": "draft",
    }, headers=H)
    post = (await resp.json())["post"]
    assert resp.status == 200
    assert post["text_md"] == "v2" and post["options"]["protect"] is True
    assert [t["channel_id"] for t in post["targets"]] == [CHANNEL2]

    when = _later(7200)
    resp = await client.put(f"/api/posts/{post_id}", json={
        "text_md": "v3", "channel_ids": [CHANNEL2], "action": "schedule", "scheduled_at": when,
    }, headers=H)
    post = (await resp.json())["post"]
    assert post["status"] == "scheduled" and post["scheduled_at"] == when

    # No action on a scheduled post keeps it scheduled (at the stored time).
    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "v4", "channel_ids": [CHANNEL2]}, headers=H)
    post = (await resp.json())["post"]
    assert resp.status == 200 and post["status"] == "scheduled" and post["scheduled_at"] == when

    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "v5", "channel_ids": [CHANNEL2],
                                                          "action": "draft"}, headers=H)
    post = (await resp.json())["post"]
    assert post["status"] == "draft" and post["scheduled_at"] is None


async def test_update_draft_and_publish(client, bot):
    _, data = await _create(client, text_md="черновик")
    post_id = data["post"]["id"]
    resp = await client.put(f"/api/posts/{post_id}", json={
        "text_md": "в канал", "channel_ids": [CHANNEL], "action": "publish",
    }, headers=H)
    body = await resp.json()
    assert resp.status == 200, body
    assert body["report"]["ok_count"] == 1
    assert body["post"]["status"] == "sent"
    assert bot.calls_of("send_message")[-1]["text"] == "в канал"


async def test_keep_photo_and_remove_photo(client):
    name = await _upload(client)
    _, data = await _create(client, text_md="pic", photo_path=name)
    post_id = data["post"]["id"]

    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "pic 2", "keep_photo": True,
                                                          "photo_path": None, "action": "draft"}, headers=H)
    post = (await resp.json())["post"]
    assert post["photo_path"] == name and post["has_photo"] is True

    # Re-sending the stored name is not a change either.
    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "pic 3", "photo_path": name,
                                                          "action": "draft"}, headers=H)
    assert (await resp.json())["post"]["photo_path"] == name

    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "no pic", "keep_photo": False,
                                                          "photo_path": None, "action": "draft"}, headers=H)
    post = (await resp.json())["post"]
    assert post["has_photo"] is False and post["photo_url"] is None


async def test_edit_sent_post_requires_apply(client, bot):
    _, data = await _create(client, text_md="старый", channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    message_id = data["post"]["targets"][0]["message_id"]

    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "новый"}, headers=H)
    assert resp.status == 400

    resp = await client.put(f"/api/posts/{post_id}", json={
        "text_md": "**новый**", "apply": True, "channel_ids": [CHANNEL2],  # channels are ignored
        "buttons": [[{"text": "Ссылка", "url": "https://x.example"}]],
    }, headers=H)
    body = await resp.json()
    assert resp.status == 200, body
    assert body["report"]["action"] == "edit" and body["report"]["ok_count"] == 1
    assert body["post"]["text_md"] == "**новый**"
    assert [t["channel_id"] for t in body["post"]["targets"]] == [CHANNEL]
    call = bot.calls_of("edit_message_text")[-1]
    assert call["chat_id"] == CHANNEL and call["message_id"] == message_id
    assert call["text"] == "<b>новый</b>"
    assert call["reply_markup"].inline_keyboard[0][0].text == "Ссылка"


async def test_edit_sent_post_cannot_add_photo(client, bot, db):
    _, data = await _create(client, text_md="текст", channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    name = await _upload(client)
    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "текст 2", "photo_path": name,
                                                          "apply": True}, headers=H)
    assert resp.status == 400
    assert (await resp.json())["error"] == KIND_MISMATCH_ERROR
    stored = await db.get_post(post_id)
    assert stored["text_md"] == "текст" and stored["photo_path"] is None
    assert not bot.calls_of("edit_message_text")


async def test_edit_sent_photo_post_replaces_photo(client, bot, db):
    first = await _upload(client)
    _, data = await _create(client, text_md="фото", photo_path=first, channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    assert (await db.get_post(post_id))["photo_file_id"]

    # Same picture (keep_photo): caption edit only.
    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "фото 2", "keep_photo": True,
                                                          "apply": True}, headers=H)
    assert resp.status == 200
    assert bot.calls_of("edit_message_caption")[-1]["caption"] == "фото 2"
    assert not bot.calls_of("edit_message_media")

    second = await _upload(client)
    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "фото 3", "photo_path": second,
                                                          "keep_photo": False, "apply": True}, headers=H)
    body = await resp.json()
    assert resp.status == 200, body
    assert bot.calls_of("edit_message_media")
    stored = await db.get_post(post_id)
    assert stored["photo_path"] == second
    assert stored["photo_file_id"] is None  # cleared when the picture was replaced
    assert body["post"]["photo_url"] != data["post"]["photo_url"]  # cache busting


async def test_edit_rejects_deleted_and_sending(client, db):
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    await client.delete(f"/api/posts/{post_id}?from_channels=1", headers=H)
    resp = await client.put(f"/api/posts/{post_id}", json={"text_md": "y", "apply": True}, headers=H)
    assert resp.status == 400

    _, data = await _create(client, text_md="z", channel_ids=[CHANNEL])
    await db.update_post(data["post"]["id"], status="sending")
    resp = await client.put(f"/api/posts/{data['post']['id']}", json={"text_md": "y"}, headers=H)
    assert resp.status == 409


# ------------------------------------------------------------------ actions

async def test_unschedule(client):
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="schedule", scheduled_at=_later())
    post_id = data["post"]["id"]
    resp = await client.post(f"/api/posts/{post_id}/unschedule", headers=H)
    post = (await resp.json())["post"]
    assert resp.status == 200
    assert post["status"] == "draft" and post["scheduled_at"] is None
    resp = await client.post(f"/api/posts/{post_id}/unschedule", headers=H)
    assert resp.status == 400
    resp = await client.post("/api/posts/424242/unschedule", headers=H)
    assert resp.status == 404


async def test_publish_endpoint_errors(client):
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="publish")
    resp = await client.post(f"/api/posts/{data['post']['id']}/publish", headers=H)
    assert resp.status == 400
    assert "error" in await resp.json()

    _, data = await _create(client, text_md="без каналов")
    resp = await client.post(f"/api/posts/{data['post']['id']}/publish", headers=H)
    body = await resp.json()
    assert resp.status == 400 and body["error"] == "Не выбраны каналы для публикации"
    assert body["post"]["status"] == "draft"


async def test_preview_sends_to_admins(client, bot, db):
    await db.add_admin(2)
    _, data = await _create(client, text_md="превью")
    resp = await client.post(f"/api/posts/{data['post']['id']}/preview", headers=H)
    body = await resp.json()
    assert resp.status == 200, body
    assert body["sent"] == 2
    assert sorted(c["chat_id"] for c in bot.calls_of("send_message")) == [1, 2]

    _, data = await _create(client, text_md="")
    resp = await client.post(f"/api/posts/{data['post']['id']}/preview", headers=H)
    assert resp.status == 400


async def test_delete_record(client):
    _, data = await _create(client, text_md="удалить")
    post_id = data["post"]["id"]
    resp = await client.delete(f"/api/posts/{post_id}", headers=H)
    assert resp.status == 200 and await resp.json() == {"ok": True}
    assert (await client.get(f"/api/posts/{post_id}")).status == 404
    assert (await client.delete(f"/api/posts/{post_id}", headers=H)).status == 404


async def test_delete_from_channels_then_record(client, bot):
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    message_id = data["post"]["targets"][0]["message_id"]

    resp = await client.delete(f"/api/posts/{post_id}", headers=H)
    assert resp.status == 400  # published: delete from channels first

    resp = await client.delete(f"/api/posts/{post_id}?from_channels=1", headers=H)
    body = await resp.json()
    assert resp.status == 200, body
    assert body["report"]["action"] == "delete" and body["report"]["ok_count"] == 1
    assert body["post"]["status"] == "deleted"
    assert body["post"]["targets"][0]["status"] == "deleted"
    assert body["post"]["targets"][0]["link"] is None
    assert bot.calls_of("delete_message") == [{"chat_id": CHANNEL, "message_id": message_id}]

    resp = await client.delete(f"/api/posts/{post_id}?from_channels=1", headers=H)
    assert resp.status == 400
    resp = await client.delete(f"/api/posts/{post_id}", headers=H)
    assert resp.status == 200


# ------------------------------------------------------------------ V2: media list

async def test_create_album_and_serialization(client):
    photo, video = await _upload(client), await _upload(client, MP4, "v.mp4")
    status, data = await _create(client, text_md="Альбом", media=[
        {"type": "photo", "path": photo}, {"type": "video", "path": video}])
    assert status == 200, data
    post = data["post"]
    assert data["schedule"] is None
    assert post["media_kind"] == "album" and post["has_media"] is True
    assert post["media_label"] == "🗂 Альбом: 1 фото, 1 видео"
    assert [(m["index"], m["type"], m["path"], m["file_id"]) for m in post["media"]] == [
        (0, "photo", photo, None), (1, "video", video, None)]
    assert post["media"][1]["url"].startswith(f"/api/posts/{post['id']}/media/1?v=")
    assert post["media"][0]["url"] != post["media"][1]["url"]
    assert post["has_photo"] is True and post["photo_url"] == post["media"][0]["url"]
    assert post["photo_path"] == photo

    status, data = await _create(client, text_md="v", media=[{"type": "video", "path": video}])
    post = data["post"]
    assert post["media_kind"] == "video" and post["has_photo"] is False and post["photo_url"] is None

    status, data = await _create(client, text_md="текст")
    assert data["post"]["media"] == [] and data["post"]["media_kind"] == "text"
    assert data["post"]["has_media"] is False


async def test_media_validation_errors(client):
    photo, gif = await _upload(client), await _upload(client, GIF, "a.gif")
    cases = [
        ([{"type": "photo", "path": photo}, {"type": "animation", "path": gif}], ALBUM_GIF_ERROR),
        ([{"type": "photo", "path": photo}] * 11, "не больше 10"),
        ([{"type": "sticker", "path": photo}], "Неизвестный тип медиа"),
        ([{"type": "photo", "path": "../../etc/passwd"}], "Некорректное имя файла"),
        ([{"type": "video", "path": photo}], "нельзя отправить как видео"),
        ([{"type": "photo", "path": "0" * 32 + ".png"}], "Картинка не найдена"),
        ([{"type": "video", "path": "0" * 32 + ".mp4"}], "Видео не найдено"),
        ([{"type": "photo"}], "загрузите его заново"),
        ([{"type": "photo", "file_id": "AgACforeign"}], "Файл медиа не найден"),
        ("photo", "Некорректный список медиа"),
    ]
    for media, message in cases:
        status, data = await _create(client, text_md="x", media=media)
        assert status == 400, media
        assert message in data["error"], (media, data)
    assert (await (await client.get("/api/posts")).json())["total"] == 0


async def test_gif_and_video_as_gif(client, bot):
    gif, video = await _upload(client, GIF, "a.gif"), await _upload(client, MP4, "v.mp4")
    status, data = await _create(client, text_md="гиф", media=[{"type": "gif", "path": gif}],
                                 channel_ids=[CHANNEL], action="publish")
    assert status == 200, data
    assert data["post"]["media_kind"] == "animation"
    assert bot.calls_of("send_animation")[0]["caption"] == "гиф"
    # A short video sent "as GIF".
    status, data = await _create(client, text_md="", media=[{"type": "animation", "path": video}],
                                 channel_ids=[CHANNEL], action="publish")
    assert status == 200, data
    assert len(bot.calls_of("send_animation")) == 2


async def test_album_cannot_have_buttons(client, db):
    a, b = await _upload(client), await _upload(client)
    album = [{"type": "photo", "path": a}, {"type": "photo", "path": b}]
    buttons = [[{"text": "Сайт", "url": "https://example.com"}]]
    for action in ("draft", "publish"):
        status, data = await _create(client, text_md="x", media=album, buttons=buttons, channel_ids=[CHANNEL],
                                     action=action)
        assert status == 400 and data["error"] == ALBUM_BUTTONS_ERROR
    # Preview of an album with buttons saved by older code is refused too.
    post_id = await db.create_post(text_md="x", source="bot", buttons=buttons, media=[
        {"type": "photo", "file_id": "AgAC1"}, {"type": "photo", "file_id": "AgAC2"}])
    resp = await client.post(f"/api/posts/{post_id}/preview", headers=H)
    assert resp.status == 400 and (await resp.json())["error"] == ALBUM_BUTTONS_ERROR


async def test_publish_album_stores_file_ids_and_message_ids(client, bot, db):
    a, b = await _upload(client), await _upload(client, MP4, "v.mp4")
    status, data = await _create(client, text_md="**Альбом**", media=[
        {"type": "photo", "path": a}, {"type": "video", "path": b}], channel_ids=[CHANNEL, CHANNEL2],
        action="publish")
    assert status == 200, data
    assert data["report"]["ok_count"] == 2
    groups = bot.calls_of("send_media_group")
    assert len(groups) == 2 and groups[0]["media"][0].caption == "<b>Альбом</b>"
    post = data["post"]
    assert all(m["file_id"] for m in post["media"])  # learned on the first send
    assert [m["path"] for m in post["media"]] == [a, b]
    target = post["targets"][0]
    assert len(target["message_ids"]) == 2 and target["message_ids"][0] == target["message_id"]
    assert post["published_kind"] == "album:2"


async def test_file_id_accepted_only_from_own_post_or_copy_source(client, db):
    source_id = await db.create_post(text_md="из бота", source="bot", media=[{"type": "photo", "file_id": "AgACsrc"}])
    item = {"type": "photo", "file_id": "AgACsrc"}
    status, data = await _create(client, text_md="копия", media=[item])
    assert status == 400 and data["error"] == "Файл медиа не найден — загрузите его заново"
    status, data = await _create(client, text_md="копия", media=[item], copy_from=99999)
    assert status == 400 and data["error"] == "Исходный пост для копии не найден"

    status, data = await _create(client, text_md="копия", media=[item], copy_from=source_id)
    assert status == 200, data
    copy_id = data["post"]["id"]
    assert data["post"]["media"][0]["file_id"] == "AgACsrc"
    # The copy now owns the file_id: later saves may echo it without copy_from.
    status, data = await _put(client, copy_id, text_md="копия 2", media=[item], action="draft")
    assert status == 200 and data["post"]["media"][0]["file_id"] == "AgACsrc"
    # Legacy photo_file_id follows the same rule.
    status, data = await _create(client, text_md="x", photo_file_id="AgACother")
    assert status == 400


async def test_echo_without_file_id_keeps_learned_file_id(client, bot, db):
    name = await _upload(client)
    _, data = await _create(client, text_md="фото", media=[{"type": "photo", "path": name}],
                            channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    learned = data["post"]["media"][0]["file_id"]
    assert learned
    status, body = await _put(client, post_id, text_md="фото 2", media=[{"type": "photo", "path": name}], apply=True)
    assert status == 200, body
    assert body["post"]["media"][0]["file_id"] == learned
    assert bot.calls_of("edit_message_caption")[-1]["caption"] == "фото 2"
    assert not bot.calls_of("edit_message_media")  # media unchanged


async def test_edit_published_media_replaced_and_type_switch(client, bot, db):
    first = await _upload(client)
    _, data = await _create(client, text_md="медиа", media=[{"type": "photo", "path": first}],
                            channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    video = await _upload(client, MP4, "v.mp4")
    status, body = await _put(client, post_id, text_md="теперь видео", media=[{"type": "video", "path": video}],
                              apply=True)
    assert status == 200, body
    call = bot.calls_of("edit_message_media")[-1]
    assert type(call["media"]).__name__ == "InputMediaVideo"
    assert body["post"]["media_kind"] == "video"

    status, body = await _put(client, post_id, text_md="альбом", apply=True, media=[
        {"type": "video", "path": video}, {"type": "photo", "path": first}])
    assert status == 400 and body["error"] == KIND_MISMATCH_ERROR
    assert (await db.get_post(post_id))["text_md"] == "теперь видео"


async def test_edit_published_album_must_keep_count(client, db):
    a, b, c = await _upload(client), await _upload(client), await _upload(client)
    _, data = await _create(client, text_md="альбом", media=[{"type": "photo", "path": a}, {"type": "photo", "path": b}],
                            channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    status, body = await _put(client, post_id, text_md="альбом", apply=True, media=[
        {"type": "photo", "path": a}, {"type": "photo", "path": b}, {"type": "photo", "path": c}])
    assert status == 400 and "должно остаться 2 файлов" in body["error"]
    status, body = await _put(client, post_id, text_md="новая подпись", apply=True, media=data["post"]["media"])
    assert status == 200, body
    assert body["report"]["ok_count"] == 1


async def test_media_type_change(client, db):
    video = await _upload(client, MP4, "v.mp4")
    post_id = await db.create_post(text_md="x", source="web", media=[
        {"type": "video", "path": video, "file_id": "BAAClearned"}])
    status, data = await _put(client, post_id, text_md="x", media=[
        {"type": "animation", "path": video, "file_id": "BAAClearned"}], action="draft")
    assert status == 200, data
    item = data["post"]["media"][0]
    assert item["type"] == "animation" and item["file_id"] is None  # re-uploaded as GIF next time

    post_id = await db.create_post(text_md="x", source="bot", media=[{"type": "video", "file_id": "BAACtg"}])
    status, data = await _put(client, post_id, text_md="x", media=[{"type": "animation", "file_id": "BAACtg"}],
                              action="draft")
    assert status == 400 and "изменить нельзя" in data["error"]


async def test_mixed_up_item_is_rejected(client, db):
    a, b = await _upload(client), await _upload(client)
    post_id = await db.create_post(text_md="x", source="web", media=[{"type": "photo", "path": a, "file_id": "AgACa"}])
    status, data = await _put(client, post_id, text_md="x", media=[{"type": "photo", "path": b, "file_id": "AgACa"}],
                              action="draft")
    assert status == 400


# ------------------------------------------------------------------ V2: schedule / queue actions

async def test_schedule_action_sets_target_times(client):
    when = _later(7200)
    status, data = await _create(client, text_md="ко времени", channel_ids=[CHANNEL, CHANNEL2], action="schedule",
                                 scheduled_at=when)
    assert status == 200, data
    assert data["schedule"] == {str(CHANNEL): when, str(CHANNEL2): when}
    post = data["post"]
    assert post["status"] == "scheduled" and post["scheduled_at"] == when
    assert {(t["scheduled_at"], t["queued"]) for t in post["targets"]} == {(when, False)}


async def test_queue_action_takes_next_free_slots(client):
    await _set_slots(client, CHANNEL)
    await _set_slots(client, CHANNEL2, {"0": ["10:00"], "3": ["10:00"]})
    status, first = await _create(client, text_md="в очередь", channel_ids=[CHANNEL, CHANNEL2], action="queue")
    assert status == 200, first
    schedule = {int(k): v for k, v in first["schedule"].items()}
    assert set(schedule) == {CHANNEL, CHANNEL2} and all(ts > time.time() for ts in schedule.values())
    post = first["post"]
    assert post["status"] == "scheduled" and post["scheduled_at"] == min(schedule.values())
    assert {t["channel_id"]: (t["scheduled_at"], t["queued"]) for t in post["targets"]} == {
        cid: (ts, True) for cid, ts in schedule.items()}

    status, second = await _create(client, text_md="следующий", channel_ids=[CHANNEL], action="queue")
    assert int(second["schedule"][str(CHANNEL)]) > schedule[CHANNEL]  # the slot is taken

    resp = await client.get(f"/api/plan/next?channel_ids={CHANNEL},{CHANNEL2}")
    nxt = (await resp.json())["next"]
    assert nxt[str(CHANNEL)] > int(second["schedule"][str(CHANNEL)])


async def test_queue_without_schedule_creates_nothing(client):
    await _set_slots(client, CHANNEL)
    status, data = await _create(client, text_md="x", channel_ids=[CHANNEL, CHANNEL2], action="queue")
    assert status == 400
    assert data["error"] == "У канала «Второй» не настроено расписание публикаций"
    assert (await (await client.get("/api/posts")).json())["total"] == 0


async def test_queue_failure_after_check_removes_new_draft(client, monkeypatch):
    await _set_slots(client, CHANNEL)

    async def full(*args, **kwargs):
        raise PlanError("В расписании канала «Первый» нет свободного времени на ближайший год")

    monkeypatch.setattr(plan_service, "queue_post", full)
    status, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="queue")
    assert status == 400 and "нет свободного времени" in data["error"]
    assert (await (await client.get("/api/posts")).json())["total"] == 0


async def test_update_actions_on_scheduled_post(client):
    await _set_slots(client, CHANNEL)
    when = _later(7200)
    _, data = await _create(client, text_md="v1", channel_ids=[CHANNEL], action="schedule", scheduled_at=when)
    post_id = data["post"]["id"]

    # No action: kept; a newly added channel gets the post's time.
    status, data = await _put(client, post_id, text_md="v2", channel_ids=[CHANNEL, CHANNEL2])
    assert status == 200, data
    assert data["post"]["status"] == "scheduled"
    assert {t["channel_id"]: t["scheduled_at"] for t in data["post"]["targets"]} == {CHANNEL: when, CHANNEL2: when}
    assert data["schedule"] == {str(CHANNEL): when, str(CHANNEL2): when}

    # Queue: the channel moves to its next free slot.
    status, data = await _put(client, post_id, text_md="v3", channel_ids=[CHANNEL], action="queue")
    assert status == 200, data
    target = data["post"]["targets"][0]
    assert target["queued"] is True and target["scheduled_at"] == int(data["schedule"][str(CHANNEL)])

    # Queue to a channel without a schedule: refused before anything is saved.
    status, data = await _put(client, post_id, text_md="v4", channel_ids=[CHANNEL2], action="queue")
    assert status == 400 and "не настроено расписание" in data["error"]
    stored = await (await client.get(f"/api/posts/{post_id}")).json()
    assert stored["post"]["text_md"] == "v3" and stored["post"]["status"] == "scheduled"

    # Draft: taken out of the plan.
    status, data = await _put(client, post_id, text_md="v5", channel_ids=[CHANNEL], action="draft")
    assert status == 200
    assert data["post"]["status"] == "draft" and data["post"]["scheduled_at"] is None
    assert data["post"]["targets"][0]["scheduled_at"] is None and data["post"]["targets"][0]["queued"] is False


async def test_update_publish_refused_keeps_schedule(client, config, monkeypatch):
    when = _later(7200)
    name = await _upload(client)
    _, data = await _create(client, text_md="x", media=[{"type": "photo", "path": name}], channel_ids=[CHANNEL],
                            action="schedule", scheduled_at=when)
    post_id = data["post"]["id"]
    (config.uploads_dir / name).unlink()
    # The upload vanished: the save itself is refused, the schedule stays.
    status, data = await _put(client, post_id, text_md="y", media=[{"type": "photo", "path": name}],
                              channel_ids=[CHANNEL], action="publish")
    assert status == 400 and "загрузите её заново" in data["error"]
    stored = (await (await client.get(f"/api/posts/{post_id}")).json())["post"]
    assert stored["status"] == "scheduled" and stored["targets"][0]["scheduled_at"] == when

    # Publishing refused by the service after the save: content saved, schedule restored.
    async def refuse(*args, **kwargs):
        raise PostError("Нельзя прямо сейчас")

    monkeypatch.setattr(api, "publish_post", refuse)
    status, data = await _put(client, post_id, text_md="z", media=[], channel_ids=[CHANNEL], action="publish")
    assert status == 400 and data["error"] == "Нельзя прямо сейчас"
    assert data["post"]["text_md"] == "z" and data["post"]["status"] == "scheduled"
    assert data["post"]["targets"][0]["scheduled_at"] == when


async def test_publish_endpoint_with_channel_ids(client, bot):
    await _set_slots(client, CHANNEL)
    await _set_slots(client, CHANNEL2)
    _, data = await _create(client, text_md="очередь", channel_ids=[CHANNEL, CHANNEL2], action="queue")
    post_id = data["post"]["id"]
    resp = await client.post(f"/api/posts/{post_id}/publish", json={"channel_ids": [CHANNEL]}, headers=H)
    body = await resp.json()
    assert resp.status == 200, body
    assert [r["channel_id"] for r in body["report"]["results"]] == [CHANNEL]
    post = body["post"]
    assert post["status"] == "scheduled"  # still queued in the second channel
    targets = {t["channel_id"]: t for t in post["targets"]}
    assert targets[CHANNEL]["status"] == "sent" and targets[CHANNEL]["scheduled_at"] is None
    assert targets[CHANNEL2]["status"] == "pending" and targets[CHANNEL2]["queued"] is True

    # Partly sent: content edits need apply and go to the sent message; the record cannot be deleted.
    status, data = await _put(client, post_id, text_md="правка", channel_ids=[CHANNEL2], action="draft")
    assert status == 400 and "apply" in data["error"]
    status, data = await _put(client, post_id, text_md="правка", apply=True)
    assert status == 200, data
    assert bot.calls_of("edit_message_text")[-1]["text"] == "правка"
    assert data["post"]["status"] == "scheduled"
    resp = await client.delete(f"/api/posts/{post_id}", headers=H)
    assert resp.status == 400

    resp = await client.post(f"/api/posts/{post_id}/publish", json={"channel_ids": []}, headers=H)
    assert resp.status == 400
    resp = await client.post(f"/api/posts/{post_id}/unschedule", headers=H)
    body = await resp.json()
    assert resp.status == 200 and body["post"]["status"] == "partial"


# ------------------------------------------------------------------ V2: posts being sent

async def test_every_mutation_refused_while_sending(client, db):
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="schedule", scheduled_at=_later())
    post_id = data["post"]["id"]
    await db.update_post(post_id, status="sending")
    calls = [
        client.put(f"/api/posts/{post_id}", json={"text_md": "y", "apply": True}, headers=H),
        client.post(f"/api/posts/{post_id}/publish", headers=H),
        client.post(f"/api/posts/{post_id}/unschedule", headers=H),
        client.delete(f"/api/posts/{post_id}", headers=H),
        client.delete(f"/api/posts/{post_id}?from_channels=1", headers=H),
    ]
    for call in calls:
        resp = await call
        assert resp.status == 409, resp.url
        assert "публикуется" in (await resp.json())["error"]
    stored = await db.get_post(post_id)
    assert stored["status"] == "sending" and stored["text_md"] == "x"


async def test_save_refused_when_claimed_between_read_and_write(client, db, monkeypatch):
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="schedule", scheduled_at=_later())
    post_id = data["post"]["id"]
    original = db.get_post

    async def claimed_after_read(pid):
        post = await original(pid)
        await db.execute("UPDATE posts SET status = 'sending' WHERE id = ?", (pid,))
        return post

    monkeypatch.setattr(db, "get_post", claimed_after_read)
    status, body = await _put(client, post_id, text_md="y", channel_ids=[CHANNEL], action="draft")
    monkeypatch.undo()
    assert status == 409
    stored = await db.get_post(post_id)
    assert stored["text_md"] == "x" and stored["status"] == "sending"
    assert stored["targets"][0]["scheduled_at"] is not None


async def test_targets_serialization_has_schedule(client):
    when = _later()
    await _create(client, text_md="x", channel_ids=[CHANNEL], action="schedule", scheduled_at=when)
    data = await (await client.get("/api/posts?status=scheduled")).json()
    target = data["posts"][0]["targets"][0]
    assert target["scheduled_at"] == when and target["queued"] is False
    assert target["folder_id"] is None and target["message_ids"] == []


async def test_media_version_ignores_learned_file_id():
    assert api.media_version({"type": "photo", "path": "a.png"}) == \
        api.media_version({"type": "photo", "path": "a.png", "file_id": "AgAC"})
    assert api.media_version({"type": "photo", "path": "a.png"}) != api.media_version({"type": "video", "path": "a.png"})


# ------------------------------------------------------------------ integration QA regressions

async def test_unticking_every_channel_removes_unsent_targets(client, bot, db):
    """«Снять все» + save: db.set_post_targets([]) removed nothing, so the channels came back."""
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL, CHANNEL2], action="draft")
    post_id = data["post"]["id"]
    status, body = await _put(client, post_id, text_md="x", channel_ids=[], action="draft")
    assert status == 200, body
    assert body["post"]["targets"] == []
    assert (await db.get_post(post_id))["targets"] == []

    # A failed post: its failed targets go too, the post becomes a plain draft.
    bot.fail_on["send_message"] = fakes_bad_request("Bad Request: chat not found")
    _, data = await _create(client, text_md="y", channel_ids=[CHANNEL], action="publish")
    bot.fail_on.pop("send_message")
    failed_id = data["post"]["id"]
    assert (await db.get_post(failed_id))["status"] == "failed"
    status, body = await _put(client, failed_id, text_md="y", channel_ids=[])
    assert status == 200, body
    assert body["post"]["status"] == "draft" and body["post"]["targets"] == []

    # A scheduled post without channels is no longer scheduled.
    _, data = await _create(client, text_md="z", channel_ids=[CHANNEL], action="schedule", scheduled_at=_later())
    status, body = await _put(client, data["post"]["id"], text_md="z", channel_ids=[], action="draft")
    assert status == 200, body
    assert body["post"]["status"] == "draft" and body["post"]["scheduled_at"] is None
    assert body["post"]["targets"] == []


async def test_unticking_keeps_sent_targets_of_partly_published_post(client, bot, db):
    """An empty selection only drops unsent targets: a channel the post is out in stays."""
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL, CHANNEL2], action="draft")
    post_id = data["post"]["id"]
    await api.set_unsent_targets(db, post_id, [])
    await db.set_post_targets(post_id, [CHANNEL, CHANNEL2])
    await db.update_target(post_id, CHANNEL, status="sent", message_id=5)
    await api.set_unsent_targets(db, post_id, [])
    assert [(t["channel_id"], t["status"]) for t in (await db.get_post(post_id))["targets"]] == [(CHANNEL, "sent")]


async def test_apply_failing_in_every_channel_keeps_db_equal_to_channels(client, bot, db):
    """Finding #9 (web): every channel refuses the edit → the DB keeps the published content."""
    first = await _upload(client)
    _, data = await _create(client, text_md="**старый**", media=[{"type": "photo", "path": first}],
                            buttons=[[{"text": "Сайт", "url": "https://old.example"}]],
                            channel_ids=[CHANNEL, CHANNEL2], action="publish")
    post_id = data["post"]["id"]
    before = await db.get_post(post_id)
    second = await _upload(client)

    bot.fail_on["edit_message_media"] = fakes_bad_request("Bad Request: message can't be edited")
    status, body = await _put(client, post_id, apply=True, text_md="новый", media=[{"type": "photo", "path": second}],
                              buttons=[[{"text": "Новая", "url": "https://new.example"}]])
    assert status == 200, body
    assert body["report"]["ok_count"] == 0 and body["report"]["fail_count"] == 2
    stored = await db.get_post(post_id)
    assert stored["text_md"] == "**старый**" and stored["text_html"] == "<b>старый</b>"
    assert stored["media"] == before["media"] and stored["buttons"] == before["buttons"]
    assert body["post"]["text_md"] == "**старый**"
    assert all(t["error"] for t in body["post"]["targets"])

    # Once Telegram accepts it, the same request applies the edit (media replaced in both channels).
    bot.fail_on.pop("edit_message_media")
    edits = len(bot.calls_of("edit_message_media"))
    status, body = await _put(client, post_id, apply=True, text_md="новый", media=[{"type": "photo", "path": second}],
                              buttons=[[{"text": "Новая", "url": "https://new.example"}]])
    assert status == 200, body
    assert body["report"]["ok_count"] == 2
    assert len(bot.calls_of("edit_message_media")) == edits + 2
    stored = await db.get_post(post_id)
    assert stored["text_md"] == "новый" and stored["media"][0]["path"] == second


async def test_apply_partly_failing_keeps_new_content(client, bot, db):
    """One channel took the edit: the new content stays (the failed channel is retried later)."""
    _, data = await _create(client, text_md="старый", channel_ids=[CHANNEL, CHANNEL2], action="publish")
    post_id = data["post"]["id"]
    bot.fail_on["edit_message_text"] = lambda kw: (
        fakes_bad_request("Bad Request: message can't be edited") if kw["chat_id"] == CHANNEL2 else None
    )
    status, body = await _put(client, post_id, apply=True, text_md="новый")
    assert status == 200, body
    assert body["report"]["ok_count"] == 1 and body["report"]["fail_count"] == 1
    assert (await db.get_post(post_id))["text_md"] == "новый"


def _claim_before(db, monkeypatch, method: str) -> None:
    """The scheduler claims the post right before `db.<method>` runs."""
    original = getattr(db, method)

    async def claimed_first(post_id, *args, **kwargs):
        await db.execute("UPDATE posts SET status = 'sending' WHERE id = ?", (post_id,))
        return await original(post_id, *args, **kwargs)

    monkeypatch.setattr(db, method, claimed_first)


async def test_channels_of_a_post_claimed_meanwhile_are_kept(client, db, monkeypatch):
    """Regression (review2 #3, web side): an empty channel selection used a raw DELETE that ignored a
    claim landing after the content was saved — the channel being sent to lost its target."""
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL, CHANNEL2], action="draft")
    post_id = data["post"]["id"]
    _claim_before(db, monkeypatch, "set_post_targets")
    status, body = await _put(client, post_id, text_md="y", channel_ids=[], action="draft")
    monkeypatch.undo()
    assert status == 409 and "публикуется" in body["error"]
    assert body["post"]["id"] == post_id
    stored = await db.get_post(post_id)
    assert sorted(t["channel_id"] for t in stored["targets"]) == sorted([CHANNEL, CHANNEL2])


async def test_delete_from_channels_claimed_meanwhile_is_409(client, db, monkeypatch):
    _, data = await _create(client, text_md="x", channel_ids=[CHANNEL], action="publish")
    post_id = data["post"]["id"]
    _claim_before(db, monkeypatch, "claim_post_for_sending")
    resp = await client.delete(f"/api/posts/{post_id}?from_channels=1", headers=H)
    monkeypatch.undo()
    body = await resp.json()
    assert resp.status == 409 and "публикуется" in body["error"] and body["post"]["status"] == "sending"

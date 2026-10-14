"""Tests for app.services.posting (publishing, editing, deleting, previews) — V2 media engine."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import (
    FSInputFile,
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
)

from app.db import CH_ACTIVE, CH_LEFT, CH_PENDING, Database, now
from app.media import ALBUM_BUTTONS_ERROR, ALBUM_GIF_ERROR, media_signature
from app.services import posting
from app.services.posting import (
    KIND_MISMATCH_ERROR,
    UNREACHABLE_NOTE,
    PostError,
    PostNotDue,
    PublishReport,
    TargetResult,
    delete_published_post,
    edit_published_post,
    message_link,
    notify_admins,
    post_kind,
    publish_post,
    render_text,
    send_post,
    send_preview,
)
from tests.fakes import BOT_ID, FakeBot, bad_request

CH_A = -1001000000001
CH_B = -1001000000002
CH_C = -1001000000003
HEX = "0123456789abcdef" * 2


@pytest.fixture
async def db(db):
    await db.migrate()
    return db


@pytest.fixture
def sleeps(monkeypatch):
    """Record the pauses posting makes instead of waiting."""
    calls: list[float] = []

    async def fake_pause(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(posting, "_pause", fake_pause)
    return calls


def retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=SendMessage(chat_id=0, text="x"), message="Too Many Requests",
                              retry_after=seconds)


def forbidden(message: str) -> TelegramForbiddenError:
    return TelegramForbiddenError(method=SendMessage(chat_id=0, text="x"), message=message)


def upload(config, ext: str, n: int = 0) -> str:
    """Create an uploaded file named like the web panel does (32 hex chars + extension)."""
    name = f"{HEX[:-1]}{n:x}.{ext}"
    (config.uploads_dir / name).write_bytes(b"\x00fake-media")
    return name


def photo(file_id=None, path=None):
    return {"type": "photo", "file_id": file_id, "path": path}


def video(file_id=None, path=None):
    return {"type": "video", "file_id": file_id, "path": path}


def gif(file_id=None, path=None):
    return {"type": "animation", "file_id": file_id, "path": path}


BUTTONS = [[{"text": "Сайт", "url": "https://example.com", "style": "primary"}]]


async def add_channel(db, bot, chat_id, title, username=None, status=CH_ACTIVE):
    bot.add_chat(chat_id, title, username)
    await db.upsert_channel(chat_id, title, username, status=status,
                            rights={"can_post": True, "can_edit": True, "can_delete": True})


async def two_channels(db, bot):
    await add_channel(db, bot, CH_A, "Alpha", "alpha_chan")
    await add_channel(db, bot, CH_B, "Beta")


async def make_post(db, channel_ids, text_md="**Привет** мир", **fields) -> int:
    post_id = await db.create_post(text_md=text_md, created_by=1, **fields)
    await db.set_post_targets(post_id, channel_ids)
    return post_id


def fail_for(chat_id, exc):
    return lambda kwargs: exc if kwargs.get("chat_id") == chat_id else None


async def targets_of(db, post_id) -> dict[int, dict]:
    return {t["channel_id"]: t for t in await db.get_targets(post_id)}


# ------------------------------------------------------------------ render_text

def test_render_text_basic():
    assert render_text("  **жирный** _курсив_  ", has_media=False) == "<b>жирный</b> <i>курсив</i>"


def test_render_text_empty():
    with pytest.raises(PostError, match="Пост пустой"):
        render_text("   ", has_media=False)
    with pytest.raises(PostError, match="Пост пустой"):
        render_text("   ")
    assert render_text("", has_media=True) == ""


def test_render_text_has_photo_alias():
    assert render_text("", has_photo=True) == ""
    with pytest.raises(PostError) as err:
        render_text("б" * 1025, has_photo=True)
    assert "1025 из 1024" in str(err.value)
    assert render_text("a" * 2000, has_photo=False) == "a" * 2000


def test_render_text_too_long_text():
    with pytest.raises(PostError) as err:
        render_text("a" * 4097, has_media=False)
    assert str(err.value) == "Текст слишком длинный: 4097 из 4096 символов"
    assert render_text("a" * 4096, has_media=False) == "a" * 4096


def test_render_text_too_long_caption():
    with pytest.raises(PostError) as err:
        render_text("б" * 1025, has_media=True)
    assert str(err.value) == "Текст слишком длинный: 1025 из 1024 символов (для поста с медиа лимит 1024)"
    assert render_text("б" * 1024, has_media=True) == "б" * 1024


def test_render_text_counts_markup_as_invisible():
    text = "**" + "a" * 1024 + "**"
    assert render_text(text, has_media=True).startswith("<b>")


def test_render_text_validation_error(monkeypatch):
    monkeypatch.setattr(posting, "md_to_html", lambda text: "<b><i>x</b></i>")
    with pytest.raises(PostError, match="^Ошибка форматирования: "):
        render_text("x", has_media=False)


def test_post_kind_compat():
    assert post_kind({"media": []}) == "text"
    assert post_kind({"photo_file_id": "F"}) == "media"
    assert post_kind({"media": [photo("A"), video("B")]}) == "album:2"


# ------------------------------------------------------------------ links & report

def test_message_link_forms():
    assert message_link({"id": CH_A, "username": "alpha"}, 5) == "https://t.me/alpha/5"
    assert message_link({"id": -1001234567890, "username": None}, 7) == "https://t.me/c/1234567890/7"
    assert message_link({"channel_id": CH_B, "channel_username": "beta"}, 9) == "https://t.me/beta/9"
    assert message_link({"channel_id": -1009876, "channel_username": None}, 1) == "https://t.me/c/9876/1"


def test_report_summary_and_dict():
    report = PublishReport(post_id=3, results=[
        TargetResult(channel_id=1, title="A & B", ok=True, message_id=10, link="https://t.me/a/10"),
        TargetResult(channel_id=2, title="<C>", ok=False, error="Нет прав на публикацию"),
    ])
    assert (report.ok_count, report.fail_count) == (1, 1)
    assert report.summary_html() == (
        "⚠️ Опубликовано: 1 из 2\n"
        '• <a href="https://t.me/a/10">A &amp; B</a>\n'
        "❌ &lt;C&gt; — Нет прав на публикацию"
    )
    data = report.to_dict()
    assert data["ok_count"] == 1 and data["fail_count"] == 1 and data["post_id"] == 3
    assert data["results"][0] == {"channel_id": 1, "title": "A & B", "ok": True, "message_id": 10,
                                  "link": "https://t.me/a/10", "error": None, "note": None}
    json.dumps(data)


def test_report_summary_all_ok_delete_wording_and_note():
    ok = PublishReport(post_id=1, results=[TargetResult(1, "A", True, 5, "https://t.me/a/5")])
    assert ok.summary_html().startswith("✅ Опубликовано: 1 из 1")
    deleted = PublishReport(post_id=1, action="delete", results=[
        TargetResult(1, "A", True, 5, "https://t.me/a/5"),
        TargetResult(2, "B<", True, 6, note="Нет доступа"),
    ])
    assert deleted.summary_html() == "✅ Удалено: 2 из 2\n• A\n⚠️ B&lt; — Нет доступа"


# ------------------------------------------------------------------ send_post

async def test_send_post_text_options(bot, config):
    bot.add_chat(CH_A, "Alpha")
    post = {"text_md": "Ссылка https://example.com", "buttons": [[{"text": "Купить", "url": "https://shop.ru",
                                                                    "style": "success"}]],
            "disable_preview": True, "silent": True, "protect": True, "caption_above": False}
    messages = await send_post(bot, post, CH_A, config)
    assert isinstance(messages, list) and len(messages) == 1
    (call,) = bot.calls_of("send_message")
    assert call["chat_id"] == CH_A and call["parse_mode"] == "HTML"
    assert call["disable_notification"] is True and call["protect_content"] is True
    assert call["link_preview_options"] == LinkPreviewOptions(is_disabled=True)
    button = call["reply_markup"].inline_keyboard[0][0]
    assert (button.text, button.url, button.style) == ("Купить", "https://shop.ru", "success")


async def test_send_post_text_defaults(bot, config):
    (message,) = await send_post(bot, {"text_md": "hi", "buttons": []}, 5, config)
    assert message.chat.id == 5
    (call,) = bot.calls_of("send_message")
    assert call["link_preview_options"] is None and call["reply_markup"] is None
    assert call["disable_notification"] is False and call["protect_content"] is False


async def test_send_post_legacy_photo_by_file_id(bot, config):
    post = {"text_md": "", "photo_file_id": "FILE", "buttons": [], "caption_above": True}
    (message,) = await send_post(bot, post, 5, config)
    assert message.photo[-1].file_id == "FILE"
    (call,) = bot.calls_of("send_photo")
    assert call["photo"] == "FILE" and call["caption"] is None
    assert call["show_caption_above_media"] is True and call["parse_mode"] == "HTML"


async def test_send_post_video_upload(bot, config):
    name = upload(config, "mp4")
    post = {"text_md": "**Видео**", "media": [video(path=name)], "buttons": BUTTONS, "silent": True,
            "caption_above": True}
    (message,) = await send_post(bot, post, 5, config)
    (call,) = bot.calls_of("send_video")
    assert isinstance(call["video"], FSInputFile) and str(call["video"].path).endswith(name)
    assert call["supports_streaming"] is True and call["caption"] == "<b>Видео</b>"
    assert call["parse_mode"] == "HTML" and call["show_caption_above_media"] is True
    assert call["disable_notification"] is True and call["reply_markup"].inline_keyboard[0][0].text == "Сайт"
    assert message.video.file_id.startswith("fake_video_")
    assert not bot.calls_of("send_photo") and not bot.calls_of("send_message")


async def test_send_post_animation_by_file_id(bot, config):
    post = {"text_md": "гиф", "media": [gif(file_id="GIF_ID")], "buttons": []}
    (message,) = await send_post(bot, post, 5, config)
    (call,) = bot.calls_of("send_animation")
    assert call["animation"] == "GIF_ID" and call["caption"] == "гиф"
    assert call["show_caption_above_media"] is None
    assert message.animation.file_id == "GIF_ID"


async def test_send_post_album(bot, config):
    name = upload(config, "jpg")
    post = {"text_md": "Подпись _альбома_", "media": [photo(path=name), video("VID"), photo("PH2")],
            "buttons": [], "silent": True, "protect": True, "caption_above": True}
    messages = await send_post(bot, post, 5, config)
    assert len(messages) == 3 and len({m.media_group_id for m in messages}) == 1
    (call,) = bot.calls_of("send_media_group")
    assert call["chat_id"] == 5 and call["disable_notification"] is True and call["protect_content"] is True
    assert "reply_markup" not in call
    first, second, third = call["media"]
    assert isinstance(first, InputMediaPhoto) and isinstance(first.media, FSInputFile)
    assert first.caption == "Подпись <i>альбома</i>" and first.parse_mode == "HTML"
    assert first.show_caption_above_media is True
    assert isinstance(second, InputMediaVideo) and second.media == "VID" and second.caption is None
    assert second.supports_streaming is True and second.show_caption_above_media is None
    assert isinstance(third, InputMediaPhoto) and third.media == "PH2" and third.caption is None


async def test_send_post_album_with_buttons_rejected(bot, config):
    post = {"text_md": "x", "media": [photo("A"), photo("B")], "buttons": BUTTONS}
    with pytest.raises(PostError) as err:
        await send_post(bot, post, 5, config)
    assert str(err.value) == ALBUM_BUTTONS_ERROR
    assert bot.calls == []


async def test_send_post_gif_in_album_rejected(bot, config):
    with pytest.raises(PostError, match=ALBUM_GIF_ERROR):
        await send_post(bot, {"text_md": "x", "media": [photo("A"), gif("G")], "buttons": []}, 5, config)
    assert bot.calls == []


async def test_send_post_caption_limit_for_media(bot, config):
    with pytest.raises(PostError, match="1100 из 1024"):
        await send_post(bot, {"text_md": "я" * 1100, "media": [video("V")], "buttons": []}, 5, config)
    with pytest.raises(PostError, match="1100 из 1024"):
        await send_post(bot, {"text_md": "я" * 1100, "media": [photo("A"), video("V")], "buttons": []}, 5, config)
    assert bot.calls == []


async def test_send_post_missing_upload(bot, config):
    missing = f"{HEX}.jpg"
    with pytest.raises(PostError, match="Файл картинки не найден"):
        await send_post(bot, {"text_md": "x", "media": [photo(path=missing)], "buttons": []}, 5, config)
    with pytest.raises(PostError, match="Файл видео не найден"):
        await send_post(bot, {"text_md": "x", "media": [video(path=f"{HEX}.mp4")], "buttons": []}, 5, config)
    with pytest.raises(PostError, match="Некорректное имя файла"):
        await send_post(bot, {"text_md": "x", "photo_path": "../secret.jpg", "buttons": []}, 5, config)
    assert bot.calls == []


async def test_media_source_stays_inside_uploads(config):
    with pytest.raises(PostError, match="Некорректный путь"):
        posting._media_source({"type": "photo", "file_id": None, "path": "../outside.jpg"}, config)


# ------------------------------------------------------------------ publish_post

async def test_publish_photo_to_two_channels_reuses_file_id(db, bot, config, sleeps):
    await two_channels(db, bot)
    name = upload(config, "jpg")
    post_id = await make_post(db, [CH_A, CH_B], media=[photo(path=name)], silent=True, buttons=BUTTONS)

    report = await publish_post(bot, db, config, post_id)

    assert report.ok_count == 2 and report.fail_count == 0
    first, second = bot.calls_of("send_photo")
    assert first["chat_id"] == CH_A and isinstance(first["photo"], FSInputFile)
    assert second["chat_id"] == CH_B and second["photo"] == "fake_file_1"
    assert first["caption"] == "<b>Привет</b> мир" and first["disable_notification"] is True
    assert first["reply_markup"].inline_keyboard[0][0].style == "primary"

    post = await db.get_post(post_id)
    assert post["status"] == "sent" and post["error"] is None and post["sent_at"]
    assert post["media"] == [{"type": "photo", "file_id": "fake_file_1", "path": name, "file_bot": BOT_ID}]
    assert post["photo_file_id"] == "fake_file_1" and post["photo_path"] == name  # legacy mirror
    assert post["published_kind"] == "media"
    assert post["text_html"] == "<b>Привет</b> мир"
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["status"] == "sent" and targets[CH_A]["message_id"] == report.results[0].message_id
    assert targets[CH_A]["message_ids"] == [targets[CH_A]["message_id"]]
    assert targets[CH_A]["message_ids_json"] is None
    assert targets[CH_B]["status"] == "sent" and targets[CH_B]["sent_at"]
    assert targets[CH_A]["media_sig"] == targets[CH_B]["media_sig"] == media_signature(post["media"])

    assert report.results[0].link == f"https://t.me/alpha_chan/{report.results[0].message_id}"
    assert report.results[1].link == f"https://t.me/c/1000000002/{report.results[1].message_id}"
    rows = await db.fetchall("SELECT channel_id, via_bot, media_group_id FROM channel_posts ORDER BY channel_id")
    assert rows == [{"channel_id": CH_B, "via_bot": 1, "media_group_id": None},
                    {"channel_id": CH_A, "via_bot": 1, "media_group_id": None}]
    assert sleeps == [posting.CHANNEL_PAUSE]
    assert "✅ Опубликовано: 2 из 2" in report.summary_html()


async def test_publish_video_upload_once_then_file_id(db, bot, config, sleeps):
    await two_channels(db, bot)
    name = upload(config, "mp4")
    post_id = await make_post(db, [CH_A, CH_B], media=[video(path=name)], caption_above=True)
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 2
    first, second = bot.calls_of("send_video")
    assert isinstance(first["video"], FSInputFile) and first["supports_streaming"] is True
    assert second["video"] == "fake_video_1" and first["show_caption_above_media"] is True
    post = await db.get_post(post_id)
    assert post["media"] == [{"type": "video", "file_id": "fake_video_1", "path": name, "file_bot": BOT_ID}]
    assert post["photo_file_id"] is None and post["photo_path"] is None  # not a photo
    assert post["published_kind"] == "media"


async def test_publish_animation(db, bot, config, sleeps):
    await add_channel(db, bot, CH_A, "Alpha")
    name = upload(config, "gif")
    post_id = await make_post(db, [CH_A], text_md="", media=[gif(path=name)])
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 1
    (call,) = bot.calls_of("send_animation")
    assert isinstance(call["animation"], FSInputFile) and call["caption"] is None
    assert (await db.get_post(post_id))["media"][0]["file_id"] == "fake_anim_1"


async def test_publish_album_to_two_channels(db, bot, config, sleeps):
    await two_channels(db, bot)
    first_name, second_name = upload(config, "jpg", 1), upload(config, "mp4", 2)
    media = [photo(path=first_name), video(path=second_name), photo(file_id="KNOWN")]
    post_id = await make_post(db, [CH_A, CH_B], text_md="Альбом", media=media)

    report = await publish_post(bot, db, config, post_id)

    assert report.ok_count == 2
    call_a, call_b = bot.calls_of("send_media_group")
    assert isinstance(call_a["media"][0].media, FSInputFile) and isinstance(call_a["media"][1].media, FSInputFile)
    assert [m.media for m in call_b["media"]] == ["fake_file_1", "fake_video_2", "KNOWN"]
    assert call_a["media"][0].caption == "Альбом" and call_b["media"][0].caption == "Альбом"
    post = await db.get_post(post_id)
    assert post["media"] == [
        {"type": "photo", "file_id": "fake_file_1", "path": first_name, "file_bot": BOT_ID},
        {"type": "video", "file_id": "fake_video_2", "path": second_name, "file_bot": BOT_ID},
        {"type": "photo", "file_id": "KNOWN", "path": None},
    ]
    assert post["published_kind"] == "album:3" and post["status"] == "sent"
    targets = await targets_of(db, post_id)
    ids_a = targets[CH_A]["message_ids"]
    assert len(ids_a) == 3 and targets[CH_A]["message_id"] == ids_a[0]
    assert json.loads(targets[CH_A]["message_ids_json"]) == ids_a
    assert report.results[0].message_id == ids_a[0]
    assert report.results[0].link == f"https://t.me/alpha_chan/{ids_a[0]}"
    rows = await db.fetchall("SELECT channel_id, message_id, media_group_id FROM channel_posts ORDER BY message_id")
    assert [(r["channel_id"], r["message_id"]) for r in rows] == [
        (CH_A, ids_a[0]), (CH_B, targets[CH_B]["message_ids"][0])]
    assert all(r["media_group_id"] for r in rows)
    # the album found by any of its messages (reactions arrive per message)
    assert (await db.find_target_by_message(CH_A, ids_a[2]))["post_id"] == post_id


async def test_publish_album_with_buttons_rejected_before_claim(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A], media=[photo("A"), photo("B")], buttons=BUTTONS)
    with pytest.raises(PostError) as err:
        await publish_post(bot, db, config, post_id)
    assert str(err.value) == ALBUM_BUTTONS_ERROR
    assert (await db.get_post(post_id))["status"] == "draft"
    assert bot.calls == []


async def test_publish_text_post(db, bot, config, sleeps):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A], text_md="Текст", disable_preview=True, protect=True)
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 1
    (call,) = bot.calls_of("send_message")
    assert call["text"] == "Текст" and call["protect_content"] is True
    assert call["link_preview_options"].is_disabled is True
    assert (await db.get_post(post_id))["published_kind"] == "text"


async def test_publish_partial_failure_then_retry_failed_only(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    bot.fail_on["send_message"] = fail_for(CH_B, bad_request("Bad Request: chat not found"))

    report = await publish_post(bot, db, config, post_id)

    assert (report.ok_count, report.fail_count) == (1, 1)
    assert report.results[1].error == "Канал не найден"
    post = await db.get_post(post_id)
    assert post["status"] == "partial"
    assert post["error"] == "Beta: Канал не найден"
    targets = await targets_of(db, post_id)
    assert targets[CH_B]["status"] == "failed" and targets[CH_B]["error"] == "Канал не найден"
    assert post["sent_at"]

    bot.fail_on.clear()
    bot.calls.clear()
    report = await publish_post(bot, db, config, post_id)
    assert [c["chat_id"] for c in bot.calls_of("send_message")] == [CH_B]
    assert report.ok_count == 1
    post = await db.get_post(post_id)
    assert post["status"] == "sent" and post["error"] is None
    assert all(t["status"] == "sent" for t in post["targets"])


@pytest.mark.parametrize("exc, expected", [
    (forbidden("Forbidden: bot is not a member of the channel chat"), "Бот удалён из канала"),
    (bad_request("Bad Request: need administrator rights in the channel chat"), "Нет прав на публикацию"),
    (bad_request("Bad Request: not enough rights to send text messages to the chat"), "Нет прав на публикацию"),
    (bad_request("Bad Request: CHAT_ADMIN_REQUIRED"), "Нет прав на публикацию"),
    (bad_request("Bad Request: can't parse entities: unsupported tag"),
     "Ошибка разметки: Bad Request: can't parse entities: unsupported tag"),
    (bad_request("Bad Request: something odd"), "Bad Request: something odd"),
])
async def test_publish_error_messages(db, bot, config, exc, expected):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    bot.fail_on["send_message"] = exc
    report = await publish_post(bot, db, config, post_id)
    assert report.results[0].error == expected
    post = await db.get_post(post_id)
    assert post["status"] == "failed"
    assert post["error"] == f"Alpha: {expected}"
    assert post["published_kind"] is None and post["sent_at"] is None


@pytest.mark.parametrize("message, expected", [
    ("Bad Request: wrong file identifier/HTTP URL specified", "Файл недоступен в Telegram — загрузите его заново"),
    ("Request Entity Too Large", "Файл слишком большой для Telegram (не больше 50 МБ)"),
    ("Bad Request: PHOTO_INVALID_DIMENSIONS", "Telegram не принял картинку: неподходящие размеры"),
    ("Bad Request: message caption is too long", "Подпись слишком длинная для Telegram"),
])
async def test_publish_media_error_messages(db, bot, config, message, expected):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A], media=[video("V")])
    bot.fail_on["send_video"] = bad_request(message)
    report = await publish_post(bot, db, config, post_id)
    assert report.results[0].error == expected


async def test_publish_retry_after_retries_once(db, bot, config, sleeps):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    attempts = []

    def flaky(kwargs):
        attempts.append(1)
        return retry_after(5) if len(attempts) == 1 else None

    bot.fail_on["send_message"] = flaky
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 1
    assert len(bot.calls_of("send_message")) == 2
    assert sleeps == [5]
    assert (await db.get_post(post_id))["status"] == "sent"


async def test_publish_retry_after_beyond_cap_fails_at_once(db, bot, config, sleeps):
    """Regression (review2 #5): a wait longer than RETRY_AFTER_MAX is not sat out for 30 s and
    retried inside the flood window (rejected again) — the manual publish fails at once."""
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    bot.fail_on["send_message"] = retry_after(100)
    report = await publish_post(bot, db, config, post_id)
    assert sleeps == []
    assert len(bot.calls_of("send_message")) == 1
    assert report.results[0].error == "Telegram ограничил частоту запросов, повторите через 100 с"
    assert (await db.get_post(post_id))["status"] == "failed"


async def test_publish_network_and_unexpected_errors_do_not_stick(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    network = TelegramNetworkError(method=SendMessage(chat_id=0, text="x"), message="timeout")
    bot.fail_on["send_message"] = lambda kw: network if kw["chat_id"] == CH_A else RuntimeError("boom")
    report = await publish_post(bot, db, config, post_id)
    assert [r.error for r in report.results] == ["Сетевая ошибка: timeout", "Неизвестная ошибка: boom"]
    assert (await db.get_post(post_id))["status"] == "failed"


async def test_publish_db_crash_mid_way_never_leaves_sending(db, bot, config, monkeypatch):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add_channel_post", broken)
    with pytest.raises(RuntimeError):
        await publish_post(bot, db, config, post_id)
    post = await db.get_post(post_id)
    assert post["status"] == "sent"  # the target was marked sent before the crash
    assert posting.active_operations() == 0


async def test_publish_crash_before_any_send_marks_failed(db, bot, config, monkeypatch):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    original = db.update_target
    calls = []

    async def broken(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("status") == "sent":
            raise RuntimeError("disk full")
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "update_target", broken)
    with pytest.raises(RuntimeError):
        await publish_post(bot, db, config, post_id)
    post = await db.get_post(post_id)
    assert post["status"] == "failed" and post["error"] == "Публикация прервана"
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["status"] == "failed" and targets[CH_A]["error"] == posting.INTERRUPTED_ERROR
    assert targets[CH_B]["status"] == "pending"  # never attempted


async def test_cancelled_due_publish_does_not_leave_target_due(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    await queue(db, post_id, CH_A, now() - 5)
    started = asyncio.Event()

    async def hanging_send(**kwargs):
        started.set()
        await asyncio.sleep(3600)

    bot.send_message = hanging_send
    task = asyncio.create_task(publish_post(bot, db, config, post_id, only_due=True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    post = await db.get_post(post_id)
    assert post["status"] == "failed" and post["targets"][0]["status"] == "failed"
    assert await db.get_due_targets(now() + 60) == []
    assert posting.active_operations() == 0


async def test_double_publish_blocked(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    await publish_post(bot, db, config, post_id)
    with pytest.raises(PostError, match="Пост уже публикуется или опубликован"):
        await publish_post(bot, db, config, post_id)
    assert len(bot.calls_of("send_message")) == 1


async def test_concurrent_publish_sends_once(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    results = await asyncio.gather(publish_post(bot, db, config, post_id),
                                   publish_post(bot, db, config, post_id), return_exceptions=True)
    assert sum(isinstance(r, PublishReport) for r in results) == 1
    assert sum(isinstance(r, PostError) for r in results) == 1
    assert len(bot.calls_of("send_message")) == 1


async def test_claimed_post_is_rejected(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    assert await db.claim_post_for_sending(post_id)
    with pytest.raises(PostError, match="уже публикуется"):
        await publish_post(bot, db, config, post_id)
    assert bot.calls == []


async def test_render_error_leaves_post_unclaimed(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A], text_md="x" * 5000, status="scheduled", scheduled_at=1)
    with pytest.raises(PostError, match="слишком длинный"):
        await publish_post(bot, db, config, post_id)
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["targets"][0]["status"] == "pending"
    assert bot.calls == []


async def test_missing_media_file_and_no_targets_leave_post_unclaimed(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A], media=[photo(path=f"{HEX}.jpg")])
    with pytest.raises(PostError, match="Файл картинки не найден"):
        await publish_post(bot, db, config, post_id)
    assert (await db.get_post(post_id))["status"] == "draft"

    album_id = await make_post(db, [CH_A], media=[photo("OK"), video(path=f"{HEX}.mp4")])
    with pytest.raises(PostError, match="Файл видео не найден"):
        await publish_post(bot, db, config, album_id)

    empty_id = await make_post(db, [])
    with pytest.raises(PostError, match="Не выбраны каналы для публикации"):
        await publish_post(bot, db, config, empty_id)
    assert (await db.get_post(empty_id))["status"] == "draft"
    with pytest.raises(PostError, match="Пост не найден"):
        await publish_post(bot, db, config, 999)
    assert bot.calls == []


async def test_inactive_channel_target_fails(db, bot, config, sleeps):
    await add_channel(db, bot, CH_A, "Alpha")
    await add_channel(db, bot, CH_B, "Beta", status=CH_PENDING)
    post_id = await make_post(db, [CH_A, CH_B, -1009999])

    report = await publish_post(bot, db, config, post_id)

    assert [c["chat_id"] for c in bot.calls_of("send_message")] == [CH_A]
    errors = {r.channel_id: r.error for r in report.results if not r.ok}
    assert errors == {CH_B: "Канал не подключён к боту", -1009999: "Канал не подключён к боту"}
    assert (await db.get_post(post_id))["status"] == "partial"


async def test_publish_works_on_unmigrated_database(bot, config):
    database = Database(":memory:")
    await database.connect()
    try:
        await add_channel(database, bot, CH_A, "Alpha")
        post_id = await make_post(database, [CH_A])
        report = await publish_post(bot, database, config, post_id)
        assert report.ok_count == 1
        assert (await database.get_post(post_id))["published_kind"] == "text"
    finally:
        await database.close()


async def test_migrate_is_idempotent(db):
    await db.migrate()
    assert await db.ensure_column("posts", "published_kind", "TEXT") is False
    columns = [r["name"] for r in await db.fetchall("PRAGMA table_info(posts)")]
    assert columns.count("published_kind") == 1


# ---------------------------------------------------- publishing remaining targets: edit class

async def test_remaining_targets_must_keep_edit_class(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    bot.fail_on["send_message"] = fail_for(CH_B, bad_request("Bad Request: chat not found"))
    await publish_post(bot, db, config, post_id)
    bot.fail_on.clear()
    await db.update_post(post_id, media=[photo("PIC")])
    with pytest.raises(PostError) as err:
        await publish_post(bot, db, config, post_id)
    assert str(err.value) == KIND_MISMATCH_ERROR
    assert (await db.get_post(post_id))["status"] == "partial"


async def test_remaining_album_targets_must_keep_count(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B], media=[photo("A"), photo("B"), photo("C")])
    bot.fail_on["send_media_group"] = fail_for(CH_B, bad_request("Bad Request: chat not found"))
    await publish_post(bot, db, config, post_id)
    bot.fail_on.clear()
    await db.update_post(post_id, media=[photo("A"), photo("B")])
    with pytest.raises(PostError, match="должно остаться 3 файлов"):
        await publish_post(bot, db, config, post_id)
    await db.update_post(post_id, media=[video("X"), photo("B"), photo("C")])  # same count: allowed
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 1


async def test_legacy_published_kind_photo_counts_as_media(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B], media=[photo("P")])
    await db.update_target(post_id, CH_A, status="sent", message_id=5)
    await db.execute("UPDATE posts SET published_kind = 'photo', status = 'partial' WHERE id = ?", (post_id,))
    await db.update_post(post_id, media=[video("V")])  # single media may switch type
    report = await publish_post(bot, db, config, post_id)
    assert report.ok_count == 1 and bot.calls_of("send_video")[0]["chat_id"] == CH_B


# ------------------------------------------------------------------ schedules & only_due

async def queue(db, post_id, channel_id, ts, queued=True):
    await db.set_target_schedule(post_id, channel_id, ts, queued=queued)
    await db.refresh_post_schedule(post_id)


async def test_manual_publish_sends_queued_targets_now_and_clears_times(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    await queue(db, post_id, CH_A, now() + 3600)
    await queue(db, post_id, CH_B, now() + 7200, queued=False)
    assert (await db.get_post(post_id))["status"] == "scheduled"

    report = await publish_post(bot, db, config, post_id)

    assert report.ok_count == 2
    post = await db.get_post(post_id)
    assert post["status"] == "sent" and post["scheduled_at"] is None
    assert all(t["scheduled_at"] is None and t["queued"] is False for t in post["targets"])


async def test_manual_publish_selected_channels_keeps_the_rest_scheduled(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    later = now() + 7200
    await queue(db, post_id, CH_A, now() + 3600)
    await queue(db, post_id, CH_B, later)

    report = await publish_post(bot, db, config, post_id, channel_ids=[CH_A])

    assert [r.channel_id for r in report.results] == [CH_A]
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["scheduled_at"] == later
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["status"] == "sent" and targets[CH_B]["status"] == "pending"
    assert targets[CH_B]["scheduled_at"] == later and targets[CH_B]["queued"] is True
    with pytest.raises(PostError, match="Не выбраны каналы"):
        await publish_post(bot, db, config, post_id, channel_ids=[CH_C])


async def test_only_due_publishes_due_targets_only(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    t0 = now()
    await queue(db, post_id, CH_A, t0 - 10)
    await queue(db, post_id, CH_B, t0 + 3600)

    report = await publish_post(bot, db, config, post_id, only_due=True, now_ts=t0)

    assert [r.channel_id for r in report.results] == [CH_A]
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["scheduled_at"] == t0 + 3600
    assert post["published_kind"] == "text" and post["sent_at"]
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["status"] == "sent" and targets[CH_A]["scheduled_at"] is None
    assert targets[CH_B]["status"] == "pending"

    with pytest.raises(PostNotDue):
        await publish_post(bot, db, config, post_id, only_due=True, now_ts=t0 + 60)

    report = await publish_post(bot, db, config, post_id, only_due=True, now_ts=t0 + 3600)
    assert [r.channel_id for r in report.results] == [CH_B]
    post = await db.get_post(post_id)
    assert post["status"] == "sent" and post["scheduled_at"] is None
    assert [c["chat_id"] for c in bot.calls_of("send_message")] == [CH_A, CH_B]


async def test_only_due_requires_scheduled_due_post(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    draft = await make_post(db, [CH_A])
    with pytest.raises(PostNotDue):
        await publish_post(bot, db, config, draft, only_due=True)
    # V1-style cancel: post back to draft but the target kept its time → still not published
    cancelled = await make_post(db, [CH_A])
    await queue(db, cancelled, CH_A, now() - 5)
    await db.update_post(cancelled, status="draft", scheduled_at=None)
    with pytest.raises(PostNotDue):
        await publish_post(bot, db, config, cancelled, only_due=True)
    # V1-style reschedule: post time moved to the future, target kept the old time
    moved = await make_post(db, [CH_A])
    await queue(db, moved, CH_A, now() - 5)
    await db.update_post(moved, scheduled_at=now() + 3600)
    with pytest.raises(PostNotDue):
        await publish_post(bot, db, config, moved, only_due=True)
    assert bot.calls == []
    assert (await db.get_post(moved))["status"] == "scheduled"


async def test_only_due_target_moved_between_check_and_claim(db, bot, config, monkeypatch):
    """Regression (review #0/#19): a target moved while the publish was being prepared is not sent."""
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    await queue(db, post_id, CH_A, now() - 5)
    later = now() + 3600
    original = db.claim_post_for_sending

    async def move_then_claim(*args, **kwargs):
        await db.set_target_schedule(post_id, CH_A, later)  # plan.move_target while the post is due
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "claim_post_for_sending", move_then_claim)
    with pytest.raises(PostNotDue):
        await publish_post(bot, db, config, post_id, only_due=True)
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["scheduled_at"] == later
    assert bot.calls == []


async def test_only_due_unscheduled_between_check_and_claim(db, bot, config, monkeypatch):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    await queue(db, post_id, CH_A, now() - 5)
    original = db.claim_post_for_sending

    async def unschedule_then_claim(*args, **kwargs):
        await db.set_target_schedule(post_id, CH_A, None)
        await db.refresh_post_schedule(post_id)  # plan.unschedule_post → draft
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "claim_post_for_sending", unschedule_then_claim)
    with pytest.raises(PostNotDue):
        await publish_post(bot, db, config, post_id, only_due=True)
    assert (await db.get_post(post_id))["status"] == "draft"
    assert bot.calls == []


async def test_only_due_claimed_but_targets_gone_restores_status(db, bot, config, monkeypatch):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    await queue(db, post_id, CH_A, now() - 5)
    original = db.claim_post_for_sending

    async def claim_then_unqueue(*args, **kwargs):
        claimed = await original(*args, **kwargs)
        await db.set_target_schedule(post_id, CH_A, None)  # unqueued right after the claim
        return claimed

    monkeypatch.setattr(db, "claim_post_for_sending", claim_then_unqueue)
    with pytest.raises(PostNotDue):
        await publish_post(bot, db, config, post_id, only_due=True)
    post = await db.get_post(post_id)
    assert post["status"] == "draft" and post["error"] is None
    assert bot.calls == []


async def test_edit_saved_just_before_claim_is_what_goes_out(db, bot, config, monkeypatch):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A], text_md="старый")
    original = db.claim_post_for_sending

    async def edit_then_claim(*args, **kwargs):
        await db.update_post(post_id, text_md="новый")
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "claim_post_for_sending", edit_then_claim)
    await publish_post(bot, db, config, post_id)
    assert channel_texts_of(bot, CH_A) == ["новый"]
    assert (await db.get_post(post_id))["text_html"] == "новый"


async def test_post_broken_just_before_claim_is_not_sent(db, bot, config, monkeypatch):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    await queue(db, post_id, CH_A, now() - 5)
    original = db.claim_post_for_sending

    async def break_then_claim(*args, **kwargs):
        await db.update_post(post_id, text_md="x" * 5000)
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "claim_post_for_sending", break_then_claim)
    with pytest.raises(PostError, match="слишком длинный"):
        await publish_post(bot, db, config, post_id, only_due=True)
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["targets"][0]["status"] == "pending"
    assert bot.calls == []


def channel_texts_of(bot, chat_id):
    return [c["text"] for c in bot.calls_of("send_message") if c["chat_id"] == chat_id]


async def test_only_due_target_failure_keeps_rest_scheduled(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    await queue(db, post_id, CH_A, now() - 5)
    await queue(db, post_id, CH_B, now() + 3600)
    bot.fail_on["send_message"] = bad_request("Bad Request: chat not found")
    report = await publish_post(bot, db, config, post_id, only_due=True)
    assert report.fail_count == 1
    post = await db.get_post(post_id)
    assert post["status"] == "scheduled" and post["error"] == "Alpha: Канал не найден"
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["status"] == "failed" and targets[CH_A]["scheduled_at"] is None


# ------------------------------------------------------------------ in-flight tracking

async def test_wait_idle_waits_for_running_publish(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    release = asyncio.Event()
    started = asyncio.Event()
    original = bot.send_message

    async def slow_send(**kwargs):
        started.set()
        await release.wait()
        return await original(**kwargs)

    bot.send_message = slow_send
    task = asyncio.create_task(publish_post(bot, db, config, post_id))
    await started.wait()
    assert posting.active_operations() == 1
    assert await posting.wait_idle(0.05, poll=0.01) is False
    asyncio.get_running_loop().call_later(0.05, release.set)
    assert await posting.wait_idle(5, poll=0.01) is True
    assert task.done() and (await task).ok_count == 1
    assert (await db.get_post(post_id))["status"] == "sent"
    assert await posting.wait_idle(0) is True


# ------------------------------------------------------------------ edit_published_post

async def published(db, bot, config, **fields) -> int:
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B], **fields)
    await publish_post(bot, db, config, post_id)
    bot.calls.clear()
    return post_id


async def test_edit_text_post(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    await db.update_post(post_id, text_md="Новый *текст*", disable_preview=True,
                         buttons=[[{"text": "Ок", "url": "https://ok.ru", "style": "danger"}]])
    report = await edit_published_post(bot, db, config, post_id)
    assert report.ok_count == 2 and report.action == "edit"
    calls = bot.calls_of("edit_message_text")
    assert [c["chat_id"] for c in calls] == [CH_A, CH_B]
    targets = await targets_of(db, post_id)
    assert calls[0]["message_id"] == targets[CH_A]["message_id"]
    assert calls[0]["text"] == "Новый <i>текст</i>" and calls[0]["parse_mode"] == "HTML"
    assert calls[0]["link_preview_options"].is_disabled is True
    assert calls[0]["reply_markup"].inline_keyboard[0][0].style == "danger"
    post = await db.get_post(post_id)
    assert post["text_html"] == "Новый <i>текст</i>" and post["status"] == "sent"
    assert report.summary_html().startswith("✅ Обновлено: 2 из 2")


async def test_edit_not_modified_counts_as_success(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    bot.fail_on["edit_message_text"] = bad_request(
        "Bad Request: message is not modified: specified new message content and reply markup are "
        "exactly the same")
    report = await edit_published_post(bot, db, config, post_id)
    assert report.ok_count == 2 and report.fail_count == 0


async def test_edit_failure_keeps_target_sent(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    bot.fail_on["edit_message_text"] = fail_for(CH_B, bad_request("Bad Request: message to edit not found"))
    report = await edit_published_post(bot, db, config, post_id)
    assert (report.ok_count, report.fail_count) == (1, 1)
    assert report.results[1].error == "Сообщение не найдено в канале (возможно, удалено вручную)"
    post = await db.get_post(post_id)
    targets = {t["channel_id"]: t for t in post["targets"]}
    assert targets[CH_B]["status"] == "sent" and targets[CH_B]["error"]
    assert post["error"].startswith("Beta: ")


async def test_edit_caption_of_photo_post(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("PHOTO")])
    await db.update_post(post_id, text_md="Подпись", caption_above=True)
    report = await edit_published_post(bot, db, config, post_id)
    assert report.ok_count == 2
    calls = bot.calls_of("edit_message_caption")
    assert len(calls) == 2 and not bot.calls_of("edit_message_media")
    assert calls[0]["caption"] == "Подпись" and calls[0]["show_caption_above_media"] is True
    assert calls[0]["parse_mode"] == "HTML" and calls[0]["reply_markup"] is None


async def test_edit_media_detected_by_signature(db, config, sleeps):
    bot = FakeBot()
    bot.edit_media_returns_message = True
    post_id = await published(db, bot, config, media=[photo("OLD")])
    name = upload(config, "png")
    await db.update_post(post_id, media=[photo(path=name)], text_md="Новая")

    report = await edit_published_post(bot, db, config, post_id)  # no flag needed: signatures differ

    assert report.ok_count == 2
    first, second = bot.calls_of("edit_message_media")
    assert isinstance(first["media"], InputMediaPhoto) and isinstance(first["media"].media, FSInputFile)
    assert first["media"].caption == "Новая" and first["media"].parse_mode == "HTML"
    assert second["media"].media == "fake_file_1"  # uploaded once, reused for the other channel
    post = await db.get_post(post_id)
    assert post["media"] == [{"type": "photo", "file_id": "fake_file_1", "path": name, "file_bot": BOT_ID}]
    assert not bot.calls_of("edit_message_caption")
    assert all(t["media_sig"] == media_signature(post["media"]) for t in post["targets"])

    bot.calls.clear()
    await edit_published_post(bot, db, config, post_id, media_changed=True)  # already shown everywhere
    assert not bot.calls_of("edit_message_media") and len(bot.calls_of("edit_message_caption")) == 2


async def test_failed_media_edit_is_retried_on_next_save(db, bot, config, sleeps):
    """Regression (review #1/#21): a channel whose media edit failed gets the media on the next save."""
    post_id = await published(db, bot, config, media=[photo("OLD")])
    await db.update_post(post_id, media=[photo("NEW")])
    network = TelegramNetworkError(method=SendMessage(chat_id=0, text="x"), message="timeout")
    bot.fail_on["edit_message_media"] = fail_for(CH_B, network)

    report = await edit_published_post(bot, db, config, post_id, media_changed=True)
    assert (report.ok_count, report.fail_count) == (1, 1)

    bot.fail_on.clear()
    bot.calls.clear()
    report = await edit_published_post(bot, db, config, post_id, media_changed=False)  # e.g. caption fix

    assert (report.ok_count, report.fail_count) == (2, 0)
    (media_call,) = bot.calls_of("edit_message_media")
    assert media_call["chat_id"] == CH_B and media_call["media"].media == "NEW"
    assert [c["chat_id"] for c in bot.calls_of("edit_message_caption")] == [CH_A]
    targets = await targets_of(db, post_id)
    assert targets[CH_B]["error"] is None
    assert targets[CH_A]["media_sig"] == targets[CH_B]["media_sig"] == media_signature([photo("NEW")])


async def test_total_media_edit_failure_retried(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("OLD")])
    await db.update_post(post_id, media=[photo("NEW")])
    bot.fail_on["edit_message_media"] = bad_request("Bad Request: message can't be edited")
    report = await edit_published_post(bot, db, config, post_id, media_changed=True)
    assert report.ok_count == 0
    bot.fail_on.clear()
    bot.calls.clear()
    report = await edit_published_post(bot, db, config, post_id)
    assert report.ok_count == 2 and len(bot.calls_of("edit_message_media")) == 2


async def test_legacy_target_without_signature_uses_flag(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("OLD")])
    await db.execute("UPDATE post_targets SET media_sig = NULL WHERE post_id = ?", (post_id,))
    await db.update_post(post_id, media=[photo("NEW")])

    await edit_published_post(bot, db, config, post_id)  # pre-V2 semantics: no flag → caption only
    assert not bot.calls_of("edit_message_media") and len(bot.calls_of("edit_message_caption")) == 2
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["media_sig"] == media_signature([photo("NEW")])  # adopted after success

    await db.execute("UPDATE post_targets SET media_sig = NULL WHERE post_id = ?", (post_id,))
    await db.update_post(post_id, media=[photo("NEWER")])
    bot.calls.clear()
    bot.fail_on["edit_message_media"] = fail_for(CH_B, bad_request("Bad Request: something odd"))
    report = await edit_published_post(bot, db, config, post_id, photo_changed=True)  # deprecated alias
    assert report.fail_count == 1 and len(bot.calls_of("edit_message_media")) == 2
    assert (await targets_of(db, post_id))[CH_B]["media_sig"] == posting.STALE_MEDIA_SIG

    bot.fail_on.clear()
    bot.calls.clear()
    await edit_published_post(bot, db, config, post_id)
    (retry,) = bot.calls_of("edit_message_media")
    assert retry["chat_id"] == CH_B


async def test_edit_single_media_can_switch_type(db, config, sleeps):
    bot = FakeBot()
    bot.edit_media_returns_message = True
    post_id = await published(db, bot, config, media=[photo("P")], buttons=BUTTONS, caption_above=True)
    name = upload(config, "gif")
    await db.update_post(post_id, media=[gif(path=name)])
    report = await edit_published_post(bot, db, config, post_id)
    assert report.ok_count == 2
    first, second = bot.calls_of("edit_message_media")
    assert isinstance(first["media"], InputMediaAnimation) and first["reply_markup"].inline_keyboard
    assert first["media"].show_caption_above_media is True
    assert second["media"].media == "fake_anim_1"
    assert (await db.get_post(post_id))["media"][0]["file_id"] == "fake_anim_1"


async def test_edit_kind_mismatch(db, bot, config):
    post_id = await published(db, bot, config)
    await db.update_post(post_id, media=[photo("PHOTO")])
    with pytest.raises(PostError) as err:
        await edit_published_post(bot, db, config, post_id)
    assert str(err.value) == KIND_MISMATCH_ERROR
    assert bot.calls == []


async def test_edit_media_to_album_and_album_count_mismatch(db, bot, config, sleeps):
    single = await published(db, bot, config, media=[photo("P")])
    await db.update_post(single, media=[photo("P"), photo("Q")])
    with pytest.raises(PostError, match="Нельзя поменять тип поста"):
        await edit_published_post(bot, db, config, single)

    album = await make_post(db, [CH_A], media=[photo("A"), photo("B")])
    await publish_post(bot, db, config, album)
    await db.update_post(album, media=[photo("A"), photo("B"), photo("C")])
    with pytest.raises(PostError, match="должно остаться 2 файлов"):
        await edit_published_post(bot, db, config, album)


async def test_edit_album_caption_only(db, bot, config, sleeps):
    post_id = await published(db, bot, config, text_md="Старая", media=[photo("A"), video("B")])
    await db.update_post(post_id, text_md="Новая **подпись**", caption_above=True)
    report = await edit_published_post(bot, db, config, post_id)
    assert report.ok_count == 2
    calls = bot.calls_of("edit_message_caption")
    targets = await targets_of(db, post_id)
    assert [(c["chat_id"], c["message_id"]) for c in calls] == [
        (CH_A, targets[CH_A]["message_ids"][0]), (CH_B, targets[CH_B]["message_ids"][0])]
    assert calls[0]["caption"] == "Новая <b>подпись</b>" and calls[0]["show_caption_above_media"] is True
    assert "reply_markup" not in calls[0]
    assert not bot.calls_of("edit_message_media")


async def test_edit_album_media(db, config, sleeps):
    bot = FakeBot()
    bot.edit_media_returns_message = True
    post_id = await published(db, bot, config, text_md="Альбом", media=[photo("A"), video("B"), photo("C")])
    name = upload(config, "mp4")
    await db.update_post(post_id, media=[photo("A"), video(path=name), photo("Z")])

    report = await edit_published_post(bot, db, config, post_id)

    assert report.ok_count == 2
    calls = bot.calls_of("edit_message_media")
    assert len(calls) == 6
    targets = await targets_of(db, post_id)
    assert [c["message_id"] for c in calls[:3]] == targets[CH_A]["message_ids"]
    assert [c["message_id"] for c in calls[3:]] == targets[CH_B]["message_ids"]
    assert calls[0]["media"].caption == "Альбом" and calls[1]["media"].caption is None
    assert isinstance(calls[1]["media"], InputMediaVideo) and isinstance(calls[1]["media"].media, FSInputFile)
    assert calls[4]["media"].media == "fake_video_1"  # second channel reuses the upload
    assert all("reply_markup" not in c or c["reply_markup"] is None for c in calls)
    assert not bot.calls_of("edit_message_caption")


async def test_edit_album_not_modified_messages_are_fine(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("A"), photo("B")])
    await db.update_post(post_id, media=[photo("A"), photo("C")])
    bot.fail_on["edit_message_media"] = lambda kw: (
        bad_request("Bad Request: message is not modified") if kw["media"].media == "A" else None)
    report = await edit_published_post(bot, db, config, post_id)
    assert report.ok_count == 2


async def test_edit_album_with_buttons_rejected(db, bot, config):
    post_id = await published(db, bot, config, media=[photo("A"), photo("B")])
    await db.update_post(post_id, buttons=BUTTONS)
    with pytest.raises(PostError, match="кнопки у альбомов"):
        await edit_published_post(bot, db, config, post_id)
    assert bot.calls == []


async def test_edit_album_message_count_mismatch_fails_target(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("A"), photo("B")])
    await db.update_target(post_id, CH_B, message_ids_json=json.dumps([1]))
    report = await edit_published_post(bot, db, config, post_id)
    assert report.fail_count == 1 and report.results[1].error == posting.ALBUM_MISMATCH_ERROR


async def test_edit_unpublished_post(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    with pytest.raises(PostError, match="Пост ещё не опубликован"):
        await edit_published_post(bot, db, config, post_id)


async def test_edit_render_error(db, bot, config):
    post_id = await published(db, bot, config)
    await db.update_post(post_id, text_md="")
    with pytest.raises(PostError, match="Пост пустой"):
        await edit_published_post(bot, db, config, post_id)


# ------------------------------------------------------------------ delete_published_post

async def test_delete_published_post(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    bot.fail_on["delete_message"] = fail_for(CH_B, bad_request("Bad Request: message to delete not found"))
    report = await delete_published_post(bot, db, post_id)
    assert report.ok_count == 2 and report.action == "delete"
    assert [c["chat_id"] for c in bot.calls_of("delete_message")] == [CH_A, CH_B]
    assert not bot.calls_of("delete_messages")
    post = await db.get_post(post_id)
    assert post["status"] == "deleted"
    assert all(t["status"] == "deleted" for t in post["targets"])
    with pytest.raises(PostError, match="не опубликован"):
        await delete_published_post(bot, db, post_id)


async def test_delete_partial_failure(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    bot.fail_on["delete_message"] = fail_for(CH_B, bad_request("Bad Request: message can't be deleted"))
    report = await delete_published_post(bot, db, post_id)
    assert (report.ok_count, report.fail_count) == (1, 1)
    post = await db.get_post(post_id)
    assert post["status"] == "sent"
    assert post["error"] == "Beta: Telegram не разрешает удалить это сообщение"
    targets = {t["channel_id"]: t["status"] for t in post["targets"]}
    assert targets == {CH_A: "deleted", CH_B: "sent"}

    bot.fail_on.clear()
    await delete_published_post(bot, db, post_id)
    assert (await db.get_post(post_id))["status"] == "deleted"


async def test_delete_album_with_delete_messages(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("A"), photo("B"), video("C")])
    targets = await targets_of(db, post_id)
    report = await delete_published_post(bot, db, post_id)
    assert report.ok_count == 2
    calls = bot.calls_of("delete_messages")
    assert [(c["chat_id"], c["message_ids"]) for c in calls] == [
        (CH_A, targets[CH_A]["message_ids"]), (CH_B, targets[CH_B]["message_ids"])]
    assert not bot.calls_of("delete_message")
    assert (await db.get_post(post_id))["status"] == "deleted"


async def test_delete_album_falls_back_to_single_deletes(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("A"), photo("B")])
    targets = await targets_of(db, post_id)
    bot.fail_on["delete_messages"] = bad_request("Bad Request: MESSAGE_DELETE_FORBIDDEN")
    gone = targets[CH_A]["message_ids"][1]
    bot.fail_on["delete_message"] = lambda kw: (
        bad_request("Bad Request: message to delete not found") if kw["message_id"] == gone else None)
    report = await delete_published_post(bot, db, post_id)
    assert report.ok_count == 2
    deleted = [(c["chat_id"], c["message_id"]) for c in bot.calls_of("delete_message")]
    assert deleted == [(CH_A, i) for i in targets[CH_A]["message_ids"]] + [
        (CH_B, i) for i in targets[CH_B]["message_ids"]]


async def test_delete_album_fallback_tries_every_message_then_fails(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("A"), photo("B")])
    bot.fail_on["delete_messages"] = bad_request("Bad Request: something")
    bot.fail_on["delete_message"] = lambda kw: (
        bad_request("Bad Request: message can't be deleted") if kw["chat_id"] == CH_B else None)
    report = await delete_published_post(bot, db, post_id)
    assert (report.ok_count, report.fail_count) == (1, 1)
    assert len([c for c in bot.calls_of("delete_message") if c["chat_id"] == CH_B]) == 2
    assert (await targets_of(db, post_id))[CH_B]["status"] == "sent"


@pytest.mark.parametrize("exc", [
    forbidden("Forbidden: bot is not a member of the channel chat"),
    forbidden("Forbidden: bot was kicked from the channel chat"),
    bad_request("Bad Request: chat not found"),
])
async def test_delete_in_unreachable_channel_can_finish(db, bot, config, sleeps, exc):
    """Regression (review #6): a channel the bot was removed from does not block deletion forever."""
    post_id = await published(db, bot, config)
    await db.set_channel_status(CH_B, CH_LEFT)
    bot.fail_on["delete_message"] = fail_for(CH_B, exc)

    report = await delete_published_post(bot, db, post_id)

    assert (report.ok_count, report.fail_count) == (2, 0)
    assert report.results[1].note == UNREACHABLE_NOTE
    assert f"⚠️ Beta — {UNREACHABLE_NOTE}" in report.summary_html()
    post = await db.get_post(post_id)
    assert post["status"] == "deleted"
    targets = await targets_of(db, post_id)
    assert targets[CH_B]["status"] == "deleted" and targets[CH_B]["error"] == UNREACHABLE_NOTE


async def test_delete_unreachable_album_bulk(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("A"), photo("B")])
    bot.fail_on["delete_messages"] = fail_for(CH_A, bad_request("Bad Request: chat not found"))
    report = await delete_published_post(bot, db, post_id)
    assert report.ok_count == 2 and report.results[0].note == UNREACHABLE_NOTE
    assert not bot.calls_of("delete_message")


async def test_delete_takes_queued_targets_out_of_plan(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    await queue(db, post_id, CH_A, now() - 5)
    await queue(db, post_id, CH_B, now() + 3600)
    await publish_post(bot, db, config, post_id, only_due=True)
    report = await delete_published_post(bot, db, post_id)
    assert report.ok_count == 1
    post = await db.get_post(post_id)
    assert post["status"] == "deleted" and post["scheduled_at"] is None
    targets = await targets_of(db, post_id)
    assert targets[CH_B]["status"] == "pending" and targets[CH_B]["scheduled_at"] is None
    assert await db.get_due_targets(now() + 7200) == []


# ------------------------------------------------------------------ preview & notify

async def test_send_preview_caches_uploaded_file_id(db, bot, config):
    name = upload(config, "jpg")
    post_id = await make_post(db, [], media=[photo(path=name)])
    messages = await send_preview(bot, db, config, post_id, 1)
    assert isinstance(messages, list) and messages[0].chat.id == 1
    (call,) = bot.calls_of("send_photo")
    assert call["chat_id"] == 1 and isinstance(call["photo"], FSInputFile)
    post = await db.get_post(post_id)
    assert post["media"] == [{"type": "photo", "file_id": "fake_file_1", "path": name, "file_bot": BOT_ID}]
    assert post["photo_file_id"] == "fake_file_1"


async def test_send_preview_album(db, bot, config):
    names = [upload(config, "jpg", 1), upload(config, "mp4", 2)]
    post_id = await make_post(db, [], media=[photo(path=names[0]), video(path=names[1])])
    messages = await send_preview(bot, db, config, post_id, 1)
    assert len(messages) == 2
    media = (await db.get_post(post_id))["media"]
    assert [m["file_id"] for m in media] == ["fake_file_1", "fake_video_2"]


async def test_send_preview_does_not_undo_concurrent_media_change(db, bot, config):
    name = upload(config, "jpg")
    post_id = await make_post(db, [], media=[photo(path=name)])
    original = bot.send_photo

    async def send_and_change(**kwargs):
        await db.update_post(post_id, media=[video("OTHER")])  # edited in the panel meanwhile
        return await original(**kwargs)

    bot.send_photo = send_and_change
    await send_preview(bot, db, config, post_id, 1)
    assert (await db.get_post(post_id))["media"] == [{"type": "video", "file_id": "OTHER", "path": None}]


async def test_send_preview_errors(db, bot, config):
    post_id = await make_post(db, [])
    bot.fail_on["send_message"] = forbidden("Forbidden: bot was blocked by the user")
    with pytest.raises(PostError, match="^Не удалось отправить превью: "):
        await send_preview(bot, db, config, post_id, 1)
    with pytest.raises(PostError, match="Пост не найден"):
        await send_preview(bot, db, config, 404, 1)


async def test_notify_admins_ignores_failures(db, bot):
    await db.add_admin(1, is_owner=True)
    await db.add_admin(2)
    bot.fail_on["send_message"] = fail_for(1, forbidden("Forbidden: bot was blocked by the user"))
    delivered = await notify_admins(bot, db, "<b>Привет</b>")
    assert delivered == 1
    calls = bot.calls_of("send_message")
    assert sorted(c["chat_id"] for c in calls) == [1, 2]
    assert all(c["parse_mode"] == "HTML" for c in calls)


# ------------------------------------------------------------------ review2: uploads (#0)

def network_error(message="Request timeout error") -> TelegramNetworkError:
    return TelegramNetworkError(method=SendMessage(chat_id=0, text="x"), message=message)


def big_upload(config, ext: str, size: int, n: int = 0) -> str:
    name = upload(config, ext, n)
    with open(config.uploads_dir / name, "wb") as handle:
        handle.truncate(size)  # sparse: the size counts, not the content
    return name


def test_upload_timeout_scales_with_the_size(config):
    small = FSInputFile(config.uploads_dir / big_upload(config, "jpg", 1000, 1))
    big = FSInputFile(config.uploads_dir / big_upload(config, "mp4", 50 * 1024 * 1024, 2))
    assert posting.upload_timeout(["FILE_ID"]) is None  # no upload: aiogram's default
    assert posting.upload_timeout([small]) == posting.UPLOAD_TIMEOUT_MIN
    assert posting.upload_timeout([big]) == 60 + 50 * 1024 * 1024 // posting.UPLOAD_RATE  # 460 s
    assert posting.upload_timeout([big] * 10) == posting.UPLOAD_TIMEOUT_MAX
    assert posting.upload_timeout([big, "FILE_ID"]) > 60


async def test_uploads_get_a_request_timeout_file_ids_do_not(db, bot, config, sleeps):
    """Regression (review2 #0): uploads used aiogram's 60 s total timeout — a 50 MB video or an
    album of them could never be published over a slow link."""
    await two_channels(db, bot)
    name = big_upload(config, "mp4", 40 * 1024 * 1024)
    post_id = await make_post(db, [CH_A, CH_B], media=[video(path=name)])
    await publish_post(bot, db, config, post_id)
    first, second = bot.calls_of("send_video")
    assert isinstance(first["video"], FSInputFile) and first["request_timeout"] == 60 + 40 * 8
    assert isinstance(second["video"], str) and "request_timeout" not in second  # learned file_id

    album_id = await make_post(db, [CH_A], media=[photo(path=big_upload(config, "jpg", 5 * 1024 * 1024, 1)),
                                                  video("VID")])
    await publish_post(bot, db, config, album_id)
    (call,) = bot.calls_of("send_media_group")
    assert call["request_timeout"] == posting.UPLOAD_TIMEOUT_MIN

    preview_id = await make_post(db, [], media=[gif(path=big_upload(config, "mp4", 45 * 1024 * 1024, 3))])
    await send_preview(bot, db, config, preview_id, 1)
    assert bot.calls_of("send_animation")[0]["request_timeout"] == 60 + 45 * 8
    bot.calls.clear()
    await send_preview(bot, db, config, preview_id, 1)  # the file_id is known now
    assert "request_timeout" not in bot.calls_of("send_animation")[0]


async def test_network_error_during_upload_is_interrupted_not_failed(db, bot, config, sleeps):
    """Regression (review2 #0): a timeout while uploading may still have posted the message — the
    channel is marked «проверьте, вышел ли пост», not an ordinary network failure to retry."""
    await two_channels(db, bot)
    name = upload(config, "mp4")
    post_id = await make_post(db, [CH_A, CH_B], media=[video(path=name)])
    bot.fail_on["send_video"] = fail_for(CH_A, network_error())
    report = await publish_post(bot, db, config, post_id)
    assert [r.ok for r in report.results] == [False, True]
    assert report.results[0].error == posting.INTERRUPTED_ERROR
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["status"] == "failed" and targets[CH_A]["error"] == posting.INTERRUPTED_ERROR
    assert targets[CH_A]["sending_at"] is None and targets[CH_B]["sending_at"] is None

    # sent by file_id (nothing uploaded): an ordinary network failure
    text_id = await make_post(db, [CH_A], media=[video("VID")])
    bot.fail_on["send_video"] = network_error("timeout")
    report = await publish_post(bot, db, config, text_id)
    assert report.results[0].error == "Сетевая ошибка: timeout"


async def test_media_edit_uploads_get_a_timeout_and_a_cut_upload_is_reported(db, bot, config, sleeps):
    post_id = await published(db, bot, config, media=[photo("OLD")])
    name = big_upload(config, "mp4", 40 * 1024 * 1024)
    await db.update_post(post_id, media=[video(path=name)])
    bot.fail_on["edit_message_media"] = fail_for(CH_B, network_error())
    report = await edit_published_post(bot, db, config, post_id)
    first, second = bot.calls_of("edit_message_media")
    assert first["request_timeout"] == 60 + 40 * 8 and isinstance(first["media"].media, FSInputFile)
    assert report.results[1].error == posting.EDIT_INTERRUPTED_ERROR
    target = (await targets_of(db, post_id))[CH_B]
    assert target["status"] == "sent" and target["media_sig"] != media_signature([video(path=name)])

    album_id = await published(db, bot, config, media=[photo("A"), photo("B")])
    await db.update_post(album_id, media=[photo("A"), photo(path=upload(config, "jpg", 5))])
    bot.fail_on.clear()
    await edit_published_post(bot, db, config, album_id)
    calls = [c for c in bot.calls_of("edit_message_media") if c["chat_id"] == CH_A]
    assert "request_timeout" not in calls[0] and calls[1]["request_timeout"] == posting.UPLOAD_TIMEOUT_MIN


# ------------------------------------------------------------------ review2: in-flight marker (#6)

async def test_target_is_marked_in_flight_during_its_send(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    seen = {}
    original = bot.send_message

    async def spy(**kwargs):
        seen[kwargs["chat_id"]] = {t["channel_id"]: t["sending_at"] for t in await db.get_targets(post_id)}
        return await original(**kwargs)

    bot.send_message = spy
    await publish_post(bot, db, config, post_id)
    assert seen[CH_A][CH_A] is not None and seen[CH_A][CH_B] is None
    assert seen[CH_B][CH_A] is None and seen[CH_B][CH_B] is not None
    assert all(t["sending_at"] is None for t in await db.get_targets(post_id))


async def test_cut_retry_of_a_failed_target_is_marked_interrupted(db, bot, config):
    await add_channel(db, bot, CH_A, "Alpha")
    post_id = await make_post(db, [CH_A])
    bot.fail_on["send_message"] = network_error("timeout")
    await publish_post(bot, db, config, post_id)
    assert (await db.get_target(post_id, CH_A))["error"] == "Сетевая ошибка: timeout"
    bot.fail_on.clear()
    started = asyncio.Event()

    async def hanging_send(**kwargs):
        started.set()
        await asyncio.sleep(3600)

    bot.send_message = hanging_send
    task = asyncio.create_task(publish_post(bot, db, config, post_id))  # «Повторить»
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    target = await db.get_target(post_id, CH_A)
    assert target["status"] == "failed" and target["error"] == posting.INTERRUPTED_ERROR
    assert target["sending_at"] is None


# ------------------------------------------------------------------ review2: delete (#1, #2)

async def test_partial_delete_still_takes_queued_channels_out_of_the_plan(db, bot, config, sleeps):
    """Regression (review2 #1): one channel refusing the deletion left the post's queued channels
    scheduled — the scheduler then published the post the owner had just deleted."""
    await two_channels(db, bot)
    await add_channel(db, bot, CH_C, "Gamma")
    post_id = await make_post(db, [CH_A, CH_B, CH_C])
    await queue(db, post_id, CH_A, now() - 5)
    await queue(db, post_id, CH_B, now() - 5)
    await queue(db, post_id, CH_C, now() + 3600)
    await publish_post(bot, db, config, post_id, only_due=True)
    bot.fail_on["delete_message"] = fail_for(CH_B, bad_request("Bad Request: message can't be deleted"))

    report = await delete_published_post(bot, db, post_id)

    assert (report.ok_count, report.fail_count) == (1, 1)
    post = await db.get_post(post_id)
    assert post["status"] == "partial" and post["scheduled_at"] is None
    assert post["error"] == "Beta: Telegram не разрешает удалить это сообщение"
    targets = await targets_of(db, post_id)
    assert (targets[CH_A]["status"], targets[CH_B]["status"]) == ("deleted", "sent")
    assert (targets[CH_C]["status"], targets[CH_C]["scheduled_at"], targets[CH_C]["queued"]) == (
        "pending", None, False)
    assert await db.get_due_targets(now() + 7200) == []

    bot.fail_on.clear()  # «Удалить из каналов» again once the right is back
    await delete_published_post(bot, db, post_id)
    assert (await db.get_post(post_id))["status"] == "deleted"


async def test_delete_claims_the_post_so_the_scheduler_cannot_publish_meanwhile(db, bot, config, sleeps):
    """Regression (review2 #2): a scheduled send landing during the deletion left a 'deleted' post
    whose message was live in the other channel."""
    from app.services.scheduler import Scheduler

    await db.add_admin(1, is_owner=True)
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    await queue(db, post_id, CH_A, now() - 5)
    await queue(db, post_id, CH_B, now() + 3600)
    await publish_post(bot, db, config, post_id, only_due=True)
    await db.set_target_schedule(post_id, CH_B, now() - 1, queued=True)  # B's slot has come
    await db.refresh_post_schedule(post_id)
    ticks = []
    original = bot.delete_message

    async def delete_with_tick(**kwargs):
        ticks.append(await Scheduler(bot, db, config).run_due_posts())  # the 15 s tick lands here
        return await original(**kwargs)

    bot.delete_message = delete_with_tick
    bot.calls.clear()
    await delete_published_post(bot, db, post_id)

    assert [c["chat_id"] for c in bot.calls_of("send_message") if c["chat_id"] < 0] == []
    post = await db.get_post(post_id)
    assert post["status"] == "deleted"
    targets = await targets_of(db, post_id)
    assert targets[CH_A]["status"] == "deleted"
    assert (targets[CH_B]["status"], targets[CH_B]["scheduled_at"]) == ("pending", None)


async def test_delete_holds_the_post_so_nothing_publishes_it_meanwhile(db, bot, config, sleeps):
    """Regression (review2 #2): the deletion claims the post for its whole run — «Опубликовать
    сейчас», a plan edit or a scheduler tick landing meanwhile cannot send it to a waiting channel
    (clearing the plan first is not enough: those come after it)."""
    from app.services import plan as plan_service
    from app.services.scheduler import Scheduler

    await db.add_admin(1, is_owner=True)
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B])
    await publish_post(bot, db, config, post_id, channel_ids=[CH_A])
    assert (await db.get_post(post_id))["status"] == "partial"
    bot.calls.clear()
    seen: dict = {}
    original = bot.delete_message

    async def delete_with_races(**kwargs):
        try:  # «Опубликовать сейчас» for the waiting channel, from the panel
            await publish_post(bot, db, config, post_id, channel_ids=[CH_B])
        except PostError as exc:
            seen["publish"] = str(exc)
        try:  # a plan edit
            await plan_service.schedule_post_at(db, post_id, [CH_B], now() + 3600)
        except plan_service.PlanError as exc:
            seen["plan"] = str(exc)
        await db.set_target_schedule(post_id, CH_B, now() - 1, queued=True)  # a due slot raced in
        seen["tick"] = await Scheduler(bot, db, config).run_due_posts()
        return await original(**kwargs)

    bot.delete_message = delete_with_races
    report = await delete_published_post(bot, db, post_id)

    assert report.ok_count == 1
    assert seen == {"publish": posting.BUSY_ERROR, "plan": plan_service.SENDING_ERROR, "tick": 0}
    assert [c["chat_id"] for c in bot.calls_of("send_message") if c["chat_id"] < 0] == []
    post = await db.get_post(post_id)
    assert post["status"] == "deleted"
    assert (await db.get_target(post_id, CH_B))["status"] == "pending"
    assert await db.get_due_targets(now() + 60) == []


async def test_delete_refused_while_the_post_is_being_published(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    assert await db.claim_post_for_sending(post_id, ("sent",))
    with pytest.raises(PostError, match="сейчас публикуется"):
        await delete_published_post(bot, db, post_id)
    assert bot.calls == []
    assert (await db.get_post(post_id))["status"] == "sending"


async def test_delete_retries_a_deleted_post_still_shown_somewhere(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    await db.update_target(post_id, CH_A, status="deleted")
    await db.update_post(post_id, status="deleted")  # state left by the old race
    report = await delete_published_post(bot, db, post_id)
    assert report.ok_count == 1 and [c["chat_id"] for c in bot.calls_of("delete_message")] == [CH_B]
    assert (await db.get_post(post_id))["status"] == "deleted"


async def test_delete_crash_mid_way_releases_the_post(db, bot, config, sleeps):
    post_id = await published(db, bot, config)
    bot.fail_on["delete_message"] = fail_for(CH_B, RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await delete_published_post(bot, db, post_id)
    post = await db.get_post(post_id)
    assert post["status"] == "sent"  # B still shows it; not left 'sending'
    assert posting.active_operations() == 0


# ------------------------------------------------------------------ SPEC_V23: premium emoji

EMOJI_MD = "Привет ![👍](tg://emoji?id=5368324170671202286) **мир**"


def test_render_text_strips_custom_emoji_without_premium():
    assert render_text(EMOJI_MD) == 'Привет <tg-emoji emoji-id="5368324170671202286">👍</tg-emoji> <b>мир</b>'
    assert render_text(EMOJI_MD, premium_emoji=False) == "Привет 👍 <b>мир</b>"


async def test_publish_edit_and_preview_follow_the_premium_setting(db, bot, config, sleeps):
    await two_channels(db, bot)
    post_id = await make_post(db, [CH_A, CH_B], text_md=EMOJI_MD)
    await publish_post(bot, db, config, post_id)  # default: off
    assert {c["text"] for c in bot.calls_of("send_message")} == {"Привет 👍 <b>мир</b>"}
    assert (await db.get_post(post_id))["text_html"] == "Привет 👍 <b>мир</b>"

    await send_preview(bot, db, config, post_id, 1)
    assert bot.calls_of("send_message")[-1]["text"] == "Привет 👍 <b>мир</b>"

    await db.set_premium_emoji(True)
    await edit_published_post(bot, db, config, post_id)
    assert {c["text"] for c in bot.calls_of("edit_message_text")} == {
        'Привет <tg-emoji emoji-id="5368324170671202286">👍</tg-emoji> <b>мир</b>'}
    await send_preview(bot, db, config, post_id, 1)
    assert "<tg-emoji" in bot.calls_of("send_message")[-1]["text"]

    caption_id = await make_post(db, [CH_A], text_md=EMOJI_MD, media=[photo("PH")])
    await db.set_premium_emoji(False)
    await publish_post(bot, db, config, caption_id)
    assert bot.calls_of("send_photo")[-1]["caption"] == "Привет 👍 <b>мир</b>"
    assert (await send_post(bot, {"text_md": EMOJI_MD, "buttons": []}, 5, config, premium_emoji=False))
    assert bot.calls_of("send_message")[-1]["text"] == "Привет 👍 <b>мир</b>"

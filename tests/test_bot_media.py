"""Composer media: photos, videos, GIFs, albums (collected from several updates), media menu."""
from __future__ import annotations

import asyncio
import time

import pytest
from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    SendAnimation,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVideo,
)
from aiogram.types import InputMediaAnimation, InputMediaPhoto, InputMediaVideo

from app.bot.callbacks import ComposeCb, ComposeMediaCb, PostCb
from app.bot.states import ComposeStates
from app.db import POST_SENT
from app.media import ALBUM_BUTTONS_ERROR
from tests.mocked_session import Harness, animation_obj, photo_sizes, video_obj

OWNER = 111
CH_A = -1001000000001


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    harness.albums.delay = 0.05
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    await harness.add_channel(CH_A, "Канал А", "kanal_a")
    yield harness
    await harness.close()


async def latest_post(h: Harness) -> dict:
    row = await h.db.fetchone("SELECT id FROM posts ORDER BY id DESC LIMIT 1")
    return await h.db.get_post(row["id"]) if row else None


def panel(h: Harness) -> SendMessage:
    panels = [r for r in h.session.sent_to(OWNER) if isinstance(r, SendMessage) and "Длина:" in r.text]
    assert panels, "no control panel was sent"
    return panels[-1]


def button_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def send_album(h: Harness, items: list[dict], group: str = "album1", *, caption_at: int | None = 0,
                     caption: str = "Подпись **альбома**") -> None:
    """Send an album the way Telegram delivers it: one message per item, same media_group_id."""
    for index, item in enumerate(items):
        fields = dict(item)
        if caption_at == index:
            fields["caption"] = caption
        await h.send(OWNER, None, media_group_id=group, **fields)
    await h.settle()


def photos(*file_ids: str) -> list[dict]:
    return [{"photo": photo_sizes(fid)} for fid in file_ids]


# ------------------------------------------------------------------ albums

async def test_album_of_three_photos_becomes_one_draft(h):
    await h.send(OWNER, "/new")
    h.session.clear()
    await send_album(h, photos("p1", "p2", "p3"))
    assert await h.db.count_posts() == 1
    post = await latest_post(h)
    assert [(m["type"], m["file_id"]) for m in post["media"]] == [("photo", "p1"), ("photo", "p2"), ("photo", "p3")]
    assert post["text_md"] == "Подпись **альбома**"
    # the preview is one album, the panel describes it
    (group,) = h.session.of(SendMediaGroup)
    assert group.chat_id == OWNER and [m.media for m in group.media] == ["p1", "p2", "p3"]
    assert group.media[0].caption == "Подпись <b>альбома</b>" and group.media[1].caption is None
    text = panel(h).text
    assert "🗂 Альбом: 3 фото" in text and "📏 Длина: 15 / 1024" in text
    assert "недоступны для альбома" in text
    state, data = await h.fsm(OWNER)
    assert state == ComposeStates.menu.state and len(data["preview_ids"]) == 3


async def test_album_items_do_not_block_each_other(h):
    """Each album item's handler only buffers it: updates are serialized per user, so a handler
    that waited for the rest of the album would block the very updates it waits for."""
    h.albums.delay = 0.5
    await h.send(OWNER, "/new")
    started = time.monotonic()
    await asyncio.gather(*(h.send(OWNER, None, photo=photo_sizes(f"p{i}"), media_group_id="g")
                           for i in range(3)))
    assert time.monotonic() - started < 0.3  # nobody waited for the album timer
    assert await h.db.count_posts() == 0  # the album is handled once it is complete
    await h.settle()
    post = await latest_post(h)
    assert len(post["media"]) == 3 and await h.db.count_posts() == 1


async def test_album_caption_from_any_item_and_mixed_types(h):
    await h.send(OWNER, "/new")
    await send_album(h, [{"photo": photo_sizes("p1")}, {"video": video_obj("v1")}], caption_at=1,
                     caption="Видео-подпись")
    post = await latest_post(h)
    assert [m["type"] for m in post["media"]] == ["photo", "video"]
    assert post["media"][1]["duration"] == 7 and post["media"][1]["mime"] == "video/mp4"
    assert post["text_md"] == "Видео-подпись"
    assert "🗂 Альбом: 1 фото, 1 видео" in panel(h).text


async def test_album_replaces_media_of_existing_draft(h):
    await h.send(OWNER, "/new")
    await h.send(OWNER, "Текст")
    post = await latest_post(h)
    await send_album(h, photos("a", "b"), caption_at=None)
    saved = await h.db.get_post(post["id"])
    assert [m["file_id"] for m in saved["media"]] == ["a", "b"]
    assert saved["text_md"] == "Текст"  # no caption → the text stays


async def test_late_album_item_is_appended(h):
    await h.send(OWNER, "/new")
    await send_album(h, photos("p1", "p2"))
    post = await latest_post(h)
    # a straggler of the same album arriving after the album was handled
    await send_album(h, photos("p3"), caption_at=None)
    assert [m["file_id"] for m in (await h.db.get_post(post["id"]))["media"]] == ["p1", "p2", "p3"]


async def test_album_with_buttons_is_blocked_until_buttons_are_removed(h):
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo_sizes("single"), caption="Пост")
    post = await latest_post(h)
    await h.db.update_post(post["id"], buttons=[[{"text": "Купить", "url": "https://shop.com", "style": None}]])

    await send_album(h, photos("a1", "a2"), caption_at=None)
    question = h.last_to(OWNER)
    assert "не поддерживает кнопки у альбомов" in question.text
    assert ComposeCb(action="album_ok", post_id=post["id"]).pack() in button_data(question.reply_markup)
    assert [m["file_id"] for m in (await h.db.get_post(post["id"]))["media"]] == ["single"]

    await h.press(OWNER, ComposeCb(action="album_no", post_id=post["id"]))
    assert [m["file_id"] for m in (await h.db.get_post(post["id"]))["media"]] == ["single"]
    await h.press(OWNER, ComposeCb(action="album_ok", post_id=post["id"]))
    assert "уже не ждёт" in h.callback_answers()[-1].text

    await send_album(h, photos("a1", "a2"), group="album2", caption_at=None)
    await h.press(OWNER, ComposeCb(action="album_ok", post_id=post["id"]))
    saved = await h.db.get_post(post["id"])
    assert saved["buttons"] == [] and [m["file_id"] for m in saved["media"]] == ["a1", "a2"]


async def test_buttons_of_an_album_are_explained(h):
    await h.send(OWNER, "/new")
    await send_album(h, photos("p1", "p2"))
    post = await latest_post(h)
    data = button_data(panel(h).reply_markup)
    assert ComposeCb(action="buttons", post_id=post["id"]).pack() in data
    await h.press(OWNER, ComposeCb(action="buttons", post_id=post["id"]))
    answer = h.callback_answers()[-1]
    assert answer.show_alert and "не поддерживает кнопки у альбомов" in answer.text
    assert (await h.fsm(OWNER))[0] == ComposeStates.menu.state


async def test_typed_buttons_for_an_album_are_refused(h):
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo_sizes("p1"))
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="buttons", post_id=post["id"]))  # prompt open for a single photo
    await h.db.update_post(post["id"], media=[{"type": "photo", "file_id": "p1"},
                                              {"type": "photo", "file_id": "p2"}])  # e.g. the web panel
    await h.send(OWNER, "Купить - https://shop.com")
    assert "не поддерживает кнопки у альбомов" in h.last_to(OWNER).text
    assert (await h.db.get_post(post["id"]))["buttons"] == []


async def test_album_publish_sends_media_group(h):
    await h.send(OWNER, "/new")
    await send_album(h, photos("p1", "p2"))
    post = await latest_post(h)
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    (group,) = [r for r in h.session.of(SendMediaGroup) if r.chat_id == CH_A]
    assert [m.media for m in group.media] == ["p1", "p2"]
    saved = await h.db.get_post(post["id"])
    assert saved["status"] == POST_SENT and len(saved["targets"][0]["message_ids"]) == 2
    assert h.last_to(OWNER).text.startswith("✅ Опубликовано: 1 из 1")


# ------------------------------------------------------------------ video / GIF

async def test_video_draft(h):
    await h.send(OWNER, "/new")
    h.session.clear()
    await h.send(OWNER, None, video=video_obj("vid"), caption="Смотрите")
    post = await latest_post(h)
    assert post["media"][0]["type"] == "video" and post["media"][0]["file_id"] == "vid"
    (preview,) = h.session.of(SendVideo)
    assert preview.video == "vid" and preview.caption == "Смотрите" and preview.supports_streaming
    assert "🖼 Медиа: 🎬 Видео" in panel(h).text


async def test_gif_draft_is_an_animation_not_a_document(h):
    from aiogram.types import Document

    await h.send(OWNER, "/new")
    h.session.clear()
    # Telegram fills both `animation` and `document` for a GIF
    await h.send(OWNER, None, animation=animation_obj("gif"),
                 document=Document(file_id="gif", file_unique_id="gif_u", mime_type="video/mp4"))
    post = await latest_post(h)
    assert post["media"][0]["type"] == "animation"
    (preview,) = h.session.of(SendAnimation)
    assert preview.animation == "gif"
    assert "🎞 GIF" in panel(h).text


async def test_video_as_file_gets_a_hint(h):
    from aiogram.types import Document

    await h.send(OWNER, "/new")
    await h.send(OWNER, None, document=Document(file_id="d", file_unique_id="d", mime_type="video/mp4"))
    assert "как видео" in h.last_to(OWNER).text
    assert await h.db.count_posts() == 0


# ------------------------------------------------------------------ media menu

async def test_media_menu_add_and_remove_items(h):
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo_sizes("p1"), caption="Пост")
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    menu = h.edits()[-1]
    assert "Сейчас: 🖼 Фото" in menu.text
    assert ComposeCb(action="media_add", post_id=post["id"]).pack() in button_data(menu.reply_markup)

    await h.press(OWNER, ComposeCb(action="media_add", post_id=post["id"]))
    assert (await h.fsm(OWNER))[1]["media_mode"] == "add"
    await h.send(OWNER, None, video=video_obj("v1"))
    saved = await h.db.get_post(post["id"])
    assert [m["file_id"] for m in saved["media"]] == ["p1", "v1"] and saved["text_md"] == "Пост"
    # «➕ Добавить медиа» stays on until «✔️ Готово» (finding #14)
    state, data = await h.fsm(OWNER)
    assert state == ComposeStates.media.state and data["media_mode"] == "add"
    await h.press(OWNER, ComposeCb(action="back", post_id=post["id"]))  # «✔️ Готово»
    state, data = await h.fsm(OWNER)
    assert state == ComposeStates.menu.state and data["media_mode"] is None

    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    menu = h.edits()[-1]
    assert "1. 🖼 фото" in menu.text and "2. 🎬 видео" in menu.text
    await h.press(OWNER, ComposeMediaCb(post_id=post["id"], index=0))
    assert [m["file_id"] for m in (await h.db.get_post(post["id"]))["media"]] == ["v1"]

    await h.press(OWNER, ComposeCb(action="rmmedia", post_id=post["id"]))
    assert (await h.db.get_post(post["id"]))["media"] == []


def last_message(h: Harness) -> SendMessage:
    sent = [r for r in h.session.sent_to(OWNER) if isinstance(r, SendMessage)]
    assert sent, "nothing was sent"
    return sent[-1]


async def test_add_mode_appends_separate_photos_until_done(h):
    """Finding #14: separate photos after «➕ Добавить медиа» (forwarded one by one, or sent without
    grouping) are all appended — not each replacing the post's media."""
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo_sizes("p0"), caption="Пост")
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="media_add", post_id=post["id"]))
    for file_id in ("p1", "p2", "p3"):
        await h.send(OWNER, None, photo=photo_sizes(file_id))
    saved = await h.db.get_post(post["id"])
    assert [m["file_id"] for m in saved["media"]] == ["p0", "p1", "p2", "p3"] and saved["text_md"] == "Пост"
    # under the fresh preview: the media menu in add mode, with «✔️ Готово»
    menu = last_message(h)
    assert "Добавление медиа" in menu.text and "ещё 6 из 10" in menu.text
    assert ComposeCb(action="back", post_id=post["id"]).pack() in button_data(menu.reply_markup)
    assert "✔️ Готово" in [b.text for row in menu.reply_markup.inline_keyboard for b in row]
    # removing a file keeps collecting
    await h.press(OWNER, ComposeMediaCb(post_id=post["id"], index=3))
    assert [m["file_id"] for m in (await h.db.get_post(post["id"]))["media"]] == ["p0", "p1", "p2"]
    assert (await h.fsm(OWNER))[0] == ComposeStates.media.state and "ещё 7 из 10" in last_message(h).text
    await h.send(OWNER, None, photo=photo_sizes("p3"))
    # a text is not taken as the caption while media are being added
    await h.send(OWNER, "новая подпись")
    assert "«✔️ Готово»" in h.last_to(OWNER).text
    assert (await h.db.get_post(post["id"]))["text_md"] == "Пост"

    await h.press(OWNER, ComposeCb(action="back", post_id=post["id"]))
    assert "Длина:" in h.edits()[-1].text  # the main panel again
    state, data = await h.fsm(OWNER)
    assert state == ComposeStates.menu.state and data["media_mode"] is None
    # with the panel shown, a new photo replaces the media (as the panel says)
    await h.send(OWNER, None, photo=photo_sizes("p9"))
    assert [m["file_id"] for m in (await h.db.get_post(post["id"]))["media"]] == ["p9"]


async def test_add_mode_ends_when_the_album_is_full(h):
    await h.send(OWNER, "/new")
    await send_album(h, photos(*(f"p{i}" for i in range(8))))
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="media_add", post_id=post["id"]))
    await h.send(OWNER, None, photo=photo_sizes("x1"))
    assert (await h.fsm(OWNER))[0] == ComposeStates.media.state
    await h.send(OWNER, None, photo=photo_sizes("x2"))
    assert len((await h.db.get_post(post["id"]))["media"]) == 10
    state, data = await h.fsm(OWNER)
    assert state == ComposeStates.menu.state and data["media_mode"] is None
    assert "В альбоме 10 файлов" in panel(h).text


async def test_gif_cannot_join_an_album(h):
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo_sizes("p1"))
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="media_add", post_id=post["id"]))
    await h.send(OWNER, None, animation=animation_obj("gif"))
    assert "GIF нельзя добавить в альбом" in h.last_to(OWNER).text
    assert [m["file_id"] for m in (await h.db.get_post(post["id"]))["media"]] == ["p1"]


async def test_album_limit_of_ten(h):
    await h.send(OWNER, "/new")
    await send_album(h, photos(*(f"p{i}" for i in range(9))))
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    await h.press(OWNER, ComposeCb(action="media_add", post_id=post["id"]))
    await send_album(h, photos("x1", "x2"), group="more", caption_at=None)
    assert "не больше 10" in h.last_to(OWNER).text
    assert len((await h.db.get_post(post["id"]))["media"]) == 9


# ------------------------------------------------------------------ editing published media

async def publish(h: Harness, **send_fields) -> dict:
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, **send_fields)
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    return await h.db.get_post(post["id"])


async def test_published_video_can_become_a_gif(h):
    post = await publish(h, video=video_obj("v1"), caption="Ролик")
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    await h.send(OWNER, None, animation=animation_obj("g1"))
    _, data = await h.fsm(OWNER)
    assert data["media_changed"] and data["overrides"]["media"][0]["type"] == "animation"
    assert (await h.db.get_post(post["id"]))["media"][0]["file_id"] == "v1"  # nothing saved yet
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    (edit,) = [m for m in h.session.of(EditMessageMedia) if m.chat_id == CH_A]
    assert isinstance(edit.media, InputMediaAnimation) and edit.media.media == "g1"
    assert edit.media.caption == "Ролик"
    assert (await h.db.get_post(post["id"]))["media"][0]["file_id"] == "g1"


async def test_published_album_keeps_its_size(h):
    await h.send(OWNER, "/new")
    await send_album(h, photos("p1", "p2"))
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))

    await h.send(OWNER, None, photo=photo_sizes("single"))
    assert "ровно из 2" in h.last_to(OWNER).text
    await send_album(h, photos("n1", "n2", "n3"), group="three", caption_at=None)
    assert "ровно из 2" in h.last_to(OWNER).text
    assert not (await h.fsm(OWNER))[1]["overrides"]

    await send_album(h, [{"photo": photo_sizes("n1")}, {"video": video_obj("n2")}], group="two",
                     caption_at=None)
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    edits = [m for m in h.session.of(EditMessageMedia) if m.chat_id == CH_A]
    assert [type(m.media) for m in edits] == [InputMediaPhoto, InputMediaVideo]
    assert [m.media.media for m in edits] == ["n1", "n2"]
    assert edits[0].media.caption == "Подпись <b>альбома</b>"


async def test_caption_edit_of_published_album(h):
    await h.send(OWNER, "/new")
    await send_album(h, photos("p1", "p2"))
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="confirm", post_id=post["id"]))
    await h.press(OWNER, PostCb(action="edit", post_id=post["id"]))
    edit_panel = panel(h)
    assert ComposeCb(action="buttons", post_id=post["id"]).pack() not in button_data(edit_panel.reply_markup)
    await h.send(OWNER, "Новая подпись")
    h.session.clear()
    await h.press(OWNER, ComposeCb(action="save_ok", post_id=post["id"]))
    (caption,) = [c for c in h.session.of(EditMessageCaption) if c.chat_id == CH_A]
    target = (await h.db.get_post(post["id"]))["targets"][0]
    assert caption.message_id == target["message_ids"][0] and caption.caption == "Новая подпись"
    assert not h.session.of(EditMessageMedia)


async def test_web_uploaded_media_preview_keeps_telegram_file_id(h, config):
    """A draft made in the web panel (uploaded file) gets Telegram's file_id from the bot preview."""
    name = "a" * 32 + ".jpg"
    (config.uploads_dir / name).write_bytes(b"\xff\xd8\xff\xe0fake")
    post_id = await h.db.create_post(text_md="Из панели", media=[{"type": "photo", "path": name}], status="draft")
    await h.db.set_post_targets(post_id, [CH_A])
    await h.press(OWNER, PostCb(action="edit", post_id=post_id))
    (upload,) = h.session.of(SendPhoto)
    assert not isinstance(upload.photo, str)
    media = (await h.db.get_post(post_id))["media"]
    assert media[0]["path"] == name and media[0]["file_id"].startswith("uploaded_")


async def test_album_buttons_error_text_matches_media_module():
    assert "Telegram не поддерживает кнопки у альбомов" in ALBUM_BUTTONS_ERROR


async def test_album_outside_the_composer_is_answered_once(h):
    for fid in ("a", "b", "c"):
        await h.send(OWNER, None, photo=photo_sizes(fid), media_group_id="stray")
    await h.settle()
    replies = [t for t in h.session.texts_to(OWNER) if "Не понял" in t]
    assert len(replies) == 1 and await h.db.count_posts() == 0


async def test_album_arriving_after_cancel_is_not_lost_silently(h):
    h.albums.delay = 0.2
    await h.send(OWNER, "/new")
    await h.send(OWNER, None, photo=photo_sizes("a"), media_group_id="late")
    await h.send(OWNER, "/cancel")
    await h.settle()
    assert "редактор поста уже закрыт" in h.last_to(OWNER).text
    assert await h.db.count_posts() == 0


async def test_album_through_real_polling(db, config):
    """Real `start_polling`: every update is its own task and updates of one user are serialized
    by SimpleEventIsolation — the album still becomes one draft with all its photos."""
    from aiogram.methods import GetUpdates
    from aiogram.types import Update

    from tests.mocked_session import MockedSession, make_bot, private_message

    class PollingSession(MockedSession):
        def __init__(self, batches: list[list[Update]]) -> None:
            super().__init__()
            self.batches = batches

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetUpdates):
                if self.batches:
                    return self.batches.pop(0)
                await asyncio.sleep(0.02)
                return []
            return await super().make_request(bot, method, timeout)

    from app.bot import create_dispatcher

    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    await db.upsert_channel(CH_A, "Канал А", None, status="active", rights={"can_post": True})
    album = [Update(update_id=10 + i, message=private_message(OWNER, None, message_id=10 + i,
                                                             photo=photo_sizes(f"p{i}"), media_group_id="poll",
                                                             caption="Альбом" if i == 0 else None))
             for i in range(4)]
    session = PollingSession([[Update(update_id=1, message=private_message(OWNER, "/new", message_id=1))], album])
    bot = make_bot(session)
    dp = create_dispatcher(db, config)
    dp["albums"].delay = 0.2
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False, polling_timeout=0,
                                                   close_bot_session=False))
    try:
        for _ in range(100):
            row = await db.fetchone("SELECT id FROM posts")
            if row and len((await db.get_post(row["id"]))["media"]) == 4:
                break
            await asyncio.sleep(0.05)
    finally:
        await dp.stop_polling()
        await polling
    assert await db.count_posts() == 1
    post = await db.get_post((await db.fetchone("SELECT id FROM posts"))["id"])
    assert [m["file_id"] for m in post["media"]] == ["p0", "p1", "p2", "p3"] and post["text_md"] == "Альбом"
    assert len(session.of(SendMediaGroup)) == 1

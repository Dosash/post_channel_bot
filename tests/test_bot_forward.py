"""Ready posts forwarded to the bot: text with formatting and emoji, media, URL buttons, options."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.methods import SendMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    LoginUrl,
    MessageEntity,
    MessageOriginChannel,
    MessageOriginHiddenUser,
)

from app.bot.callbacks import ComposeCb
from app.bot.states import ComposeStates
from app.db import CH_ACTIVE, POST_DRAFT
from tests.mocked_session import Harness, channel_chat, photo_sizes

OWNER = 111
CH_A = -1001000000001
FOREIGN = -1009000000001


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    harness.albums.delay = 0.05
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    yield harness
    await harness.close()


@pytest.fixture
async def one_channel(h):
    await h.db.upsert_channel(CH_A, "Канал А", "kanal_a", status=CH_ACTIVE,
                              rights={"can_post": True, "can_edit": True, "can_delete": True})
    h.session.add_chat(CH_A, "Канал А", "kanal_a")


def from_channel(chat_id: int = FOREIGN, title: str = "Чужой канал") -> MessageOriginChannel:
    return MessageOriginChannel(date=datetime.now(timezone.utc), chat=channel_chat(chat_id, title), message_id=7)


def hidden_user() -> MessageOriginHiddenUser:
    return MessageOriginHiddenUser(date=datetime.now(timezone.utc), sender_user_name="Кто-то")


def keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


async def fsm_state(h: Harness) -> str | None:
    from aiogram.fsm.storage.base import StorageKey
    return await h.dp.storage.get_state(StorageKey(bot_id=h.bot.id, chat_id=OWNER, user_id=OWNER))


async def latest_post(h: Harness) -> dict | None:
    row = await h.db.fetchone("SELECT id FROM posts ORDER BY id DESC LIMIT 1")
    return await h.db.get_post(row["id"]) if row else None


def last_panel(h: Harness) -> str:
    panels = [r.text for r in h.session.sent_to(OWNER) if isinstance(r, SendMessage) and "Длина:" in r.text]
    assert panels, "no control panel was sent"
    return panels[-1]


# ------------------------------------------------------------------ from the main menu

async def test_forwarded_post_opens_a_draft_with_buttons_and_formatting(h, one_channel):
    markup = keyboard(
        [InlineKeyboardButton(text="Купить", url="https://shop.com", style="success"),
         InlineKeyboardButton(text="Сайт", url="https://example.com")],
        [InlineKeyboardButton(text="Голосовать", callback_data="vote:1")],
    )
    await h.send(OWNER, None, forward_origin=hidden_user(), photo=photo_sizes("fwd_photo"),
                 caption="Привет 🔥 мир", caption_entities=[
                     MessageEntity(type="bold", offset=0, length=6),
                     MessageEntity(type="custom_emoji", offset=7, length=2, custom_emoji_id="5368324170671202286"),
                 ], show_caption_above_media=True, reply_markup=markup)
    post = await latest_post(h)
    assert post["status"] == POST_DRAFT and post["source"] == "bot"
    assert post["text_md"] == "**Привет** ![🔥](tg://emoji?id=5368324170671202286) мир"
    assert [m["file_id"] for m in post["media"]] == ["fwd_photo"]
    assert post["buttons"] == [[{"text": "Купить", "url": "https://shop.com", "style": "success"},
                                {"text": "Сайт", "url": "https://example.com", "style": None}]]
    assert post["caption_above"]
    assert [t["channel_id"] for t in post["targets"]] == [CH_A]
    assert await fsm_state(h) == ComposeStates.menu.state
    panel = last_panel(h)
    assert "Пропущено кнопок: 1" in panel


async def test_forwarded_text_keeps_disabled_link_preview(h, one_channel):
    await h.send(OWNER, "Ссылка https://example.com", forward_origin=hidden_user(),
                 link_preview_options=LinkPreviewOptions(is_disabled=True),
                 reply_markup=keyboard([InlineKeyboardButton(
                     text="Войти", login_url=LoginUrl(url="https://login.example.com"))]))
    post = await latest_post(h)
    assert post["disable_preview"]
    assert post["buttons"] == [[{"text": "Войти", "url": "https://login.example.com", "style": None}]]


async def test_post_from_an_unconnected_channel_hints_how_to_connect_it(h, one_channel):
    await h.send(OWNER, "Пост", forward_origin=from_channel())
    assert (await latest_post(h))["text_md"] == "Пост"
    assert "Канал «Чужой канал» не подключён к боту" in last_panel(h)


async def test_post_from_a_connected_channel_has_no_hint(h, one_channel):
    await h.send(OWNER, "Пост", forward_origin=from_channel(CH_A, "Канал А"))
    assert "не подключён" not in last_panel(h)


async def test_forwarded_album_becomes_one_draft(h, one_channel):
    origin = from_channel()
    for i in range(3):
        fields = {"caption": "Альбом"} if i == 0 else {}
        await h.send(OWNER, None, forward_origin=origin, media_group_id="fwd_album",
                     photo=photo_sizes(f"alb_{i}"), **fields)
    await h.settle()
    rows = await h.db.fetchall("SELECT id FROM posts")
    assert len(rows) == 1
    post = await latest_post(h)
    assert [m["file_id"] for m in post["media"]] == ["alb_0", "alb_1", "alb_2"]
    assert post["text_md"] == "Альбом" and post["buttons"] == []


async def test_forward_without_channels_explains_once(h):
    for i in range(3):
        await h.send(OWNER, None, forward_origin=hidden_user(), media_group_id="g", photo=photo_sizes(f"p{i}"))
    await h.settle()
    texts = [t for t in h.session.texts_to(OWNER) if "нет подключённых каналов" in t]
    assert len(texts) == 1
    assert await latest_post(h) is None
    assert await fsm_state(h) is None


async def test_forwarded_document_still_gets_the_fallback(h, one_channel):
    from aiogram.types import Document
    await h.send(OWNER, None, forward_origin=hidden_user(),
                 document=Document(file_id="doc", file_unique_id="doc_u", mime_type="application/pdf"))
    assert await latest_post(h) is None
    assert "Не понял" in h.last_to(OWNER).text


# ------------------------------------------------------------------ inside the composer

async def test_forward_into_the_panel_replaces_text_and_buttons(h, one_channel):
    await h.send(OWNER, "/new")
    await h.send(OWNER, "Черновик")
    post = await latest_post(h)
    await h.send(OWNER, "Готовый пост", forward_origin=hidden_user(),
                 reply_markup=keyboard([InlineKeyboardButton(text="Читать", url="https://t.me/x", style="primary")]))
    saved = await h.db.get_post(post["id"])
    assert saved["text_md"] == "Готовый пост"
    assert saved["buttons"] == [[{"text": "Читать", "url": "https://t.me/x", "style": "primary"}]]


async def test_text_step_takes_only_the_text(h, one_channel):
    await h.send(OWNER, "/new")
    await h.send(OWNER, "Черновик")
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="text", post_id=post["id"]))
    await h.send(OWNER, "Новый текст", forward_origin=hidden_user(),
                 reply_markup=keyboard([InlineKeyboardButton(text="Читать", url="https://t.me/x")]))
    saved = await h.db.get_post(post["id"])
    assert saved["text_md"] == "Новый текст" and saved["buttons"] == []


async def test_media_menu_takes_only_the_media(h, one_channel):
    await h.send(OWNER, "/new")
    await h.send(OWNER, "Черновик")
    post = await latest_post(h)
    await h.press(OWNER, ComposeCb(action="media", post_id=post["id"]))
    await h.send(OWNER, None, forward_origin=hidden_user(), photo=photo_sizes("m1"), show_caption_above_media=True,
                 reply_markup=keyboard([InlineKeyboardButton(text="Читать", url="https://t.me/x")]))
    saved = await h.db.get_post(post["id"])
    assert [m["file_id"] for m in saved["media"]] == ["m1"]
    assert saved["buttons"] == [] and not saved["caption_above"]


async def test_invalid_forwarded_buttons_are_dropped_with_a_note(h, one_channel):
    await h.send(OWNER, "Пост", forward_origin=hidden_user(),
                 reply_markup=keyboard([InlineKeyboardButton(text="Плохая", url="javascript:alert(1)")]))
    post = await latest_post(h)
    assert post["text_md"] == "Пост" and post["buttons"] == []
    assert "Кнопки не перенесены" in last_panel(h)

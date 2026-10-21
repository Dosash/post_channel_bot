"""A fake aiogram HTTP session: a real `aiogram.Bot` whose API calls never leave the process.

Every request is recorded in `session.requests` (the aiogram method objects) and answered with a
plausible result: sent messages get incrementing ids, edits / deletes return True, chat lookups
come from `session.chats` (configure with `add_chat`, like `tests.fakes.FakeBot`).
Failures: `session.fail_on[MethodClass] = exception` or a callable(method) -> exception | None.

Media: SendPhoto / SendVideo / SendAnimation return messages with `photo` / `video` / `animation`
(a string file is echoed as the file_id, an upload gets `uploaded_N`); SendMediaGroup returns one
message per item sharing a `media_group_id`; EditMessageMedia returns True, or the edited message
when `session.edit_media_returns_message` is set.
"""
from __future__ import annotations

import itertools
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    DeleteMessages,
    EditMessageMedia,
    EditMessageText,
    GetChat,
    GetChatAdministrators,
    GetChatMember,
    GetChatMemberCount,
    GetMe,
    SendAnimation,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import (
    AcceptedGiftTypes,
    Animation,
    CallbackQuery,
    Chat,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberOwner,
    InputMediaAnimation,
    InputMediaVideo,
    Message,
    PhotoSize,
    TelegramObject,
    Update,
    User,
    Video,
)

from app.bot import create_dispatcher
from app.markdown import validate_telegram_html, visible_length
from tests.fakes import admin_bot_member

BOT_TOKEN = "42:TEST"
BOT_ID = 42
BOT_USERNAME = "post_test_bot"


class MockedSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.fail_on: dict[type, Any] = {}
        self._message_ids = itertools.count(1000)
        self._file_ids = itertools.count(1)
        # chat_id or "@username" → {"id", "title", "username", "type"}
        self.chats: dict[int | str, dict] = {}
        self.bot_status: dict[int, str] = {}
        self.bot_rights: dict[int, dict] = {}
        self.member_counts: dict[int, int] = {}
        # chat_id → usernames of other bots that are its admins (getChatAdministrators)
        self.admin_bots: dict[int, list[str]] = {}
        self.edit_media_returns_message = False
        self._groups = itertools.count(1)

    # ------------------------------------------------------------ configuration
    def add_chat(self, chat_id: int, title: str, username: str | None = None, type: str = "channel",
                 bot_status: str = "administrator", members: int = 10, **rights: bool) -> None:
        info = {"id": chat_id, "title": title, "username": username, "type": type}
        self.chats[chat_id] = info
        if username:
            self.chats["@" + username.lower()] = info
        self.bot_status[chat_id] = bot_status
        self.bot_rights[chat_id] = {"can_post_messages": True, "can_edit_messages": True,
                                    "can_delete_messages": True, **rights}
        self.member_counts[chat_id] = members

    # ------------------------------------------------------------ inspection
    def of(self, method_type: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, method_type)]

    SEND_TYPES = (SendMessage, SendPhoto, SendVideo, SendAnimation, SendMediaGroup)

    def sent_to(self, chat_id: int) -> list[Any]:
        """SendMessage / SendPhoto / SendVideo / SendAnimation / SendMediaGroup requests to a chat."""
        return [r for r in self.requests if isinstance(r, self.SEND_TYPES) and r.chat_id == chat_id]

    def deleted_ids(self, chat_id: int | None = None) -> list[int]:
        """Message ids deleted with DeleteMessage / DeleteMessages (optionally in one chat)."""
        ids: list[int] = []
        for r in self.requests:
            if chat_id is not None and getattr(r, "chat_id", None) != chat_id:
                continue
            if isinstance(r, DeleteMessage):
                ids.append(r.message_id)
            elif isinstance(r, DeleteMessages):
                ids.extend(r.message_ids)
        return ids

    def texts_to(self, chat_id: int) -> list[str]:
        texts = []
        for r in self.sent_to(chat_id):
            if isinstance(r, SendMediaGroup):
                texts.append(r.media[0].caption or "")
            else:
                texts.append(getattr(r, "text", None) or getattr(r, "caption", None) or "")
        return texts

    def clear(self) -> None:
        self.requests.clear()

    # ------------------------------------------------------------ BaseSession
    async def close(self) -> None:
        return None

    async def stream_content(self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        failure = self.fail_on.get(type(method))
        if failure is not None:
            exc = failure(method) if callable(failure) and not isinstance(failure, BaseException) else failure
            if exc is not None:
                raise exc
        result = self._result(bot, method)
        if isinstance(result, TelegramObject):
            result.as_(bot)
        elif isinstance(result, list):
            for item in result:
                if isinstance(item, TelegramObject):
                    item.as_(bot)
        return result

    # ------------------------------------------------------------ results
    def _chat_info(self, method: TelegramMethod[Any], chat_id: int | str) -> dict:
        key = chat_id.lower() if isinstance(chat_id, str) else chat_id
        if key not in self.chats:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        return self.chats[key]

    def _chat(self, method: TelegramMethod[Any], chat_id: int | str) -> Chat:
        if isinstance(chat_id, int) and chat_id > 0:
            return Chat(id=chat_id, type="private", first_name="Admin")
        info = self._chat_info(method, chat_id)
        return Chat(id=info["id"], type=info["type"], title=info["title"], username=info["username"])

    def _message(self, method: TelegramMethod[Any], chat_id: int | str, **fields: Any) -> Message:
        return Message(message_id=next(self._message_ids), date=datetime.now(timezone.utc),
                       chat=self._chat(method, chat_id), **fields)

    def _media(self, kind: str, source: Any) -> dict[str, Any]:
        file_id = source if isinstance(source, str) else f"uploaded_{next(self._file_ids)}"
        if kind == "video":
            return {"video": Video(file_id=file_id, file_unique_id=f"{file_id}_u", width=1280, height=720,
                                   duration=5)}
        if kind == "animation":
            return {"animation": Animation(file_id=file_id, file_unique_id=f"{file_id}_u", width=320,
                                           height=240, duration=2)}
        return {"photo": [PhotoSize(file_id=file_id, file_unique_id=f"{file_id}_u", width=800, height=600)]}

    @staticmethod
    def _input_kind(media: Any) -> str:
        if isinstance(media, InputMediaVideo):
            return "video"
        if isinstance(media, InputMediaAnimation):
            return "animation"
        return "photo"

    def _result(self, bot: Bot, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Test bot", username=BOT_USERNAME)
        if isinstance(method, SendMessage):
            return self._message(method, method.chat_id, text=method.text)
        if isinstance(method, SendPhoto):
            return self._message(method, method.chat_id, caption=method.caption, **self._media("photo", method.photo))
        if isinstance(method, SendVideo):
            return self._message(method, method.chat_id, caption=method.caption, **self._media("video", method.video))
        if isinstance(method, SendAnimation):
            return self._message(method, method.chat_id, caption=method.caption,
                                 **self._media("animation", method.animation))
        if isinstance(method, SendMediaGroup):
            group = f"group_{next(self._groups)}"
            return [self._message(method, method.chat_id, media_group_id=group, caption=item.caption,
                                  **self._media(self._input_kind(item), item.media))
                    for item in method.media]
        if isinstance(method, EditMessageMedia) and self.edit_media_returns_message:
            return self._message(method, method.chat_id, caption=method.media.caption,
                                 **self._media(self._input_kind(method.media), method.media.media))
        if isinstance(method, GetChat):
            info = self._chat_info(method, method.chat_id)
            return ChatFullInfo(
                id=info["id"], type=info["type"], title=info["title"], username=info["username"],
                accent_color_id=0, max_reaction_count=11,
                accepted_gift_types=AcceptedGiftTypes(unlimited_gifts=False, limited_gifts=False,
                                                      unique_gifts=False, premium_subscription=False,
                                                      gifts_from_channels=False),
            )
        if isinstance(method, GetChatMember):
            return self._member(method, bot)
        if isinstance(method, GetChatAdministrators):
            info = self._chat_info(method, method.chat_id)
            return [admin_bot_member(name, 900 + i) for i, name in enumerate(self.admin_bots.get(info["id"], []))]
        if isinstance(method, GetChatMemberCount):
            info = self._chat_info(method, method.chat_id)
            return self.member_counts.get(info["id"], 0)
        # Edit*, DeleteMessage, AnswerCallbackQuery, SetMyCommands, …
        return True

    def _member(self, method: GetChatMember, bot: Bot) -> Any:
        info = self._chat_info(method, method.chat_id)
        user = User(id=method.user_id, is_bot=method.user_id == bot.id, first_name="x")
        if method.user_id != bot.id:
            return ChatMemberOwner(user=user, is_anonymous=False)
        status = self.bot_status.get(info["id"], "left")
        if status == "administrator":
            rights = self.bot_rights.get(info["id"], {})
            return ChatMemberAdministrator(
                user=user, can_be_edited=False, is_anonymous=False, can_manage_chat=True,
                can_delete_messages=rights.get("can_delete_messages", False), can_manage_video_chats=False,
                can_restrict_members=False, can_promote_members=False, can_change_info=False,
                can_invite_users=True, can_post_stories=False, can_edit_stories=False,
                can_delete_stories=False, can_send_welcome_messages=False,
                can_post_messages=rights.get("can_post_messages", False),
                can_edit_messages=rights.get("can_edit_messages", False),
            )
        if status == "creator":
            return ChatMemberOwner(user=user, is_anonymous=False)
        return ChatMemberLeft(user=user)


def make_bot(session: MockedSession | None = None) -> Bot:
    """A real aiogram Bot (default parse_mode HTML) wired to a MockedSession."""
    return Bot(BOT_TOKEN, session=session or MockedSession(),
               default=DefaultBotProperties(parse_mode="HTML"))


# ---------------------------------------------------------------- update builders & harness

def tg_user(user_id: int, first_name: str = "Admin", username: str | None = None) -> User:
    return User(id=user_id, is_bot=False, first_name=first_name, username=username)


def private_message(user_id: int, text: str | None = None, *, message_id: int = 1,
                    first_name: str = "Admin", **fields: Any) -> Message:
    """A message from a user in their private chat with the bot."""
    return Message(
        message_id=message_id, date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private", first_name=first_name),
        from_user=tg_user(user_id, first_name), text=text, **fields,
    )


def callback_query(user_id: int, data: str, *, message_id: int = 500, text: str = "panel") -> CallbackQuery:
    message = Message(message_id=message_id, date=datetime.now(timezone.utc),
                      chat=Chat(id=user_id, type="private", first_name="Admin"), text=text)
    return CallbackQuery(id=f"cb{message_id}", from_user=tg_user(user_id), chat_instance="ci",
                         data=data, message=message)


def photo_sizes(file_id: str = "photo_big") -> list[PhotoSize]:
    """What Telegram puts into Message.photo: several sizes, the largest last."""
    return [PhotoSize(file_id="photo_small", file_unique_id="s", width=90, height=90),
            PhotoSize(file_id=file_id, file_unique_id=f"{file_id}_u", width=1280, height=960)]


def video_obj(file_id: str = "video_1") -> Video:
    return Video(file_id=file_id, file_unique_id=f"{file_id}_u", width=1280, height=720, duration=7,
                 mime_type="video/mp4", file_size=1_000_000)


def animation_obj(file_id: str = "gif_1") -> Animation:
    return Animation(file_id=file_id, file_unique_id=f"{file_id}_u", width=320, height=240, duration=3,
                     mime_type="video/mp4")


def channel_chat(chat_id: int, title: str = "Канал", username: str | None = None) -> Chat:
    return Chat(id=chat_id, type="channel", title=title, username=username)


def bot_admin_member(bot_id: int = BOT_ID, **rights: bool) -> ChatMemberAdministrator:
    values = {"can_post_messages": True, "can_edit_messages": True, "can_delete_messages": True, **rights}
    return ChatMemberAdministrator(
        user=User(id=bot_id, is_bot=True, first_name="Test bot"), can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=values["can_delete_messages"],
        can_manage_video_chats=False, can_restrict_members=False, can_promote_members=False,
        can_change_info=False, can_invite_users=True, can_post_stories=False, can_edit_stories=False,
        can_delete_stories=False, can_send_welcome_messages=False,
        can_post_messages=values["can_post_messages"], can_edit_messages=values["can_edit_messages"],
    )


class Harness:
    """Dispatcher + real Bot over a MockedSession, fed with hand-made updates."""

    def __init__(self, db: Any, config: Any) -> None:
        self.db = db
        self.config = config
        self.session = MockedSession()
        self.bot = make_bot(self.session)
        self.dp = create_dispatcher(db, config)
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self.errors: list[logging.LogRecord] = []

        harness = self

        class _Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                harness.errors.append(record)

        self._log_handler = _Collector(level=logging.ERROR)
        logging.getLogger("app").addHandler(self._log_handler)

    @property
    def albums(self) -> Any:
        return self.dp["albums"]

    async def settle(self) -> None:
        """Wait until buffered albums were handled; fail on an error logged meanwhile."""
        await self.albums.wait_idle(5)
        self._raise_logged_error()

    async def close(self) -> None:
        await self.albums.close()
        logging.getLogger("app").removeHandler(self._log_handler)
        await self.dp.storage.close()
        await self.bot.session.close()

    def _raise_logged_error(self) -> None:
        if self.errors:
            record = self.errors[0]
            raise AssertionError(f"Unhandled error in a handler: {record.getMessage()}") from (
                record.exc_info[1] if record.exc_info else None)

    async def feed(self, **update_fields: Any) -> Any:
        update = Update(update_id=next(self._update_ids), **update_fields)
        first_new = len(self.session.requests)
        result = await self.dp.feed_update(self.bot, update)
        self._check_html(self.session.requests[first_new:])
        self._raise_logged_error()
        return result

    async def send(self, user_id: int, text: str | None = None, **fields: Any) -> Any:
        message = private_message(user_id, text, message_id=next(self._message_ids), **fields)
        return await self.feed(message=message)

    async def press(self, user_id: int, data: Any, *, message_id: int = 500) -> Any:
        """Press an inline button; every callback must be answered exactly once."""
        packed = data if isinstance(data, str) else data.pack()
        before = len(self.session.of(AnswerCallbackQuery))
        result = await self.feed(callback_query=callback_query(user_id, packed, message_id=message_id))
        answered = len(self.session.of(AnswerCallbackQuery)) - before
        assert answered == 1, f"callback {packed!r} was answered {answered} times"
        return result

    MAX_BUTTONS = 100  # Telegram refuses bigger inline keyboards («reply markup is too long»)

    @classmethod
    def _check_markup(cls, request: Any) -> None:
        """Inline keyboards must fit Telegram: ≤ 100 buttons, callback data ≤ 64 bytes."""
        markup = getattr(request, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None)
        if not rows:
            return
        buttons = [button for row in rows for button in row]
        if len(buttons) > cls.MAX_BUTTONS:
            raise AssertionError(f"{type(request).__name__} has {len(buttons)} inline buttons (> {cls.MAX_BUTTONS})")
        for button in buttons:
            if button.callback_data is not None and len(button.callback_data.encode()) > 64:
                raise AssertionError(f"callback data longer than 64 bytes: {button.callback_data!r}")

    @classmethod
    def _check_html(cls, requests: list[Any]) -> None:
        """Every text the bot sends is parsed as HTML by Telegram: it must be valid and fit."""
        for request in requests:
            cls._check_markup(request)
            for field in ("text", "caption"):
                value = getattr(request, field, None)
                if isinstance(value, str) and value:
                    if field == "text" and visible_length(value) > 4096:
                        raise AssertionError(f"{type(request).__name__}.text is longer than 4096 characters")
                    try:
                        validate_telegram_html(value)
                    except ValueError as exc:
                        raise AssertionError(f"{type(request).__name__}.{field} is not valid HTML: "
                                             f"{exc}\n{value}") from exc

    # ------------------------------------------------------------ setup helpers
    async def add_channel(self, chat_id: int, title: str, username: str | None = None, **fields: Any) -> None:
        """An active channel the bot may post to (DB row + fake Telegram chat)."""
        await self.db.upsert_channel(chat_id, title, username, status="active",
                                     rights={"can_post": True, "can_edit": True, "can_delete": True})
        if fields:
            await self.db.update_channel(chat_id, **fields)
        self.session.add_chat(chat_id, title, username)

    async def fsm(self, user_id: int) -> tuple[str | None, dict]:
        from aiogram.fsm.storage.base import StorageKey

        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return await self.dp.storage.get_state(key), await self.dp.storage.get_data(key)

    # ------------------------------------------------------------ inspection helpers
    def last_to(self, chat_id: int) -> Any:
        sent = self.session.sent_to(chat_id)
        assert sent, f"nothing was sent to {chat_id}"
        return sent[-1]

    def callback_answers(self) -> list[Any]:
        return self.session.of(AnswerCallbackQuery)

    def edits(self) -> list[Any]:
        return self.session.of(EditMessageText)

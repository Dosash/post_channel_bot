"""Test doubles for code that talks to Telegram.

FakeBot mimics the subset of aiogram.Bot used by the app. Every call is recorded in
`bot.calls` as (method_name, kwargs). Configure failures with `bot.fail_on[method] = exc`
(an exception instance, or a callable(kwargs) -> exception | None).

Media: sent photos / videos / GIFs / albums come back as Messages carrying `photo` / `video` /
`animation` objects; a file given as a string is echoed as its file_id, an upload (FSInputFile…)
gets a fresh 'fake_file_N' / 'fake_video_N' / 'fake_anim_N' id. Album messages share a
`media_group_id`. `edit_message_media` returns True unless `bot.edit_media_returns_message` is set
(real Telegram returns the edited Message for channel posts).
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import (
    Animation,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberOwner,
    InputMediaAnimation,
    InputMediaVideo,
    Message,
    PhotoSize,
    User,
    Video,
)

BOT_ID = 987654321


def bad_request(message: str = "Bad Request: something went wrong") -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=0, text="x"), message=message)


class FakeBot:
    def __init__(self, bot_id: int = BOT_ID, username: str = "test_bot"):
        self.id = bot_id
        self.username = username
        self.calls: list[tuple[str, dict]] = []
        self.fail_on: dict[str, Any] = {}
        self._msg_ids = itertools.count(100)
        self._file_ids = itertools.count(1)
        # chat_id (int) or "@username" -> dict(id, title, username, type)
        self.chats: dict[int | str, dict] = {}
        # chat_id -> bot member status: "administrator" | "left" | "member"
        self.bot_status: dict[int, str] = {}
        self.bot_rights: dict[int, dict] = {}
        self.member_counts: dict[int, int] = {}
        self.edit_media_returns_message = False
        self._groups = itertools.count(1)

    # ------------------------------------------------------------ helpers
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

    def calls_of(self, method: str) -> list[dict]:
        return [kw for name, kw in self.calls if name == method]

    def _record(self, method: str, kwargs: dict) -> None:
        self.calls.append((method, kwargs))
        failure = self.fail_on.get(method)
        if failure is not None:
            exc = failure(kwargs) if callable(failure) and not isinstance(failure, BaseException) else failure
            if exc is not None:
                raise exc

    def _chat_info(self, chat_id: int | str) -> dict:
        key = chat_id.lower() if isinstance(chat_id, str) else chat_id
        if key not in self.chats:
            raise bad_request("Bad Request: chat not found")
        return self.chats[key]

    def _file_id(self, source: Any, prefix: str) -> str:
        return source if isinstance(source, str) else f"{prefix}_{next(self._file_ids)}"

    def _media_fields(self, kind: str, source: Any) -> dict[str, Any]:
        """Message fields describing a sent media file of `kind` ('photo' | 'video' | 'animation')."""
        if kind == "video":
            fid = self._file_id(source, "fake_video")
            return {"video": Video(file_id=fid, file_unique_id=fid + "_u", width=1280, height=720, duration=5)}
        if kind == "animation":
            fid = self._file_id(source, "fake_anim")
            return {"animation": Animation(file_id=fid, file_unique_id=fid + "_u", width=320, height=240,
                                           duration=2)}
        fid = self._file_id(source, "fake_file")
        return {"photo": [PhotoSize(file_id=fid, file_unique_id=fid + "_u", width=800, height=600)]}

    @staticmethod
    def _input_kind(media: Any) -> str:
        if isinstance(media, InputMediaVideo):
            return "video"
        if isinstance(media, InputMediaAnimation):
            return "animation"
        return "photo"

    def _message(self, chat_id: int | str, **fields: Any) -> Message:
        if isinstance(chat_id, int) and chat_id > 0:
            chat = Chat(id=chat_id, type="private")
        else:
            info = self._chat_info(chat_id)
            chat = Chat(id=info["id"], type=info["type"], title=info["title"], username=info["username"])
        return Message(message_id=next(self._msg_ids), date=datetime.now(timezone.utc), chat=chat, **fields)

    # ------------------------------------------------------------ API
    async def get_me(self) -> User:
        self._record("get_me", {})
        return User(id=self.id, is_bot=True, first_name="Test bot", username=self.username)

    async def send_message(self, chat_id: int | str, text: str, **kwargs: Any) -> Message:
        self._record("send_message", {"chat_id": chat_id, "text": text, **kwargs})
        return self._message(chat_id, text=text)

    async def send_photo(self, chat_id: int | str, photo: Any, caption: str | None = None, **kwargs: Any) -> Message:
        self._record("send_photo", {"chat_id": chat_id, "photo": photo, "caption": caption, **kwargs})
        return self._message(chat_id, caption=caption, **self._media_fields("photo", photo))

    async def send_video(self, chat_id: int | str, video: Any, caption: str | None = None, **kwargs: Any) -> Message:
        self._record("send_video", {"chat_id": chat_id, "video": video, "caption": caption, **kwargs})
        return self._message(chat_id, caption=caption, **self._media_fields("video", video))

    async def send_animation(self, chat_id: int | str, animation: Any, caption: str | None = None,
                             **kwargs: Any) -> Message:
        self._record("send_animation", {"chat_id": chat_id, "animation": animation, "caption": caption, **kwargs})
        return self._message(chat_id, caption=caption, **self._media_fields("animation", animation))

    async def send_media_group(self, chat_id: int | str, media: list[Any], **kwargs: Any) -> list[Message]:
        self._record("send_media_group", {"chat_id": chat_id, "media": media, **kwargs})
        group_id = f"group_{next(self._groups)}"
        return [
            self._message(chat_id, media_group_id=group_id, caption=item.caption,
                          **self._media_fields(self._input_kind(item), item.media))
            for item in media
        ]

    async def edit_message_text(self, text: str, chat_id: int | str | None = None,
                                message_id: int | None = None, **kwargs: Any) -> Message | bool:
        self._record("edit_message_text", {"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})
        return True

    async def edit_message_caption(self, chat_id: int | str | None = None, message_id: int | None = None,
                                   caption: str | None = None, **kwargs: Any) -> Message | bool:
        self._record("edit_message_caption", {"chat_id": chat_id, "message_id": message_id, "caption": caption, **kwargs})
        return True

    async def edit_message_media(self, media: Any, chat_id: int | str | None = None,
                                 message_id: int | None = None, **kwargs: Any) -> Message | bool:
        self._record("edit_message_media", {"chat_id": chat_id, "message_id": message_id, "media": media, **kwargs})
        if not self.edit_media_returns_message:
            return True
        return self._message(chat_id, caption=media.caption, **self._media_fields(self._input_kind(media), media.media))

    async def edit_message_reply_markup(self, chat_id: int | str | None = None, message_id: int | None = None,
                                        **kwargs: Any) -> Message | bool:
        self._record("edit_message_reply_markup", {"chat_id": chat_id, "message_id": message_id, **kwargs})
        return True

    async def delete_message(self, chat_id: int | str, message_id: int, **kwargs: Any) -> bool:
        self._record("delete_message", {"chat_id": chat_id, "message_id": message_id, **kwargs})
        return True

    async def delete_messages(self, chat_id: int | str, message_ids: list[int], **kwargs: Any) -> bool:
        self._record("delete_messages", {"chat_id": chat_id, "message_ids": list(message_ids), **kwargs})
        return True

    async def get_chat(self, chat_id: int | str, **kwargs: Any) -> Chat:
        self._record("get_chat", {"chat_id": chat_id, **kwargs})
        info = self._chat_info(chat_id)
        photo = None
        if info.get("photo"):  # set info["photo"] = "<id>" to give the chat an avatar
            from aiogram.types import ChatPhoto
            pid = info["photo"]
            photo = ChatPhoto(small_file_id=f"{pid}_s", small_file_unique_id=f"{pid}_su",
                              big_file_id=f"{pid}_b", big_file_unique_id=f"{pid}_bu")
        return Chat(id=info["id"], type=info["type"], title=info["title"], username=info["username"], photo=photo)

    async def get_chat_member(self, chat_id: int | str, user_id: int, **kwargs: Any):
        self._record("get_chat_member", {"chat_id": chat_id, "user_id": user_id, **kwargs})
        info = self._chat_info(chat_id)
        user = User(id=user_id, is_bot=user_id == self.id, first_name="x")
        status = self.bot_status.get(info["id"], "left") if user_id == self.id else "creator"
        if status == "administrator":
            r = self.bot_rights.get(info["id"], {})
            return ChatMemberAdministrator(
                user=user, can_be_edited=False, is_anonymous=False, can_manage_chat=True,
                can_delete_messages=r.get("can_delete_messages", False), can_manage_video_chats=False,
                can_restrict_members=False, can_promote_members=False, can_change_info=False,
                can_invite_users=True, can_post_stories=False, can_edit_stories=False,
                can_delete_stories=False, can_send_welcome_messages=False,
                can_post_messages=r.get("can_post_messages", False),
                can_edit_messages=r.get("can_edit_messages", False),
            )
        if status == "creator":
            return ChatMemberOwner(user=user, is_anonymous=False)
        return ChatMemberLeft(user=user)

    async def get_chat_member_count(self, chat_id: int | str, **kwargs: Any) -> int:
        self._record("get_chat_member_count", {"chat_id": chat_id, **kwargs})
        info = self._chat_info(chat_id)
        return self.member_counts.get(info["id"], 0)

    async def get_file(self, file_id: str, **kwargs: Any):
        self._record("get_file", {"file_id": file_id, **kwargs})
        from aiogram.types import File
        return File(file_id=file_id, file_unique_id=file_id + "_u", file_path=f"photos/{file_id}.jpg")

    async def download(self, file: Any, destination: Any = None, **kwargs: Any):
        self._record("download", {"file": file, "destination": destination, **kwargs})
        data = b"\xff\xd8\xff\xe0fakejpeg"
        if destination is None:
            import io
            return io.BytesIO(data)
        if isinstance(destination, (str, Path)):
            Path(destination).write_bytes(data)
            return None
        destination.write(data)
        return destination

    async def set_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        self._record("set_my_commands", kwargs)
        return True

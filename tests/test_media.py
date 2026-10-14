"""Tests for app.media (pure media-list helpers)."""
from __future__ import annotations

import pytest

from app.media import (
    ALBUM_BUTTONS_ERROR,
    ALBUM_GIF_ERROR,
    ALBUM_TYPES,
    MAX_ALBUM,
    MEDIA_TYPES,
    PHOTO_MAX_BYTES,
    VIDEO_MAX_BYTES,
    MediaError,
    check_buttons,
    content_type,
    describe_media,
    edit_class,
    media_icon,
    media_kind,
    media_signature,
    normalize_media,
    post_media,
)

HEX = "0123456789abcdef" * 2


def name(ext: str, n: int = 0) -> str:
    return f"{HEX[:-1]}{n:x}.{ext}"


def photo(**kw):
    return {"type": "photo", "file_id": "PHOTO_1", **kw}


def video(**kw):
    return {"type": "video", "file_id": "VIDEO_1", **kw}


def gif(**kw):
    return {"type": "animation", "file_id": "GIF_1", **kw}


def test_constants():
    assert MEDIA_TYPES == ("photo", "video", "animation")
    assert ALBUM_TYPES == ("photo", "video")
    assert MAX_ALBUM == 10
    assert PHOTO_MAX_BYTES == 10 * 1024 * 1024 and VIDEO_MAX_BYTES == 50 * 1024 * 1024
    assert issubclass(MediaError, ValueError)


# ------------------------------------------------------------------ normalize_media

def test_normalize_empty():
    assert normalize_media(None) == []
    assert normalize_media([]) == []


def test_normalize_single_items_keep_extras_and_drop_unknown():
    items = normalize_media([{"type": "video", "path": name("mp4"), "file_id": None, "mime": "video/mp4",
                              "size": 1234, "width": 640, "height": 360, "duration": 7, "name": "a.mp4",
                              "evil": "<script>", "nested": {"x": 1}}])
    assert items == [{"type": "video", "file_id": None, "path": name("mp4"), "mime": "video/mp4", "name": "a.mp4",
                      "size": 1234, "width": 640, "height": 360, "duration": 7}]


def test_normalize_fills_missing_keys_and_gif_alias():
    assert normalize_media([{"type": "photo", "file_id": "AgACAgIAAx-_9"}]) == [
        {"type": "photo", "file_id": "AgACAgIAAx-_9", "path": None}]
    assert normalize_media([{"type": "gif", "path": name("gif")}]) == [
        {"type": "animation", "file_id": None, "path": name("gif")}]
    assert normalize_media([{"type": "photo", "file_id": "", "path": name("png")}])[0]["file_id"] is None


def test_normalize_album_of_photos_and_videos():
    items = [photo(), video(), {"type": "photo", "path": name("webp")}]
    assert [i["type"] for i in normalize_media(items)] == ["photo", "video", "photo"]
    assert len(normalize_media([photo()] * MAX_ALBUM)) == MAX_ALBUM


@pytest.mark.parametrize("items, message", [
    ("photo", "Некорректный список медиа"),
    ({"type": "photo"}, "Некорректный список медиа"),
    (["x"], "Некорректный элемент медиа"),
    ([{"type": "audio", "file_id": "A"}], "Неизвестный тип медиа: audio"),
    ([{"file_id": "A"}], "Неизвестный тип медиа: None"),
    ([{"type": "photo"}], "У медиа нет файла"),
    ([{"type": "photo", "file_id": None, "path": None}], "У медиа нет файла"),
    ([{"type": "photo", "path": "../etc/passwd"}], "Некорректное имя файла"),
    ([{"type": "photo", "path": "pic.jpg"}], "Некорректное имя файла"),
    ([{"type": "photo", "path": HEX + ".exe"}], "Некорректное имя файла"),
    ([{"type": "photo", "path": HEX.upper() + ".jpg"}], "Некорректное имя файла"),
    ([{"type": "photo", "path": 5}], "Некорректное имя файла"),
    ([{"type": "photo", "file_id": "bad id with spaces"}], "Некорректный идентификатор файла"),
    ([{"type": "photo", "file_id": 123}], "Некорректный идентификатор файла"),
    ([{"type": "photo", "file_id": "x" * 256}], "Некорректный идентификатор файла"),
    ([{"type": "photo", "path": HEX + ".mp4"}], "нельзя отправить как фото"),
    ([{"type": "video", "path": HEX + ".gif"}], "нельзя отправить как видео"),
    ([{"type": "animation", "path": HEX + ".png"}], "нельзя отправить как GIF"),
    ([photo()] * (MAX_ALBUM + 1), "не больше 10"),
    ([photo(), gif()], ALBUM_GIF_ERROR),
    ([gif(), gif()], ALBUM_GIF_ERROR),
])
def test_normalize_errors(items, message):
    with pytest.raises(MediaError) as err:
        normalize_media(items)
    assert message in str(err.value)


def test_normalize_animation_may_be_mp4_and_video_mov():
    assert normalize_media([{"type": "animation", "path": name("mp4")}])[0]["type"] == "animation"
    assert normalize_media([{"type": "video", "path": name("mov")}])[0]["path"] == name("mov")


def test_normalize_returns_copies():
    source = [photo()]
    result = normalize_media(source)
    result[0]["file_id"] = "changed"
    assert source[0]["file_id"] == "PHOTO_1"


def test_normalize_keeps_the_bot_of_a_file_id():
    """SPEC_V22: `file_bot` (id of the bot that owns the file_id) survives normalization."""
    (item,) = normalize_media([{"type": "photo", "file_id": "F", "file_bot": 123456, "path": name("jpg")}])
    assert item == {"type": "photo", "file_id": "F", "path": name("jpg"), "file_bot": 123456}
    for junk in ("123456", True, 0, -5, 1.5, None, [1]):
        (item,) = normalize_media([{"type": "photo", "file_id": "F", "file_bot": junk}])
        assert "file_bot" not in item, junk
    # without a file_id there is nothing to own
    (item,) = normalize_media([{"type": "photo", "path": name("jpg"), "file_bot": 123456}])
    assert "file_bot" not in item
    assert media_signature([{"type": "photo", "file_id": "F"}]) == media_signature(
        [{"type": "photo", "file_id": "F", "file_bot": 1}])


# ------------------------------------------------------------------ kinds & classes

@pytest.mark.parametrize("items, kind, cls, description, icon", [
    ([], "text", "text", "", "📝"),
    (None, "text", "text", "", "📝"),
    ([photo()], "photo", "media", "🖼 Фото", "🖼"),
    ([video()], "video", "media", "🎬 Видео", "🎬"),
    ([gif()], "animation", "media", "🎞 GIF", "🎞"),
    ([photo(), photo(), video(), photo()], "album", "album:4", "🗂 Альбом: 3 фото, 1 видео", "🗂"),
    ([video(), video()], "album", "album:2", "🗂 Альбом: 2 видео", "🗂"),
    ([photo(), photo()], "album", "album:2", "🗂 Альбом: 2 фото", "🗂"),
])
def test_kind_class_description(items, kind, cls, description, icon):
    assert media_kind(items) == kind
    assert edit_class(items) == cls
    assert describe_media(items) == description
    assert media_icon(items) == icon


def test_single_media_types_share_edit_class():
    assert edit_class([photo()]) == edit_class([video()]) == edit_class([gif()]) == "media"


# ------------------------------------------------------------------ buttons

def test_check_buttons():
    buttons = [[{"text": "A", "url": "https://a.ru", "style": None}]]
    check_buttons([], buttons)
    check_buttons([photo()], buttons)
    check_buttons([photo(), video()], [])
    check_buttons([photo(), video()], None)
    check_buttons([photo(), video()], [[]])  # empty rows are no buttons
    with pytest.raises(MediaError) as err:
        check_buttons([photo(), video()], buttons)
    assert str(err.value) == ALBUM_BUTTONS_ERROR
    assert "Telegram не поддерживает кнопки у альбомов" in ALBUM_BUTTONS_ERROR


# ------------------------------------------------------------------ signature & legacy

def test_signature_stable_across_file_id_write_back():
    uploaded = [{"type": "photo", "file_id": None, "path": name("jpg")}]
    after_send = [{"type": "photo", "file_id": "AgAC_new", "path": name("jpg")}]
    assert media_signature(uploaded) == media_signature(after_send)


def test_signature_changes_with_media():
    base = [{"type": "photo", "file_id": "A", "path": None}]
    assert media_signature(base) != media_signature([{"type": "photo", "file_id": "B", "path": None}])
    assert media_signature(base) != media_signature([{"type": "video", "file_id": "A", "path": None}])
    assert media_signature(base) != media_signature(base + base)
    two = [photo(file_id="A"), photo(file_id="B")]
    assert media_signature(two) != media_signature(list(reversed(two)))
    assert media_signature([]) == media_signature(None)
    assert len(media_signature(base)) == 40


def test_post_media_prefers_media_then_legacy():
    assert post_media({"media": [video()], "photo_file_id": "OLD"}) == [video()]
    assert post_media({"media": [], "photo_file_id": "OLD"}) == []
    assert post_media({"photo_file_id": "F", "photo_path": None}) == [
        {"type": "photo", "file_id": "F", "path": None}]
    assert post_media({"photo_path": name("png")}) == [{"type": "photo", "file_id": None, "path": name("png")}]
    assert post_media({"text_md": "x"}) == []


def test_content_type():
    assert content_type(name("jpg")) == "image/jpeg"
    assert content_type(name("mov")) == "video/quicktime"
    assert content_type(name("gif")) == "image/gif"
    assert content_type("noext") == "application/octet-stream"

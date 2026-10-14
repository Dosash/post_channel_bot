"""Tests for app.services.folders (channel folders)."""
from __future__ import annotations

import pytest

from app.db import CH_ACTIVE, CH_LEFT, CH_PENDING
from app.services.folders import (
    MAX_NAME_LENGTH,
    FolderError,
    apply_slots_to_folder,
    clean_name,
    create_folder,
    delete_folder,
    grouped_channels,
    move_folder,
    rename_folder,
    set_channel_folder,
)

CH_A = -1001000000001
CH_B = -1001000000002
CH_C = -1001000000003
CH_D = -1001000000004


async def add_channel(db, chat_id, title, status=CH_ACTIVE):
    return await db.upsert_channel(chat_id, title, None, status=status)


async def names(db) -> list[str]:
    return [f["name"] for f in await db.list_folders()]


# ------------------------------------------------------------------ create / rename

async def test_create_folder_returns_row_and_appends(db):
    first = await create_folder(db, "  Новости  ")
    second = await create_folder(db, "Работа\n и   хобби")
    assert first["name"] == "Новости" and first["channels_count"] == 0
    assert second["name"] == "Работа и хобби"
    assert second["position"] > first["position"]
    assert await names(db) == ["Новости", "Работа и хобби"]


@pytest.mark.parametrize("name, message", [
    ("", "Название папки не может быть пустым"),
    ("   \n\t", "Название папки не может быть пустым"),
    (None, "Название папки не может быть пустым"),
    ("я" * (MAX_NAME_LENGTH + 1), "Слишком длинное название (максимум 40 символов)"),
    ("без ПАПКИ", "Название «Без папки» зарезервировано — выберите другое"),
])
async def test_create_folder_validation(db, name, message):
    with pytest.raises(FolderError) as exc:
        await create_folder(db, name)
    assert str(exc.value) == message
    assert await names(db) == []


async def test_create_folder_max_length_ok(db):
    folder = await create_folder(db, "ж" * MAX_NAME_LENGTH)
    assert len(folder["name"]) == MAX_NAME_LENGTH


@pytest.mark.parametrize("duplicate", ["Новости", "НОВОСТИ", " новости ", "нОвОсТи"])
async def test_create_folder_duplicate_case_insensitive(db, duplicate):
    await create_folder(db, "Новости")
    with pytest.raises(FolderError, match="^Папка «Новости» уже есть$"):
        await create_folder(db, duplicate)
    assert await names(db) == ["Новости"]


def test_clean_name():
    assert clean_name("  a   b ") == "a b"
    with pytest.raises(FolderError):
        clean_name("")


async def test_rename_folder(db):
    folder = await create_folder(db, "Новости")
    renamed = await rename_folder(db, folder["id"], "  Свежие новости ")
    assert renamed["name"] == "Свежие новости" and renamed["id"] == folder["id"]
    # changing only the letter case of its own name is fine
    assert (await rename_folder(db, folder["id"], "СВЕЖИЕ НОВОСТИ"))["name"] == "СВЕЖИЕ НОВОСТИ"


async def test_rename_folder_errors(db):
    a = await create_folder(db, "Альфа")
    await create_folder(db, "Бета")
    with pytest.raises(FolderError, match="^Папка «Бета» уже есть$"):
        await rename_folder(db, a["id"], "бета")
    with pytest.raises(FolderError, match="^Название папки не может быть пустым$"):
        await rename_folder(db, a["id"], " ")
    with pytest.raises(FolderError, match="^Папка не найдена$"):
        await rename_folder(db, 999, "Гамма")
    assert await names(db) == ["Альфа", "Бета"]


# ------------------------------------------------------------------ delete / move

async def test_delete_folder_keeps_channels(db):
    folder = await create_folder(db, "Новости")
    await add_channel(db, CH_A, "A")
    await set_channel_folder(db, CH_A, folder["id"])
    await delete_folder(db, folder["id"])
    assert await names(db) == []
    assert (await db.get_channel(CH_A))["folder_id"] is None
    with pytest.raises(FolderError, match="^Папка не найдена$"):
        await delete_folder(db, folder["id"])


async def test_move_folder_swaps_with_neighbour(db):
    ids = [(await create_folder(db, n))["id"] for n in ("A", "B", "C")]
    await move_folder(db, ids[2], -1)
    assert await names(db) == ["A", "C", "B"]
    await move_folder(db, ids[0], 1)
    assert await names(db) == ["C", "A", "B"]
    await move_folder(db, ids[2], -5)   # any negative = one step up
    assert await names(db) == ["C", "A", "B"]  # already first → no change
    await move_folder(db, ids[1], 1)    # already last → no change
    assert await names(db) == ["C", "A", "B"]
    await move_folder(db, ids[0], 0)
    assert await names(db) == ["C", "A", "B"]
    assert [f["position"] for f in await db.list_folders()] == [0, 1, 2]


async def test_move_folder_with_equal_positions(db):
    ids = [(await create_folder(db, n))["id"] for n in ("A", "B", "C")]
    for folder_id in ids:
        await db.update_folder(folder_id, position=0)
    await move_folder(db, ids[1], -1)
    assert await names(db) == ["B", "A", "C"]


async def test_move_folder_unknown(db):
    with pytest.raises(FolderError, match="^Папка не найдена$"):
        await move_folder(db, 42, 1)


# ------------------------------------------------------------------ set_channel_folder

async def test_set_channel_folder_and_clear(db):
    folder = await create_folder(db, "Новости")
    await add_channel(db, CH_A, "A")
    channel = await set_channel_folder(db, CH_A, folder["id"])
    assert channel["folder_id"] == folder["id"]
    assert (await db.list_folders())[0]["channels_count"] == 1
    assert (await set_channel_folder(db, CH_A, None))["folder_id"] is None


async def test_set_channel_folder_errors(db):
    folder = await create_folder(db, "Новости")
    await add_channel(db, CH_A, "A")
    with pytest.raises(FolderError, match="^Канал не найден$"):
        await set_channel_folder(db, CH_B, folder["id"])
    with pytest.raises(FolderError, match="^Папка не найдена$"):
        await set_channel_folder(db, CH_A, folder["id"] + 100)
    assert (await db.get_channel(CH_A))["folder_id"] is None


# ------------------------------------------------------------------ grouped_channels

async def test_grouped_channels_order(db):
    news = await create_folder(db, "Новости")
    work = await create_folder(db, "Работа")
    empty = await create_folder(db, "Пустая")
    await move_folder(db, work["id"], -1)  # Работа, Новости, Пустая
    await add_channel(db, CH_A, "бета")
    await add_channel(db, CH_B, "Альфа")
    await add_channel(db, CH_C, "вега")
    await add_channel(db, CH_D, "Zeta")
    for ch in (CH_A, CH_B, CH_C):
        await set_channel_folder(db, ch, news["id"])
    groups = await grouped_channels(db)
    assert [g["folder"]["name"] if g["folder"] else None for g in groups] == [
        "Работа", "Новости", "Пустая", None,
    ]
    assert [c["title"] for c in groups[1]["channels"]] == ["Альфа", "бета", "вега"]
    assert groups[0]["channels"] == [] and groups[2]["channels"] == []
    assert groups[1]["folder"] == {
        "id": news["id"], "name": "Новости", "position": groups[1]["folder"]["position"], "channels_count": 3,
    }
    assert [c["id"] for c in groups[3]["channels"]] == [CH_D]
    assert empty["id"] == groups[2]["folder"]["id"]


async def test_grouped_channels_no_folder_group_only_when_needed(db):
    folder = await create_folder(db, "Все")
    await add_channel(db, CH_A, "A")
    await set_channel_folder(db, CH_A, folder["id"])
    groups = await grouped_channels(db)
    assert len(groups) == 1 and groups[0]["folder"]["id"] == folder["id"]
    assert [c["id"] for c in groups[0]["channels"]] == [CH_A]


async def test_grouped_channels_without_folders(db):
    assert await grouped_channels(db) == []
    await add_channel(db, CH_A, "A")
    assert [(g["folder"], [c["id"] for c in g["channels"]]) for g in await grouped_channels(db)] == [
        (None, [CH_A]),
    ]


async def test_grouped_channels_statuses(db):
    folder = await create_folder(db, "Папка")
    await add_channel(db, CH_A, "Активный")
    await add_channel(db, CH_B, "Ожидает", status=CH_PENDING)
    await add_channel(db, CH_C, "Ушёл", status=CH_LEFT)
    for ch in (CH_A, CH_B, CH_C):
        await set_channel_folder(db, ch, folder["id"])
    default = await grouped_channels(db)
    assert [c["id"] for c in default[0]["channels"]] == [CH_A]
    assert default[0]["folder"]["channels_count"] == 3
    every = await grouped_channels(db, statuses=None)
    assert {c["id"] for c in every[0]["channels"]} == {CH_A, CH_B, CH_C}
    assert await grouped_channels(db, statuses="all") == every
    pending = await grouped_channels(db, statuses=CH_PENDING)
    assert [c["id"] for c in pending[0]["channels"]] == [CH_B]
    two = await grouped_channels(db, statuses=[CH_ACTIVE, CH_LEFT])
    assert [c["title"] for c in two[0]["channels"]] == ["Активный", "Ушёл"]


async def test_grouped_channels_dangling_folder_id(db):
    await add_channel(db, CH_A, "A")
    await db.update_channel(CH_A, folder_id=777)  # folder removed outside the service
    groups = await grouped_channels(db)
    assert groups == [{"folder": None, "channels": [await db.get_channel(CH_A)]}]


# ------------------------------------------------------------------ apply_slots_to_folder

async def test_apply_slots_to_folder_dict_and_text(db):
    folder = await create_folder(db, "Новости")
    other = await create_folder(db, "Другое")
    await add_channel(db, CH_A, "A")
    await add_channel(db, CH_B, "B", status=CH_LEFT)
    await add_channel(db, CH_C, "C")
    await set_channel_folder(db, CH_A, folder["id"])
    await set_channel_folder(db, CH_B, folder["id"])
    await set_channel_folder(db, CH_C, other["id"])
    assert await apply_slots_to_folder(db, folder["id"], {"0": ["9:00"], "4": ["18:00", "09:00"]}) == 2
    assert await db.get_channel_slots(CH_A) == {0: ["09:00"], 4: ["09:00", "18:00"]}
    assert await db.get_channel_slots(CH_B) == {0: ["09:00"], 4: ["09:00", "18:00"]}
    assert await db.get_channel_slots(CH_C) == {}
    assert await apply_slots_to_folder(db, other["id"], "будни 10:00") == 1
    assert await db.get_channel_slots(CH_C) == {d: ["10:00"] for d in range(5)}


async def test_apply_slots_to_folder_clear_and_errors(db):
    folder = await create_folder(db, "Новости")
    await add_channel(db, CH_A, "A")
    await set_channel_folder(db, CH_A, folder["id"])
    await apply_slots_to_folder(db, folder["id"], "пн 09:00")
    assert await apply_slots_to_folder(db, folder["id"], {}) == 1
    assert await db.get_channel_slots(CH_A) == {}
    with pytest.raises(FolderError, match="^Строка 1: не понял время «25:00»$"):
        await apply_slots_to_folder(db, folder["id"], "пн 25:00")
    with pytest.raises(FolderError, match="Неверный день недели"):
        await apply_slots_to_folder(db, folder["id"], {"9": ["09:00"]})
    with pytest.raises(FolderError, match="^Папка не найдена$"):
        await apply_slots_to_folder(db, 404, {"0": ["09:00"]})
    assert await apply_slots_to_folder(db, (await create_folder(db, "Пустая"))["id"], "пн 9:00") == 0

"""Folders, channel publishing schedules and the «📅 Контент-план» section of the bot."""
from __future__ import annotations

import time

import pytest
from aiogram.methods import SendMessage

from app.bot.callbacks import (
    ChannelCb,
    ChannelFolderCb,
    ComposeCb,
    ComposeFolderCb,
    FolderCb,
    PlanCb,
)
from app.bot.keyboards import BTN_CHANNELS, BTN_PLAN
from app.bot.states import ChannelStates, PlanStates
from app.db import POST_DRAFT, POST_SCHEDULED
from app.services import plan as plan_service
from tests.mocked_session import Harness

OWNER = 111
CH_A = -1001000000001
CH_B = -1001000000002
CH_C = -1001000000003
EVERY_HOUR = "ежедневно " + ", ".join(f"{h:02d}:00" for h in range(24))


@pytest.fixture
async def h(db, config):
    await db.migrate()
    harness = Harness(db, config)
    await db.add_admin(OWNER, "boss", "Boss", is_owner=True)
    await harness.add_channel(CH_A, "Канал А", "kanal_a")
    await harness.add_channel(CH_B, "Канал Б", "kanal_b")
    yield harness
    await harness.close()


def data_of(message) -> list[str]:
    return [b.callback_data for row in message.reply_markup.inline_keyboard for b in row if b.callback_data]


def last_screen(h: Harness):
    """The last message the bot showed (edited in place or sent)."""
    for request in reversed(h.session.requests):
        if type(request).__name__ in ("EditMessageText", "SendMessage") and getattr(request, "chat_id", None) == OWNER:
            return request
    raise AssertionError("nothing shown")


async def folder_id(h: Harness, name: str) -> int:
    row = await h.db.fetchone("SELECT id FROM folders WHERE name = ?", (name,))
    assert row, f"folder {name} not found"
    return row["id"]


async def queued_post(h: Harness, config, text: str, channels: list[int]) -> int:
    post_id = await h.db.create_post(text_md=text, text_html=text, status=POST_DRAFT)
    await h.db.set_post_targets(post_id, channels)
    await plan_service.queue_post(h.db, post_id, channels, config.tz)
    return post_id


async def ts_of(h: Harness, post_id: int, channel_id: int) -> int | None:
    return (await h.db.get_target(post_id, channel_id))["scheduled_at"]


# ------------------------------------------------------------------ folders

async def test_folder_create_move_rename_reorder_and_delete(h):
    await h.send(OWNER, BTN_CHANNELS)
    assert FolderCb(action="new").pack() in data_of(h.last_to(OWNER))
    await h.press(OWNER, FolderCb(action="new"))
    assert (await h.fsm(OWNER))[0] == ChannelStates.folder_name.state
    await h.send(OWNER, "Без папки")
    assert "зарезервировано" in h.last_to(OWNER).text
    await h.send(OWNER, "Новости")
    news = await folder_id(h, "Новости")
    assert "📁 <b>Новости</b>" in last_screen(h).text and (await h.fsm(OWNER))[0] is None

    # move channel А into the folder from its card
    await h.press(OWNER, ChannelCb(action="folder", chat_id=CH_A))
    assert ChannelFolderCb(chat_id=CH_A, folder_id=news).pack() in data_of(last_screen(h))
    await h.press(OWNER, ChannelFolderCb(chat_id=CH_A, folder_id=news))
    assert (await h.db.get_channel(CH_A))["folder_id"] == news
    assert "📁 Папка: Новости" in last_screen(h).text

    # the list shows the folder instead of its channel; the loose channel stays
    await h.send(OWNER, BTN_CHANNELS)
    listing = h.last_to(OWNER)
    assert FolderCb(action="open", folder_id=news).pack() in data_of(listing)
    assert ChannelCb(action="card", chat_id=CH_A).pack() not in data_of(listing)
    assert ChannelCb(action="card", chat_id=CH_B).pack() in data_of(listing)
    await h.press(OWNER, FolderCb(action="open", folder_id=news))
    assert ChannelCb(action="card", chat_id=CH_A).pack() in data_of(last_screen(h))

    # a new folder straight from the card of channel Б
    await h.press(OWNER, ChannelFolderCb(chat_id=CH_B, folder_id=-1))
    await h.send(OWNER, "Блоги")
    blogs = await folder_id(h, "Блоги")
    assert (await h.db.get_channel(CH_B))["folder_id"] == blogs
    assert "канал перемещён в «Блоги»" in last_screen(h).text

    await h.press(OWNER, FolderCb(action="rename", folder_id=news))
    await h.send(OWNER, "Главное")
    assert (await h.db.get_folder(news))["name"] == "Главное"

    await h.press(OWNER, FolderCb(action="down", folder_id=news))
    assert [f["id"] for f in await h.db.list_folders()] == [blogs, news]

    await h.press(OWNER, FolderCb(action="del", folder_id=news))
    assert "Каналы останутся" in last_screen(h).text and await h.db.get_folder(news) is not None
    await h.press(OWNER, FolderCb(action="del_ok", folder_id=news))
    assert await h.db.get_folder(news) is None
    assert (await h.db.get_channel(CH_A))["folder_id"] is None


async def test_navigation_cancels_the_folder_name_prompt(h):
    await h.press(OWNER, FolderCb(action="new"))
    await h.press(OWNER, ChannelCb(action="list"))
    assert (await h.fsm(OWNER))[0] is None
    await h.send(OWNER, "просто текст")
    assert await h.db.list_folders() == []


# ------------------------------------------------------------------ schedules

async def test_channel_schedule_is_set_and_cleared_in_the_bot(h):
    await h.press(OWNER, ChannelCb(action="card", chat_id=CH_A))
    assert "📅 Расписание: не настроено" in last_screen(h).text
    await h.press(OWNER, ChannelCb(action="slots", chat_id=CH_A))
    prompt = last_screen(h).text
    assert "пн-пт 09:00, 18:00" in prompt and (await h.fsm(OWNER))[0] == ChannelStates.slots.state

    await h.send(OWNER, "пн 25:00")
    assert h.last_to(OWNER).text.startswith("❌ Строка 1")
    assert (await h.fsm(OWNER))[0] == ChannelStates.slots.state

    await h.send(OWNER, "пн-пт 9:00, 18:00\nсб, вс 12:00")
    slots = await h.db.get_channel_slots(CH_A)
    assert slots == {**{d: ["09:00", "18:00"] for d in range(5)}, 5: ["12:00"], 6: ["12:00"]}
    card = last_screen(h).text
    assert "✅ Расписание сохранено: пн-пт 09:00, 18:00 · сб, вс 12:00" in card
    assert (await h.fsm(OWNER))[0] is None

    await h.press(OWNER, ChannelCb(action="slots", chat_id=CH_A))
    assert "<pre>пн-пт 09:00, 18:00\nсб, вс 12:00</pre>" in last_screen(h).text
    await h.press(OWNER, ChannelCb(action="slots_clear", chat_id=CH_A))
    assert await h.db.get_channel_slots(CH_A) == {}
    assert (await h.fsm(OWNER))[0] is None


async def test_folder_wide_schedule(h):
    fid = await h.db.create_folder("Все")
    await h.db.set_channel_folder(CH_A, fid)
    await h.db.set_channel_folder(CH_B, fid)
    await h.press(OWNER, FolderCb(action="slots", folder_id=fid))
    assert (await h.fsm(OWNER))[0] == ChannelStates.folder_slots.state
    await h.send(OWNER, "ежедневно 10:00")
    for cid in (CH_A, CH_B):
        assert await h.db.get_channel_slots(cid) == {d: ["10:00"] for d in range(7)}
    assert "задано для 2 каналов" in last_screen(h).text
    await h.press(OWNER, FolderCb(action="slots", folder_id=fid))
    await h.press(OWNER, FolderCb(action="slots_clear", folder_id=fid))  # asks first (finding #12)
    assert await h.db.get_channel_slots(CH_A) == {d: ["10:00"] for d in range(7)}
    await h.press(OWNER, FolderCb(action="slots_clear_ok", folder_id=fid))
    assert await h.db.get_channel_slots(CH_A) == {} and await h.db.get_channel_slots(CH_B) == {}
    assert "Расписание очищено у 2 каналов" in last_screen(h).text


async def test_folder_schedule_with_different_channel_schedules(h):
    """Findings #12/#16: the folder prompt does not claim «no schedule» when the channels have
    different ones, and «🧹 Очистить» asks before wiping every channel's schedule."""
    fid = await h.db.create_folder("Новости")
    await h.db.set_channel_folder(CH_A, fid)
    await h.db.set_channel_folder(CH_B, fid)
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots("пн 09:00"))
    await h.db.set_channel_slots(CH_B, plan_service.parse_slots("чт 21:00"))
    await h.press(OWNER, FolderCb(action="slots", folder_id=fid))
    prompt = last_screen(h)
    assert "разные расписания — новое заменит их все" in prompt.text
    assert "Сейчас расписания нет" not in prompt.text
    assert FolderCb(action="slots_clear", folder_id=fid).pack() in data_of(prompt)

    await h.press(OWNER, FolderCb(action="slots_clear", folder_id=fid))
    confirm = last_screen(h)
    assert "Очистить расписание у 2 каналов папки «Новости»" in confirm.text
    assert "Канал А — пн 09:00" in confirm.text and "Канал Б — чт 21:00" in confirm.text
    assert await h.db.get_channel_slots(CH_A) == {0: ["09:00"]}  # nothing cleared yet
    assert await h.db.get_channel_slots(CH_B) == {3: ["21:00"]}
    assert (await h.fsm(OWNER))[0] is None  # the schedule prompt no longer waits for text
    assert FolderCb(action="slots", folder_id=fid).pack() in data_of(confirm)  # «↩️ Назад»

    await h.press(OWNER, FolderCb(action="slots_clear_ok", folder_id=fid))
    assert await h.db.get_channel_slots(CH_A) == {} and await h.db.get_channel_slots(CH_B) == {}


async def test_folder_without_schedules_offers_no_clear_button(h):
    fid = await h.db.create_folder("Пустая")
    await h.db.set_channel_folder(CH_A, fid)
    await h.press(OWNER, FolderCb(action="slots", folder_id=fid))
    prompt = last_screen(h)
    assert "Сейчас расписания нет" in prompt.text
    assert FolderCb(action="slots_clear", folder_id=fid).pack() not in data_of(prompt)


async def test_schedule_editor_from_the_plan_returns_to_the_plan(h):
    await h.press(OWNER, PlanCb(action="slots", chat_id=CH_A))
    await h.send(OWNER, "ежедневно 09:00")
    screen = last_screen(h).text
    assert "Контент-план: Канал А" in screen and "Расписание сохранено" in screen


# ------------------------------------------------------------------ plan screens

async def test_plan_home_groups_channels_by_folders(h, config):
    fid = await h.db.create_folder("Новости")
    await h.db.set_channel_folder(CH_A, fid)
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    await queued_post(h, config, "Пост", [CH_A])
    await h.send(OWNER, BTN_PLAN)
    home = h.last_to(OWNER)
    assert "Запланировано публикаций: 1" in home.text
    data = data_of(home)
    for cb in (PlanCb(action="all"), PlanCb(action="all", folder_id=fid), PlanCb(action="all", folder_id=-1),
               PlanCb(action="ch", chat_id=CH_A), PlanCb(action="ch", chat_id=CH_B)):
        assert cb.pack() in data
    labels = [b.text for row in home.reply_markup.inline_keyboard for b in row]
    assert any("Канал А · ⏰ 1" in label for label in labels)
    assert any("Канал Б · ⚙️ нет расписания" in label for label in labels)


async def test_channel_plan_and_item_actions(h, config):
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    first = await queued_post(h, config, "Первый пост", [CH_A])
    second = await queued_post(h, config, "Второй пост", [CH_A])
    t1, t2 = await ts_of(h, first, CH_A), await ts_of(h, second, CH_A)
    assert t1 < t2

    await h.press(OWNER, PlanCb(action="ch", chat_id=CH_A))
    screen = last_screen(h)
    assert "Контент-план: Канал А" in screen.text and "В очереди: 2" in screen.text
    assert "Первый пост" in screen.text and "свободно" in screen.text
    assert PlanCb(action="item", chat_id=CH_A, post_id=second).pack() in data_of(screen)

    await h.press(OWNER, PlanCb(action="item", chat_id=CH_A, post_id=second))
    assert f"Пост #{second}" in last_screen(h).text and "по расписанию" in last_screen(h).text
    await h.press(OWNER, PlanCb(action="down", chat_id=CH_A, post_id=second))
    assert "Позже в очереди ничего нет" in h.callback_answers()[-1].text
    await h.press(OWNER, PlanCb(action="up", chat_id=CH_A, post_id=second))
    assert (await ts_of(h, second, CH_A), await ts_of(h, first, CH_A)) == (t1, t2)

    # another time typed by hand
    await h.press(OWNER, PlanCb(action="time", chat_id=CH_A, post_id=second))
    assert (await h.fsm(OWNER))[0] == PlanStates.move_time.state
    await h.send(OWNER, "+3h")
    target = await h.db.get_target(second, CH_A)
    assert abs(target["scheduled_at"] - (time.time() + 3 * 3600)) < 120 and not target["queued"]
    assert "Перенесено на" in last_screen(h).text and (await h.fsm(OWNER))[0] is None

    # back onto a free slot of the schedule
    free = (await plan_service.next_free_slots(h.db, CH_A, config.tz, 1, exclude=(second, CH_A)))[0]
    await h.press(OWNER, PlanCb(action="slot", chat_id=CH_A, post_id=second, ts=free))
    target = await h.db.get_target(second, CH_A)
    assert target["scheduled_at"] == free and target["queued"]

    await h.press(OWNER, PlanCb(action="unq", chat_id=CH_A, post_id=first))
    assert await ts_of(h, first, CH_A) is None
    assert (await h.db.get_post(first))["status"] == POST_DRAFT
    assert "снова черновик" in last_screen(h).text
    await h.press(OWNER, PlanCb(action="up", chat_id=CH_A, post_id=first))
    assert "уже нет в плане" in h.callback_answers()[-1].text


async def test_compact_queue(h, config):
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    first = await queued_post(h, config, "Первый", [CH_A])
    second = await queued_post(h, config, "Второй", [CH_A])
    t1, t2 = await ts_of(h, first, CH_A), await ts_of(h, second, CH_A)
    await plan_service.unqueue_target(h.db, first, CH_A)
    # finding #20: one tap only asks — the times are rewritten after «✅ Да, уплотнить»
    await h.press(OWNER, PlanCb(action="compact", chat_id=CH_A))
    confirm = last_screen(h)
    assert "Уплотнить очередь «Канал А»" in confirm.text and "1 пост из очереди займёт" in confirm.text
    assert plan_service.slot_label(t1, config.tz) in confirm.text  # where the first one goes
    assert PlanCb(action="compact_ok", chat_id=CH_A).pack() in data_of(confirm)
    assert await ts_of(h, second, CH_A) == t2
    await h.press(OWNER, PlanCb(action="compact_ok", chat_id=CH_A))
    assert await ts_of(h, second, CH_A) == t1
    assert "Очередь уплотнена" in last_screen(h).text

    # nothing to move: said at once, no confirmation
    await h.press(OWNER, PlanCb(action="compact", chat_id=CH_A))
    assert "Очередь и так плотная" in last_screen(h).text
    assert PlanCb(action="compact_ok", chat_id=CH_A).pack() not in data_of(last_screen(h))

    await h.press(OWNER, PlanCb(action="compact", chat_id=CH_B))
    assert "не настроено расписание" in h.callback_answers()[-1].text


async def test_compact_preview_matches_the_service(h, config):
    """The confirmation counts exactly the posts `compact_queue` moves (explicit times stay)."""
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    ids = [await queued_post(h, config, f"Пост {i}", [CH_A]) for i in range(5)]
    times = [await ts_of(h, post_id, CH_A) for post_id in ids]
    await plan_service.unqueue_target(h.db, ids[0], CH_A)
    await plan_service.unqueue_target(h.db, ids[2], CH_A)
    await plan_service.move_target(h.db, ids[1], CH_A, times[1] + 1800, queued=False)  # exact time ⏱
    await h.press(OWNER, PlanCb(action="compact", chat_id=CH_A))
    assert "2 поста из очереди займут" in last_screen(h).text
    await h.press(OWNER, PlanCb(action="compact_ok", chat_id=CH_A))
    assert "2 поста перенесены" in last_screen(h).text
    assert await ts_of(h, ids[1], CH_A) == times[1] + 1800


async def test_other_time_does_not_offer_the_posts_own_time(h, config):
    """Finding #10: «⏰ Другое время» lists free slots only — not the slot the post already has —
    and an old button with its own time does not report a move that did not happen."""
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    post_id = await queued_post(h, config, "Пост", [CH_A])
    own = await ts_of(h, post_id, CH_A)
    await h.press(OWNER, PlanCb(action="time", chat_id=CH_A, post_id=post_id))
    prompt = last_screen(h)
    slots = [d for d in data_of(prompt) if d.startswith("pn:slot:")]
    assert len(slots) == 4
    assert PlanCb(action="slot", chat_id=CH_A, post_id=post_id, ts=own).pack() not in slots

    await h.press(OWNER, PlanCb(action="slot", chat_id=CH_A, post_id=post_id, ts=own))
    assert h.callback_answers()[-1].text == "Пост уже стоит на это время"
    assert "Перенесено" not in last_screen(h).text
    target = await h.db.get_target(post_id, CH_A)
    assert target["scheduled_at"] == own and target["queued"]


async def test_publish_now_from_the_plan_only_in_that_channel(h, config):
    for cid in (CH_A, CH_B):
        await h.db.set_channel_slots(cid, plan_service.parse_slots(EVERY_HOUR))
    post_id = await queued_post(h, config, "Срочно", [CH_A, CH_B])
    await h.press(OWNER, PlanCb(action="pub", chat_id=CH_A, post_id=post_id))
    assert "В остальные каналы он выйдет по своему плану" in last_screen(h).text
    await h.press(OWNER, PlanCb(action="pub_ok", chat_id=CH_A, post_id=post_id))
    assert [r.text for r in h.session.sent_to(CH_A)] == ["Срочно"] and h.session.sent_to(CH_B) == []
    post = await h.db.get_post(post_id)
    targets = {t["channel_id"]: t for t in post["targets"]}
    assert targets[CH_A]["status"] == "sent" and targets[CH_B]["status"] == "pending"
    assert targets[CH_B]["scheduled_at"] and post["status"] == POST_SCHEDULED
    assert last_screen(h).text.startswith("✅ Опубликовано: 1 из 1")
    # a second tap does not publish again nor overwrite the report
    reports = len(h.edits())
    await h.press(OWNER, PlanCb(action="pub_ok", chat_id=CH_A, post_id=post_id))
    assert len(h.session.sent_to(CH_A)) == 1
    assert "уже нет в плане" in h.callback_answers()[-1].text
    assert len(h.edits()) == reports + 1  # only the channel plan is shown again


async def test_timeline_of_all_channels_and_of_a_folder(h, config):
    fid = await h.db.create_folder("Новости")
    await h.db.set_channel_folder(CH_A, fid)
    for cid in (CH_A, CH_B):
        await h.db.set_channel_slots(cid, plan_service.parse_slots(EVERY_HOUR))
    a_post = await queued_post(h, config, "Пост для А", [CH_A])
    b_post = await queued_post(h, config, "Пост для Б", [CH_B])
    await h.press(OWNER, PlanCb(action="all"))
    screen = last_screen(h)
    assert "Все каналы — 7 дней" in screen.text
    assert "Канал А — Пост для А" in screen.text and "Канал Б — Пост для Б" in screen.text
    assert PlanCb(action="item", chat_id=CH_B, post_id=b_post).pack() in data_of(screen)

    await h.press(OWNER, PlanCb(action="all", folder_id=fid))
    screen = last_screen(h)
    assert "Папка «Новости»" in screen.text and "Пост для А" in screen.text and "Пост для Б" not in screen.text
    await h.press(OWNER, PlanCb(action="all", folder_id=-1))
    assert "Пост для Б" in last_screen(h).text and "Пост для А" not in last_screen(h).text
    assert a_post


async def test_plan_of_a_channel_without_schedule(h):
    await h.press(OWNER, PlanCb(action="ch", chat_id=CH_B))
    assert "не настроено" in last_screen(h).text and "В очереди пусто" in last_screen(h).text


async def test_edit_from_the_plan_opens_the_composer(h, config):
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots(EVERY_HOUR))
    post_id = await queued_post(h, config, "Пост", [CH_A])
    await h.press(OWNER, PlanCb(action="edit", chat_id=CH_A, post_id=post_id))
    panels = [r for r in h.session.sent_to(OWNER) if isinstance(r, SendMessage) and "Длина:" in r.text]
    assert panels and f"Запланированный пост #{post_id}" in panels[-1].text
    assert "(по расписанию)" in panels[-1].text


# ------------------------------------------------------------------ composer channel picker

async def test_composer_picker_is_grouped_by_folders(h):
    await h.add_channel(CH_C, "Канал В", "kanal_c")
    fid = await h.db.create_folder("Пара")
    await h.db.set_channel_folder(CH_A, fid)
    await h.db.set_channel_folder(CH_B, fid)
    await h.send(OWNER, "/new")
    await h.send(OWNER, "Текст")
    post = await h.db.fetchone("SELECT MAX(id) AS id FROM posts")
    post_id = post["id"]
    await h.press(OWNER, ComposeCb(action="channels", post_id=post_id))
    picker = last_screen(h)
    labels = [b.text for row in picker.reply_markup.inline_keyboard for b in row]
    assert "📁 Пара (0/2)" in labels and "📂 Без папки (0/1)" in labels
    assert any(label.startswith("↳ ▫️ Канал А") for label in labels)
    assert ComposeCb(action="ch_none", post_id=post_id).pack() in data_of(picker)

    await h.press(OWNER, ComposeFolderCb(post_id=post_id, folder_id=fid))
    targets = sorted(t["channel_id"] for t in (await h.db.get_post(post_id))["targets"])
    assert targets == sorted([CH_A, CH_B])
    assert "📁 Пара (2/2)" in [b.text for row in last_screen(h).reply_markup.inline_keyboard for b in row]
    await h.press(OWNER, ComposeFolderCb(post_id=post_id, folder_id=0))
    assert len((await h.db.get_post(post_id))["targets"]) == 3
    await h.press(OWNER, ComposeFolderCb(post_id=post_id, folder_id=fid))
    assert [t["channel_id"] for t in (await h.db.get_post(post_id))["targets"]] == [CH_C]
    await h.press(OWNER, ComposeCb(action="ch_none", post_id=post_id))
    assert (await h.db.get_post(post_id))["targets"] == []


async def test_leaving_a_schedule_prompt_cancels_it(h):
    await h.press(OWNER, PlanCb(action="slots", chat_id=CH_A))
    assert (await h.fsm(OWNER))[0] == ChannelStates.slots.state
    await h.press(OWNER, PlanCb(action="ch", chat_id=CH_A))  # «↩️ Назад»
    assert (await h.fsm(OWNER))[0] is None
    await h.press(OWNER, PlanCb(action="time", chat_id=CH_A, post_id=1))  # not in the plan: refused
    assert (await h.fsm(OWNER))[0] is None
    await h.send(OWNER, "ежедневно 09:00")
    assert await h.db.get_channel_slots(CH_A) == {}


async def test_stale_plan_buttons(h, config):
    await h.press(OWNER, PlanCb(action="item", chat_id=CH_A, post_id=999))
    assert "уже нет в плане" in h.callback_answers()[-1].text
    await h.press(OWNER, PlanCb(action="ch", chat_id=-100999))  # a channel removed from the bot
    assert "Канал не найден" in last_screen(h).text and "Контент-план" in last_screen(h).text


async def test_posts_beyond_the_week_are_summed_up(h, config):
    await h.db.set_channel_slots(CH_A, plan_service.parse_slots("ежедневно 09:00"))
    for i in range(9):
        await queued_post(h, config, f"Пост {i}", [CH_A])
    await h.press(OWNER, PlanCb(action="ch", chat_id=CH_A))
    text = last_screen(h).text
    shown = sum(1 for i in range(9) if f"Пост {i}" in text)
    later = 9 - shown
    assert later >= 1 and f"Дальше: ещё {later}" in text
    assert len([d for d in data_of(last_screen(h)) if d.startswith("pn:item:")]) == 9


async def test_plan_with_many_channels_is_paged(h):
    """Finding #18 (plan side): 120 channels without folders, then all of them in one folder — the
    plan screens keep their keyboards within Telegram's limit (checked by the Harness) and every
    channel stays reachable through the pages."""
    from app.bot.callbacks import PlanPageCb

    ids = [CH_A, CH_B]
    for i in range(118):
        cid = -1004000000000 - i
        await h.add_channel(cid, f"Канал {i:03d}")
        ids.append(cid)
    await h.send(OWNER, BTN_PLAN)
    seen = {d for d in data_of(h.last_to(OWNER)) if d.startswith("pn:ch:")}
    assert PlanPageCb(folder_id=0, page=1).pack() in data_of(h.last_to(OWNER))
    for page in (1, 2):
        await h.press(OWNER, PlanPageCb(folder_id=0, page=page))
        seen |= {d for d in data_of(last_screen(h)) if d.startswith("pn:ch:")}
    assert seen == {PlanCb(action="ch", chat_id=cid).pack() for cid in ids}

    fid = await h.db.create_folder("Все")
    for cid in ids:
        await h.db.set_channel_folder(cid, fid)
    await h.send(OWNER, BTN_PLAN)
    assert PlanCb(action="folder", folder_id=fid).pack() in data_of(h.last_to(OWNER))
    await h.press(OWNER, PlanCb(action="folder", folder_id=fid))
    assert PlanPageCb(folder_id=fid, page=1).pack() in data_of(last_screen(h))
    await h.press(OWNER, PlanPageCb(folder_id=fid, page=2))
    assert len([d for d in data_of(last_screen(h)) if d.startswith("pn:ch:")]) == 40


async def test_plan_item_of_a_post_queued_to_many_channels(h, config):
    ids = []
    for i in range(110):
        cid = -1005000000000 - i
        await h.add_channel(cid, f"Новости и аналитика рынка недвижимости {i:03d}")
        await h.db.set_channel_slots(cid, plan_service.parse_slots(EVERY_HOUR))
        ids.append(cid)
    post_id = await queued_post(h, config, "Пост", ids)
    await h.press(OWNER, PlanCb(action="item", chat_id=ids[0], post_id=post_id))  # ≤ 4096: checked by the Harness
    assert "Другие каналы: Новости и аналитика" in last_screen(h).text

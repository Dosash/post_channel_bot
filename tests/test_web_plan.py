"""Web API: channel folders, publishing slots and the content plan (SPEC_V2 §7)."""
from __future__ import annotations

import time

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db import CH_PENDING
from app.web import create_web_app

H = {"X-Requested-With": "pcb"}
CHANNEL = -1001111111111
CHANNEL2 = -1002222222222
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
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


async def _json(resp: aiohttp.ClientResponse) -> tuple[int, dict]:
    return resp.status, await resp.json()


async def _post(client: TestClient, path: str, **body) -> tuple[int, dict]:
    return await _json(await client.post(path, json=body, headers=H))


async def _put(client: TestClient, path: str, **body) -> tuple[int, dict]:
    return await _json(await client.put(path, json=body, headers=H))


async def _set_slots(client: TestClient, channel_id: int, slots: dict | None = None) -> dict:
    status, data = await _put(client, f"/api/channels/{channel_id}/slots", slots=slots or EVERY_DAY)
    assert status == 200, data
    return data["channel"]


async def _queue(client: TestClient, text: str, channels: list[int], **extra) -> dict:
    status, data = await _post(client, "/api/posts", text_md=text, channel_ids=channels, action="queue", **extra)
    assert status == 200, data
    return data


def _target(post: dict, channel_id: int) -> dict:
    return next(t for t in post["targets"] if t["channel_id"] == channel_id)


# ------------------------------------------------------------------ folders

async def test_folders_crud(client, db):
    assert await (await client.get("/api/folders")).json() == {"folders": []}
    status, data = await _post(client, "/api/folders", name="  Новости  ")
    assert status == 200, data
    news = data["folder"]
    assert news["name"] == "Новости" and news["channels_count"] == 0 and set(news) == {
        "id", "name", "position", "channels_count"}
    status, data = await _post(client, "/api/folders", name="Блоги")
    blogs = data["folder"]
    assert [f["name"] for f in data["folders"]] == ["Новости", "Блоги"]

    for name, message in (("новости", "уже есть"), ("", "не может быть пустым"), ("Без папки", "зарезервировано"),
                          ("x" * 41, "Слишком длинное")):
        status, data = await _post(client, "/api/folders", name=name)
        assert status == 400 and message in data["error"], name
    status, data = await _post(client, "/api/folders", name=5)
    assert status == 400

    status, data = await _put(client, f"/api/folders/{blogs['id']}", name="Блоги и мнения", direction=-1)
    assert status == 200, data
    assert data["folder"]["name"] == "Блоги и мнения"
    assert [f["name"] for f in data["folders"]] == ["Блоги и мнения", "Новости"]
    status, data = await _put(client, f"/api/folders/{blogs['id']}", direction=-1)  # already first
    assert status == 200 and data["folders"][0]["id"] == blogs["id"]
    status, data = await _put(client, f"/api/folders/{news['id']}", name="Новости")  # unchanged name is fine
    assert status == 200

    assert (await _put(client, f"/api/folders/{news['id']}"))[0] == 400
    assert (await _put(client, f"/api/folders/{news['id']}", direction="up"))[0] == 400
    status, data = await _put(client, "/api/folders/999", name="X")
    assert status == 404 and data["error"] == "Папка не найдена"

    await db.set_channel_folder(CHANNEL, news["id"])
    status, data = await _json(await client.delete(f"/api/folders/{news['id']}", headers=H))
    assert status == 200 and data["ok"] is True and [f["id"] for f in data["folders"]] == [blogs["id"]]
    assert (await db.get_channel(CHANNEL))["folder_id"] is None  # channels stay, without a folder
    assert (await client.delete(f"/api/folders/{news['id']}", headers=H)).status == 404


async def test_channel_folder_and_grouped(client, db):
    _, data = await _post(client, "/api/folders", name="Новости")
    news = data["folder"]
    await _post(client, "/api/folders", name="Пустая")
    await db.upsert_channel(-100777, "Ждёт", None, status=CH_PENDING)

    status, data = await _put(client, f"/api/channels/{CHANNEL}/folder", folder_id=news["id"])
    assert status == 200, data
    assert data["channel"]["folder_id"] == news["id"] and "slots" in data["channel"]

    status, data = await _json(await client.get("/api/channels/grouped"))
    assert status == 200
    groups = data["groups"]
    assert [(g["folder"]["name"] if g["folder"] else None) for g in groups] == ["Новости", "Пустая", None]
    assert groups[0]["folder"]["channels_count"] == 1
    assert [c["id"] for c in groups[0]["channels"]] == [CHANNEL]
    assert groups[1]["channels"] == []
    assert {c["title"] for c in groups[2]["channels"]} == {"Второй", "Ждёт"}  # any status
    assert groups[0]["channels"][0]["link"] == "https://t.me/first_chan"

    status, data = await _put(client, f"/api/channels/{CHANNEL}/folder", folder_id=None)
    assert status == 200 and data["channel"]["folder_id"] is None
    assert (await _put(client, f"/api/channels/{CHANNEL}/folder", folder_id=999))[0] == 404
    assert (await _put(client, f"/api/channels/{CHANNEL}/folder", folder_id="abc"))[0] == 400
    assert (await _put(client, f"/api/channels/{CHANNEL}/folder"))[0] == 400
    assert (await _put(client, "/api/channels/-100404/folder", folder_id=None))[0] == 404


# ------------------------------------------------------------------ slots

async def test_channel_slots_text_and_object(client):
    status, data = await _put(client, f"/api/channels/{CHANNEL}/slots", text="пн-пт 9:00, 18:00\nсб, вс 12:00")
    assert status == 200, data
    channel = data["channel"]
    assert channel["slots"] == {"0": ["09:00", "18:00"], "1": ["09:00", "18:00"], "2": ["09:00", "18:00"],
                                "3": ["09:00", "18:00"], "4": ["09:00", "18:00"], "5": ["12:00"], "6": ["12:00"]}
    assert channel["slots_text"] == "пн-пт 09:00, 18:00\nсб, вс 12:00"
    assert channel["slots_summary"] == "пн-пт 09:00, 18:00 · сб, вс 12:00"

    status, data = await _put(client, f"/api/channels/{CHANNEL}/slots", slots={"2": ["10:00", "8:30", "10:00"]})
    assert status == 200 and data["channel"]["slots"]["2"] == ["08:30", "10:00"]
    assert data["channel"]["slots"]["0"] == []

    status, data = await _put(client, f"/api/channels/{CHANNEL}/slots", text="пн 25:00")
    assert status == 400 and "Строка 1" in data["error"]
    status, data = await _put(client, f"/api/channels/{CHANNEL}/slots", slots={"9": ["10:00"]})
    assert status == 400 and "день недели" in data["error"]
    assert (await _put(client, f"/api/channels/{CHANNEL}/slots", slots="пн 10:00"))[0] == 400
    assert (await _put(client, f"/api/channels/{CHANNEL}/slots"))[0] == 400
    assert (await _put(client, "/api/channels/-100404/slots", slots={}))[0] == 404

    status, data = await _put(client, f"/api/channels/{CHANNEL}/slots", slots=None)
    assert status == 200 and data["channel"]["slots_summary"] == "не настроено"
    status, data = await _put(client, f"/api/channels/{CHANNEL}/slots", text="")
    assert status == 200 and data["channel"]["slots_text"] == ""


async def test_folder_slots(client, db):
    _, data = await _post(client, "/api/folders", name="Новости")
    folder_id = data["folder"]["id"]
    await db.set_channel_folder(CHANNEL, folder_id)
    await db.set_channel_folder(CHANNEL2, folder_id)
    status, data = await _put(client, f"/api/folders/{folder_id}/slots", text="будни 10:00")
    assert status == 200, data
    assert data["changed"] == 2 and data["folder"]["id"] == folder_id
    channels = (await (await client.get("/api/channels")).json())["channels"]
    assert {c["slots_summary"] for c in channels} == {"пн-пт 10:00"}

    status, data = await _put(client, f"/api/folders/{folder_id}/slots", slots={"0": ["7:00"]})
    assert status == 200 and data["changed"] == 2
    status, data = await _put(client, f"/api/folders/{folder_id}/slots", text="когда-нибудь")
    assert status == 400
    assert (await _put(client, "/api/folders/999/slots", text="пн 10:00"))[0] == 404


# ------------------------------------------------------------------ plan views

async def test_channel_plan(client, db):
    await _set_slots(client, CHANNEL)
    form = aiohttp.FormData()
    form.add_field("file", PNG, filename="a.png", content_type="image/png")
    photo = (await (await client.post("/api/upload", data=form, headers=H)).json())["path"]
    queued = await _queue(client, "С картинкой", [CHANNEL], media=[{"type": "photo", "path": photo}])
    post_id = queued["post"]["id"]
    ts = int(queued["schedule"][str(CHANNEL)])

    status, data = await _json(await client.get(f"/api/plan/channel/{CHANNEL}?days=3"))
    assert status == 200, data
    assert len(data["days"]) == 3 and data["days"][0]["weekday"] in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
    assert data["channel"]["id"] == CHANNEL and data["channel"]["slots"]["0"] == ["09:00", "21:00"]
    assert data["slots"] == EVERY_DAY and data["slots_summary"] == "ежедневно 09:00, 21:00"
    items = [item for day in data["days"] for item in day["items"]]
    filled = [item for item in items if item["post"]]
    assert [(i["ts"], i["post"]["id"]) for i in filled] == [(ts, post_id)]
    summary = filled[0]["post"]
    assert summary["queued"] is True and summary["media_kind"] == "photo"
    assert summary["thumb_url"].startswith(f"/api/posts/{post_id}/media/0?v=")
    assert any(item["post"] is None and item["slot"] for item in items)

    data = await (await client.get(f"/api/plan/channel/{CHANNEL}?days=abc")).json()
    assert len(data["days"]) == 14
    data = await (await client.get(f"/api/plan/channel/{CHANNEL}?days=9999")).json()
    assert len(data["days"]) == 366
    later = await (await client.get(f"/api/plan/channel/{CHANNEL}?days=1&start={ts + 3 * 86400}")).json()
    assert all(item["post"] is None for day in later["days"] for item in day["items"])
    assert (await client.get(f"/api/plan/channel/{CHANNEL}?start=-5")).status == 400
    assert (await client.get("/api/plan/channel/-100404")).status == 404


async def test_timeline(client, db):
    await _set_slots(client, CHANNEL)
    await _set_slots(client, CHANNEL2)
    _, data = await _post(client, "/api/folders", name="Новости")
    folder_id = data["folder"]["id"]
    await db.set_channel_folder(CHANNEL, folder_id)
    first = await _queue(client, "первый", [CHANNEL, CHANNEL2])

    data = await (await client.get("/api/plan/timeline")).json()
    items = data["items"]
    assert {(i["channel"]["id"], i["post"]["id"], i["ts"]) for i in items} == {
        (cid, first["post"]["id"], int(ts)) for cid, ts in ((CHANNEL, first["schedule"][str(CHANNEL)]),
                                                           (CHANNEL2, first["schedule"][str(CHANNEL2)]))}
    assert [i["ts"] for i in items] == sorted(i["ts"] for i in items)
    assert items[0]["post"]["thumb_url"] is None  # text post
    assert {"ts", "time", "date", "weekday", "channel", "post"} <= set(items[0])

    data = await (await client.get(f"/api/plan/timeline?days=7&folder_id={folder_id}")).json()
    assert [i["channel"]["id"] for i in data["items"]] == [CHANNEL]
    data = await (await client.get("/api/plan/timeline?folder_id=0")).json()
    assert [i["channel"]["id"] for i in data["items"]] == [CHANNEL2]
    data = await (await client.get(f"/api/plan/timeline?channel_ids={CHANNEL2}")).json()
    assert [i["channel"]["id"] for i in data["items"]] == [CHANNEL2]
    assert (await client.get("/api/plan/timeline?folder_id=abc")).status == 400
    assert (await client.get("/api/plan/timeline?channel_ids=a,b")).status == 400


async def test_plan_next(client):
    await _set_slots(client, CHANNEL)
    data = await (await client.get(f"/api/plan/next?channel_ids={CHANNEL},{CHANNEL2}")).json()
    nxt = data["next"]
    assert nxt[str(CHANNEL2)] is None and nxt[str(CHANNEL)] > time.time()
    queued = await _queue(client, "x", [CHANNEL])
    assert int(queued["schedule"][str(CHANNEL)]) == nxt[str(CHANNEL)]
    after = (await (await client.get(f"/api/plan/next?channel_ids={CHANNEL}")).json())["next"][str(CHANNEL)]
    assert after > nxt[str(CHANNEL)]
    # With post_id the post's own slot counts as free (what re-queueing it would give).
    own = await (await client.get(f"/api/plan/next?channel_ids={CHANNEL}&post_id={queued['post']['id']}")).json()
    assert own["next"][str(CHANNEL)] == nxt[str(CHANNEL)]
    assert await (await client.get("/api/plan/next")).json() == {"next": {}}
    assert (await client.get("/api/plan/next?channel_ids=x")).status == 400


# ------------------------------------------------------------------ plan editing

async def test_move_shift_unqueue_compact(client, db):
    await _set_slots(client, CHANNEL)
    a = await _queue(client, "A", [CHANNEL])
    b = await _queue(client, "B", [CHANNEL])
    a_id, b_id = a["post"]["id"], b["post"]["id"]
    ta, tb = int(a["schedule"][str(CHANNEL)]), int(b["schedule"][str(CHANNEL)])
    assert ta < tb

    # Shift B earlier: the two posts swap times.
    status, data = await _post(client, "/api/plan/shift", post_id=b_id, channel_id=CHANNEL, direction=-1)
    assert status == 200, data
    assert data["moved"] is True and _target(data["post"], CHANNEL)["scheduled_at"] == ta
    status, data = await _post(client, "/api/plan/shift", post_id=b_id, channel_id=CHANNEL, direction=-1)
    assert status == 200 and data["moved"] is False  # nothing earlier
    assert (await _post(client, "/api/plan/shift", post_id=b_id, channel_id=CHANNEL, direction=0))[0] == 400

    # Move A (now at tb) to an arbitrary time.
    new_ts = tb + 3 * 86400 + 600
    status, data = await _post(client, "/api/plan/move", post_id=a_id, channel_id=CHANNEL, ts=new_ts, queued=False)
    assert status == 200, data
    target = _target(data["post"], CHANNEL)
    assert target["scheduled_at"] == new_ts and target["queued"] is False
    # Dropping A onto B's slot swaps them.
    status, data = await _post(client, "/api/plan/move", post_id=a_id, channel_id=CHANNEL, ts=ta)
    assert status == 200
    assert _target((await (await client.get(f"/api/posts/{b_id}")).json())["post"], CHANNEL)["scheduled_at"] == new_ts

    status, data = await _post(client, "/api/plan/move", post_id=a_id, channel_id=CHANNEL, ts=int(time.time()) - 60)
    assert status == 400 and data["error"] == "Это время уже прошло"
    assert (await _post(client, "/api/plan/move", post_id=999, channel_id=CHANNEL, ts=new_ts))[0] == 404
    assert (await _post(client, "/api/plan/move", post_id=a_id, channel_id=CHANNEL))[0] == 400
    assert (await _post(client, "/api/plan/move", post_id="x", channel_id=CHANNEL, ts=new_ts))[0] == 400
    assert (await _post(client, "/api/plan/move", post_id=a_id, channel_id=CHANNEL, ts=new_ts, queued="y"))[0] == 400
    status, data = await _post(client, "/api/plan/move", post_id=a_id, channel_id=CHANNEL2, ts=new_ts)
    assert status == 400 and data["error"] == "Пост не запланирован в этом канале"

    # Take A out of the plan, then compact: B moves up to the earliest free slot.
    status, data = await _post(client, "/api/plan/unqueue", post_id=a_id, channel_id=CHANNEL)
    assert status == 200 and data["post"]["status"] == "draft"
    assert _target(data["post"], CHANNEL)["scheduled_at"] is None
    await _post(client, "/api/plan/move", post_id=b_id, channel_id=CHANNEL, ts=new_ts, queued=True)
    status, data = await _post(client, "/api/plan/compact", channel_id=CHANNEL)
    assert status == 200 and data == {"moved": 1}
    post_b = (await (await client.get(f"/api/posts/{b_id}")).json())["post"]
    assert _target(post_b, CHANNEL)["scheduled_at"] == ta

    status, data = await _post(client, "/api/plan/compact", channel_id=CHANNEL2)
    assert status == 400 and "не настроено расписание" in data["error"]
    assert (await _post(client, "/api/plan/compact"))[0] == 400
    assert (await _post(client, "/api/plan/compact", channel_id=-100404))[0] == 404


async def test_plan_refuses_post_being_sent(client, db):
    await _set_slots(client, CHANNEL)
    queued = await _queue(client, "A", [CHANNEL])
    post_id = queued["post"]["id"]
    await db.update_post(post_id, status="sending")
    ts = int(queued["schedule"][str(CHANNEL)]) + 86400
    status, data = await _post(client, "/api/plan/move", post_id=post_id, channel_id=CHANNEL, ts=ts)
    assert status == 409 and data["error"] == "Пост сейчас публикуется"
    status, data = await _post(client, "/api/plan/unqueue", post_id=post_id, channel_id=CHANNEL)
    assert status == 409

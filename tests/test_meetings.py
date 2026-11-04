"""«Запись на встречу»: слоты, гонка двух родителей, перенос, отмена, напоминание за 2 часа, календарь."""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.demo_ids import DEMO_BASE
from app.models import Meeting, MeetingSlot, Member, User
from app.services import meetings, quiet, retention
from app.timeutil import local_now, to_local, utcnow
from app.webapp import api as webapi

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update

T = TEACHER["user_id"]
P1, P2, P3 = (p["user_id"] for p in PARENTS)
TZ = "Europe/Moscow"


def run(loop, coro):
    return loop.run_until_complete(coro)


def connect(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))


def payloads(sent: dict) -> list[str]:
    return [b.get("payload") or "" for att in sent.get("attachments") or []
            for row in att.get("payload", {}).get("buttons", []) for b in row]


def make_meeting(loop, starts: list[datetime], minutes: int = 15) -> Meeting:
    return run(loop, meetings.create(T, CHAT_ID, "Консультации по итогам четверти", "office", "каб. 214",
                                     [(at, minutes) for at in starts]))


def slots(loop, mid: int) -> list[MeetingSlot]:
    async def go():
        async with session() as s:
            return (await s.scalars(select(MeetingSlot).where(MeetingSlot.meeting_id == mid)
                                    .order_by(MeetingSlot.starts_at))).all()
    return run(loop, go())


def future(hours: float) -> datetime:
    return (utcnow() + timedelta(hours=hours)).replace(second=0, microsecond=0)


# ====================================================================== окна → слоты
def test_parse_windows_and_build_slots():
    today = date(2026, 11, 4)
    text = "Консультации по итогам четверти\nкаб. 214\n12.11 15:00-16:00 по 20 мин\n13.11 с 16:00 до 16:30\n01.02 9:00-9:15"
    windows, rest, errors = meetings.parse_windows(text, TZ, today=today)
    assert rest == ["Консультации по итогам четверти", "каб. 214"] and errors == []
    assert [(w.day, w.start, w.end, w.minutes) for w in windows] == [
        (date(2026, 11, 12), 900, 960, 20), (date(2026, 11, 13), 960, 990, 15), (date(2027, 2, 1), 540, 555, 15)]
    assert "ни одна встреча" in errors_of("01.02 9:00-9:10")[0]  # окно короче встречи
    assert errors_of("12.11 17:00-16:00")  # конец раньше начала
    slots_ = meetings.build_slots(windows, TZ, now=datetime(2026, 11, 4, 9, 0))
    local = [to_local(at, TZ).strftime("%d.%m %H:%M") for at, _ in slots_]
    assert local == ["12.11 15:00", "12.11 15:20", "12.11 15:40", "13.11 16:00", "13.11 16:15", "01.02 09:00"]
    assert meetings.build_slots(windows, TZ, now=datetime(2027, 3, 1)) == []  # всё в прошлом


def errors_of(line: str) -> list[str]:
    return meetings.parse_windows(line, TZ, today=date(2026, 11, 4))[2]


# ====================================================================== гонка и уникальность
def test_two_parents_race_for_one_slot(env):
    loop, api, c = env
    connect(loop)
    m = make_meeting(loop, [future(30), future(31)])
    first = m.slots[0].id

    async def race():
        return await asyncio.gather(meetings.book(first, P1), meetings.book(first, P2))
    (s1, _, _), (s2, _, _) = run(loop, race())
    assert sorted([s1, s2]) == ["ok", "taken"]
    booked = [x for x in slots(loop, m.id) if x.user_id]
    assert len(booked) == 1 and booked[0].id == first and booked[0].user_id in (P1, P2)


def test_one_parent_holds_one_slot_and_can_move_and_cancel(env):
    loop, api, c = env
    connect(loop)
    m = make_meeting(loop, [future(30), future(31), future(32)])
    a, b, cc = (x.id for x in m.slots)

    async def double():  # двойное нажатие на два разных слота — запись всё равно одна
        return await asyncio.gather(meetings.book(a, P1), meetings.book(b, P1))
    run(loop, double())
    mine = [x for x in slots(loop, m.id) if x.user_id == P1]
    assert len(mine) == 1

    status, slot, _ = run(loop, meetings.book(cc, P1))
    assert status == "moved" and slot.id == cc
    assert [x.id for x in slots(loop, m.id) if x.user_id == P1] == [cc]
    assert run(loop, meetings.book(cc, P1))[0] == "same"
    assert run(loop, meetings.book(a, T))[0] == "forbidden"            # учитель сам к себе не записывается
    assert run(loop, meetings.book(a, 424242))[0] == "forbidden"       # посторонний — тоже

    freed = run(loop, meetings.cancel(m.id, P1))
    assert freed and freed.slot_id == cc and not any(x.user_id for x in slots(loop, m.id))
    assert run(loop, meetings.cancel(m.id, P1)) is None


def test_bot_flow_dispatch_book_reschedule_cancel(env):
    loop, api, c = env
    connect(loop)
    d = (local_now(TZ) + timedelta(days=3)).strftime("%d.%m")
    run(loop, handle_update(msg_update(TEACHER, "/meet", mid="m1")))
    assert "Как будете встречаться?" in api.texts_to(user_id=T)[-1]
    run(loop, handle_update(cb_update(TEACHER, "mt:fmt:call", "m2")))
    run(loop, handle_update(msg_update(TEACHER, f"Разговор о профориентации\n{d} 15:00-16:00 по 30 мин", mid="m3")))
    preview = api.texts_to(user_id=T)[-1]
    assert "Слотов: <b>2</b>" in preview and "звонок" in preview
    run(loop, handle_update(cb_update(TEACHER, "mt:go", "m4")))
    assert "Разослал: в личку дошло 3 из 3" in api.texts_to(user_id=T)[-1]

    invite = next(s for s in api.sent if s.get("user_id") == P1 and "Запись на встречу" in (s.get("text") or ""))
    slot_pl = [p for p in payloads(invite) if p.startswith("mt:b:")]
    assert len(slot_pl) == 2
    run(loop, handle_update(cb_update(PARENTS[0], slot_pl[0], "m5")))
    assert "Записал вас" in api.texts_to(user_id=P1)[-1] and "Напомню за 2 часа" in api.texts_to(user_id=P1)[-1]
    run(loop, handle_update(cb_update(PARENTS[1], slot_pl[0], "m6")))   # тот же слот — уже занят
    assert any(a["notification"] == "Это время уже заняли" for a in api.answers)
    assert "успели занять" in api.texts_to(user_id=P2)[-1]

    mid = int(slot_pl[0].split(":")[2])
    m = run(loop, meetings.get(run(loop, _meeting_of(mid))))
    run(loop, handle_update(cb_update(PARENTS[0], f"mt:mv:{m.id}", "m7")))
    run(loop, handle_update(cb_update(PARENTS[0], slot_pl[1], "m8")))
    assert "Перенёс вашу запись" in api.texts_to(user_id=P1)[-1]
    run(loop, handle_update(cb_update(TEACHER, f"mt:s:{m.id}", "m9")))
    sched = api.texts_to(user_id=T)[-1]
    assert "Записались: 1 из 2" in sched and "Иванова" in sched and "свободно" in sched
    run(loop, handle_update(cb_update(PARENTS[0], f"mt:c:{m.id}", "m10")))
    assert "Запись отменена" in api.texts_to(user_id=P1)[-1]
    assert not any(x.user_id for x in slots(loop, m.id))


async def _meeting_of(slot_id: int) -> int:
    async with session() as s:
        return (await s.get(MeetingSlot, slot_id)).meeting_id


# ====================================================================== напоминание за 2 часа
def test_reminder_two_hours_before_once(env):
    loop, api, c = env
    connect(loop)
    m = make_meeting(loop, [future(5), future(6)])
    run(loop, meetings.book(m.slots[0].id, P1))
    run(loop, meetings.book(m.slots[1].id, P2))
    assert run(loop, meetings.remind_tick()) == 0  # до встречи ещё 5 часов

    async def shift(slot_id, hours):
        async with session() as s:
            (await s.get(MeetingSlot, slot_id)).starts_at = future(hours)
    run(loop, shift(m.slots[0].id, 1.9))
    assert run(loop, meetings.remind_tick()) == 1
    assert "Напоминаю: сегодня в" in api.texts_to(user_id=P1)[-1]
    c.kv._d.clear()  # даже без отметки в KV второй раз не пишем: слот помечен в базе
    assert run(loop, meetings.remind_tick()) == 0
    assert sum("Напоминаю" in t for t in api.texts_to(user_id=P1)) == 1

    # записался впритык (меньше чем за 2 часа) — напоминание не нужно
    m2 = make_meeting(loop, [future(1)])
    status, slot, _ = run(loop, meetings.book(m2.slots[0].id, P3))
    assert status == "ok" and slot.reminded is True
    assert run(loop, meetings.remind_tick()) == 0


def test_demo_slots_are_not_reminded(env):
    loop, api, c = env

    async def seed():
        async with session() as s:
            m = Meeting(teacher_id=DEMO_BASE + 1, chat_id=-(DEMO_BASE + 1), title="Демо")
            s.add(m)
            await s.flush()
            s.add(MeetingSlot(meeting_id=m.id, starts_at=future(1), user_id=DEMO_BASE + 11, name="Демо"))
    run(loop, seed())
    assert run(loop, meetings.remind_tick()) == 0


# ====================================================================== расписание, сводка, календарь, API
def test_digest_calendar_and_api(env):
    loop, api, c = env
    connect(loop)
    teacher = run(loop, webapi.chats.get_user(T))
    day = (local_now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    body = webapi.MeetingIn(chat_id=CHAT_ID, title="Консультация", format="office", place="каб. 214",
                            windows=[webapi.WindowIn(date=day, start="15:00", end="15:30", minutes=10)])
    out = run(loop, webapi.create_meeting(body, teacher))
    assert out["total"] == 3 and out["dispatch"]["total"] == 3 and out["format_text"] == "очно, каб. 214"
    with pytest.raises(HTTPException):  # чужой класс
        run(loop, webapi.create_meeting(webapi.MeetingIn(chat_id=-1, windows=body.windows), teacher))

    parent = run(loop, webapi.chats.upsert_user(PARENTS[0]))
    mine = run(loop, webapi.my_meetings(parent))
    assert len(mine) == 1 and "name" not in mine[0]["slots"][0]  # родителю чужие имена не показываем
    slot_id = mine[0]["slots"][0]["id"]
    res = run(loop, webapi.book_meeting(out["id"], webapi.BookIn(slot_id=slot_id), parent))
    assert res["status"] == "ok" and res["meeting"]["slots"][0]["mine"] is True
    other = run(loop, webapi.chats.upsert_user(PARENTS[1]))
    with pytest.raises(HTTPException) as e:
        run(loop, webapi.book_meeting(out["id"], webapi.BookIn(slot_id=slot_id), other))
    assert e.value.status_code == 409
    teacher_view = run(loop, webapi.list_meetings(teacher))
    assert teacher_view[0]["busy"] == 1 and teacher_view[0]["slots"][0]["name"]

    # утренняя сводка учителя: «Сегодня встречи с родителями» (встреча через 3 минуты — точно сегодня,
    # кроме последних минут суток)
    soon = make_meeting(loop, [future(0.05)])
    run(loop, meetings.book(soon.slots[0].id, P3))
    text, _, _ = run(loop, quiet.build_digest(teacher, include_empty=True))
    if to_local(soon.slots[0].starts_at, TZ).date() == local_now(TZ).date():
        assert "Сегодня встречи с родителями (1)" in text and "Козлова" in text

    # календарь: у родителя — своя встреча с напоминанием, у учителя — слот без имени родителя
    from app.integrations import calendar

    feed_parent = run(loop, calendar.feed_for(parent))
    assert "Встреча с учителем: Консультация" in feed_parent and "TRIGGER:-PT120M" in feed_parent
    feed_teacher = run(loop, calendar.feed_for(teacher))
    assert "🤝 Консультация" in feed_teacher and "Иванова" not in feed_teacher

    assert run(loop, webapi.cancel_meeting(out["id"], parent))["ok"] is True
    run(loop, webapi.close_meeting(out["id"], teacher))
    assert out["id"] not in [x["id"] for x in run(loop, webapi.my_meetings(parent))]  # закрытая — не видна


def test_forget_and_retention(env):
    loop, api, c = env
    connect(loop)
    m = make_meeting(loop, [future(30)])
    run(loop, meetings.book(m.slots[0].id, P1))

    async def past_meeting():
        async with session() as s:
            old = Meeting(teacher_id=T, chat_id=CHAT_ID, title="Старая")
            s.add(old)
            await s.flush()
            s.add(MeetingSlot(meeting_id=old.id, starts_at=utcnow() - timedelta(days=40), user_id=P2, name="Сидоров"))
            return old.id
    old_id = run(loop, past_meeting())
    out = run(loop, retention.cleanup())
    assert out["meetings"] == 1 and run(loop, meetings.get(old_id)) is None and run(loop, meetings.get(m.id))
    res = run(loop, retention.forget(P1))
    assert res["meetings"] == 1 and not any(x.user_id for x in slots(loop, m.id))

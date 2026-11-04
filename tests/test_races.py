"""Двойные нажатия, одновременные события и подсчёты: всё происходит ровно один раз и сходится."""
import asyncio

from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.models import Member, PollTarget
from app.services import chats, polls, shelf

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))


def make_poll(loop, template="consent", amount=None):
    async def go():
        p = await polls.create_poll(TEACHER["user_id"], CHAT_ID, template, "Экскурсия", [], None, amount=amount)
        await polls.send_poll(p.id)
        return p.id
    return run(loop, go())


def test_double_tap_remind_sends_once(env, monkeypatch):
    loop, api, c = env
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: True)
    added(loop)
    pid = make_poll(loop)
    before = len(api.texts_to(user_id=PARENTS[0]["user_id"]))
    run(loop, handle_update(cb_update(TEACHER, f"pr:{pid}", "r1")))
    run(loop, handle_update(cb_update(TEACHER, f"pr:{pid}", "r2")))
    assert len(api.texts_to(user_id=PARENTS[0]["user_id"])) == before + 1


def test_double_tap_send_to_class_posts_once(env):
    loop, api, c = env
    added(loop)
    media = {"type": "file", "payload": {"token": "t"}, "filename": "Памятка.pdf", "key": "k1"}
    m, _ = run(loop, shelf.save(TEACHER["user_id"], media, "Памятка"))
    run(loop, handle_update(cb_update(TEACHER, f"sh:to:{m.id}:{CHAT_ID}", "s1")))
    run(loop, handle_update(cb_update(TEACHER, f"sh:to:{m.id}:{CHAT_ID}", "s2")))
    assert len([s for s in api.sent if s.get("chat_id") == CHAT_ID and s.get("attachments")
                and s["attachments"][0].get("type") == "file"]) == 1


def test_double_tap_paid_keeps_mark(env):
    loop, api, c = env
    added(loop)
    pid = make_poll(loop, "money", 200)
    t = run(loop, polls.get_poll(pid)).targets[0]
    run(loop, handle_update(cb_update(TEACHER, f"pp:{pid}:{t.id}:1:0", "p1")))
    run(loop, handle_update(cb_update(TEACHER, f"pp:{pid}:{t.id}:1:0", "p2")))  # старая клавиатура, второй тап
    assert polls.money(run(loop, polls.get_poll(pid)))["paid"] == 1


def test_close_twice_reports_once(env):
    loop, api, c = env
    added(loop)
    pid = make_poll(loop)

    async def both():
        await asyncio.gather(polls.close_poll(pid), polls.close_poll(pid, by_deadline=True))
    run(loop, both())
    reports = [t for t in api.texts_to(user_id=TEACHER["user_id"]) if "итог" in t]
    assert len(reports) == 1


def test_all_answered_reported_once(env):
    loop, api, c = env
    added(loop)
    pid = make_poll(loop)
    for i, p in enumerate(PARENTS):
        run(loop, polls.record_answer(pid, p, 0))
    poll = run(loop, polls.get_poll(pid))

    async def two_last():
        await asyncio.gather(polls.after_answer(poll, PARENTS[1]["user_id"], 0, "ok"),
                             polls.after_answer(poll, PARENTS[2]["user_id"], 0, "ok"))
    run(loop, two_last())
    assert len([t for t in api.texts_to(user_id=TEACHER["user_id"]) if "Ответили все" in t]) == 1


def test_left_member_is_dropped_from_open_poll(env):
    loop, api, c = env
    added(loop)
    pid = make_poll(loop)
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{pid}:0", "l0")))
    leaver = PARENTS[1]
    run(loop, handle_update({"update_type": "user_removed", "timestamp": 7, "chat_id": CHAT_ID, "user": leaver}))
    poll = run(loop, polls.get_poll(pid))
    assert all(t.user_id != leaver["user_id"] for t in poll.targets)
    assert polls.counts(poll)[:2] == (1, 2)
    before = len(api.texts_to(user_id=leaver["user_id"]))
    run(loop, polls.remind(pid, auto=True))
    assert len(api.texts_to(user_id=leaver["user_id"])) == before


def test_paid_then_skip_still_counted_and_can_be_unmarked(env):
    loop, api, c = env
    added(loop)
    pid = make_poll(loop, "money", 200)
    p0 = PARENTS[0]
    t = next(t for t in run(loop, polls.get_poll(pid)).targets if t.user_id == p0["user_id"])
    run(loop, polls.set_paid(pid, t.id, True))
    run(loop, handle_update(cb_update(p0, f"pa:{pid}:{polls.PAY_SKIP}", "k1")))
    poll = run(loop, polls.get_poll(pid))
    m = polls.money(poll)
    assert m["collected"] == 200 and m["paid"] == 1
    payloads = [b["payload"] for row in polls.pay_buttons(poll) for b in row]
    assert f"pp:{pid}:{t.id}:0:0" in payloads  # отметку можно снять


def test_pay_keyboard_fits_max_row_limit(env):
    loop, api, c = env
    added(loop)
    pid = make_poll(loop, "money", 100)

    async def many():
        async with session() as s:
            for i in range(60):
                s.add(PollTarget(poll_id=pid, user_id=10_000 + i, name=f"Родитель {i:02d}"))
    run(loop, many())
    poll = run(loop, polls.get_poll(pid))
    pages = [polls.pay_buttons(poll, page) for page in range(3)]
    assert all(len(rows) <= 30 for rows in pages)
    names = {b["text"] for rows in pages for row in rows for b in row if b["payload"].startswith("pp:")}
    assert len(names) == 63


def test_empty_member_list_does_not_mark_everyone_gone(env):
    loop, api, c = env
    added(loop)

    async def empty(chat_id, limit=2000):
        return []
    api.get_members = empty
    assert run(loop, chats.sync_members(CHAT_ID)) == -1

    async def present():
        async with session() as s:
            return (await s.scalars(select(Member).where(Member.chat_id == CHAT_ID, Member.present.is_(True)))).all()
    assert len(run(loop, present())) >= len(PARENTS)

"""«Тихий режим» и утренняя сводка: срочное проходит, ничего не теряется, сводка приходит всем."""
from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.models import InboxItem, User
from app.services import quiet
from app.timeutil import local_now

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))


def teacher(loop) -> User:
    async def go():
        async with session() as s:
            return await s.get(User, TEACHER["user_id"])
    return run(loop, go())


def set_teacher(loop, **fields):
    async def go():
        async with session() as s:
            u = await s.get(User, TEACHER["user_id"])
            for k, v in fields.items():
                setattr(u, k, v)
    run(loop, go())


def inbox(loop):
    async def go():
        async with session() as s:
            return (await s.scalars(select(InboxItem).order_by(InboxItem.id))).all()
    return run(loop, go())


def test_urgent_without_question_mark_reaches_teacher_at_night(env, monkeypatch):
    loop, api, c = env
    added(loop)
    monkeypatch.setattr(quiet, "in_work_window", lambda u: False)
    # обычный вопрос ночью — во «входящие», и тот же родитель сразу пишет срочное
    run(loop, handle_update(msg_update(PARENTS[0], "Когда собрание?", chat_id=CHAT_ID, mid="g1")))
    run(loop, handle_update(msg_update(PARENTS[0], "Срочно! Ребёнок упал на перемене", chat_id=CHAT_ID, mid="g2")))
    to_teacher = api.texts_to(user_id=TEACHER["user_id"])
    assert any("Ребёнок упал" in t and "Срочно" in t for t in to_teacher)


def make_items(loop, n, **kw):
    async def go():
        async with session() as s:
            for i in range(n):
                s.add(InboxItem(teacher_id=TEACHER["user_id"], kind=kw.get("kind", "question"),
                                text=f"вопрос {i}", from_name="Иванова А.", ref=kw.get("ref")))
    run(loop, go())


def test_digest_marks_only_shown_items(env):
    loop, api, c = env
    added(loop)
    make_items(loop, 20)
    assert run(loop, quiet.send_digest(teacher(loop)))
    items = inbox(loop)
    assert sum(1 for it in items if it.delivered) == quiet.DIGEST_ITEMS
    assert "и ещё 5" in api.texts_to(user_id=TEACHER["user_id"])[-1]
    run(loop, quiet.send_digest(teacher(loop)))  # остаток приходит следующей сводкой
    assert all(it.delivered for it in inbox(loop))


def test_item_arriving_during_send_is_not_lost(env):
    loop, api, c = env
    added(loop)
    make_items(loop, 2)
    orig = c.safe_send

    async def slow_send(**kw):
        async with session() as s:  # пока сводка «летит», пришёл новый вопрос
            s.add(InboxItem(teacher_id=TEACHER["user_id"], kind="question", text="поздний"))
        return await orig(**kw)
    c.safe_send = slow_send
    assert run(loop, quiet.send_digest(teacher(loop)))
    late = [it for it in inbox(loop) if it.text == "поздний"]
    assert late and not late[0].delivered


def test_failed_digest_does_not_mark_day(env):
    loop, api, c = env
    added(loop)
    now = local_now(teacher(loop).tz)
    set_teacher(loop, weekends_off=False, work_start=max(0, now.hour * 60 + now.minute - 5))
    make_items(loop, 1)
    api.blocked.add(TEACHER["user_id"])
    run(loop, quiet.morning_tick())
    assert teacher(loop).last_digest_on is None
    api.blocked.clear()
    run(loop, c.kv.delete(f"dg:{TEACHER['user_id']}:{now:%Y-%m-%d}"))  # 20 минут прошло
    run(loop, quiet.morning_tick())
    assert teacher(loop).last_digest_on == now.strftime("%Y-%m-%d")


def test_morning_digest_comes_with_quiet_mode_off(env):
    loop, api, c = env
    added(loop)
    now = local_now(teacher(loop).tz)
    set_teacher(loop, quiet_enabled=False, weekends_off=False, work_start=max(0, now.hour * 60 + now.minute - 5))
    make_items(loop, 1)
    run(loop, quiet.morning_tick())
    assert any("вопрос 0" in t for t in api.texts_to(user_id=TEACHER["user_id"]))


def test_night_question_can_be_answered_from_digest(env, monkeypatch):
    loop, api, c = env
    added(loop)
    monkeypatch.setattr(quiet, "in_work_window", lambda u: False)
    run(loop, handle_update(msg_update(PARENTS[0], "Во сколько завтра экскурсия в музей?", mid="n1")))
    run(loop, handle_update(cb_update(PARENTS[0], "fw:ask", "n2")))
    assert inbox(loop)[0].ref
    run(loop, quiet.send_digest(teacher(loop)))
    digest = api.sent[-1]
    buttons = digest["attachments"][0]["payload"]["buttons"]
    reply = next(b for row in buttons for b in row if b["payload"].startswith("fr:"))
    run(loop, handle_update(cb_update(TEACHER, reply["payload"], "n3")))
    run(loop, handle_update(msg_update(TEACHER, "В 10:00 у школы", mid="n4")))
    assert any("В 10:00 у школы" in t for t in api.texts_to(user_id=PARENTS[0]["user_id"]))

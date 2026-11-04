"""Стабильность: цикл получения событий, сбои хранилища состояния, прерванная рассылка, напоминания."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import updates
from app.bot.router import handle_update
from app.db import session
from app.models import Poll, PollTarget, User
from app.services import polls
from app.state import MemoryKV
from app.timeutil import to_utc, utcnow, zone

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))


def set_user(loop, uid, **fields):
    async def go():
        async with session() as s:
            u = await s.get(User, uid)
            for k, v in fields.items():
                setattr(u, k, v)
    run(loop, go())


def set_poll(loop, pid, **fields):
    async def go():
        async with session() as s:
            p = await s.get(Poll, pid)
            for k, v in fields.items():
                setattr(p, k, v)
    run(loop, go())


# ---------------------------------------------------------------- long polling
class FlakyKV(MemoryKV):
    def __init__(self, fail_sets: int = 0, fail_add_once: bool = False):
        super().__init__()
        self.fail_sets = fail_sets
        self.fail_add_once = fail_add_once

    async def set(self, key, value, ttl=3600):
        if key == "updates_marker" and self.fail_sets > 0:
            self.fail_sets -= 1
            raise RuntimeError("redis down")
        await super().set(key, value, ttl)

    async def add_once(self, key, ttl=3600):
        if self.fail_add_once and key.startswith("dd:"):
            raise RuntimeError("redis down")
        return await super().add_once(key, ttl)


def test_polling_survives_kv_errors_and_reports_liveness(env, monkeypatch):
    loop, api, c = env
    c.kv = FlakyKV(fail_sets=2)
    monkeypatch.setattr(updates, "POLL_BACKOFF_MAX", 0)
    calls = {"n": 0}

    async def get_updates(marker, timeout=30):
        await asyncio.sleep(0.2)  # long polling: пока ждём, прошлая пачка успевает обработаться
        # на PostgreSQL обработка дольше 0,2 с — ждём её явно, иначе маркер зависит от скорости машины
        pending = [t for t in updates._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        calls["n"] += 1
        if calls["n"] > 4:
            raise asyncio.CancelledError
        return {"updates": [msg_update(PARENTS[0], "/start", mid=f"p{calls['n']}")], "marker": calls["n"]}
    api.get_updates = get_updates
    assert c.polling_alive_at is None

    async def go():
        try:
            await updates.polling_loop(c)
        except asyncio.CancelledError:
            pass
        await updates.drain(5)
    run(loop, go())
    # цикл не умер на первой ошибке хранилища: дошёл до 4-го запроса и обработал все события
    assert calls["n"] == 5
    assert len(api.texts_to(user_id=PARENTS[0]["user_id"])) >= 4
    assert c.polling_alive_at is not None
    # маркер сохраняется, как только обработана пачка (последняя дообработалась уже при остановке)
    assert run(loop, c.kv.get("updates_marker")) in (3, 4)


def test_safe_marker_waits_for_unfinished_batch(env):
    loop, api, c = env

    async def go():
        slow = asyncio.get_running_loop().create_future()
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        batches = [(10, {done}), (11, {slow})]
        first = updates._safe_marker(batches, 12)
        slow.set_result(None)
        second = updates._safe_marker(batches, 12)
        return first, second
    assert run(loop, go()) == (11, 12)


def test_event_is_handled_when_dedup_store_fails(env):
    loop, api, c = env
    added(loop)
    c.kv = FlakyKV(fail_add_once=True)
    run(loop, handle_update(cb_update(PARENTS[0], "m:help", "kv1")))
    # кнопке ответили, меню пришло — событие не потерялось
    assert any(a["callback_id"] == "kv1" for a in api.answers)
    assert api.texts_to(user_id=PARENTS[0]["user_id"])


# ---------------------------------------------------------------- прерванная рассылка
def test_interrupted_dispatch_is_resumed_without_duplicates(env):
    loop, api, c = env
    added(loop)
    set_user(loop, TEACHER["user_id"], quiet_enabled=False)

    async def interrupted():
        p = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия", [], None)
        # «процесс упал» после первого адресата: цели записаны, одному доставлено, рассылка не закончена
        async with session() as s:
            first = PARENTS[0]
            for par in PARENTS:
                s.add(PollTarget(poll_id=p.id, user_id=par["user_id"], name=par["first_name"],
                                 delivered=par is first, message_id="old" if par is first else None))
            row = await s.get(Poll, p.id)
            row.created_at = utcnow() - timedelta(minutes=5)
        return p.id
    pid = run(loop, interrupted())
    run(loop, polls.tick())
    poll = run(loop, polls.get_poll(pid))
    assert poll.dispatched and poll.group_message_id
    # первому второй раз не писали, остальным дослали
    assert len(api.texts_to(user_id=PARENTS[0]["user_id"])) == 0
    assert all(len(api.texts_to(user_id=p["user_id"])) == 1 for p in PARENTS[1:])
    run(loop, polls.tick())
    assert all(len(api.texts_to(user_id=p["user_id"])) == 1 for p in PARENTS[1:])


def test_fresh_poll_is_not_resumed_by_tick_while_sending(env):
    loop, api, c = env
    added(loop)
    pid = run(loop, polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия", [], None)).id
    run(loop, polls.tick())  # только что созданный — рассылает тот, кто создал
    assert not run(loop, polls.get_poll(pid)).dispatched
    assert not api.texts_to(user_id=PARENTS[0]["user_id"])


# ---------------------------------------------------------------- напоминания
def _local_utc(hour, days=0, tz="Europe/Moscow"):
    now = datetime.now(zone(tz))
    return to_utc((now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0))


def test_plan_moves_night_reminder_to_morning_and_promises_it():
    tz = "Europe/Moscow"
    created = _local_utc(21)  # вечером
    deadline = _local_utc(9, days=1)  # «завтра 9:00»
    plan = polls.reminder_plan(deadline, created, tz)
    assert len(plan) == 1  # 2-часовое в 7:00 — ночь, переносится на 8:00
    local = plan[0].replace(tzinfo=timezone.utc).astimezone(zone(tz))
    assert (local.hour, local.minute) == (8, 0)
    # срок 7:00 — переносить некуда, обещать нечего
    assert polls.reminder_plan(_local_utc(7, days=1), created, tz) == []


def make_poll(loop, deadline, created):
    async def go():
        p = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия", [], deadline)
        await polls.send_poll(p.id)
        async with session() as s:
            (await s.get(Poll, p.id)).created_at = created
        return p.id
    return run(loop, go())


def test_reminder_not_marked_when_nothing_sent(env, monkeypatch):
    loop, api, c = env
    added(loop)
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: True)
    now = utcnow()
    pid = make_poll(loop, now + timedelta(hours=1), now - timedelta(hours=10))
    api.blocked.update(p["user_id"] for p in PARENTS)
    orig = c.safe_send

    async def chat_down(**kw):
        return None if kw.get("chat_id") else await orig(**kw)
    c.safe_send = chat_down  # и в чат класса не получилось
    run(loop, polls.tick())
    p = run(loop, polls.get_poll(pid))
    assert not p.reminded_2h  # ничего не ушло — отметки нет, будет новая попытка


def test_single_reminder_after_downtime(env, monkeypatch):
    loop, api, c = env
    added(loop)
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: True)
    now = utcnow()
    # сбор на трое суток, сервис лежал — до срока остался час: должно уйти одно напоминание, а не два
    pid = make_poll(loop, now + timedelta(hours=1), now - timedelta(days=3))
    before = len(api.texts_to(user_id=PARENTS[0]["user_id"]))
    run(loop, polls.tick())
    run(loop, polls.tick())
    after = api.texts_to(user_id=PARENTS[0]["user_id"])[before:]
    assert len(after) == 1 and "Напоминание" in after[0]
    p = run(loop, polls.get_poll(pid))
    assert p.reminded_24h and p.reminded_2h


def test_night_reminder_waits_for_morning(env, monkeypatch):
    loop, api, c = env
    added(loop)
    now = utcnow()
    pid = make_poll(loop, now + timedelta(hours=1, minutes=30), now - timedelta(hours=10))
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: False)
    run(loop, polls.tick())
    assert not run(loop, polls.get_poll(pid)).reminded_2h  # ночью не отправлено и не «съедено»
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: True)
    run(loop, polls.tick())
    assert run(loop, polls.get_poll(pid)).reminded_2h
    assert "Напоминание" in api.texts_to(user_id=PARENTS[0]["user_id"])[-1]


def test_confirm_text_uses_real_plan(env):
    loop, api, c = env
    added(loop)
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="c1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:consent", "c2")))
    run(loop, handle_update(msg_update(TEACHER, "Согласие на поездку", mid="c3")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:none", "c4")))
    run(loop, handle_update(cb_update(TEACHER, "f:send", "c5")))
    texts = "\n".join(api.texts_to(user_id=TEACHER["user_id"]))
    assert "за сутки и за 2 часа" not in texts and "Срока нет" in texts

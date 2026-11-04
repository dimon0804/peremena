"""Масштабирование: лидер планировщика, общий лимитер MAX API, распределённые замки.

Офлайн-тесты идут на поддельном Redis. С настоящим Redis (TEST_REDIS_URL=redis://…) дополнительно
проверяются Lua-скрипты лимитера и замков; с TEST_DATABASE_URL=postgresql://… — advisory-замок лидера.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from app import leader as leader_mod
from app.leader import Leader, _AlwaysBackend, _PgBackend, _RedisBackend, make_backend
from app.max_api import SharedRateLimiter, _RateLimiter
from app.state import MemoryKV

REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
PG_URL = os.environ.get("TEST_DATABASE_URL", "") if "postgres" in os.environ.get("TEST_DATABASE_URL", "") else ""


class FakeRedis:
    """Минимум Redis для замков: SET NX PX, GET, DEL и два Lua-скрипта (продлить / снять своё)."""

    def __init__(self) -> None:
        self.d: dict[str, tuple[str, float]] = {}
        self.down = False

    def _alive(self, k):
        v = self.d.get(k)
        if v and v[1] < time.monotonic():
            self.d.pop(k, None)
            return None
        return v

    def _check(self):
        if self.down:
            raise ConnectionError("redis down")

    async def set(self, k, v, px=None, nx=False, ex=None):
        self._check()
        if nx and self._alive(k):
            return None
        ttl = px / 1000 if px else (ex or 3600)
        self.d[k] = (str(v), time.monotonic() + ttl)
        return True

    async def get(self, k):
        self._check()
        v = self._alive(k)
        return v[0] if v else None

    async def eval(self, script, n, key, token, *args):
        self._check()
        v = self._alive(key)
        if not v or v[0] != token:
            return 0
        if script == leader_mod._EXTEND:
            self.d[key] = (v[0], time.monotonic() + int(args[0]) / 1000)
            return 1
        if script == leader_mod._RELEASE:
            self.d.pop(key, None)
            return 1
        raise AssertionError("неизвестный скрипт")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- лидер
def test_only_one_leader_and_takeover_after_stop():
    async def go():
        r = FakeRedis()
        events = []
        a = Leader(_RedisBackend(r, "pm:leader:t", 5), ttl=5, on_elected=lambda: events.append("a+"),
                   on_demoted=lambda: events.append("a-"))
        b = Leader(_RedisBackend(r, "pm:leader:t", 5), ttl=5, on_elected=lambda: events.append("b+"))
        assert await a.step() is True
        assert await b.step() is False  # место занято — второй экземпляр планировщик не запускает
        assert await a.step() is True   # лидер продлевает свой замок
        assert await b.step() is False
        await a.stop()                  # остановка: место освобождается сразу, а не через срок замка
        assert await b.step() is True
        assert events == ["a+", "a-", "b+"]
    run(go())


def test_leader_expires_when_process_hangs():
    async def go():
        r = FakeRedis()
        a = Leader(_RedisBackend(r, "k", 0.2), ttl=0.2)
        b = Leader(_RedisBackend(r, "k", 0.2), ttl=0.2)
        assert await a.step() and not await b.step()
        await asyncio.sleep(0.3)        # лидер завис и не продлевал замок
        assert await b.step() is True
        assert await a.step() is False  # очнувшийся бывший лидер видит, что замок чужой, и уступает
        assert not a.is_leader
    run(go())


def test_leader_keeps_role_briefly_when_redis_blinks_then_steps_down():
    async def go():
        r = FakeRedis()
        demoted = []
        a = Leader(_RedisBackend(r, "k", 2), ttl=2, on_demoted=lambda: demoted.append(1))
        assert await a.step()
        r.down = True
        assert await a.step() is True     # срок замка ещё не вышел — никто другой его не возьмёт
        a._valid_until = time.monotonic()  # срок вышел, а Redis всё ещё недоступен
        assert await a.step() is False and demoted == [1]
        b = Leader(_RedisBackend(r, "k", 2), ttl=2)
        assert await b.step() is False     # без Redis лидера нет ни у кого: лучше пауза, чем два лидера
    run(go())


def test_scheduler_jobs_run_only_on_leader(monkeypatch):
    from app import updates

    calls = []

    async def job():
        calls.append(1)

    async def go():
        wrapped = updates.leader_only(job)
        r = FakeRedis()
        a = Leader(_RedisBackend(r, "k", 5), ttl=5)
        b = Leader(_RedisBackend(r, "k", 5), ttl=5)
        await a.step()
        await b.step()
        monkeypatch.setattr(updates, "current_leader", b)
        await wrapped()
        assert calls == []          # не лидер — задача планировщика ничего не делает
        monkeypatch.setattr(updates, "current_leader", a)
        await wrapped()
        assert calls == [1]
    run(go())


def test_background_pauses_scheduler_and_polling_on_demotion(env, monkeypatch):
    from app import updates

    loop, api, c = env
    started = []

    async def fake_polling(ctx):
        started.append(1)
        await asyncio.sleep(3600)
    monkeypatch.setattr(updates, "polling_loop", fake_polling)

    async def go():
        bg = updates.Background(c, updates.make_scheduler(c), polling=True, subscribe_webhook=False)
        r = FakeRedis()
        lead = Leader(_RedisBackend(r, "k", 5), ttl=5, on_elected=bg.on_elected, on_demoted=bg.on_demoted)
        await lead.step()
        await asyncio.sleep(0)
        assert started == [1] and bg.polling_ok() is True and bg.sched.state == 1  # STATE_RUNNING
        r.d.clear()
        await r.set("k", "чужой", px=5000)  # замок перехватил другой экземпляр
        await lead.step()
        assert bg.polling_task is None and bg.sched.state == 2  # STATE_PAUSED
        bg.shutdown()
    loop.run_until_complete(go())


def test_make_backend_choice(env):
    loop, api, c = env
    from app import db

    assert isinstance(make_backend(MemoryKV(), None), _AlwaysBackend)
    expected = _PgBackend if db.dialect() == "postgresql" else _AlwaysBackend
    assert isinstance(make_backend(MemoryKV(), db.engine()), expected)

    class K(MemoryKV):
        redis = FakeRedis()
    assert isinstance(make_backend(K(), db.engine(), "tok"), _RedisBackend)


@pytest.mark.skipif(not PG_URL, reason="нужен TEST_DATABASE_URL=postgresql://…")
def test_postgres_advisory_leader():
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import _normalize_url

    async def go():
        engine = create_async_engine(_normalize_url(PG_URL))
        try:
            a = Leader(_PgBackend(engine, "t"), ttl=5)
            b = Leader(_PgBackend(engine, "t"), ttl=5)
            assert await a.step() is True
            assert await b.step() is False
            assert await a.step() is True
            await a.stop()
            assert await b.step() is True
            await b.stop()
        finally:
            await engine.dispose()
    run(go())


# ---------------------------------------------------------------- лимитер MAX API
def test_shared_limiter_falls_back_to_local_when_redis_down():
    class Down:
        async def eval(self, *a, **k):
            raise ConnectionError("redis down")

    async def go():
        lim = SharedRateLimiter(Down(), "tok", rps=20)
        t = time.monotonic()
        for _ in range(11):
            await lim.wait()
        return time.monotonic() - t
    # локальный лимит 20 в секунду: 11 запросов — не быстрее ~0,5 с, и ни одной ошибки
    assert run(go()) >= 0.45


def test_local_limiter_keeps_rps():
    async def go():
        lim = _RateLimiter(rps=50)
        t = time.monotonic()
        for _ in range(26):
            await lim.wait()
        return time.monotonic() - t
    assert 0.45 <= run(go()) < 2.0  # верх с запасом: на загруженной машине asyncio.sleep опаздывает


@pytest.mark.skipif(not REDIS_URL, reason="нужен TEST_REDIS_URL=redis://…")
def test_shared_limiter_is_one_limit_for_all_instances():
    import redis.asyncio as redis

    async def go():
        clients = [redis.from_url(REDIS_URL, decode_responses=True) for _ in range(3)]
        token = f"t{time.time()}"
        # три «экземпляра» с общим лимитом 30 в секунду
        lims = [SharedRateLimiter(r, token, rps=30) for r in clients]
        stamps: list[float] = []

        async def worker(lim, n):
            for _ in range(n):
                await lim.wait()
                stamps.append(time.monotonic())
        t = time.monotonic()
        await asyncio.gather(*(worker(lim, 20) for lim in lims))
        total = time.monotonic() - t
        for r in clients:
            await r.aclose()
        return total, sorted(stamps)
    total, stamps = run(go())
    # 60 запросов при общем лимите 30/с — около 2 с (каждый экземпляр отдельно уложился бы в 0,7 с)
    assert total >= 1.8
    # в любом окне в 1 с — не больше 30 запросов (+1 на границе)
    worst = max(sum(1 for s in stamps if t0 <= s < t0 + 1.0) for t0 in stamps)
    assert worst <= 31


@pytest.mark.skipif(not REDIS_URL, reason="нужен TEST_REDIS_URL=redis://…")
def test_shared_limiter_pause_after_429_is_global():
    import redis.asyncio as redis

    async def go():
        r1, r2 = redis.from_url(REDIS_URL, decode_responses=True), redis.from_url(REDIS_URL, decode_responses=True)
        token = f"p{time.time()}"
        a, b = SharedRateLimiter(r1, token, rps=100), SharedRateLimiter(r2, token, rps=100)
        a.pause(0.5)            # первый экземпляр получил 429
        await asyncio.sleep(0.05)
        t = time.monotonic()
        await b.wait()          # второй тоже ждёт общую паузу
        waited = time.monotonic() - t
        await r1.aclose()
        await r2.aclose()
        return waited
    assert run(go()) >= 0.35


@pytest.mark.skipif(not REDIS_URL, reason="нужен TEST_REDIS_URL=redis://…")
def test_redis_lock_serializes_across_instances():
    from app.state import RedisKV

    async def go():
        k1, k2 = RedisKV(REDIS_URL), RedisKV(REDIS_URL)  # как два процесса: у каждого свой клиент
        inside, peak, order = 0, 0, []
        key = f"actor:u{time.time()}"

        async def work(kv, name):
            nonlocal inside, peak
            async with kv.lock(key, ttl=5, wait=5) as got:
                assert got
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.05)
                order.append(name)
                inside -= 1
        await asyncio.gather(*(work(k1 if i % 2 else k2, i) for i in range(6)))
        await k1.close()
        await k2.close()
        return peak, order
    peak, order = run(go())
    assert peak == 1 and len(order) == 6


@pytest.mark.skipif(not REDIS_URL, reason="нужен TEST_REDIS_URL=redis://…")
def test_redis_leader_real():
    import redis.asyncio as redis

    async def go():
        r1, r2 = redis.from_url(REDIS_URL, decode_responses=True), redis.from_url(REDIS_URL, decode_responses=True)
        key = f"pm:leader:test:{time.time()}"
        a, b = Leader(_RedisBackend(r1, key, 1), ttl=1), Leader(_RedisBackend(r2, key, 1), ttl=1)
        assert await a.step() and not await b.step()
        await asyncio.sleep(0.5)
        assert await a.step() and not await b.step()  # продление работает
        await a.stop()
        assert await b.step()
        await b.stop()
        await r1.aclose()
        await r2.aclose()
    run(go())


# ---------------------------------------------------------------- рассылка сбора — в одном месте
def test_dispatch_is_single_across_instances(env):
    """Отметка «рассылается» лежит в общем хранилище: второй экземпляр (досылка в планировщике)
    не начинает тот же сбор параллельно, а после окончания рассылки отметка снимается."""
    from app.bot.router import handle_update
    from app.services import polls
    from tests.conftest import BOT, CHAT_ID, TEACHER

    loop, api, c = env

    async def go():
        await handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER})
        p = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия", [], None)
        await c.kv.set(polls.DISPATCH_KEY.format(p.id), 1, 60)  # «другой экземпляр» уже рассылает
        assert (await polls.send_poll(p.id)).get("busy")
        await c.kv.delete(polls.DISPATCH_KEY.format(p.id))
        rep = await polls.send_poll(p.id)
        assert not rep.get("busy") and rep["delivered"] == 3
        assert await c.kv.get(polls.DISPATCH_KEY.format(p.id)) is None
        assert BOT["user_id"] not in {s.get("user_id") for s in api.sent}
    loop.run_until_complete(go())


def test_tick_reads_only_due_polls(env):
    """tick не читает все открытые сборы: только просроченные и те, кому пора напоминать."""
    from datetime import timedelta

    from app.services import polls
    from app.timeutil import utcnow
    from tests.conftest import CHAT_ID, TEACHER

    loop, api, c = env

    async def go():
        now = utcnow()
        far = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Далёкий", [], now + timedelta(days=10))
        soon = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Скоро", [], now + timedelta(hours=1))
        late = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Просрочен", [], now - timedelta(minutes=1))
        from app.db import session
        from app.models import Poll
        async with session() as s:
            for pid in (far.id, soon.id, late.id):
                (await s.get(Poll, pid)).dispatched = True
        ids = [p.id async for p in polls.due_polls(now, batch=1)]  # пачки по одному — проверяем курсор
        assert ids == [late.id, soon.id]
    loop.run_until_complete(go())


# ---------------------------------------------------------------- очередь событий (Redis Streams)
@pytest.mark.skipif(not REDIS_URL, reason="нужен TEST_REDIS_URL=redis://…")
def test_event_queue_each_event_once_across_consumers():
    import redis.asyncio as redis

    from app import events

    async def go():
        stream = f"pm:test:{time.time()}"
        r_web, r1, r2 = (redis.from_url(REDIS_URL, decode_responses=True) for _ in range(3))
        web = events.EventQueue(r_web, stream=stream)
        q1, q2 = events.EventQueue(r1, stream=stream), events.EventQueue(r2, stream=stream)
        q1.consumer, q2.consumer = "c1", "c2"
        seen: list[tuple[str, int]] = []

        def handler(name):
            async def h(update):
                await asyncio.sleep(0.001)
                seen.append((name, update["n"]))
            return h
        await web.ensure_group()
        for n in range(300):
            await web.publish({"update_type": "x", "n": n})
        tasks = [asyncio.create_task(q1.consume(handler("c1"), 8)), asyncio.create_task(q2.consume(handler("c2"), 8))]
        for _ in range(200):
            if len(seen) >= 300:
                break
            await asyncio.sleep(0.05)
        for q in (q1, q2):
            await q.drain()
        for t in tasks:
            t.cancel()
        backlog = await web.backlog()
        await r_web.delete(stream)
        for r in (r_web, r1, r2):
            await r.aclose()
        return seen, backlog
    seen, backlog = run(go())
    assert sorted(n for _, n in seen) == list(range(300))  # ни потерь, ни дублей
    assert {name for name, _ in seen} == {"c1", "c2"}       # работу поделили оба обработчика
    assert backlog.get("pending") == 0                      # всё подтверждено (XACK)


@pytest.mark.skipif(not REDIS_URL, reason="нужен TEST_REDIS_URL=redis://…")
def test_event_queue_reclaims_events_of_dead_consumer(monkeypatch):
    import redis.asyncio as redis

    from app import events

    monkeypatch.setattr(events, "RECLAIM_IDLE_MS", 100)
    monkeypatch.setattr(events, "RECLAIM_EVERY_S", 0)

    async def go():
        stream = f"pm:test:{time.time()}"
        r = redis.from_url(REDIS_URL, decode_responses=True)
        q = events.EventQueue(r, stream=stream)
        await q.ensure_group()
        for n in range(5):
            await q.publish({"n": n})
        await r.xreadgroup(events.GROUP, "dead", {stream: ">"}, count=5)  # «умерший» взял и не подтвердил
        await asyncio.sleep(0.2)
        got = []

        async def h(update):
            got.append(update["n"])
        q.consumer = "alive"
        task = asyncio.create_task(q.consume(h, 4))
        for _ in range(100):
            if len(got) >= 5:
                break
            await asyncio.sleep(0.05)
        await q.drain()
        task.cancel()
        await r.delete(stream)
        await r.aclose()
        return sorted(got)
    assert run(go()) == [0, 1, 2, 3, 4]


def test_webhook_goes_to_queue_and_falls_back_when_redis_down(env, monkeypatch):
    """Вебхук с очередью только публикует; если Redis недоступен — событие обрабатывается на месте."""
    from app import updates

    loop, api, c = env
    published, handled = [], []

    class Q:
        fail = False

        async def publish(self, update):
            if self.fail:
                raise ConnectionError("redis down")
            published.append(update)

    async def fake_handle(update):
        handled.append(update)
    monkeypatch.setattr(updates, "queue", Q())
    monkeypatch.setattr(updates, "handle_update", fake_handle)

    async def go():
        await updates.submit({"update_type": "a"})
        updates.queue.fail = True
        await updates.submit({"update_type": "b"})
    loop.run_until_complete(go())
    assert [u["update_type"] for u in published] == ["a"]
    assert [u["update_type"] for u in handled] == ["b"]

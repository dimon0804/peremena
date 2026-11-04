"""Шлюз событий: dead-letter, повторы, backpressure, метрики, /health/ready, переподписка вебхука, CLI.

Очередь проверяется на маленьком поддельном Redis Streams (ниже, без зависимостей) и — если задан
TEST_REDIS_URL=redis://… — те же сценарии на настоящем Redis: счётчик доставок XPENDING и XAUTOCLAIM
должен вести себя так, как на него рассчитывает dead-letter.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from app import events, metrics

REDIS_URL = os.environ.get("TEST_REDIS_URL", "")


# ---------------------------------------------------------------- поддельный Redis Streams
def _id(s: str) -> tuple[int, int]:
    a, _, b = s.partition("-")
    return int(a), int(b or 0)


class FakeStreams:
    """Ровно то подмножество Redis, которым пользуются app/events.py и app/metrics.py."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.groups: dict[tuple[str, str], dict] = {}
        self.hashes: dict[str, dict] = {}
        self.keys: dict[str, str] = {}
        self._seq = 0

    # потоки
    async def xadd(self, name, fields, maxlen=None, approximate=True):
        self._seq += 1
        mid = f"{int(time.time() * 1000)}-{self._seq}"
        self.streams.setdefault(name, []).append((mid, {k: str(v) for k, v in fields.items()}))
        return mid

    async def xlen(self, name):
        return len(self.streams.get(name, []))

    async def xdel(self, name, *ids):
        before = len(self.streams.get(name, []))
        self.streams[name] = [m for m in self.streams.get(name, []) if m[0] not in ids]
        return before - len(self.streams[name])

    def _range(self, name, lo, hi):
        lo = (0, 0) if lo == "-" else _id(lo)
        hi = (1 << 62, 0) if hi == "+" else _id(hi)
        return [m for m in self.streams.get(name, []) if lo <= _id(m[0]) <= hi]

    async def xrange(self, name, min="-", max="+", count=None):
        rows = self._range(name, min, max)
        return rows[:count] if count else rows

    async def xrevrange(self, name, max="+", min="-", count=None):
        rows = list(reversed(self._range(name, min, max)))
        return rows[:count] if count else rows

    async def xgroup_create(self, name, group, id="0", mkstream=False):
        if (name, group) in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.streams.setdefault(name, [])
        self.groups[(name, group)] = {"last": (0, 0), "pel": {}}

    async def xreadgroup(self, group, consumer, streams, count=None, block=None):
        (name, _), = streams.items()
        g = self.groups.get((name, group))
        if g is None:
            raise RuntimeError("NOGROUP No such key or consumer group")
        new = [m for m in self.streams.get(name, []) if _id(m[0]) > g["last"]][:count]
        if not new:
            await asyncio.sleep(0.005)
            return []
        for mid, _f in new:
            g["pel"][mid] = [consumer, time.monotonic(), 1]
            g["last"] = _id(mid)
        return [[name, new]]

    async def xack(self, name, group, *ids):
        g = self.groups[(name, group)]
        return sum(1 for i in ids if g["pel"].pop(i, None) is not None)

    async def xpending_range(self, name, group, min, max, count, consumername=None, idle=None):
        g = self.groups[(name, group)]
        lo = (0, 0) if min == "-" else _id(min)
        hi = (1 << 62, 0) if max == "+" else _id(max)
        now = time.monotonic()
        rows = [{"message_id": mid, "consumer": c, "time_since_delivered": int((now - t) * 1000),
                 "times_delivered": n}
                for mid, (c, t, n) in sorted(g["pel"].items(), key=lambda kv: _id(kv[0])) if lo <= _id(mid) <= hi]
        return rows[:count]

    async def xautoclaim(self, name, group, consumer, min_idle_time, start_id="0-0", count=None):
        g = self.groups[(name, group)]
        now = time.monotonic()
        data = dict(self.streams.get(name, []))
        out = []
        for mid, e in sorted(g["pel"].items(), key=lambda kv: _id(kv[0])):
            if count and len(out) >= count:
                break
            if (now - e[1]) * 1000 >= min_idle_time:
                e[0], e[1], e[2] = consumer, now, e[2] + 1
                out.append((mid, data.get(mid)))
        return ["0-0", out, []]

    async def xinfo_groups(self, name):
        out = []
        for (s, gname), g in self.groups.items():
            if s == name:
                lag = sum(1 for m in self.streams.get(s, []) if _id(m[0]) > g["last"])
                consumers = len({e[0] for e in g["pel"].values()})
                out.append({"name": gname, "pending": len(g["pel"]), "lag": lag, "consumers": consumers})
        return out

    # хеши и ключи (метрики, дедупликация)
    async def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value

    async def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    async def hdel(self, name, *keys):
        for k in keys:
            self.hashes.get(name, {}).pop(k, None)

    async def delete(self, *names):
        for n in names:
            self.keys.pop(n, None)
            self.streams.pop(n, None)

    async def aclose(self):
        pass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(params=["fake", "real"])
def redis_factory(request):
    """Фабрика клиентов Redis: поддельный — всегда, настоящий — при TEST_REDIS_URL."""
    if request.param == "real":
        if not REDIS_URL:
            pytest.skip("нужен TEST_REDIS_URL=redis://…")
        import redis.asyncio as redis

        return lambda: redis.from_url(REDIS_URL, decode_responses=True)
    fake = FakeStreams()
    return lambda: fake


@pytest.fixture(autouse=True)
def fast_reclaim(monkeypatch):
    monkeypatch.setattr(events, "RECLAIM_EVERY_S", 0)
    metrics.REG.reset()


async def _until(cond, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if await cond():
            return True
        await asyncio.sleep(0.02)
    return False


def _queue(r, **kw):
    q = events.EventQueue(r, stream=f"pm:test:{time.time_ns()}", **kw)
    q.consumer = "c1"
    return q


async def _cleanup(r, q):
    await r.delete(q.stream, q.dead)
    await r.aclose()


# ---------------------------------------------------------------- dead-letter
def test_failing_event_retried_then_dead_and_stops(redis_factory):
    """Обработка падает всегда: ровно EVENT_MAX_ATTEMPTS попыток, затем dead-letter с причиной — и больше не крутится."""
    async def go():
        r = redis_factory()
        q = _queue(r, max_attempts=3, reclaim_idle_ms=50)
        calls = []

        async def handler(update):
            calls.append(update["n"])
            raise RuntimeError("база недоступна")
        await q.ensure_group()
        await q.publish({"update_type": "message_created", "n": 1})
        task = asyncio.create_task(q.consume(handler, 4))
        assert await _until(lambda: _dead_len(r, q, 1))
        await asyncio.sleep(0.3)  # не крутится дальше
        await q.drain()
        task.cancel()
        st = await q.stats()
        dead = await q.dead_entries()
        await _cleanup(r, q)
        return calls, st, dead
    calls, st, dead = run(go())
    assert calls == [1, 1, 1]
    assert st["pending"] == 0 and st["dead"] == 1
    assert dead[0]["attempts"] == "3" and "RuntimeError: база недоступна" in dead[0]["reason"]
    assert dead[0]["update_type"] == "message_created"
    snap = metrics.REG.snapshot()["c"]
    assert snap[metrics._key("peremena_events_failed_total", {})] == 3
    assert snap[metrics._key("peremena_events_dead_total", {})] == 1


async def _dead_len(r, q, n):
    return await r.xlen(q.dead) >= n


def test_transient_failure_recovers_without_dead(redis_factory):
    async def go():
        r = redis_factory()
        q = _queue(r, max_attempts=5, reclaim_idle_ms=50)
        calls, done = [], []

        async def handler(update):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("временный сбой")
            done.append(update["n"])
        await q.ensure_group()
        await q.publish({"update_type": "x", "n": 7})
        task = asyncio.create_task(q.consume(handler, 4))
        assert await _until(lambda: _true(bool(done)))
        await q.drain()
        task.cancel()
        st = await q.stats()
        await _cleanup(r, q)
        return calls, done, st
    calls, done, st = run(go())
    assert len(calls) == 3 and done == [7]
    assert st["pending"] == 0 and st["dead"] == 0


async def _true(v):
    return v


def test_event_that_kills_consumers_goes_dead_without_handling(redis_factory):
    """Событие брали и не подтверждали (процесс падал на нём) больше EVENT_MAX_ATTEMPTS раз —
    живой обработчик по счётчику доставок отправляет его в dead-letter, не запуская обработку снова."""
    async def go():
        r = redis_factory()
        q = _queue(r, max_attempts=2, reclaim_idle_ms=200)
        await q.ensure_group()
        await q.publish({"update_type": "x", "n": 1})
        await r.xreadgroup(q.group, "dead-1", {q.stream: ">"}, count=10)               # доставка 1
        await r.xautoclaim(q.stream, q.group, "dead-2", min_idle_time=0, start_id="0-0")  # доставка 2
        await asyncio.sleep(0.25)  # «dead-2» тоже умер: событие простаивает дольше EVENT_RECLAIM_S
        calls = []

        async def handler(update):
            calls.append(update)
        task = asyncio.create_task(q.consume(handler, 4))                                  # доставка 3 > 2
        assert await _until(lambda: _dead_len(r, q, 1))
        await q.drain()
        task.cancel()
        dead = await q.dead_entries()
        st = await q.stats()
        await _cleanup(r, q)
        return calls, dead, st
    calls, dead, st = run(go())
    assert calls == []
    assert dead[0]["attempts"] == "3" and "превышено число доставок" in dead[0]["reason"]
    assert st["pending"] == 0


def test_reclaimed_event_already_acked_is_skipped(redis_factory):
    """Событие подобрали, а другой обработчик тем временем его подтвердил — повторно не обрабатываем."""
    async def go():
        r = redis_factory()
        q = _queue(r, max_attempts=5)
        await q.ensure_group()
        await q.publish({"update_type": "x", "n": 1})
        res = await r.xreadgroup(q.group, "other", {q.stream: ">"}, count=1)
        msg_id, fields = res[0][1][0]
        await r.xack(q.stream, q.group, msg_id)  # «other» успел закончить
        calls = []

        async def handler(update):
            calls.append(update)
        slots = asyncio.Semaphore(1)
        await slots.acquire()
        await q._handle(msg_id, fields, handler, slots, reclaimed=True)
        st = await q.stats()
        await _cleanup(r, q)
        return calls, st
    calls, st = run(go())
    assert calls == [] and st["dead"] == 0


def test_unparsable_event_goes_dead_at_once(redis_factory):
    async def go():
        r = redis_factory()
        q = _queue(r, max_attempts=5)
        await q.ensure_group()
        await r.xadd(q.stream, {"u": "{не json"})
        calls = []

        async def handler(update):
            calls.append(update)
        task = asyncio.create_task(q.consume(handler, 4))
        assert await _until(lambda: _dead_len(r, q, 1))
        await q.drain()
        task.cancel()
        dead = await q.dead_entries()
        await _cleanup(r, q)
        return calls, dead
    calls, dead = run(go())
    assert calls == [] and "JSON" in dead[0]["reason"]


def test_replay_dead_returns_events_and_forgets_dedup(redis_factory):
    async def go():
        r = redis_factory()
        q = _queue(r, max_attempts=1, reclaim_idle_ms=50)
        broken = True
        done = []

        async def handler(update):
            if broken:
                raise RuntimeError("ошибка в коде")
            done.append(update["n"])
        await q.ensure_group()
        for n in (1, 2):
            await q.publish({"update_type": "x", "n": n})
        task = asyncio.create_task(q.consume(handler, 4))
        assert await _until(lambda: _dead_len(r, q, 2))
        broken = False  # «выкатили исправление»
        forgotten = []

        async def forget(update):
            forgotten.append(update["n"])
        moved = await q.replay_dead(forget=forget)
        assert await _until(lambda: _true(len(done) == 2))
        await q.drain()
        task.cancel()
        st = await q.stats()
        await _cleanup(r, q)
        return moved, forgotten, sorted(done), st
    moved, forgotten, done, st = run(go())
    assert moved == 2 and sorted(forgotten) == [1, 2] and done == [1, 2]
    assert st["dead"] == 0 and st["pending"] == 0


def test_handle_update_failure_clears_dedup_mark(env, monkeypatch):
    """Сбой до разбора события снимает отметку «обработано» — повтор из очереди не будет отброшен как дубль."""
    from app.bot import router
    from tests.conftest import PARENTS, msg_update

    loop, api, c = env

    def broken_lock(*a, **kw):
        raise ConnectionError("замок недоступен")
    monkeypatch.setattr(c.kv, "lock", broken_lock)
    upd = msg_update(PARENTS[0], "Привет", mid="dd-1")

    async def go():
        with pytest.raises(ConnectionError):
            await router.handle_update(upd)
        return await c.kv.add_once("dd:" + router._dedup_key(upd))
    assert loop.run_until_complete(go()) is True  # отметки нет — повтор обработается


# ---------------------------------------------------------------- backpressure
def test_backpressure_webhook_still_200_with_warning(env, monkeypatch, caplog):
    from app import main, updates

    loop, api, c = env
    r = FakeStreams()
    q = _queue(r, backlog_warn=5)
    monkeypatch.setattr(updates, "queue", q)
    monkeypatch.setattr(main, "get_settings", lambda: c.settings)
    secret = c.settings.effective_webhook_secret.encode()

    async def go():
        await q.ensure_group()
        codes = []
        for n in range(10):
            body = json.dumps({"update_type": "message_created", "n": n}).encode()
            codes.append((await main.accept_webhook(secret, body))[0])
            q._backlog_at = 0  # не ждать секунду между оценками длины очереди
        return codes, await r.xlen(q.stream)
    with caplog.at_level("WARNING"):
        codes, length = loop.run_until_complete(go())
    assert codes == [200] * 10 and length == 10  # ничего не потеряно и MAX не будет повторять
    assert any("EVENT_BACKLOG_WARN" in rec.getMessage() for rec in caplog.records)
    snap = metrics.REG.snapshot()["c"]
    assert snap[metrics._key("peremena_webhook_backpressure_total", {})] == 10 - 5
    assert snap[metrics._key("peremena_events_received_total", {"source": "webhook"})] == 10


# ---------------------------------------------------------------- метрики
def test_metrics_render_prometheus_text():
    metrics.REG.reset()
    metrics.inc("peremena_events_processed_total")
    metrics.inc("peremena_max_requests_total", result="429")
    for v in (0.003, 0.2, 7.0):
        metrics.observe("peremena_event_handle_seconds", v)
    text = metrics.render({"w1": metrics.REG.snapshot()}, {"peremena_queue_lag": 12, "peremena_queue_dead": None})
    lines = text.splitlines()
    assert "# TYPE peremena_event_handle_seconds histogram" in lines
    assert 'peremena_events_processed_total{proc="w1"} 1' in lines
    assert 'peremena_max_requests_total{result="429",proc="w1"} 1' in lines
    assert 'peremena_event_handle_seconds_bucket{proc="w1",le="0.005"} 1' in lines
    assert 'peremena_event_handle_seconds_bucket{proc="w1",le="0.25"} 2' in lines   # корзины накопительные
    assert 'peremena_event_handle_seconds_bucket{proc="w1",le="+Inf"} 3' in lines
    assert 'peremena_event_handle_seconds_count{proc="w1"} 3' in lines
    assert "peremena_queue_lag 12" in lines
    assert not any(x.startswith("peremena_queue_dead") for x in lines)  # неизвестное не выдумываем


def test_metrics_collect_merges_live_processes_and_drops_stale():
    r = FakeStreams()

    async def go():
        await r.hset(metrics.HASH, "other", json.dumps({"t": time.time(), "c": {}, "h": {}, "g": {}}))
        await r.hset(metrics.HASH, "gone", json.dumps({"t": time.time() - 3600, "c": {}, "h": {}, "g": {}}))
        snaps = await metrics.collect(r)
        return set(snaps), set((await r.hgetall(metrics.HASH)))
    procs, left = run(go())
    assert procs == {"other", metrics.PROC}
    assert "gone" not in left


def test_metrics_access_private_or_token(monkeypatch):
    from starlette.requests import Request

    from app import main
    from app.config import Settings

    def req(host, headers=()):
        return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers],
                        "client": (host, 5000), "method": "GET", "path": "/metrics"})
    monkeypatch.setattr(main, "get_settings", lambda: Settings(metrics_token="s3cret-token"))
    assert main._metrics_allowed(req("10.0.0.5"))
    assert main._metrics_allowed(req("172.18.0.3"))
    assert main._metrics_allowed(req("127.0.0.1"))
    assert not main._metrics_allowed(req("95.24.1.7"))
    assert not main._metrics_allowed(req("95.24.1.7", [("authorization", "Bearer wrong")]))
    assert main._metrics_allowed(req("95.24.1.7", [("authorization", "Bearer s3cret-token")]))
    assert main._metrics_allowed(req("95.24.1.7", [("x-metrics-token", "s3cret-token")]))
    monkeypatch.setattr(main, "get_settings", lambda: Settings(metrics_token=""))
    assert not main._metrics_allowed(req("95.24.1.7", [("authorization", "Bearer ")]))


def test_metrics_and_ready_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-token-1")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    try:
        with TestClient(app) as client:
            assert client.get("/metrics").status_code == 404  # клиент «testclient» — не приватный адрес
            m = client.get("/metrics", headers={"Authorization": "Bearer metrics-token-1"})
            assert m.status_code == 200 and m.headers["content-type"].startswith("text/plain")
            assert "peremena_up{" in m.text
            ready = client.get("/health/ready")
            assert ready.status_code == 200 and ready.json()["checks"] == {"db": True, "kv": True}
    finally:
        get_settings.cache_clear()


def test_ready_fails_on_long_queue(env, monkeypatch):
    from app import main, updates

    loop, api, c = env
    r = FakeStreams()
    q = _queue(r)
    monkeypatch.setattr(updates, "queue", q)
    c.settings.event_ready_backlog = 3

    class State:
        ctx = c
        background: list = []
    monkeypatch.setattr(main.app, "state", State())

    async def go():
        await q.ensure_group()
        out = []
        for n in range(5):
            await q.publish({"update_type": "x", "n": n})
            resp = await main.health_ready()
            out.append(resp.status_code)
        return out, json.loads(resp.body)
    codes, body = loop.run_until_complete(go())
    assert codes == [200, 200, 200, 503, 503]
    assert body["checks"]["queue"] is False and body["backlog"] == 5


# ---------------------------------------------------------------- переподписка вебхука
class SubsApi:
    def __init__(self, subs):
        self.subs = subs
        self.subscribed: list[tuple[str, str]] = []

    async def get_subscriptions(self):
        return self.subs

    async def subscribe(self, url, secret):
        self.subscribed.append((url, secret))
        self.subs = [{"url": url}]
        return {"success": True}


def _ctx(api, url="https://bot.example.ru/webhook"):
    from app.config import Settings
    from app.core import Ctx
    from app.state import MemoryKV

    return Ctx(settings=Settings(max_bot_token="t", webhook_url=url), api=api, kv=MemoryKV())


def test_ensure_webhook_resubscribes_only_when_missing():
    from app.updates import ensure_webhook

    lost = SubsApi([{"url": "https://old.example.ru/webhook"}])
    run(ensure_webhook(_ctx(lost)))
    assert [u for u, _ in lost.subscribed] == ["https://bot.example.ru/webhook"]

    ok = SubsApi([{"url": "https://bot.example.ru/webhook/"}])  # тот же адрес со слешем
    run(ensure_webhook(_ctx(ok)))
    assert ok.subscribed == []

    forced = SubsApi([{"url": "https://bot.example.ru/webhook"}])  # при старте — всегда: секрет мог смениться
    run(ensure_webhook(_ctx(forced), force=True))
    assert len(forced.subscribed) == 1 and forced.subscribed[0][1]


def test_resubscribe_scheduled_and_on_election():
    from app import updates

    api = SubsApi([])
    c = _ctx(api)
    sched = updates.make_scheduler(c)
    job = sched.get_job("webhook")
    assert job is not None and job.trigger.interval.total_seconds() == 30 * 60

    async def go():
        bg = updates.Background(c, updates.make_scheduler(c), polling=False, subscribe_webhook=True)
        try:
            await bg.on_elected()
        finally:
            bg.shutdown()
    run(go())
    assert len(api.subscribed) == 1


# ---------------------------------------------------------------- ответ на нажатие после обрыва
def test_callback_answer_retried_after_read_error():
    """Обрыв чтения ответа MAX на POST /answers — повторяем: иначе кнопка у родителя остаётся «нажатой».
    Отправку сообщения по-прежнему не повторяем (tests/test_max_api.py) — там повтор дал бы дубль."""
    import httpx

    from app.max_api import MaxClient

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ReadError("connection reset")
        return httpx.Response(200, json={"success": True})
    c = MaxClient("token", "https://max.test")
    c._http = httpx.AsyncClient(base_url="https://max.test", transport=httpx.MockTransport(handler))
    assert run(c.answer_callback("cb-1", notification="Принято")) == {"success": True}
    assert calls == ["/answers", "/answers"]


# ---------------------------------------------------------------- командная строка
def test_cli_events_status_and_replay(monkeypatch, capsys):
    import redis.asyncio as redis_mod

    from app import __main__ as cli
    from app.config import get_settings

    fake = FakeStreams()
    monkeypatch.setattr(redis_mod, "from_url", lambda *a, **kw: fake)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    get_settings.cache_clear()

    async def prepare():
        q = events.EventQueue(fake)
        await q.ensure_group()
        await q.publish({"update_type": "message_callback", "callback": {"callback_id": "cb-1"}})
        await fake.xreadgroup(q.group, "c", {q.stream: ">"}, count=10)
        await q.to_dead((await fake.xrange(q.stream))[0][0], (await fake.xrange(q.stream))[0][1], "проверка", 5)
        fake.keys["pm:dd:cb:cb-1"] = "1"
    try:
        run(prepare())
        assert cli.main(["events"]) == 0
        out = capsys.readouterr().out
        assert '"dead": 1' in out and "проверка" in out
        assert cli.main(["events", "--replay-dead"]) == 0
        out = capsys.readouterr().out
        assert "Возвращено в очередь pm:events: 1" in out and '"dead": 0' in out
        assert "pm:dd:cb:cb-1" not in fake.keys  # отметка дедупликации снята
    finally:
        get_settings.cache_clear()

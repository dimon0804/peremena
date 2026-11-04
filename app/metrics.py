"""Метрики в текстовом формате Prometheus — без внешних зависимостей.

Каждый процесс считает своё (счётчики и гистограммы в памяти) и раз в METRICS_PUSH_S секунд кладёт
снимок в Redis (хеш ``pm:metrics``, поле — имя процесса). ``/metrics`` любого процесса отдаёт снимки всех
живых процессов с меткой ``proc`` — так Prometheus видит всю установку, в какой бы процесс ни попал запрос
через балансировщик. Без Redis — только свой процесс.

Общие для всей установки величины (длина очереди, отставание группы, dead-letter) считаются в момент запроса.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time

log = logging.getLogger(__name__)

PROC = f"{socket.gethostname()}-{os.getpid()}"
HASH = "pm:metrics"
PUSH_S = 5.0
STALE_S = 60.0  # снимок старше — процесс, видимо, остановлен

# Время обработки события и ответа на вебхук, секунды
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

HELP = {
    "peremena_events_received_total": ("counter", "События MAX, принятые вебхуком или long polling"),
    "peremena_events_processed_total": ("counter", "События, обработанные до конца"),
    "peremena_events_failed_total": ("counter", "Неудачные попытки обработки события (будет повтор или dead-letter)"),
    "peremena_events_dead_total": ("counter", "События, отправленные в dead-letter (pm:events:dead)"),
    "peremena_event_handle_seconds": ("histogram", "Время обработки одного события"),
    "peremena_webhook_seconds": ("histogram", "Время ответа на вебхук MAX (приём и постановка в очередь)"),
    "peremena_webhook_backpressure_total": ("counter", "Вебхуки, принятые при очереди длиннее EVENT_BACKLOG_WARN"),
    "peremena_max_requests_total": ("counter", "Запросы к MAX Bot API по исходу"),
    "peremena_db_pool_size": ("gauge", "Размер пула соединений PostgreSQL"),
    "peremena_db_pool_checked_out": ("gauge", "Соединений пула занято"),
    "peremena_db_pool_overflow": ("gauge", "Соединений сверх DB_POOL_SIZE"),
    "peremena_queue_length": ("gauge", "Длина потока событий pm:events (включая уже обработанные)"),
    "peremena_queue_pending": ("gauge", "События, выданные обработчикам и ещё не подтверждённые"),
    "peremena_queue_lag": ("gauge", "События, ещё не выданные ни одному обработчику"),
    "peremena_queue_dead": ("gauge", "События в dead-letter pm:events:dead"),
    "peremena_up": ("gauge", "Процесс жив и отдаёт метрики"),
}


def _key(name: str, labels: dict) -> str:
    return json.dumps([name, sorted(labels.items())], ensure_ascii=False)


class Registry:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counters: dict[str, float] = {}
        self.hists: dict[str, list] = {}  # ключ → [счётчики по корзинам..., сумма, количество]

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        k = _key(name, labels)
        self.counters[k] = self.counters.get(k, 0.0) + value

    def observe(self, name: str, value: float, **labels) -> None:
        k = _key(name, labels)
        h = self.hists.get(k)
        if h is None:
            h = self.hists[k] = [0] * len(BUCKETS) + [0.0, 0]
        for i, b in enumerate(BUCKETS):
            if value <= b:
                h[i] += 1
        h[-2] += value
        h[-1] += 1

    def snapshot(self) -> dict:
        gauges = {}
        from . import db

        pool = getattr(db.engine(), "pool", None)
        if pool is not None and hasattr(pool, "checkedout"):
            try:
                gauges = {_key("peremena_db_pool_size", {}): pool.size(),
                          _key("peremena_db_pool_checked_out", {}): pool.checkedout(),
                          _key("peremena_db_pool_overflow", {}): max(0, pool.overflow())}
            except Exception:
                gauges = {}
        return {"t": time.time(), "c": dict(self.counters), "h": {k: list(v) for k, v in self.hists.items()},
                "g": gauges}


REG = Registry()
inc = REG.inc
observe = REG.observe


def _labels(pairs, extra: dict) -> str:
    items = list(pairs) + list(extra.items())
    if not items:
        return ""
    esc = (lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in items) + "}"


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def render(snapshots: dict[str, dict], global_gauges: dict[str, float]) -> str:
    """Текст для Prometheus. snapshots: процесс → снимок Registry.snapshot()."""
    lines: dict[str, list[str]] = {}

    def add(name: str, line: str) -> None:
        lines.setdefault(name, []).append(line)

    for proc, snap in sorted(snapshots.items()):
        add("peremena_up", f"peremena_up{_labels([], {'proc': proc})} 1")
        for k, v in snap.get("c", {}).items():
            name, pairs = json.loads(k)
            add(name, f"{name}{_labels(pairs, {'proc': proc})} {_num(v)}")
        for k, v in snap.get("g", {}).items():
            name, pairs = json.loads(k)
            add(name, f"{name}{_labels(pairs, {'proc': proc})} {_num(v)}")
        for k, h in snap.get("h", {}).items():
            name, pairs = json.loads(k)
            for i, b in enumerate(BUCKETS):
                add(name, f"{name}_bucket{_labels(pairs, {'proc': proc, 'le': _num(b)})} {h[i]}")
            add(name, f"{name}_bucket{_labels(pairs, {'proc': proc, 'le': '+Inf'})} {h[-1]}")
            add(name, f"{name}_sum{_labels(pairs, {'proc': proc})} {_num(round(h[-2], 6))}")
            add(name, f"{name}_count{_labels(pairs, {'proc': proc})} {h[-1]}")
    for name, v in global_gauges.items():
        if v is not None:
            add(name, f"{name} {_num(v)}")
    out = []
    for name in sorted(lines):
        kind, text = HELP.get(name, ("untyped", ""))
        out.append(f"# HELP {name} {text}")
        out.append(f"# TYPE {name} {kind}")
        out += lines[name]
    return "\n".join(out) + "\n"


async def push(redis) -> None:
    await redis.hset(HASH, PROC, json.dumps(REG.snapshot(), ensure_ascii=False))


async def collect(redis) -> dict[str, dict]:
    """Снимки всех живых процессов; свой — свежий, а не из Redis. Устаревшие поля удаляются."""
    snaps: dict[str, dict] = {}
    if redis is not None:
        try:
            raw = await redis.hgetall(HASH)
            now = time.time()
            stale = []
            for proc, data in raw.items():
                try:
                    snap = json.loads(data)
                except ValueError:
                    stale.append(proc)
                    continue
                if now - snap.get("t", 0) > STALE_S:
                    stale.append(proc)
                else:
                    snaps[proc] = snap
            if stale:
                await redis.hdel(HASH, *stale)
        except Exception as e:
            log.warning("Метрики других процессов недоступны: %s", e)
    snaps[PROC] = REG.snapshot()
    return snaps


async def pusher(redis, every: float = PUSH_S) -> None:
    """Фоновая задача процесса: снимок в Redis раз в every секунд, при остановке — удалить свой."""
    try:
        while True:
            try:
                await push(redis)
            except Exception as e:
                log.debug("Метрики не отправлены в Redis: %s", e)
            await asyncio.sleep(every)
    finally:
        try:
            await asyncio.shield(redis.hdel(HASH, PROC))
        except BaseException:
            pass

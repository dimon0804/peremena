"""Очередь событий MAX в Redis Streams: приём отделён от обработки.

Без очереди вебхук обрабатывается в том же процессе, что его принял: на пике (вечер, массовая рассылка)
обработка съедает цикл событий, ответ MAX «200» задерживается на секунды, а необработанное живёт
только в памяти процесса и пропадает при его падении.

С очередью (EVENT_QUEUE=redis или auto при наличии Redis):
* вебхук и long polling только кладут событие в поток ``pm:events`` (XADD) и сразу отвечают;
* обработчики — все процессы с ROLE=all или worker — читают поток группой потребителей (XREADGROUP),
  каждое событие получает ровно один потребитель, после обработки — XACK;
* события умершего потребителя (взял, но не подтвердил) через EVENT_RECLAIM_S забирает другой (XAUTOCLAIM);
* обработка упала — событие остаётся неподтверждённым и через EVENT_RECLAIM_S приходит снова; после
  EVENT_MAX_ATTEMPTS доставок (счётчик Redis из XPENDING/XAUTOCLAIM) оно уходит в ``pm:events:dead``
  с причиной и больше не крутится. Вернуть в работу — ``python -m app events --replay-dead``;
* очередь длиннее EVENT_BACKLOG_WARN — вебхук всё равно отвечает 200, но пишет предупреждение и метрику;
* дубли повторной доставки MAX по-прежнему отсекает дедупликация в router.handle_update.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from typing import Awaitable, Callable

from . import metrics

log = logging.getLogger(__name__)

STREAM = "pm:events"
GROUP = "handlers"
MAXLEN = 1_000_000          # потолок длины потока (приблизительный, ~): старые подтверждённые события вытесняются
DEAD_MAXLEN = 100_000
RECLAIM_IDLE_MS = 120_000   # событие без подтверждения дольше — потребитель умер или обработка упала
RECLAIM_EVERY_S = 30
MAX_ATTEMPTS = 5
BACKLOG_TTL_S = 1.0         # как часто приём обновляет оценку длины очереди (backpressure)


def dead_stream(stream: str) -> str:
    return stream + ":dead"


class EventQueue:
    def __init__(self, redis, stream: str = STREAM, group: str = GROUP, *, max_attempts: int | None = None,
                 reclaim_idle_ms: int | None = None, backlog_warn: int = 0, maxlen: int | None = None) -> None:
        self.r = redis
        self.stream = stream
        self.dead = dead_stream(stream)
        self.group = group
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.max_attempts = max(1, max_attempts or MAX_ATTEMPTS)
        self.reclaim_idle_ms = reclaim_idle_ms
        self.backlog_warn = backlog_warn
        self.maxlen = maxlen or MAXLEN
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()
        self.processed = 0
        self.failed = 0
        self.dead_count = 0
        self._backlog = 0
        self._backlog_at = 0.0
        self._warned_at = 0.0

    # ------------------------------------------------------------------ приём
    async def publish(self, update: dict) -> None:
        await self.r.xadd(self.stream, {"u": json.dumps(update, ensure_ascii=False)}, maxlen=self.maxlen,
                          approximate=True)
        if self.backlog_warn:
            await self._check_backpressure()

    async def _check_backpressure(self) -> None:
        """Очередь длиннее порога: вебхук всё равно отвечает 200 (иначе MAX начнёт повторять и добавит
        нагрузки), но это видно в логе и в метрике — пора добавлять обработчиков."""
        now = time.monotonic()
        if now - self._backlog_at >= BACKLOG_TTL_S:
            self._backlog_at = now
            try:
                st = await self.backlog()
                self._backlog = (st.get("pending") or 0) + (st.get("lag") or 0)
            except Exception:
                pass
        if self._backlog > self.backlog_warn:
            metrics.inc("peremena_webhook_backpressure_total")
            if now - self._warned_at > 10:
                self._warned_at = now
                log.warning("Очередь событий: %s ждут обработки (порог EVENT_BACKLOG_WARN=%s) — "
                            "обработчики не успевают, добавьте worker", self._backlog, self.backlog_warn)

    async def ensure_group(self) -> None:
        try:
            await self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP — группа уже есть
            if "BUSYGROUP" not in str(e):
                raise

    async def backlog(self) -> dict:
        """Сколько событий ждёт обработки: pending — выданы и не подтверждены, lag — ещё не выданы."""
        try:
            info = await self.r.xinfo_groups(self.stream)
            g = next((x for x in info if x.get("name") == self.group), {})
            return {"pending": g.get("pending", 0), "lag": g.get("lag")}
        except Exception:
            return {}

    async def stats(self) -> dict:
        """Состояние очереди целиком — для /metrics, /health/ready и `python -m app events`."""
        out: dict = {"stream": self.stream, "length": None, "pending": None, "lag": None, "dead": None,
                     "consumers": None, "oldest_pending_ms": None}
        try:
            out["length"] = await self.r.xlen(self.stream)
            out["dead"] = await self.r.xlen(self.dead)
            info = await self.r.xinfo_groups(self.stream)
            g = next((x for x in info if x.get("name") == self.group), None)
            if g is not None:
                out["pending"] = g.get("pending", 0)
                out["lag"] = g.get("lag")
                out["consumers"] = g.get("consumers")
                if out["pending"]:
                    first = await self.r.xpending_range(self.stream, self.group, min="-", max="+", count=1)
                    if first:
                        out["oldest_pending_ms"] = first[0].get("time_since_delivered")
        except Exception as e:
            out["error"] = str(e)
        return out

    # ------------------------------------------------------------------ dead-letter
    async def _deliveries(self, msg_id: str) -> int:
        """Сколько раз событие выдавалось обработчикам (счётчик Redis, растёт при XREADGROUP и XAUTOCLAIM)."""
        try:
            rows = await self.r.xpending_range(self.stream, self.group, min=msg_id, max=msg_id, count=1)
            return int(rows[0].get("times_delivered") or 0) if rows else 0
        except Exception:
            return 0

    async def to_dead(self, msg_id: str, fields: dict, reason: str, attempts: int) -> None:
        """Событие больше не обрабатываем: копия с причиной — в dead-letter, из основной очереди — XACK."""
        await self.r.xadd(self.dead, {"u": fields.get("u") or "", "id": msg_id, "reason": reason[:500],
                                      "attempts": str(attempts), "consumer": self.consumer,
                                      "at": str(int(time.time()))}, maxlen=DEAD_MAXLEN, approximate=True)
        await self.r.xack(self.stream, self.group, msg_id)
        self.dead_count += 1
        metrics.inc("peremena_events_dead_total")
        log.error("Событие %s отправлено в dead-letter %s (доставок: %s): %s", msg_id, self.dead, attempts, reason)

    # ------------------------------------------------------------------ обработка
    async def _handle(self, msg_id: str, fields: dict, handler: Callable[[dict], Awaitable[None]],
                      slots: asyncio.Semaphore, reclaimed: bool = False) -> None:
        t0 = time.perf_counter()
        try:
            try:
                update = json.loads(fields.get("u") or "")
            except ValueError:
                update = None
            if not isinstance(update, dict):  # повтор не поможет — сразу в dead-letter
                await self.to_dead(msg_id, fields, "не разбирается как событие MAX (ожидался JSON-объект)", 1)
                return
            if reclaimed:
                n = await self._deliveries(msg_id)
                if n == 0:  # уже подтверждено: другой обработчик успел довести событие или отправить в dead-letter
                    return
                if n > self.max_attempts:  # процесс умирал или зависал на этом событии раз за разом
                    await self.to_dead(msg_id, fields, f"превышено число доставок ({n} > {self.max_attempts}): "
                                                       "обработчик падал или зависал", n)
                    return
            await handler(update)
            await self.r.xack(self.stream, self.group, msg_id)
            self.processed += 1
            metrics.inc("peremena_events_processed_total")
            metrics.observe("peremena_event_handle_seconds", time.perf_counter() - t0)
        except asyncio.CancelledError:
            raise  # без XACK: событие заберёт другой потребитель
        except Exception as e:
            self.failed += 1
            metrics.inc("peremena_events_failed_total")
            n = await self._deliveries(msg_id)
            if n >= self.max_attempts:
                log.exception("Обработка события %s из очереди — попытка %s из %s", msg_id, n, self.max_attempts)
                try:
                    await self.to_dead(msg_id, fields, f"{type(e).__name__}: {e}", n)
                except Exception:
                    log.exception("Не удалось отправить событие %s в dead-letter", msg_id)
            else:
                # без XACK: через EVENT_RECLAIM_S событие подберёт XAUTOCLAIM и попробует снова
                log.warning("Обработка события %s упала (попытка %s из %s): %s — повтор позже",
                            msg_id, n or "?", self.max_attempts, e, exc_info=True)
        finally:
            slots.release()

    def _start(self, msg_id, fields, handler, slots, reclaimed: bool = False) -> None:
        t = asyncio.create_task(self._handle(msg_id, fields, handler, slots, reclaimed))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def consume(self, handler: Callable[[dict], Awaitable[None]], concurrency: int = 64) -> None:
        """Читает поток, пока процесс жив. Одновременно в работе — не больше concurrency событий."""
        await self.ensure_group()
        slots = asyncio.Semaphore(max(1, concurrency))
        last_reclaim = 0.0
        failures = 0
        idle_ms = self.reclaim_idle_ms if self.reclaim_idle_ms is not None else RECLAIM_IDLE_MS
        log.info("Обработчик очереди событий запущен: %s (до %s одновременно, попыток до dead-letter: %s)",
                 self.consumer, concurrency, self.max_attempts)
        while not self._stopping:
            free = started = 0
            try:
                await slots.acquire()
                free = 1
                while free < 100 and not slots.locked():  # берём пачку по числу свободных мест
                    await slots.acquire()
                    free += 1
                batch: list = []
                reclaimed = False
                if time.monotonic() - last_reclaim > RECLAIM_EVERY_S:
                    last_reclaim = time.monotonic()
                    claimed = await self.r.xautoclaim(self.stream, self.group, self.consumer,
                                                      min_idle_time=idle_ms, start_id="0-0", count=free)
                    batch = [m for m in (claimed[1] if len(claimed) > 1 else []) if m and m[1] is not None]
                    if batch:
                        reclaimed = True
                        log.warning("Подобрано %s неподтверждённых событий (обработчик умер или обработка упала)",
                                    len(batch))
                if not batch:
                    res = await self.r.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=free, block=2000)
                    batch = [m for _, msgs in (res or []) for m in msgs]
                for msg_id, fields in batch:
                    self._start(msg_id, fields, handler, slots, reclaimed)  # место освободит сама обработка
                    started += 1
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                delay = min(2 ** failures, 30)
                log.warning("Очередь событий недоступна (%s) — повтор через %s с", e, delay)
                if "NOGROUP" in str(e):
                    try:
                        await self.ensure_group()
                    except Exception:
                        pass
                await asyncio.sleep(delay)
            finally:
                for _ in range(free - started):
                    slots.release()

    async def drain(self, timeout: float = 20.0) -> None:
        """Остановка: новых событий не берём, начатые доводим; недоделанные останутся в очереди без XACK."""
        self._stopping = True
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            done, rest = await asyncio.wait(pending, timeout=timeout)
            for t in rest:
                t.cancel()

    # ------------------------------------------------------------------ dead-letter: просмотр и возврат
    async def dead_entries(self, count: int = 20) -> list[dict]:
        """Последние события в dead-letter — без тела события (там персональные данные)."""
        rows = await self.r.xrevrange(self.dead, count=count)
        out = []
        for dead_id, f in rows:
            try:
                u = json.loads(f.get("u") or "")
            except ValueError:
                u = None
            out.append({"dead_id": dead_id, "id": f.get("id"), "reason": f.get("reason"),
                        "attempts": f.get("attempts"), "at": f.get("at"),
                        "update_type": u.get("update_type") if isinstance(u, dict) else None})
        return out

    async def replay_dead(self, count: int | None = None, forget=None) -> int:
        """Вернуть события из dead-letter в основную очередь (после исправления причины).
        forget(update) — снять отметку дедупликации, иначе возвращённое событие отбросится как повтор.
        Неразбираемые записи возвращать бессмысленно — они только удаляются."""
        moved = 0
        while count is None or moved < count:
            n = 100 if count is None else min(100, count - moved)
            rows = await self.r.xrange(self.dead, min="-", max="+", count=n)
            if not rows:
                break
            for dead_id, f in rows:
                raw = f.get("u") or ""
                try:
                    update = json.loads(raw)
                except ValueError:
                    update = None
                if isinstance(update, dict):
                    if forget is not None:
                        await forget(update)
                    await self.r.xadd(self.stream, {"u": raw}, maxlen=self.maxlen, approximate=True)
                    moved += 1
                await self.r.xdel(self.dead, dead_id)
        return moved

"""Очередь событий MAX в Redis Streams: приём отделён от обработки.

Без очереди вебхук обрабатывается в том же процессе, что его принял: на пике (вечер, массовая рассылка)
обработка съедает цикл событий, ответ MAX «200» задерживается на секунды, а необработанное живёт
только в памяти процесса и пропадает при его падении.

С очередью (EVENT_QUEUE=redis или auto при наличии Redis):
* вебхук и long polling только кладут событие в поток ``pm:events`` (XADD) и сразу отвечают;
* обработчики — все процессы с ROLE=all или worker — читают поток группой потребителей (XREADGROUP),
  каждое событие получает ровно один потребитель, после обработки — XACK;
* события умершего потребителя (взял, но не подтвердил) через EVENT_RECLAIM_S забирает другой (XAUTOCLAIM);
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

log = logging.getLogger(__name__)

STREAM = "pm:events"
GROUP = "handlers"
MAXLEN = 1_000_000          # потолок длины потока (приблизительный, ~): старые подтверждённые события вытесняются
RECLAIM_IDLE_MS = 120_000   # событие без подтверждения дольше — потребитель, видимо, умер
RECLAIM_EVERY_S = 30


class EventQueue:
    def __init__(self, redis, stream: str = STREAM, group: str = GROUP) -> None:
        self.r = redis
        self.stream = stream
        self.group = group
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()
        self.processed = 0

    async def publish(self, update: dict) -> None:
        await self.r.xadd(self.stream, {"u": json.dumps(update, ensure_ascii=False)}, maxlen=MAXLEN, approximate=True)

    async def ensure_group(self) -> None:
        try:
            await self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP — группа уже есть
            if "BUSYGROUP" not in str(e):
                raise

    async def backlog(self) -> dict:
        """Сколько событий ждёт обработки (для /health в разработке и для замеров)."""
        try:
            info = await self.r.xinfo_groups(self.stream)
            g = next((x for x in info if x.get("name") == self.group), {})
            return {"pending": g.get("pending", 0), "lag": g.get("lag")}
        except Exception:
            return {}

    async def _handle(self, msg_id: str, fields: dict, handler: Callable[[dict], Awaitable[None]],
                      slots: asyncio.Semaphore) -> None:
        try:
            try:
                update = json.loads(fields.get("u") or "{}")
            except ValueError:
                update = None
            if isinstance(update, dict):
                await handler(update)
            await self.r.xack(self.stream, self.group, msg_id)
            self.processed += 1
        except asyncio.CancelledError:
            raise  # без XACK: событие заберёт другой потребитель
        except Exception:
            log.exception("Обработка события %s из очереди", msg_id)
            try:
                await self.r.xack(self.stream, self.group, msg_id)  # ошибка обработчика не лечится повтором
            except Exception:
                pass
        finally:
            slots.release()

    def _start(self, msg_id, fields, handler, slots) -> None:
        t = asyncio.create_task(self._handle(msg_id, fields, handler, slots))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def consume(self, handler: Callable[[dict], Awaitable[None]], concurrency: int = 64) -> None:
        """Читает поток, пока процесс жив. Одновременно в работе — не больше concurrency событий."""
        await self.ensure_group()
        slots = asyncio.Semaphore(max(1, concurrency))
        last_reclaim = 0.0
        failures = 0
        log.info("Обработчик очереди событий запущен: %s (до %s одновременно)", self.consumer, concurrency)
        while not self._stopping:
            free = started = 0
            try:
                await slots.acquire()
                free = 1
                while free < 100 and not slots.locked():  # берём пачку по числу свободных мест
                    await slots.acquire()
                    free += 1
                batch: list = []
                if time.monotonic() - last_reclaim > RECLAIM_EVERY_S:
                    last_reclaim = time.monotonic()
                    claimed = await self.r.xautoclaim(self.stream, self.group, self.consumer,
                                                      min_idle_time=RECLAIM_IDLE_MS, start_id="0-0", count=free)
                    batch = [m for m in (claimed[1] if len(claimed) > 1 else []) if m and m[1] is not None]
                    if batch:
                        log.warning("Подобрано %s событий умершего обработчика", len(batch))
                if not batch:
                    res = await self.r.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=free, block=2000)
                    batch = [m for _, msgs in (res or []) for m in msgs]
                for msg_id, fields in batch:
                    self._start(msg_id, fields, handler, slots)  # место освободит сама обработка
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

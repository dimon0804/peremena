"""Получение событий: вебхук (прод) или long polling (разработка), плюс фоновые задачи."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .bot.router import handle_update
from .core import Ctx
from .max_api import MaxApiError
from .timeutil import utcnow
from .services import board, demo, polls, quiet, retention, websession

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


# Очередь событий в Redis (app/events.py) или None — обработка в этом процессе
queue = None


async def enqueue(update: dict) -> bool:
    """Кладёт событие в общую очередь. False — очереди нет или Redis недоступен: обработаем сами,
    чтобы не потерять событие."""
    if queue is None:
        return False
    try:
        await queue.publish(update)
        return True
    except Exception as e:
        log.warning("Очередь событий недоступна (%s) — обрабатываю событие в этом процессе", e)
        return False


def submit(update: dict) -> asyncio.Task:
    """Принять событие: в очередь (если она есть) или в обработку здесь же. Задача завершается, когда
    событие надёжно принято (в очереди) или обработано — на это смотрит маркер long polling."""
    async def run():
        if not await enqueue(update):
            await handle_update(update)
    return spawn(run())


def spawn(coro) -> asyncio.Task:
    """Обработка в фоне: вебхук должен сразу вернуть 200, иначе MAX будет повторять доставку."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain(timeout: float = 20.0) -> None:
    """При остановке даём начатым обработкам закончиться (рассылка сбора, ответ на кнопку),
    оставшиеся по таймауту отменяем — они вернут свой ключ дедупликации и придут снова."""
    pending = [t for t in _tasks if not t.done()]
    if not pending:
        return
    log.info("Дожидаемся %s фоновых задач (до %s с)", len(pending), timeout)
    done, rest = await asyncio.wait(pending, timeout=timeout)
    for t in rest:
        t.cancel()
    if rest:
        await asyncio.wait(rest, timeout=5)
        log.warning("Не успели завершиться и отменены: %s задач", len(rest))


def _safe_marker(batches: list[tuple[int | None, set[asyncio.Task]]], current: int | None) -> int | None:
    """Маркер, который можно сохранить: до первой пачки, обработка которой ещё идёт.
    После перезапуска эти события придут снова, а уже обработанные отсеет дедупликация."""
    while batches and all(t.done() for t in batches[0][1]):
        batches.pop(0)
    return batches[0][0] if batches else current


POLL_BACKOFF_MAX = 60


async def polling_loop(c: Ctx) -> None:
    marker = None
    try:
        marker = await c.kv.get("updates_marker")
    except Exception:
        log.exception("Не удалось прочитать сохранённый маркер обновлений — начинаем с текущих")
    log.info("Long polling запущен")
    try:
        subs = await c.api.get_subscriptions()
        if subs:
            log.warning("У бота есть вебхук-подписки (%s) — long polling не получит события. "
                        "Удалите их: python -m app unsubscribe --all", ", ".join(s.get("url", "") for s in subs))
    except Exception as e:
        log.warning("GET /subscriptions: %s", e)
    batches: list[tuple[int | None, set[asyncio.Task]]] = []
    saved = marker
    failures = 0
    while True:
        # Любая ошибка шага (сеть, MAX, хранилище состояния) не должна останавливать цикл навсегда:
        # пишем в лог, ждём с нарастающей паузой и пробуем снова.
        try:
            data = await c.api.get_updates(marker, timeout=30)
            started_from = marker
            tasks = {submit(upd) for upd in data.get("updates") or []}
            if tasks:
                batches.append((started_from, tasks))
            new_marker = data.get("marker")
            if new_marker is not None:
                marker = new_marker
            safe = _safe_marker(batches, marker)
            if safe is not None and safe != saved:
                await c.kv.set("updates_marker", safe, ttl=7 * 24 * 3600)
                saved = safe
            failures = 0
            c.polling_alive_at = utcnow()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failures += 1
            delay = 60 if isinstance(e, MaxApiError) and e.status == 401 else min(2 ** failures, POLL_BACKOFF_MAX)
            if isinstance(e, MaxApiError):
                log.warning("Long polling: %s — повтор через %s с", e, delay)
            else:
                log.exception("Long polling: ошибка шага — повтор через %s с", delay)
            await asyncio.sleep(delay)


async def ensure_webhook(c: Ctx, force: bool = False) -> None:
    """Подписка слетает, если сервер долго не отвечает 200, — проверяем и переподписываемся.
    При старте (force) подписываемся всегда: MAX не возвращает секрет в GET /subscriptions,
    и после смены WEBHOOK_SECRET старая подписка слала бы запросы со старым секретом (403)."""
    url = c.settings.webhook_url
    if not url:
        return
    try:
        if not force:
            subs = await c.api.get_subscriptions()
            if any(s.get("url") == url for s in subs):
                return
        await c.api.subscribe(url, c.settings.effective_webhook_secret)
        log.info("Вебхук подписан: %s", url)
    except MaxApiError as e:
        log.error("Не удалось подписать вебхук %s: %s", url, e)


# Лидер этого процесса (app/leader.py): задачи планировщика выполняются, только пока процесс — лидер.
# Планировщик и так ставится на паузу при потере лидерства; проверка в самой задаче страхует от
# прохода, который начался за миг до смены лидера.
current_leader = None


def leader_only(fn):
    async def job(*args, **kwargs):
        if current_leader is not None and not current_leader.is_leader:
            return None
        return await fn(*args, **kwargs)

    job.__name__ = getattr(fn, "__name__", "job")
    job.__qualname__ = getattr(fn, "__qualname__", job.__name__)
    return job


JOB_OPTS = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 120}


def make_scheduler(c: Ctx) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    opts = JOB_OPTS
    sched.add_job(leader_only(polls.tick), "interval", seconds=60, id="polls", **opts)
    sched.add_job(leader_only(quiet.morning_tick), "interval", seconds=60, id="morning", **opts)
    sched.add_job(leader_only(board.tick), "interval", seconds=60, id="board", **opts)
    sched.add_job(leader_only(websession.cleanup), "interval", hours=1, id="sessions", **opts)
    sched.add_job(leader_only(demo.cleanup), "interval", hours=1, id="demo", **opts)
    sched.add_job(leader_only(retention.cleanup), "interval", hours=24, id="retention", **opts)  # сроки хранения
    if hasattr(c.kv, "cleanup"):  # состояние в базе — устаревшие записи удаляем сами (Redis делает это сам)
        sched.add_job(leader_only(c.kv.cleanup), "interval", hours=1, id="kv", **opts)
    if c.settings.mode == "webhook":
        sched.add_job(leader_only(ensure_webhook), "interval", minutes=30, args=[c], id="webhook", **opts)
    add_embeddings_job(sched)
    return sched


def add_embeddings_job(sched: AsyncIOScheduler) -> None:
    """EMBEDDINGS=on: лидер раз в 5 минут досчитывает векторы карточек, пропущенные фоновой индексацией."""
    from .services import semantic

    if semantic.enabled():
        sched.add_job(leader_only(semantic.catch_up), "interval", minutes=5, id="embeddings", **JOB_OPTS)


class Background:
    """Фоновая часть процесса: планировщик и long polling. Работает, только пока процесс — лидер;
    при потере лидерства планировщик встаёт на паузу, long polling останавливается."""

    def __init__(self, c: Ctx, sched: AsyncIOScheduler, polling: bool, subscribe_webhook: bool) -> None:
        self.c = c
        self.sched = sched
        self.polling = polling
        self.subscribe_webhook = subscribe_webhook
        self.polling_task: asyncio.Task | None = None
        self.sched.start(paused=True)

    async def on_elected(self) -> None:
        if self.subscribe_webhook:
            await ensure_webhook(self.c, force=True)
        self.sched.resume()
        if self.polling and (self.polling_task is None or self.polling_task.done()):
            self.polling_task = asyncio.create_task(polling_loop(self.c))

    async def on_demoted(self) -> None:
        if self.sched.running:
            self.sched.pause()
        task, self.polling_task = self.polling_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def polling_ok(self) -> bool | None:
        """None — long polling не должен работать в этом процессе (не лидер или вебхук)."""
        if self.polling_task is None:
            return None
        return not self.polling_task.done()

    def shutdown(self) -> None:
        if self.polling_task is not None:
            self.polling_task.cancel()
        if self.sched.running:
            self.sched.shutdown(wait=False)

"""Получение событий: вебхук (прод) или long polling (разработка), плюс фоновые задачи."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .bot.router import handle_update
from .core import Ctx
from .max_api import MaxApiError
from .timeutil import utcnow
from .services import board, demo, polls, quiet, websession

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


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
            tasks = {spawn(handle_update(upd)) for upd in data.get("updates") or []}
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


def make_scheduler(c: Ctx) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    opts = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 120}
    sched.add_job(polls.tick, "interval", seconds=60, id="polls", **opts)
    sched.add_job(quiet.morning_tick, "interval", seconds=60, id="morning", **opts)
    sched.add_job(board.tick, "interval", seconds=60, id="board", **opts)
    sched.add_job(websession.cleanup, "interval", hours=1, id="sessions", **opts)
    sched.add_job(demo.cleanup, "interval", hours=1, id="demo", **opts)
    if hasattr(c.kv, "cleanup"):  # состояние в базе — устаревшие записи удаляем сами (Redis делает это сам)
        sched.add_job(c.kv.cleanup, "interval", hours=1, id="kv", **opts)
    if c.settings.mode == "webhook":
        sched.add_job(ensure_webhook, "interval", minutes=30, args=[c], id="webhook", **opts)
    return sched

"""Получение событий: вебхук (прод) или long polling (разработка), плюс фоновые задачи."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .bot.router import handle_update
from .core import Ctx
from .max_api import MaxApiError
from .services import board, polls, quiet, websession

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """Обработка в фоне: вебхук должен сразу вернуть 200, иначе MAX будет повторять доставку."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def polling_loop(c: Ctx) -> None:
    marker = await c.kv.get("updates_marker")
    log.info("Long polling запущен")
    try:
        subs = await c.api.get_subscriptions()
        if subs:
            log.warning("У бота есть вебхук-подписки (%s) — long polling не получит события. "
                        "Удалите их: python -m app unsubscribe --all", ", ".join(s.get("url", "") for s in subs))
    except MaxApiError:
        pass
    while True:
        try:
            data = await c.api.get_updates(marker, timeout=30)
        except asyncio.CancelledError:
            raise
        except MaxApiError as e:
            log.warning("GET /updates: %s", e)
            await asyncio.sleep(5 if e.status != 401 else 60)
            continue
        except Exception:
            log.exception("GET /updates")
            await asyncio.sleep(5)
            continue
        for upd in data.get("updates") or []:
            spawn(handle_update(upd))
        new_marker = data.get("marker")
        if new_marker is not None and new_marker != marker:
            marker = new_marker
            await c.kv.set("updates_marker", marker, ttl=7 * 24 * 3600)


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
    if c.settings.mode == "webhook":
        sched.add_job(ensure_webhook, "interval", minutes=30, args=[c], id="webhook", **opts)
    return sched

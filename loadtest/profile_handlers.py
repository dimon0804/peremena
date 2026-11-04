"""Профиль обработки событий в одном процессе (без HTTP): куда уходит процессор на событие.

    python loadtest/seed.py --db postgresql://pm:pm@127.0.0.1:55433/pm --classes 300 --polls --reset
    python loadtest/profile_handlers.py --events 2000

MAX подменён поддельным клиентом из tests/conftest.py, база и Redis — настоящие.
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import pstats
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import roster  # noqa: E402
from run_all import CHATTER, QUESTIONS, callback, chat_message  # noqa: E402


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="postgresql+asyncpg://pm:pm@127.0.0.1:55433/pm")
    p.add_argument("--redis", default="redis://127.0.0.1:56380/0")
    p.add_argument("--events", type=int, default=2000)
    p.add_argument("--classes", type=int, default=300)
    p.add_argument("--concurrency", type=int, default=64)
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--no-profile", action="store_true")
    a = p.parse_args()

    from app import db
    from app.bot.router import handle_update
    from app.config import Settings
    from app.core import Ctx, set_ctx
    from app.state import RedisKV
    from tests.conftest import FakeApi

    s = Settings(max_bot_token="load-test-token", database_url=a.db, redis_url=a.redis)
    await db.init_db(s.database_url)
    kv = RedisKV(s.redis_url)
    await kv.redis.flushdb()
    set_ctx(Ctx(settings=s, api=FakeApi(), kv=kv, bot_user_id=roster.BOT_ID, bot_username="peremena_load_bot"))
    import asyncpg

    conn = await asyncpg.connect(a.db.replace("+asyncpg", ""))
    polls = {r["chat_id"]: r["id"] for r in await conn.fetch("select id, chat_id from polls")}
    await conn.close()
    rnd = random.Random(5)
    evs = []
    for k in range(a.events):
        i, j = rnd.randrange(a.classes), rnd.randrange(roster.PARENTS)
        x = rnd.random()
        if x < 0.15:
            evs.append(callback(roster.parent_id(i, j), f"pa:{polls[roster.chat_id(i)]}:0", f"cbp-{k}"))
        elif x < 0.25:
            evs.append(chat_message(roster.parent_id(i, j), roster.chat_id(i), f"{rnd.choice(QUESTIONS)} {k}", f"mp-{k}"))
        else:
            evs.append(chat_message(roster.parent_id(i, j), roster.chat_id(i), rnd.choice(CHATTER), f"mp-{k}"))
    from collections import Counter

    from sqlalchemy import event

    stmts: Counter = Counter()

    @event.listens_for(db.engine().sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, *args):
        stmts[statement.split()[0].upper()] += 1

    @event.listens_for(db.engine().sync_engine.pool, "checkout")
    def _checkout(*args):
        stmts["(checkout)"] += 1
    sem = asyncio.Semaphore(a.concurrency)

    async def one(e):
        async with sem:
            await handle_update(e)
    pr = cProfile.Profile()
    t, cpu = time.perf_counter(), time.process_time()
    if not a.no_profile:
        pr.enable()
    await asyncio.gather(*(one(e) for e in evs))
    pr.disable()
    dt, cpu = time.perf_counter() - t, time.process_time() - cpu
    print(f"{len(evs)} событий за {dt:.2f} с: {len(evs) / dt:.0f} событий/с в одном процессе; "
          f"процессор {cpu:.2f} с ({cpu / len(evs) * 1000:.2f} мс на событие)")
    print("SQL на событие:", {k: round(v / len(evs), 2) for k, v in stmts.most_common()})
    if not a.no_profile:
        pstats.Stats(pr).sort_stats("tottime").print_stats(a.top)
    await kv.close()
    await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())

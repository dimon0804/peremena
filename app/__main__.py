"""Командная строка «Перемены».

    python -m app run              — запустить сервис (бот + API + мини-приложение)
    python -m app check            — проверить токен и доступ к MAX API (GET /me)
    python -m app subscribe        — подписать вебхук на WEBHOOK_URL
    python -m app unsubscribe --all — снять все вебхук-подписки (нужно для long polling)
    python -m app subscriptions    — показать текущие подписки
    python -m app commands         — обновить меню команд бота
    python -m app init-db          — создать таблицы в базе
    python -m app digest <user_id> — отправить учителю утреннюю сводку прямо сейчас
    python -m app copy-data <url>  — перенести данные из другой базы (SQLite → PostgreSQL)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import get_settings


def _client():
    from .max_api import MaxClient

    s = get_settings()
    if not s.max_bot_token:
        sys.exit("Задайте MAX_BOT_TOKEN (переменная окружения или файл .env)")
    return MaxClient(s.max_bot_token, s.max_api_url, s.extra_ca_file)


async def _check() -> int:
    api = _client()
    try:
        me = await api.get_me()
        print(json.dumps(me, ensure_ascii=False, indent=2))
        subs = await api.get_subscriptions()
        print(f"Вебхук-подписок: {len(subs)}")
        return 0
    finally:
        await api.close()


async def _subscribe() -> int:
    s = get_settings()
    if not s.webhook_url:
        sys.exit("Задайте WEBHOOK_URL (https://<домен>/webhook, порт 443)")
    api = _client()
    try:
        print(await api.subscribe(s.webhook_url, s.effective_webhook_secret))
        return 0
    finally:
        await api.close()


async def _unsubscribe(url: str | None, all_: bool) -> int:
    api = _client()
    try:
        urls = [x["url"] for x in await api.get_subscriptions()] if all_ else [url or get_settings().webhook_url]
        for u in urls:
            if u:
                print(u, await api.unsubscribe(u))
        return 0
    finally:
        await api.close()


async def _subscriptions() -> int:
    api = _client()
    try:
        print(json.dumps(await api.get_subscriptions(), ensure_ascii=False, indent=2))
        return 0
    finally:
        await api.close()


async def _commands() -> int:
    from .bot.common import COMMANDS

    api = _client()
    try:
        print(await api.set_commands(COMMANDS))
        return 0
    finally:
        await api.close()


async def _init_db() -> int:
    from . import db

    await db.init_db(get_settings().database_url)
    await db.close_db()
    print("Таблицы созданы:", db.dialect() or "ok")
    return 0


async def _copy_data(src_url: str) -> int:
    """Переносит все данные из другой базы (например, SQLite) в DATABASE_URL — для переезда на PostgreSQL."""
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from . import db, models  # noqa: F401

    dst_url = get_settings().database_url
    await db.init_db(dst_url)  # схема назначения — миграциями
    src = create_async_engine(db._normalize_url(src_url))
    copied = {}
    try:
        async with src.connect() as s_conn, db._engine.begin() as d_conn:
            for table in db.Base.metadata.sorted_tables:  # родительские таблицы раньше дочерних
                if await d_conn.scalar(select(func.count()).select_from(table)):
                    sys.exit(f"В таблице {table.name} базы назначения уже есть данные — перенос остановлен")
                rows = [dict(r._mapping) for r in await s_conn.execute(select(table))]
                if rows:
                    await d_conn.execute(table.insert(), rows)
                copied[table.name] = len(rows)
            if d_conn.dialect.name == "postgresql":
                # счётчики автоинкремента — после максимального перенесённого id
                for table in db.Base.metadata.sorted_tables:
                    if "id" in table.c and table.c.id.primary_key:  # таблицы с числовым id-счётчиком
                        await d_conn.execute(text(
                            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"))
    finally:
        await src.dispose()
        await db.close_db()
    for name, n in copied.items():
        if n:
            print(f"{name}: {n}")
    print("Перенос завершён")
    return 0


async def _digest(user_id: int) -> int:
    from . import db
    from .core import Ctx, set_ctx
    from .services import chats, quiet
    from .state import MemoryKV

    s = get_settings()
    await db.init_db(s.database_url)
    api = _client()
    try:
        c = Ctx(settings=s, api=api, kv=MemoryKV(), bot_username=s.bot_username)
        set_ctx(c)
        user = await chats.get_user(user_id)
        if not user:
            print("Пользователь не найден")
            return 1
        ok = await quiet.send_digest(user, include_empty=True)
        print("Отправлено" if ok else "Не отправлено")
        return 0 if ok else 1
    finally:
        await api.close()
        await db.close_db()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app", description="«Перемена» — бот классного руководителя в MAX")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="запустить сервис")
    r.add_argument("--host", default=None)
    r.add_argument("--port", type=int, default=None)
    sub.add_parser("check", help="проверить токен (GET /me)")
    sub.add_parser("subscribe", help="подписать вебхук на WEBHOOK_URL")
    u = sub.add_parser("unsubscribe", help="снять вебхук")
    u.add_argument("--url", default=None)
    u.add_argument("--all", action="store_true")
    sub.add_parser("subscriptions", help="список вебхук-подписок")
    sub.add_parser("commands", help="обновить меню команд бота")
    sub.add_parser("init-db", help="создать таблицы")
    cp = sub.add_parser("copy-data", help="перенести данные из другой базы в DATABASE_URL")
    cp.add_argument("source", help="адрес исходной базы, например sqlite:////srv/data/peremena.db")
    d = sub.add_parser("digest", help="утренняя сводка учителю сейчас")
    d.add_argument("user_id", type=int)
    args = p.parse_args(argv)

    if args.cmd in (None, "run"):
        import uvicorn

        s = get_settings()
        uvicorn.run("app.main:app", host=getattr(args, "host", None) or s.host,
                    port=getattr(args, "port", None) or s.port, proxy_headers=True, forwarded_allow_ips="*")
        return 0
    if args.cmd == "check":
        return asyncio.run(_check())
    if args.cmd == "subscribe":
        return asyncio.run(_subscribe())
    if args.cmd == "unsubscribe":
        return asyncio.run(_unsubscribe(args.url, args.all))
    if args.cmd == "subscriptions":
        return asyncio.run(_subscriptions())
    if args.cmd == "commands":
        return asyncio.run(_commands())
    if args.cmd == "init-db":
        return asyncio.run(_init_db())
    if args.cmd == "copy-data":
        return asyncio.run(_copy_data(args.source))
    if args.cmd == "digest":
        return asyncio.run(_digest(args.user_id))
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Время в UTC без tzinfo — одинаково ведёт себя в SQLite и PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("sqlite:///"):
        url = "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
    return url


# Номер замка pg_advisory_xact_lock на время миграций: несколько экземпляров стартуют одновременно,
# и без замка два alembic upgrade наперегонки создают одну и ту же таблицу или индекс.
MIGRATION_LOCK_ID = 0x5045524D  # «PERM»


def engine_options(url: str, pool_size: int | None = None, max_overflow: int | None = None,
                   pool_timeout: float | None = None) -> dict:
    """Параметры пула. Для PostgreSQL — из DB_POOL_SIZE / DB_MAX_OVERFLOW / DB_POOL_TIMEOUT;
    SQLite — файл на одной машине, пул по умолчанию."""
    opts: dict = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        if pool_size is None or max_overflow is None or pool_timeout is None:
            from .config import get_settings

            s = get_settings()
            pool_size = s.db_pool_size if pool_size is None else pool_size
            max_overflow = s.db_max_overflow if max_overflow is None else max_overflow
            pool_timeout = s.db_pool_timeout if pool_timeout is None else pool_timeout
        opts |= {"pool_size": max(1, pool_size), "max_overflow": max(0, max_overflow),
                 "pool_timeout": pool_timeout,
                 # соединение, которое прокси или сервер могли тихо закрыть, не берём из пула
                 "pool_recycle": 1800}
    return opts


async def init_db(url: str, *, pool_size: int | None = None, max_overflow: int | None = None,
                  pool_timeout: float | None = None) -> None:
    global _engine, _sessionmaker
    url = _normalize_url(url)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(url, **engine_options(url, pool_size, max_overflow, pool_timeout))
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    from . import models  # noqa: F401  регистрирует таблицы

    async with _engine.begin() as conn:
        if _engine.dialect.name == "postgresql":
            from sqlalchemy import text

            # экземпляры, стартовавшие вместе, мигрируют по очереди; замок снимается с концом транзакции
            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK_ID})
        await conn.run_sync(_migrate)


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _migrate(conn) -> None:
    """Схема базы — через миграции Alembic (app/migrations).

    База, созданная до появления миграций (create_all), достраивается до текущих моделей и помечается
    последней версией (head) — данные сохраняются. Помечать её первой версией нельзя: create_all уже создал
    таблицы из следующих миграций, и они упали бы на «table already exists»."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    insp = inspect(conn)
    legacy = insp.has_table("users") and not insp.has_table("alembic_version")
    if legacy:
        Base.metadata.create_all(conn)
        _add_missing_columns(conn)
        command.stamp(cfg, "head")  # модели = head: это проверяет tests/test_migrations.py
    command.upgrade(cfg, "head")


def _add_missing_columns(conn) -> None:
    """Для баз без миграций: недостающие столбцы добавляются в старые таблицы перед отметкой последней версией."""
    from sqlalchemy import inspect

    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl_type = col.type.compile(dialect=conn.dialect)
            default = ""
            if col.default is not None and getattr(col.default, "is_scalar", False):
                v = col.default.arg
                default = f" DEFAULT {'TRUE' if v is True else 'FALSE' if v is False else repr(v)}"
            conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default}')


async def close_db() -> None:
    if _engine is not None:
        await _engine.dispose()


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    assert _sessionmaker is not None, "init_db() не вызван"
    async with _sessionmaker() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


def dialect() -> str:
    return _engine.dialect.name if _engine is not None else ""


def engine():
    """Движок базы (для замка лидера на PostgreSQL)."""
    return _engine


def pool_status() -> str:
    """Состояние пула для логов и /health в режиме разработки."""
    pool = getattr(_engine, "pool", None)
    return pool.status() if pool is not None and hasattr(pool, "status") else ""

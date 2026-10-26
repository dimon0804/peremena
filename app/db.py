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


async def init_db(url: str) -> None:
    global _engine, _sessionmaker
    url = _normalize_url(url)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    from . import models  # noqa: F401  регистрирует таблицы

    async with _engine.begin() as conn:
        await conn.run_sync(_migrate)


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _migrate(conn) -> None:
    """Схема базы — через миграции Alembic (app/migrations).

    База, созданная до появления миграций (create_all), сначала достраивается до схемы первой миграции,
    затем помечается ею — данные сохраняются, дальше применяются только новые миграции."""
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
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")


def _add_missing_columns(conn) -> None:
    """Для баз без миграций: недостающие столбцы добавляются в старые таблицы перед отметкой 0001."""
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

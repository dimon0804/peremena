"""Миграции Alembic: новая база, база без миграций и совпадение моделей со схемой."""
import asyncio
import os

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import db


def _url(tmp_path, name):
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / name}"


async def _reset(url):
    if "postgres" in url:
        from .conftest import _reset_postgres

        await _reset_postgres(url)


def test_fresh_database_is_built_by_migrations(tmp_path):
    url = _url(tmp_path, "fresh.db")

    async def go():
        await _reset(url)
        await db.init_db(url)
        async with db._engine.connect() as conn:
            version = (await conn.execute(text("select version_num from alembic_version"))).scalar()

            def diff(sync_conn):
                from alembic.autogenerate import compare_metadata
                from alembic.migration import MigrationContext

                return compare_metadata(MigrationContext.configure(sync_conn), db.Base.metadata)
            changes = await conn.run_sync(diff)
        await db.close_db()
        return version, changes

    version, changes = asyncio.run(go())
    assert version == "0001"
    # модели изменили, а миграцию не написали — здесь будет список отличий
    assert changes == [], f"нужна новая миграция: alembic revision --autogenerate ({changes})"


def test_legacy_database_is_stamped_and_keeps_data(tmp_path):
    url = _url(tmp_path, "legacy.db")

    async def go():
        await _reset(url)
        # база «до миграций»: таблицы через create_all, без alembic_version
        engine = create_async_engine(db._normalize_url(url))
        from app import models  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all)
            await conn.execute(text("insert into users (user_id, first_name, dialog_open, created_at, tz, "
                                    "work_start, work_end, weekends_off, quiet_enabled, urgent_words, suggest_save) "
                                    "values (1, 'Мария', true, CURRENT_TIMESTAMP, 'Europe/Moscow', 480, 1140, "
                                    "true, true, 'срочно', true)"))
        await engine.dispose()

        await db.init_db(url)
        await db.init_db(url)  # повторный старт ничего не ломает
        async with db._engine.connect() as conn:
            version = (await conn.execute(text("select version_num from alembic_version"))).scalar()
            users = (await conn.execute(text("select count(*) from users"))).scalar()
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        await db.close_db()
        return version, users, tables

    version, users, tables = asyncio.run(go())
    assert version == "0001" and users == 1 and "kv" in tables

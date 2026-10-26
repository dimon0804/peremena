"""Окружение Alembic.

Два режима:
* из сервиса — app.db.init_db передаёт готовое соединение в config.attributes["connection"];
* из командной строки разработчика — `alembic revision --autogenerate -m "..."`,
  адрес базы берётся из DATABASE_URL (как у сервиса).
"""
from __future__ import annotations

import asyncio

from alembic import context

from app import models  # noqa: F401  регистрирует таблицы в Base.metadata
from app.db import Base, _normalize_url

target_metadata = Base.metadata


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # ALTER TABLE в SQLite — через пересоздание
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_from_cli() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings

    async def go() -> None:
        engine = create_async_engine(_normalize_url(get_settings().database_url))
        async with engine.connect() as conn:
            await conn.run_sync(_configure_and_run)
            await conn.commit()
        await engine.dispose()

    asyncio.run(go())


connection = context.config.attributes.get("connection")
if connection is not None:
    _configure_and_run(connection)
else:
    _run_from_cli()

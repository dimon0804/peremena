"""Короткоживущее состояние: шаги диалогов, дедупликация событий, черновики.

Redis, если задан REDIS_URL; иначе — таблица в базе данных, чтобы состояние переживало перезапуск.
MemoryKV остаётся для тестов.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class KV:
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any, ttl: int = 3600) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def add_once(self, key: str, ttl: int = 3600) -> bool:
        """True, если ключа не было (и он создан)."""
        ...
    async def ping(self) -> bool: return True
    async def close(self) -> None: ...


class MemoryKV(KV):
    def __init__(self) -> None:
        self._d: dict[str, tuple[float, Any]] = {}

    def _gc(self) -> None:
        if len(self._d) > 20000:
            now = time.time()
            self._d = {k: v for k, v in self._d.items() if v[0] > now}

    async def get(self, key: str) -> Any:
        item = self._d.get(key)
        if not item:
            return None
        if item[0] < time.time():
            self._d.pop(key, None)
            return None
        return item[1]

    async def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        self._gc()
        self._d[key] = (time.time() + ttl, json.loads(json.dumps(value)))

    async def delete(self, key: str) -> None:
        self._d.pop(key, None)

    async def add_once(self, key: str, ttl: int = 3600) -> bool:
        if await self.get(key) is not None:
            return False
        await self.set(key, 1, ttl)
        return True


class RedisKV(KV):
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Any:
        raw = await self._r.get("pm:" + key)
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        await self._r.set("pm:" + key, json.dumps(value, ensure_ascii=False), ex=ttl)

    async def delete(self, key: str) -> None:
        await self._r.delete("pm:" + key)

    async def add_once(self, key: str, ttl: int = 3600) -> bool:
        return bool(await self._r.set("pm:" + key, "1", ex=ttl, nx=True))

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._r.aclose()


class DbKV(KV):
    """Состояние в той же базе, что и «Память класса», — переживает перезапуск без Redis."""

    async def get(self, key: str) -> Any:
        from .db import session, utcnow
        from .models import KVEntry

        async with session() as s:
            row = await s.get(KVEntry, key)
            if row is None or row.expires_at <= utcnow():
                return None
            return json.loads(row.value)

    async def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        from datetime import timedelta

        from .db import session, utcnow
        from .models import KVEntry

        raw = json.dumps(value, ensure_ascii=False)
        expires = utcnow() + timedelta(seconds=ttl)
        async with session() as s:
            row = await s.get(KVEntry, key)
            if row is None:
                s.add(KVEntry(key=key, value=raw, expires_at=expires))
            else:
                row.value, row.expires_at = raw, expires

    async def delete(self, key: str) -> None:
        from sqlalchemy import delete as sa_delete

        from .db import session
        from .models import KVEntry

        async with session() as s:
            await s.execute(sa_delete(KVEntry).where(KVEntry.key == key))

    async def add_once(self, key: str, ttl: int = 3600) -> bool:
        from datetime import timedelta

        from sqlalchemy.exc import IntegrityError

        from .db import session, utcnow
        from .models import KVEntry

        try:
            async with session() as s:
                row = await s.get(KVEntry, key)
                if row is not None and row.expires_at > utcnow():
                    return False
                if row is not None:
                    await s.delete(row)
                    await s.flush()
                s.add(KVEntry(key=key, value="1", expires_at=utcnow() + timedelta(seconds=ttl)))
            return True
        except IntegrityError:  # параллельная вставка того же ключа
            return False

    async def cleanup(self) -> None:
        from sqlalchemy import delete as sa_delete

        from .db import session, utcnow
        from .models import KVEntry

        async with session() as s:
            await s.execute(sa_delete(KVEntry).where(KVEntry.expires_at <= utcnow()))


async def make_kv(url: str) -> KV:
    """Redis, если задан и доступен; иначе база данных (init_db должен быть вызван раньше)."""
    if url:
        kv = RedisKV(url)
        if await kv.ping():
            log.info("Состояние хранится в Redis")
            return kv
        log.warning("Redis %s недоступен — состояние будет в базе данных", url)
    log.info("Состояние хранится в базе данных")
    return DbKV()

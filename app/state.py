"""Короткоживущее состояние: шаги диалогов, дедупликация событий, черновики.

Redis, если задан REDIS_URL; иначе — словарь в памяти процесса (для одного контейнера этого хватает).
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


async def make_kv(url: str) -> KV:
    if url:
        kv = RedisKV(url)
        if await kv.ping():
            log.info("Состояние хранится в Redis")
            return kv
        log.warning("Redis %s недоступен — состояние будет в памяти процесса", url)
    return MemoryKV()

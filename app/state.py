"""Короткоживущее состояние: шаги диалогов, дедупликация событий, черновики.

Redis, если задан REDIS_URL; иначе — таблица в базе данных, чтобы состояние переживало перезапуск.
MemoryKV остаётся для тестов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)


class KV:
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any, ttl: int = 3600) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def add_once(self, key: str, ttl: int = 3600) -> bool:
        """True, если ключа не было (и он создан)."""
        ...
    async def pop(self, key: str) -> Any:
        """Атомарно забирает значение и удаляет ключ: из параллельных вызовов значение получит только один."""
        ...
    async def ping(self) -> bool: return True
    async def close(self) -> None: ...

    @property
    def redis(self):
        """Клиент Redis, если состояние в нём (общий лимитер MAX API, замок лидера), иначе None."""
        return None

    @asynccontextmanager
    async def lock(self, key: str, ttl: float = 60.0, wait: float = 30.0) -> AsyncIterator[bool]:
        """Замок «по одному»: события одного человека, рассылка одного сбора.
        Здесь — в памяти процесса; RedisKV делает его общим для всех экземпляров.
        Отдаёт True, если замок взят; False — не дождались за wait секунд (работаем без него)."""
        async with _local_lock(key, wait) as got:
            yield got


class _LocalLocks:
    """asyncio.Lock на ключ; запись удаляется, когда замок никому не нужен (память не растёт)."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str, wait: float) -> AsyncIterator[bool]:
        lock, users = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, users + 1)
        got = False
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=wait)
                got = True
            except asyncio.TimeoutError:
                log.warning("Замок %s не освободился за %s с — продолжаем без него", key, wait)
            yield got
        finally:
            if got:
                lock.release()
            lock_, users = self._locks.get(key, (lock, 1))
            if users <= 1:
                self._locks.pop(key, None)
            else:
                self._locks[key] = (lock_, users - 1)

    def __len__(self) -> int:
        return len(self._locks)


_LOCAL = _LocalLocks()


def _local_lock(key: str, wait: float):
    return _LOCAL.hold(key, wait)


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

    async def pop(self, key: str) -> Any:
        item = self._d.pop(key, None)  # без await между чтением и удалением — атомарно для asyncio
        if not item or item[0] < time.time():
            return None
        return item[1]


# Снять замок, только если он всё ещё наш (а не взят другим экземпляром после истечения срока)
_UNLOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_EXTEND = ("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) "
           "else return 0 end")


class RedisKV(KV):
    def __init__(self, url: str, max_connections: int = 200) -> None:
        import redis.asyncio as redis

        # пул ограничен: при пике событий лишние запросы ждут соединение, а не открывают тысячи сокетов
        self._r = redis.from_url(url, decode_responses=True, max_connections=max_connections,
                                 health_check_interval=30)

    @property
    def redis(self):
        return self._r

    @asynccontextmanager
    async def lock(self, key: str, ttl: float = 60.0, wait: float = 30.0) -> AsyncIterator[bool]:
        """Распределённый замок: SET NX PX с уникальным значением, продление, пока работа идёт,
        снятие — только своего значения. Внутри процесса сначала локальный замок: ждущие события
        одного человека не крутят опрос Redis, а встают в очередь asyncio."""
        started = time.monotonic()
        async with _local_lock(key, wait) as got_local:
            name = "pm:lock:" + key
            token = secrets.token_hex(8)
            ttl_ms = int(ttl * 1000)
            got = False
            delay = 0.005
            try:
                while True:
                    if await self._r.set(name, token, px=ttl_ms, nx=True):
                        got = True
                        break
                    if time.monotonic() - started >= wait:
                        log.warning("Общий замок %s не освободился за %s с — продолжаем без него", key, wait)
                        break
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 0.1)
            except Exception:
                log.exception("Redis недоступен для замка %s — только локальный замок", key)
            renew = asyncio.create_task(self._renew(name, token, ttl_ms)) if got else None
            try:
                yield got and got_local
            finally:
                if renew is not None:
                    renew.cancel()
                if got:
                    try:
                        await self._r.eval(_UNLOCK, 1, name, token)
                    except Exception:
                        log.warning("Не удалось снять замок %s — истечёт сам через %s с", key, ttl)

    async def _renew(self, name: str, token: str, ttl_ms: int) -> None:
        """Длинная работа (рассылка сбора) не должна терять замок посреди дела."""
        try:
            while True:
                await asyncio.sleep(ttl_ms / 3000)
                if not await self._r.eval(_EXTEND, 1, name, token, ttl_ms):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Не удалось продлить замок %s", name)

    async def get(self, key: str) -> Any:
        raw = await self._r.get("pm:" + key)
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        await self._r.set("pm:" + key, json.dumps(value, ensure_ascii=False), ex=ttl)

    async def delete(self, key: str) -> None:
        await self._r.delete("pm:" + key)

    async def add_once(self, key: str, ttl: int = 3600) -> bool:
        return bool(await self._r.set("pm:" + key, "1", ex=ttl, nx=True))

    async def pop(self, key: str) -> Any:
        raw = await self._r.getdel("pm:" + key)  # Redis ≥ 6.2
        return json.loads(raw) if raw is not None else None

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

    async def pop(self, key: str) -> Any:
        from sqlalchemy import delete as sa_delete

        from .db import session, utcnow
        from .models import KVEntry

        async with session() as s:
            row = await s.get(KVEntry, key)
            if row is None:
                return None
            raw, alive = row.value, row.expires_at > utcnow()
            # удаляем именно прочитанное значение: из параллельных удалений строку получит только одно
            res = await s.execute(sa_delete(KVEntry).where(KVEntry.key == key, KVEntry.value == raw)
                                  .execution_options(synchronize_session=False))
            if res.rowcount != 1 or not alive:
                return None
            return json.loads(raw)

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

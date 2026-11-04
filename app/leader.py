"""Выбор лидера: планировщик и long polling работают ровно в одном процессе из всех экземпляров.

Остальные экземпляры (и воркеры uvicorn) обслуживают только HTTP — вебхук, API, сайт — и раз в несколько
секунд проверяют, не освободилось ли место лидера (лидер упал или остановлен).

Замок:
* Redis (если состояние в нём) — ``SET pm:leader:<имя> <токен> NX PX <срок>`` с продлением на треть срока
  и снятием только своего значения;
* PostgreSQL без Redis — ``pg_try_advisory_lock`` на отдельном соединении: замок держится, пока живо
  соединение, и снимается сервером сам, если процесс умер;
* SQLite — база на одной машине и в одном процессе: процесс всегда лидер.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import socket
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

_EXTEND = ("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) "
           "else return 0 end")
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

Callback = Callable[[], Awaitable[None] | None]


class _RedisBackend:
    def __init__(self, redis, key: str, ttl: float) -> None:
        self._r = redis
        self._key = key
        self._ttl_ms = int(ttl * 1000)
        self._token = f"{socket.gethostname()}:{secrets.token_hex(6)}"

    async def acquire(self) -> bool:
        return bool(await self._r.set(self._key, self._token, px=self._ttl_ms, nx=True))

    async def renew(self) -> bool:
        return bool(await self._r.eval(_EXTEND, 1, self._key, self._token, self._ttl_ms))

    async def release(self) -> None:
        await self._r.eval(_RELEASE, 1, self._key, self._token)

    async def holder(self) -> str | None:
        return await self._r.get(self._key)


class _PgBackend:
    """Сессионный advisory-замок PostgreSQL на выделенном соединении (не из общего пула запросов)."""

    def __init__(self, engine, name: str) -> None:
        self._engine = engine
        self._key = int.from_bytes(hashlib.sha256(("peremena:" + name).encode()).digest()[:8], "big", signed=True)
        self._conn = None

    async def acquire(self) -> bool:
        from sqlalchemy import text

        if self._conn is None:
            self._conn = await self._engine.connect()
        got = bool(await self._conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key}))
        await self._conn.commit()  # не держим открытую транзакцию: замок сессионный, ему она не нужна
        if not got:
            await self._close()
        return got

    async def renew(self) -> bool:
        from sqlalchemy import text

        if self._conn is None:
            return False
        try:
            await self._conn.scalar(text("SELECT 1"))  # соединение живо — значит, замок наш
            await self._conn.commit()
            return True
        except Exception as e:
            # соединение умерло — сервер уже снял замок, и его мог взять другой экземпляр: лидерство потеряно
            log.warning("Соединение с замком лидера потеряно: %s", e)
            await self._close()
            return False

    async def release(self) -> None:
        from sqlalchemy import text

        if self._conn is not None:
            try:
                await self._conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
                await self._conn.commit()
            finally:
                await self._close()

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


class _AlwaysBackend:
    async def acquire(self) -> bool:
        return True

    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class Leader:
    """Цикл выборов. ``is_leader`` — этот процесс сейчас лидер. on_elected / on_demoted вызываются
    при смене статуса (запуск и остановка планировщика и long polling)."""

    def __init__(self, backend, *, ttl: float = 30.0, name: str = "scheduler",
                 on_elected: Callback | None = None, on_demoted: Callback | None = None) -> None:
        self.backend = backend
        self.ttl = ttl
        self.name = name
        self.is_leader = False
        self._on_elected = on_elected
        self._on_demoted = on_demoted
        self._valid_until = 0.0
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def kind(self) -> str:
        return {"_RedisBackend": "redis", "_PgBackend": "postgres"}.get(type(self.backend).__name__, "local")

    async def _call(self, cb: Callback | None) -> None:
        if cb is None:
            return
        try:
            res = cb()
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            log.exception("Лидер %s: ошибка при смене статуса", self.name)

    async def step(self) -> bool:
        """Один шаг: лидер продлевает замок, остальные пробуют его взять. Возвращает is_leader."""
        now = time.monotonic()
        try:
            ok = await (self.backend.renew() if self.is_leader else self.backend.acquire())
        except Exception as e:
            # Хранилище замка недоступно. Лидер остаётся лидером, пока не истёк срок его замка:
            # раньше никто другой его не возьмёт. Дальше — уступаем, чтобы не было двух лидеров.
            log.warning("Лидер %s: замок недоступен (%s)", self.name, e)
            ok = self.is_leader and now < self._valid_until - 1.0
        if ok:
            self._valid_until = now + self.ttl
        if ok and not self.is_leader:
            self.is_leader = True
            log.info("Этот процесс — лидер (%s, замок: %s): планировщик и фоновые задачи здесь", self.name, self.kind)
            await self._call(self._on_elected)
        elif not ok and self.is_leader:
            self.is_leader = False
            log.warning("Лидерство потеряно (%s) — фоновые задачи остановлены в этом процессе", self.name)
            await self._call(self._on_demoted)
        return self.is_leader

    async def run(self) -> None:
        interval = max(self.ttl / 3, 0.2)
        while not self._stopping:
            await self.step()
            await asyncio.sleep(interval)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Остановка процесса: сразу освобождаем место, чтобы другой экземпляр подхватил задачи
        через секунды, а не через срок замка."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.is_leader:
            self.is_leader = False
            await self._call(self._on_demoted)
        try:
            await self.backend.release()
        except Exception:
            log.warning("Не удалось освободить замок лидера — истечёт сам")


def make_backend(kv, engine, token: str = "", name: str = "scheduler", ttl: float = 30.0):
    """Redis, если состояние в нём; иначе PostgreSQL; иначе (SQLite) — процесс всегда лидер."""
    redis = getattr(kv, "redis", None)
    if redis is not None:
        ns = hashlib.sha256(token.encode()).hexdigest()[:10] if token else "web"
        return _RedisBackend(redis, f"pm:leader:{ns}:{name}", ttl)
    if engine is not None and engine.dialect.name == "postgresql":
        return _PgBackend(engine, name)
    return _AlwaysBackend()

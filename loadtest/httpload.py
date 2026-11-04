"""Лёгкий HTTP/1.1-клиент для генератора нагрузки: keep-alive соединения на голых asyncio-потоках.

httpx на тысячах запросов в секунду сам съедает процессор генератора и искажает замер — здесь
только то, что нужно: запрос, статус, тело по Content-Length (или chunked).
"""
from __future__ import annotations

import asyncio
import json
import time


class Conn:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.r: asyncio.StreamReader | None = None
        self.w: asyncio.StreamWriter | None = None

    async def _open(self) -> None:
        self.r, self.w = await asyncio.open_connection(self.host, self.port, limit=1 << 20)

    async def close(self) -> None:
        if self.w is not None:
            self.w.close()
            try:
                await self.w.wait_closed()
            except Exception:
                pass
        self.r = self.w = None

    async def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None,
                      timeout: float = 30.0) -> tuple[int, bytes]:
        for attempt in (0, 1):  # сервер мог закрыть простаивавшее keep-alive соединение — переоткрываем раз
            if self.w is None:
                await self._open()
            try:
                return await asyncio.wait_for(self._do(method, path, body, headers or {}), timeout)
            except (ConnectionError, asyncio.IncompleteReadError) as e:
                await self.close()
                if attempt:
                    raise e
            except BaseException:
                await self.close()
                raise
        raise RuntimeError("unreachable")

    async def _do(self, method, path, body, headers) -> tuple[int, bytes]:
        head = [f"{method} {path} HTTP/1.1", f"Host: {self.host}:{self.port}", f"Content-Length: {len(body)}"]
        head += [f"{k}: {v}" for k, v in headers.items()]
        self.w.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        await self.w.drain()
        status_line = await self.r.readline()
        if not status_line:
            raise ConnectionError("closed")
        status = int(status_line.split()[1])
        length, chunked, close = 0, False, False
        while True:
            line = await self.r.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            k = k.strip().lower()
            if k == "content-length":
                length = int(v.strip())
            elif k == "transfer-encoding" and "chunked" in v.lower():
                chunked = True
            elif k == "connection" and "close" in v.lower():
                close = True
        if chunked:
            data = b""
            while True:
                size = int((await self.r.readline()).strip(), 16)
                if size == 0:
                    await self.r.readline()
                    break
                data += await self.r.readexactly(size)
                await self.r.readline()
        else:
            data = await self.r.readexactly(length) if length else b""
        if close:
            await self.close()
        return status, data


class Pool:
    """Соединения ко всем экземплярам бота по кругу — как балансировщик перед ними."""

    def __init__(self, targets: list[tuple[str, int]], size: int) -> None:
        self.q: asyncio.Queue[Conn] = asyncio.Queue()
        for i in range(size):
            h, p = targets[i % len(targets)]
            self.q.put_nowait(Conn(h, p))

    async def request(self, *a, **kw) -> tuple[int, bytes, float]:
        c = await self.q.get()
        t = time.perf_counter()
        try:
            status, body = await c.request(*a, **kw)
        except Exception:
            status, body = 0, b""
        finally:
            self.q.put_nowait(c)
        return status, body, time.perf_counter() - t

    async def close(self) -> None:
        while not self.q.empty():
            await self.q.get_nowait().close()


def percentiles(xs: list[float]) -> dict:
    if not xs:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    s = sorted(xs)

    def p(q):
        return round(s[min(len(s) - 1, int(q * len(s)))] * 1000, 1)
    return {"p50": p(0.50), "p95": p(0.95), "p99": p(0.99), "max": round(s[-1] * 1000, 1)}


async def get_json(host: str, port: int, path: str):
    c = Conn(host, port)
    try:
        status, body = await c.request("GET", path)
        return json.loads(body) if body else None
    finally:
        await c.close()


async def post_json(host: str, port: int, path: str, data=None, headers: dict | None = None, timeout: float = 300):
    c = Conn(host, port)
    try:
        status, body = await c.request("POST", path, json.dumps(data or {}).encode(),
                                       {"Content-Type": "application/json", **(headers or {})}, timeout=timeout)
        return status, (json.loads(body) if body else None)
    finally:
        await c.close()

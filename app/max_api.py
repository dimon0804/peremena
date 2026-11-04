"""Тонкий асинхронный клиент MAX Bot API (https://dev.max.ru/docs-api).

Особенности платформы, которые здесь учтены:
* токен передаётся в заголовке ``Authorization`` без префикса ``Bearer``;
* личные диалоги адресуются по ``user_id``, групповые чаты — по ``chat_id``;
* не больше ~2 сообщений в секунду в один диалог и ~30 запросов в секунду всего —
  поэтому есть общий ограничитель и пауза между сообщениями одному адресату;
* на 429 ждём Retry-After (общая пауза для всех); 5xx и обрывы повторяем только для идемпотентных
  запросов — отправку сообщения после таймаута не повторяем, иначе родитель получит дубль.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import ssl
import time
from typing import Any

import certifi
import httpx

from . import metrics
from .demo_ids import is_demo, is_demo_mid

log = logging.getLogger(__name__)

UPDATE_TYPES = [
    "message_created",
    "message_callback",
    "message_edited",
    "bot_added",
    "bot_removed",
    "bot_started",
    "user_added",
    "user_removed",
    "chat_title_changed",
    "bot_admin_permissions_changed",
]


TEXT_LIMIT = 4000  # NewMessageBody.text maxLength


def split_text(text: str, limit: int = TEXT_LIMIT - 100) -> list[str]:
    """Режет длинный текст на части по границам строк (разметка у нас не переходит через строки)."""
    if not text or len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # одна гигантская строка — режем по пробелу
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return [p for p in parts if p.strip()] or [text[:limit]]


class MaxApiError(Exception):
    def __init__(self, status: int, code: str = "", message: str = ""):
        super().__init__(f"MAX API {status}: {code} {message}".strip())
        self.status = status
        self.code = code
        self.message = message


def build_ssl_context(extra_ca_file: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    if extra_ca_file and os.path.exists(extra_ca_file):
        try:
            ctx.load_verify_locations(cafile=extra_ca_file)
        except ssl.SSLError as e:  # битый файл не должен ронять сервис
            log.warning("Не удалось загрузить сертификаты %s: %s", extra_ca_file, e)
    return ctx


class _RateLimiter:
    """Общий потолок запросов в секунду + минимальный интервал на одного адресата.

    Слот вычисляется под замком, а ждём уже вне его: пауза перед вторым сообщением одному родителю
    не задерживает отправку всем остальным. После 429 все запросы ждут общую паузу (Retry-After)."""

    def __init__(self, rps: float = 25.0, per_peer_interval: float = 0.55):
        self._interval = 1.0 / rps
        self._per_peer = per_peer_interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._paused_until = 0.0
        self._peer_last: dict[str, float] = {}

    def pause(self, seconds: float) -> None:
        """MAX ответил 429: никому не стучимся, пока не пройдёт пауза."""
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    async def wait(self, peer: str | None = None) -> None:
        async with self._lock:
            now = time.monotonic()
            # общий поток занимает ближайший свободный слот; пауза одного адресата сдвигает только его запрос
            # (запас между 25 и 30 запросами в секунду покрывает редкое совпадение слотов)
            slot = self._last = max(now, self._last + self._interval, self._paused_until)
            if peer:
                slot = max(slot, self._peer_last.get(peer, 0.0) + self._per_peer)
            if peer:
                self._peer_last[peer] = slot
                if len(self._peer_last) > 5000:
                    cutoff = now - 10
                    self._peer_last = {k: v for k, v in self._peer_last.items() if v > cutoff}
        delay = slot - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


# Общий для всех экземпляров ограничитель: «ближайший свободный слот» хранится в Redis и вычисляется
# атомарно в Lua по часам Redis (у экземпляров часы могут расходиться). Логика та же, что у _RateLimiter:
# общий поток занимает слот через интервал 1/rps, пауза одного адресата сдвигает только его запрос,
# после 429 все ждут общую паузу. Возвращает, сколько миллисекунд ждать до своего слота.
_SHARED_WAIT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local interval = tonumber(ARGV[1])
local slot = now
local last = tonumber(redis.call('GET', KEYS[1]) or '0')
if last + interval > slot then slot = last + interval end
local paused = tonumber(redis.call('GET', KEYS[2]) or '0')
if paused > slot then slot = paused end
redis.call('SET', KEYS[1], slot, 'PX', 60000)
if ARGV[3] == '1' then
  local peer = tonumber(redis.call('GET', KEYS[3]) or '0')
  if peer + tonumber(ARGV[2]) > slot then slot = peer + tonumber(ARGV[2]) end
  redis.call('SET', KEYS[3], slot, 'PX', 10000)
end
return slot - now
"""
_SHARED_PAUSE = """
local t = redis.call('TIME')
local untl = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000) + tonumber(ARGV[1])
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if untl > cur then redis.call('SET', KEYS[1], untl, 'PX', tonumber(ARGV[1]) + 1000) end
return untl
"""


class SharedRateLimiter:
    """Потолок запросов к MAX на бота, общий для всех экземпляров и воркеров (Redis).
    Если Redis недоступен — откат на локальный ограничитель процесса (с предупреждением в логе)."""

    def __init__(self, redis, token: str, rps: float = 25.0, per_peer_interval: float = 0.55,
                 fallback: "_RateLimiter | None" = None) -> None:
        import hashlib

        self._r = redis
        self._interval_ms = max(1, round(1000.0 / rps))
        self._per_peer_ms = max(0, round(per_peer_interval * 1000))
        ns = "pm:rl:" + hashlib.sha256(token.encode()).hexdigest()[:10]  # у разных ботов — разные лимиты
        self._k_slot, self._k_pause, self._k_peer = ns + ":slot", ns + ":pause", ns + ":p:"
        self._fallback = fallback or _RateLimiter(rps, per_peer_interval)
        self._warned_at = 0.0

    def _degraded(self, e: Exception) -> None:
        now = time.monotonic()
        if now - self._warned_at > 60:
            self._warned_at = now
            log.warning("Общий лимитер MAX API недоступен (%s) — временно лимит этого процесса", e)

    def pause(self, seconds: float) -> None:
        self._fallback.pause(seconds)
        task = asyncio.ensure_future(self._pause(seconds))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # без «exception never retrieved»

    async def _pause(self, seconds: float) -> None:
        try:
            await self._r.eval(_SHARED_PAUSE, 1, self._k_pause, int(seconds * 1000))
        except Exception as e:
            self._degraded(e)

    async def wait(self, peer: str | None = None) -> None:
        try:
            delay_ms = await self._r.eval(_SHARED_WAIT, 3, self._k_slot, self._k_pause, self._k_peer + (peer or "-"),
                                          self._interval_ms, self._per_peer_ms, "1" if peer else "0")
        except Exception as e:
            self._degraded(e)
            await self._fallback.wait(peer)
            return
        if delay_ms and int(delay_ms) > 0:
            await asyncio.sleep(int(delay_ms) / 1000)


# Ошибки, при которых запрос точно не дошёл до MAX, — их безопасно повторять даже для отправки сообщения.
# Таймаут чтения или 5xx после отправки так не считаются: сообщение могло уйти, повтор дал бы дубль родителю.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
REQUEST_TIMEOUT = 10.0  # обычные запросы; long polling GET /updates задаёт свой таймаут
DEFAULT_RETRIES = 2     # итого до 3 попыток
RETRY_AFTER_MAX = 30.0


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        if raw is not None:
            return min(max(float(raw), 0.5), RETRY_AFTER_MAX)
    except ValueError:
        pass
    return float(min(2 ** attempt, 8))


def _result(status: int) -> str:
    """Метка исхода запроса к MAX для /metrics: 2xx, 4xx, 429, 5xx (без кодов поштучно — меньше рядов)."""
    return "429" if status == 429 else f"{status // 100}xx"


class MaxClient:
    def __init__(self, token: str, base_url: str, extra_ca_file: str | None = None, rps: float = 25.0):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._ssl = build_ssl_context(extra_ca_file)
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": token},
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=5.0),
            verify=self._ssl,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
        )
        self._rps = rps
        self._limiter: _RateLimiter | SharedRateLimiter = _RateLimiter(rps)
        self._demo_seq = itertools.count(1)

    def use_shared_limiter(self, redis) -> None:
        """Несколько экземпляров бота — один лимит MAX на всех: ограничитель переезжает в Redis."""
        local = self._limiter if isinstance(self._limiter, _RateLimiter) else _RateLimiter(self._rps)
        self._limiter = SharedRateLimiter(redis, self.token, self._rps, fallback=local)

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        peer: str | None = None,
        timeout: float | None = None,
        retries: int = DEFAULT_RETRIES,
        idempotent: bool | None = None,
    ) -> Any:
        """Запрос к MAX с повторами. Неидемпотентные запросы (POST) повторяем только когда точно знаем,
        что MAX их не выполнил: соединение не установлено или 429 «слишком часто»."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if idempotent is None:
            idempotent = method.upper() != "POST"
        attempt = 0
        while True:
            await self._limiter.wait(peer)
            try:
                resp = await self._http.request(
                    method, path, params=params, json=json,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                metrics.inc("peremena_max_requests_total", result="network")
                attempt += 1
                if attempt > retries or not (idempotent or isinstance(e, NOT_SENT_ERRORS)):
                    raise MaxApiError(0, "network", f"{type(e).__name__}: {e}") from e
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
                continue

            metrics.inc("peremena_max_requests_total", result=_result(resp.status_code))
            if resp.status_code == 429:
                attempt += 1
                self._limiter.pause(_retry_after(resp, attempt))
                if attempt > retries:
                    raise MaxApiError(429, "too_many_requests", resp.text[:300])
                continue  # пауза выдерживается в limiter.wait — общая для всех запросов

            if resp.status_code >= 500 and idempotent:
                attempt += 1
                if attempt <= retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                raise MaxApiError(resp.status_code, "retry_exhausted", resp.text[:300])

            data: Any
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                data = {"raw": resp.text}

            if resp.status_code >= 400:
                code = data.get("code", "") if isinstance(data, dict) else ""
                msg = data.get("message", "") if isinstance(data, dict) else str(data)
                raise MaxApiError(resp.status_code, code, msg)
            if isinstance(data, dict) and data.get("success") is False:
                raise MaxApiError(resp.status_code, "not_success", data.get("message", ""))
            return data

    # ------------------------------------------------------------------ bot
    async def get_me(self) -> dict:
        return await self.request("GET", "/me")

    async def set_commands(self, commands: list[tuple[str, str]]) -> dict:
        body = {"commands": [{"name": n, "description": d} for n, d in commands]}
        return await self.request("PATCH", "/me/commands", json=body)

    # ------------------------------------------------------------------ messages
    async def send_message(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        text: str | None = None,
        attachments: list[dict] | None = None,
        fmt: str | None = "html",
        reply_to: str | None = None,
        notify: bool = True,
        disable_link_preview: bool = True,
    ) -> dict:
        if is_demo(user_id) or is_demo(chat_id):
            # демо-режим: вымышленным «родителям» и чатам ничего не отправляем, но делаем вид, что доставили
            return {"body": {"mid": f"demo.{next(self._demo_seq)}"}}
        chunks = split_text(text) if text else [text]
        for head in chunks[:-1]:  # длинное сообщение — несколько частей, кнопки у последней
            await self.send_message(user_id=user_id, chat_id=chat_id, text=head, fmt=fmt, reply_to=reply_to,
                                    notify=notify, disable_link_preview=disable_link_preview)
            reply_to, notify = None, False
        text = chunks[-1]
        body: dict[str, Any] = {"text": text, "notify": notify}
        if attachments:
            body["attachments"] = attachments
        if fmt:
            body["format"] = fmt
        if reply_to:
            body["link"] = {"type": "reply", "mid": reply_to}
        peer = f"u{user_id}" if user_id else f"c{chat_id}"
        params = {
            "user_id": user_id,
            "chat_id": chat_id,
            "disable_link_preview": str(disable_link_preview).lower(),
        }
        # Вложение с файлом может ещё обрабатываться на стороне MAX — тогда 400 attachment.not.ready
        for attempt in range(5):
            try:
                res = await self.request("POST", "/messages", params=params, json=body, peer=peer)
                return res.get("message", res)
            except MaxApiError as e:
                if e.status == 400 and "not.ready" in (e.code or "") and attempt < 4:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise MaxApiError(400, "attachment.not.ready")

    async def edit_message(self, message_id: str, *, text: str | None, attachments: list[dict] | None = None,
                           fmt: str | None = "html") -> dict:
        if is_demo_mid(message_id):
            return {"success": True}
        body: dict[str, Any] = {"text": text[:TEXT_LIMIT] if text else text, "attachments": attachments}
        if fmt:
            body["format"] = fmt
        return await self.request("PUT", "/messages", params={"message_id": message_id}, json=body)

    async def delete_message(self, message_id: str) -> dict:
        if is_demo_mid(message_id):
            return {"success": True}
        return await self.request("DELETE", "/messages", params={"message_id": message_id})

    async def get_messages(self, chat_id: int, *, count: int = 100, before: int | None = None) -> dict:
        """История чата, новые первыми. Параметра before у MAX нет: верхняя граница времени (мс)
        передаётся в from — «сообщения до этого времени» (https://dev.max.ru/docs-api/methods/GET/messages)."""
        if is_demo(chat_id):
            return {"messages": []}
        return await self.request("GET", "/messages", params={"chat_id": chat_id, "count": count, "from": before})

    async def answer_callback(self, callback_id: str, *, notification: str | None = None,
                              message: dict | None = None) -> dict:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        if message is not None:
            if message.get("text") and len(message["text"]) > TEXT_LIMIT:
                message = {**message, "text": message["text"][:TEXT_LIMIT - 1] + "…"}
            body["message"] = message
        return await self.request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    # ------------------------------------------------------------------ chats
    async def get_chat(self, chat_id: int) -> dict:
        if is_demo(chat_id):
            return {"chat_id": chat_id, "type": "chat"}
        return await self.request("GET", f"/chats/{chat_id}")

    async def get_members(self, chat_id: int, limit: int = 2000) -> list[dict]:
        if is_demo(chat_id):
            raise MaxApiError(404, "demo", "демо-чат: список ведётся в базе")
        members: list[dict] = []
        marker = None
        while len(members) < limit:
            data = await self.request("GET", f"/chats/{chat_id}/members", params={"count": 100, "marker": marker})
            members.extend(data.get("members", []))
            marker = data.get("marker")
            if not marker:
                break
        return members

    async def get_my_membership(self, chat_id: int) -> dict:
        if is_demo(chat_id):
            return {"is_admin": True, "is_owner": False}
        return await self.request("GET", f"/chats/{chat_id}/members/me")

    # ------------------------------------------------------------------ updates
    async def get_updates(self, marker: int | None, timeout: int = 30) -> dict:
        return await self.request(
            "GET", "/updates",
            params={"marker": marker, "timeout": timeout, "limit": 100, "types": ",".join(UPDATE_TYPES)},
            timeout=timeout + 15, retries=1,
        )

    async def get_subscriptions(self) -> list[dict]:
        data = await self.request("GET", "/subscriptions")
        return data.get("subscriptions", [])

    async def subscribe(self, url: str, secret: str | None) -> dict:
        body: dict[str, Any] = {"url": url, "update_types": UPDATE_TYPES}
        if secret:
            body["secret"] = secret
        return await self.request("POST", "/subscriptions", json=body, idempotent=True)

    async def unsubscribe(self, url: str) -> dict:
        return await self.request("DELETE", "/subscriptions", params={"url": url})

    # ------------------------------------------------------------------ uploads
    async def upload(self, kind: str, filename: str, content: bytes, content_type: str | None = None) -> dict:
        """Загружает файл в MAX и возвращает payload для вложения (обычно {"token": ...})."""
        endpoint = await self.request("POST", "/uploads", params={"type": kind}, idempotent=True)
        url = endpoint["url"]
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0), verify=self._ssl) as up:
            resp = await up.post(url, files={"data": (filename, content, content_type or "application/octet-stream")})
        if resp.status_code >= 400:
            raise MaxApiError(resp.status_code, "upload_failed", resp.text[:300])
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if endpoint.get("token"):  # для видео/аудио токен выдаётся заранее
            return {"token": endpoint["token"]}
        if kind == "image" and "photos" in data:
            return data
        if "token" in data:
            return {"token": data["token"]}
        return data

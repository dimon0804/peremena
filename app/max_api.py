"""Тонкий асинхронный клиент MAX Bot API (https://dev.max.ru/docs-api).

Особенности платформы, которые здесь учтены:
* токен передаётся в заголовке ``Authorization`` без префикса ``Bearer``;
* личные диалоги адресуются по ``user_id``, групповые чаты — по ``chat_id``;
* не больше ~2 сообщений в секунду в один диалог и ~30 запросов в секунду всего —
  поэтому есть общий ограничитель и пауза между сообщениями одному адресату;
* на 429 и 5xx делаем несколько повторов с нарастающей паузой.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
from typing import Any

import certifi
import httpx

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
    """Общий потолок запросов в секунду + минимальный интервал на одного адресата."""

    def __init__(self, rps: float = 25.0, per_peer_interval: float = 0.55):
        self._interval = 1.0 / rps
        self._per_peer = per_peer_interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._peer_last: dict[str, float] = {}

    async def wait(self, peer: str | None = None) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self._interval - now
            if peer:
                delay = max(delay, self._peer_last.get(peer, 0.0) + self._per_peer - now)
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.monotonic()
            self._last = now
            if peer:
                self._peer_last[peer] = now
                if len(self._peer_last) > 5000:
                    cutoff = now - 10
                    self._peer_last = {k: v for k, v in self._peer_last.items() if v > cutoff}


class MaxClient:
    def __init__(self, token: str, base_url: str, extra_ca_file: str | None = None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._ssl = build_ssl_context(extra_ca_file)
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": token},
            timeout=httpx.Timeout(40.0, connect=10.0),
            verify=self._ssl,
        )
        self._limiter = _RateLimiter()

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
        retries: int = 4,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            await self._limiter.wait(peer)
            try:
                resp = await self._http.request(
                    method, path, params=params, json=json,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                attempt += 1
                if attempt > retries:
                    raise MaxApiError(0, "network", str(e)) from e
                await asyncio.sleep(min(2 ** attempt, 15))
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                attempt += 1
                if attempt > retries:
                    raise MaxApiError(resp.status_code, "retry_exhausted", resp.text[:300])
                await asyncio.sleep(min(2 ** attempt, 15))
                continue

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
        body: dict[str, Any] = {"text": text[:TEXT_LIMIT] if text else text, "attachments": attachments}
        if fmt:
            body["format"] = fmt
        return await self.request("PUT", "/messages", params={"message_id": message_id}, json=body)

    async def delete_message(self, message_id: str) -> dict:
        return await self.request("DELETE", "/messages", params={"message_id": message_id})

    async def get_messages(self, chat_id: int, *, count: int = 100, before: int | None = None) -> dict:
        return await self.request("GET", "/messages", params={"chat_id": chat_id, "count": count, "before": before})

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
        return await self.request("GET", f"/chats/{chat_id}")

    async def get_members(self, chat_id: int, limit: int = 2000) -> list[dict]:
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
        return await self.request("POST", "/subscriptions", json=body)

    async def unsubscribe(self, url: str) -> dict:
        return await self.request("DELETE", "/subscriptions", params={"url": url})

    # ------------------------------------------------------------------ uploads
    async def upload(self, kind: str, filename: str, content: bytes, content_type: str | None = None) -> dict:
        """Загружает файл в MAX и возвращает payload для вложения (обычно {"token": ...})."""
        endpoint = await self.request("POST", "/uploads", params={"type": kind})
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

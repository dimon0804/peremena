"""Исходящие вебхуки: «Перемена» сама сообщает внешней системе о событиях класса.

Школьный портал, электронный журнал, 1С, Битрикс24 или таблица через сервис автоматизации получают
POST с JSON, когда родитель ответил на сбор, сбор закрыт, ребёнок отмечен отсутствующим.

Подпись: ``X-Peremena-Signature: sha256=<hex>`` = HMAC-SHA256(секрет, ``<X-Peremena-Timestamp>.<тело>``).
Секрет выдаётся при создании; в базе его нет — он выводится из соли и ключа подписей сервиса.

Безопасность: только https (http — в режиме разработки), адрес должен вести в публичный интернет
(никаких 127.0.0.1, 10.x, 192.168.x, метаданных облака — защита от SSRF, проверка и при создании, и перед
каждой отправкой), без перехода по редиректам, тайм-аут 5 с, до 3 попыток. После 20 неудач подряд
вебхук выключается. Демо-песочница никуда не отправляет — доставка пишется в журнал с пометкой «демо».
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select

from ..core import ctx
from ..db import session, utcnow
from ..demo_ids import is_demo
from ..models import Webhook, WebhookDelivery
from . import tokens

log = logging.getLogger(__name__)

EVENTS = {
    "poll.created": "Учитель запустил сбор",
    "poll.answered": "Родитель ответил на сбор",
    "poll.closed": "Сбор закрыт (итог)",
    "absence.created": "Ребёнок отмечен отсутствующим",
}
MAX_HOOKS = 5
KEEP_DELIVERIES = 30
KEEP_DAYS = 14
DISABLE_AFTER = 20
TIMEOUT = 5.0
RETRY_DELAYS = (0, 2, 8)


class BadUrl(ValueError):
    pass


def secret_for(hook: Webhook) -> str:
    return "whsec_" + tokens._derived("webhook", hook.salt)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ проверка адреса (SSRF)
def _ip_ok(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


async def check_url(url: str) -> str:
    """Проверяет адрес вебхука; возвращает нормализованный или бросает BadUrl с понятной причиной."""
    url = (url or "").strip()
    if len(url) > 1000:
        raise BadUrl("Слишком длинный адрес")
    p = urlsplit(url)
    allow_http = ctx().settings.dev_mode
    if p.scheme != "https" and not (allow_http and p.scheme == "http"):
        raise BadUrl("Адрес должен начинаться с https://")
    if not p.hostname or p.username or p.password:
        raise BadUrl("Укажите адрес вида https://example.ru/hook")
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise BadUrl("Адрес внутри сервера или локальной сети не подходит")
    if allow_http and host in ("127.0.0.1", "::1"):
        return url  # разработка: свой приёмник на этом же компьютере
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM), timeout=5)
    except (OSError, asyncio.TimeoutError):
        raise BadUrl("Не удалось найти такой адрес")
    ips = {i[4][0] for i in infos}
    if not ips or not all(_ip_ok(ip) for ip in ips):
        raise BadUrl("Адрес ведёт во внутреннюю сеть — так нельзя")
    return url


# ------------------------------------------------------------------ управление
async def create(owner_id: int, url: str, events: list[str]) -> tuple[Webhook, str]:
    events = [e for e in events if e in EVENTS] or list(EVENTS)
    if not is_demo(owner_id):
        url = await check_url(url)
    elif not url.startswith("https://") or len(url) > 1000:
        raise BadUrl("Адрес должен начинаться с https://")
    async with session() as s:
        n = len((await s.scalars(select(Webhook.id).where(Webhook.owner_id == owner_id))).all())
        if n >= MAX_HOOKS:
            raise BadUrl(f"Не больше {MAX_HOOKS} вебхуков")
        hook = Webhook(owner_id=owner_id, url=url, events=",".join(events), salt=secrets.token_urlsafe(24))
        s.add(hook)
        await s.flush()
    return hook, secret_for(hook)


async def list_for(owner_id: int) -> list[Webhook]:
    async with session() as s:
        return list((await s.scalars(select(Webhook).where(Webhook.owner_id == owner_id)
                                     .order_by(Webhook.created_at))).all())


async def get_own(owner_id: int, hook_id: int) -> Webhook | None:
    async with session() as s:
        h = await s.get(Webhook, hook_id)
        return h if h and h.owner_id == owner_id else None


async def remove(owner_id: int, hook_id: int) -> bool:
    async with session() as s:
        h = await s.get(Webhook, hook_id)
        if not h or h.owner_id != owner_id:
            return False
        await s.execute(delete(WebhookDelivery).where(WebhookDelivery.webhook_id == hook_id))
        await s.delete(h)
        return True


async def set_active(owner_id: int, hook_id: int, active: bool) -> Webhook | None:
    async with session() as s:
        h = await s.get(Webhook, hook_id)
        if not h or h.owner_id != owner_id:
            return None
        h.active, h.failures = active, 0
        return h


async def deliveries(hook_id: int, limit: int = KEEP_DELIVERIES) -> list[WebhookDelivery]:
    async with session() as s:
        return list((await s.scalars(select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook_id)
                                     .order_by(WebhookDelivery.id.desc()).limit(limit))).all())


# ------------------------------------------------------------------ отправка
def envelope(event: str, data: dict) -> dict:
    return {"id": str(uuid.uuid4()), "event": event, "created_at": utcnow().isoformat() + "Z", "data": data}


async def _record(hook_id: int, event: str, status: str, payload: str, code: int | None = None,
                  error: str = "") -> None:
    async with session() as s:
        s.add(WebhookDelivery(webhook_id=hook_id, event=event, status=status, code=code, error=error[:300],
                              payload=payload[:8000]))
        h = await s.get(Webhook, hook_id)
        if h is not None:
            h.last_at = utcnow()
            h.last_status = {"ok": f"доставлено, HTTP {code}", "demo": "демо: не отправлялось"}.get(
                status, (f"ошибка: {error}" if error else f"ошибка: HTTP {code}"))[:200]
            if status == "error":
                h.failures = (h.failures or 0) + 1
                if h.failures >= DISABLE_AFTER:
                    h.active = False
            else:
                h.failures = 0
        # журнал не растёт: последние KEEP_DELIVERIES на вебхук и не старше KEEP_DAYS
        old = (await s.scalars(select(WebhookDelivery.id).where(WebhookDelivery.webhook_id == hook_id)
                               .order_by(WebhookDelivery.id.desc()).offset(KEEP_DELIVERIES))).all()
        if old:
            await s.execute(delete(WebhookDelivery).where(WebhookDelivery.id.in_(old)))
        await s.execute(delete(WebhookDelivery).where(
            WebhookDelivery.created_at < utcnow() - timedelta(days=KEEP_DAYS)))


async def deliver(hook: Webhook, event: str, data: dict) -> dict:
    """Одна доставка с повторами. Возвращает {"status", "code", "error"} — для кнопки «Проверить»."""
    body_obj = envelope(event, data)
    body = json.dumps(body_obj, ensure_ascii=False).encode()
    pretty = json.dumps(body_obj, ensure_ascii=False, indent=2)
    if is_demo(hook.owner_id):
        await _record(hook.id, event, "demo", pretty)
        return {"status": "demo", "code": None, "error": ""}
    secret = secret_for(hook)
    code, error = None, ""
    for delay in RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            await check_url(hook.url)  # DNS мог поменяться после создания вебхука
        except BadUrl as e:
            code, error = None, str(e)
            break
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "Peremena-Webhook/1.0",
                   "X-Peremena-Event": event, "X-Peremena-Delivery": body_obj["id"],
                   "X-Peremena-Timestamp": ts, "X-Peremena-Signature": sign(secret, ts, body)}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
                r = await client.post(hook.url, content=body, headers=headers)
            code, error = r.status_code, ""
            if 200 <= r.status_code < 300:
                await _record(hook.id, event, "ok", pretty, code)
                return {"status": "ok", "code": code, "error": ""}
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                break  # приёмник отказал осознанно — повторять незачем
        except httpx.HTTPError as e:
            code, error = None, type(e).__name__
    await _record(hook.id, event, "error", pretty, code, error)
    return {"status": "error", "code": code, "error": error}


async def emit(event: str, owner_ids: set[int], data: dict) -> int:
    """Событие для всех активных вебхуков владельцев, подписанных на него. Доставка — в фоне."""
    owner_ids = {o for o in owner_ids if o}
    if not owner_ids:
        return 0
    try:
        async with session() as s:
            hooks = (await s.scalars(select(Webhook).where(Webhook.owner_id.in_(owner_ids),
                                                           Webhook.active.is_(True)))).all()
    except Exception:
        log.exception("Вебхуки: не удалось прочитать подписки")
        return 0
    hooks = [h for h in hooks if event in (h.events or "").split(",")]
    if not hooks:
        return 0
    from ..updates import spawn  # фоновая задача; при остановке сервиса её дождётся drain()

    async def run(h):
        try:
            await deliver(h, event, data)
        except Exception:
            log.exception("Вебхук %s: ошибка доставки", h.id)

    for h in hooks:
        spawn(run(h))
    return len(hooks)


# ------------------------------------------------------------------ события предметной области
async def _owners_for_chat(chat_id: int, teacher_id: int | None) -> set[int]:
    """Кому интересно событие класса: классному руководителю и администрации его школы."""
    from ..models import SchoolMember

    owners = {teacher_id} if teacher_id else set()
    if teacher_id:
        async with session() as s:
            sid = await s.scalar(select(SchoolMember.school_id).where(SchoolMember.user_id == teacher_id))
            if sid:
                owners |= set((await s.scalars(select(SchoolMember.user_id).where(
                    SchoolMember.school_id == sid, SchoolMember.role == "admin"))).all())
    return owners


async def _chat_title(chat_id: int) -> str:
    from ..services import chats

    c = await chats.get_chat(chat_id)
    return c.title if c else ""


async def poll_brief(p) -> dict:
    from ..services import polls

    answered, total, per = polls.counts(p)
    out = {"id": p.id, "title": p.title, "template": p.template, "options": p.options,
           "class": {"id": p.chat_id, "title": await _chat_title(p.chat_id)},
           "deadline": p.deadline.isoformat() + "Z" if p.deadline else None, "closed": p.closed,
           "answered": answered, "total": total, "per_option": per}
    m = polls.money(p)
    if m:
        out["money"] = {k: m[k] for k in ("amount", "expected", "collected", "participants", "paid")}
    return out


async def safe(coro) -> None:
    """Вебхук не должен ломать основной сценарий (ответ родителя, закрытие сбора)."""
    try:
        await coro
    except Exception:
        log.exception("Вебхуки: событие не отправлено")


async def on_poll_created(p) -> None:
    await emit("poll.created", await _owners_for_chat(p.chat_id, p.teacher_id), {"poll": await poll_brief(p)})


async def on_poll_answered(p, user_id: int, option: int, status: str) -> None:
    from ..services import chats, polls

    p = await polls.get_poll(p.id)  # свежий список участников (объект из record_answer уже без сессии)
    if p is None:
        return
    target = next((t for t in p.targets if t.user_id == user_id), None)
    child = None
    async with session() as s:
        from ..models import Member

        m = await s.scalar(select(Member).where(Member.chat_id == p.chat_id, Member.user_id == user_id))
        child = m.child_name if m else None
    data = {"poll": await poll_brief(p),
            "participant": {"name": target.name if target else await chats.member_name(p.chat_id, user_id),
                            "child": child},
            "answer": {"index": option, "text": p.options[option] if 0 <= option < len(p.options) else ""},
            "changed": status == "changed"}
    await emit("poll.answered", await _owners_for_chat(p.chat_id, p.teacher_id), data)


async def on_poll_closed(p) -> None:
    data = {"poll": await poll_brief(p),
            "results": [{"name": t.name, "answer": p.options[t.answer] if t.answer is not None
                         and t.answer < len(p.options) else None, "paid": t.paid if p.amount else None}
                        for t in p.targets]}
    await emit("poll.closed", await _owners_for_chat(p.chat_id, p.teacher_id), data)


async def on_absence(a, teacher_id: int | None) -> None:
    from ..services.attendance import REASONS

    data = {"class": {"id": a.chat_id, "title": await _chat_title(a.chat_id)}, "child": a.child_name or None,
            "day": a.day, "reason": a.reason, "reason_text": REASONS.get(a.reason, "")}
    await emit("absence.created", await _owners_for_chat(a.chat_id, teacher_id), data)


SAMPLE = {"poll": {"id": 0, "title": "Проверка вебхука", "class": {"id": 0, "title": "9Б"}, "answered": 0, "total": 0}}

"""Секреты интеграций: ссылки на календарь и ключи API.

В базе хранится только SHA-256 токена — по нему токен и находится. Ключ API показывается один раз.
Ссылку на календарь человек может открыть снова: токен выводится из случайной соли и ключа подписей
сервиса (SECRET_KEY), так что одной копии таблицы для подделки недостаточно. Отзыв — отметка revoked_at.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from sqlalchemy import select, update

from ..core import ctx
from ..db import session, utcnow
from ..models import ClassChat, IntegrationToken
from ..services import school

API_PREFIX = "prm_"
ICAL_PREFIX = "cal_"
MAX_API_KEYS = 5


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _derived(purpose: str, salt: str) -> str:
    mac = hmac.new(ctx().settings.signing_key, f"{purpose}:{salt}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:24]).decode().rstrip("=")


def ical_token(salt: str) -> str:
    return ICAL_PREFIX + _derived("ical", salt)


def public_base(request=None) -> str:
    """Публичный адрес сервиса для ссылок наружу: PUBLIC_URL, домен вебхука или адрес запроса."""
    s = ctx().settings
    if s.public_url:
        return s.public_url.rstrip("/")
    if s.webhook_url:
        p = urlsplit(s.webhook_url)
        return f"{p.scheme}://{p.netloc}"
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


# ------------------------------------------------------------------ календарь
async def ical_row(owner_id: int, create: bool = True) -> IntegrationToken | None:
    async with session() as s:
        row = await s.scalar(select(IntegrationToken).where(
            IntegrationToken.owner_id == owner_id, IntegrationToken.kind == "ical",
            IntegrationToken.revoked_at.is_(None)))
        if row is None and create:
            salt = secrets.token_urlsafe(24)
            raw = ical_token(salt)
            row = IntegrationToken(owner_id=owner_id, kind="ical", name="Календарь", salt=salt,
                                   token_hash=digest(raw), prefix=raw[:8])
            s.add(row)
            await s.flush()
        return row


async def ical_link(owner_id: int) -> str:
    row = await ical_row(owner_id)
    return ical_token(row.salt)


async def rotate_ical(owner_id: int) -> str:
    """Новая ссылка; старая перестаёт работать сразу (если её переслали не тому)."""
    async with session() as s:
        await s.execute(update(IntegrationToken).where(
            IntegrationToken.owner_id == owner_id, IntegrationToken.kind == "ical",
            IntegrationToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    return await ical_link(owner_id)


# ------------------------------------------------------------------ ключи API
async def create_api_key(owner_id: int, name: str, can_write: bool = False) -> tuple[IntegrationToken, str] | None:
    """(запись, ключ) или None, если ключей уже слишком много. Ключ больше нигде не хранится."""
    async with session() as s:
        n = len((await s.scalars(select(IntegrationToken.id).where(
            IntegrationToken.owner_id == owner_id, IntegrationToken.kind == "api",
            IntegrationToken.revoked_at.is_(None)))).all())
        if n >= MAX_API_KEYS:
            return None
        raw = API_PREFIX + secrets.token_urlsafe(32)
        row = IntegrationToken(owner_id=owner_id, kind="api", name=(name or "Ключ API").strip()[:100],
                               token_hash=digest(raw), prefix=raw[:10], can_write=can_write)
        s.add(row)
        await s.flush()
        return row, raw


async def list_api_keys(owner_id: int) -> list[IntegrationToken]:
    async with session() as s:
        return list((await s.scalars(select(IntegrationToken).where(
            IntegrationToken.owner_id == owner_id, IntegrationToken.kind == "api",
            IntegrationToken.revoked_at.is_(None)).order_by(IntegrationToken.created_at))).all())


async def revoke(owner_id: int, token_id: int) -> bool:
    async with session() as s:
        row = await s.get(IntegrationToken, token_id)
        if row is None or row.owner_id != owner_id or row.revoked_at is not None:
            return False
        row.revoked_at = utcnow()
        return True


async def resolve(raw: str, kind: str) -> IntegrationToken | None:
    """Действующий токен по его значению; отмечает использование."""
    if not raw or len(raw) > 200:
        return None
    async with session() as s:
        row = await s.scalar(select(IntegrationToken).where(IntegrationToken.token_hash == digest(raw)))
        if row is None or row.kind != kind or row.revoked_at is not None:
            return None
        # сравнение хешей уже прошло в индексе; повторная проверка — постоянного времени
        if not hmac.compare_digest(row.token_hash, digest(raw)):
            return None
        row.last_used_at = utcnow()
        row.uses = (row.uses or 0) + 1
        return row


# ------------------------------------------------------------------ чьи данные видит интеграция
async def scope_chats(owner_id: int) -> dict[int, ClassChat]:
    """Классы, данные которых доступны интеграции владельца: свои классы, а у администрации — вся школа."""
    r = await school.roles(owner_id)
    out = {c.chat_id: c for c in r.class_chats}
    if r.is_admin and r.school:
        for c in await school.school_chats(r.school.id):
            out.setdefault(c.chat_id, c)
    return out


async def can_integrate(owner_id: int) -> bool:
    """Ключи API и вебхуки — для классного руководителя и администрации школы."""
    r = await school.roles(owner_id)
    return r.is_class_teacher or r.is_admin

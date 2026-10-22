"""Вход на сайт через бота — без паролей.

1. Сайт запрашивает одноразовый код и показывает ссылку max.ru/<бот>?start=login_<код> (и QR).
2. Пользователь открывает ссылку в MAX, бот спрашивает «Подтвердить вход?» — подтверждение кнопкой
   защищает от того, кто подсунет чужую ссылку.
3. Сайт опрашивает статус кода и, как только вход подтверждён, получает сессию в httpOnly-cookie.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select

from ..core import ctx
from ..db import session, utcnow
from ..models import User, WebSession

CODE_TTL = 10 * 60
SESSION_DAYS = 30
COOKIE = "pm_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def start_login(user_agent: str = "") -> str:
    code = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")  # payload MAX: [A-Za-z0-9_-]
    await ctx().kv.set(f"login:{code}", {"status": "pending", "ua": user_agent[:300]}, CODE_TTL)
    return code


async def get_login(code: str) -> dict | None:
    if not code or len(code) > 64:
        return None
    return await ctx().kv.get(f"login:{code}")


async def confirm_login(code: str, user_id: int) -> bool:
    data = await get_login(code)
    if not data or data.get("status") != "pending":
        return False
    data.update(status="confirmed", user_id=user_id)
    await ctx().kv.set(f"login:{code}", data, CODE_TTL)
    return True


async def reject_login(code: str) -> None:
    data = await get_login(code)
    if data:
        data["status"] = "rejected"
        await ctx().kv.set(f"login:{code}", data, CODE_TTL)


async def finish_login(code: str) -> tuple[str, str | None]:
    """(статус, токен). Токен выдаётся один раз — код после этого сгорает."""
    data = await get_login(code)
    if not data:
        return "expired", None
    if data.get("status") != "confirmed":
        return data.get("status", "pending"), None
    await ctx().kv.delete(f"login:{code}")
    token = secrets.token_urlsafe(32)
    async with session() as s:
        s.add(WebSession(token_hash=_hash(token), user_id=data["user_id"], user_agent=data.get("ua", ""),
                         expires_at=utcnow() + timedelta(days=SESSION_DAYS)))
    return "ok", token


async def user_by_token(token: str | None) -> User | None:
    if not token or len(token) > 100:
        return None
    async with session() as s:
        ws = await s.scalar(select(WebSession).where(WebSession.token_hash == _hash(token)))
        if ws is None or ws.expires_at < utcnow():
            return None
        return await s.get(User, ws.user_id)


async def logout(token: str | None) -> None:
    if token:
        async with session() as s:
            await s.execute(delete(WebSession).where(WebSession.token_hash == _hash(token)))


async def cleanup() -> None:
    async with session() as s:
        await s.execute(delete(WebSession).where(WebSession.expires_at < utcnow()))

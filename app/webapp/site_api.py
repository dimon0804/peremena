"""API сайта: вход через бота и публичная статистика для главной страницы."""
from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import func, select

from ..core import ctx
from ..db import session, utcnow
from ..demo_ids import is_demo
from ..models import ClassChat, FaqEntry, Metric, Poll, PollTarget, School
from ..services import demo, websession

router = APIRouter(prefix="/api")


def _qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]  # без XML-пролога — вставляется прямо в страницу


@router.post("/auth/start")
async def auth_start(request: Request):
    c = ctx()
    if not c.bot_username:
        raise HTTPException(503, "Бот не подключён — вход через MAX временно недоступен")
    code = await websession.start_login(request.headers.get("user-agent", ""))
    pin = await websession.make_pin(code)
    link = c.start_link(f"login_{code}")
    return {"code": code, "pin": f"{pin[:3]} {pin[3:]}", "link": link, "qr_svg": _qr_svg(link),
            "bot_username": c.bot_username, "expires_in": websession.CODE_TTL}


@router.get("/auth/status")
async def auth_status(request: Request, response: Response, code: str = Query(max_length=64)):
    status, token = await websession.finish_login(code)
    if token:
        secure = request.url.scheme == "https"
        response.set_cookie(websession.COOKIE, token, max_age=websession.SESSION_DAYS * 86400,
                            httponly=True, samesite="lax", secure=secure, path="/")
    return {"status": status}


@router.get("/auth/session")
async def auth_session(request: Request):
    """Вошёл ли посетитель. Всегда 200 — гость не должен видеть ошибку 401 в консоли."""
    user = await websession.user_by_token(request.cookies.get(websession.COOKIE))
    return {"logged_in": user is not None, "name": user.display_name if user else "",
            "demo": bool(user and is_demo(user.user_id)), "demo_enabled": ctx().settings.demo_enabled}


def _set_session(request: Request, response: Response, token: str, max_age: int) -> None:
    response.set_cookie(websession.COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


@router.post("/demo/start")
async def demo_start(request: Request, response: Response):
    """Песочница для жюри и гостей: вымышленная школа, вход без MAX. Сообщения никуда не уходят."""
    c = ctx()
    if not c.settings.demo_enabled:
        raise HTTPException(404, "Демо-режим выключен")
    if request.headers.get("x-peremena") != "1":
        raise HTTPException(403, "Запрос без заголовка X-Peremena")
    key = f"demoip:{_client_ip(request)}:{utcnow():%Y%m%d%H}"
    used = int(await c.kv.get(key) or 0)
    if used >= c.settings.demo_per_ip_hour:
        raise HTTPException(429, "Слишком много демо-песочниц с одного адреса — попробуйте через час")
    await c.kv.set(key, used + 1, 3600)
    await websession.logout(request.cookies.get(websession.COOKIE))
    sb = await demo.create_sandbox()
    token = await websession.issue(sb.teacher_id, "demo", days=1)
    _set_session(request, response, token, demo.TTL_HOURS * 3600)
    return {"ok": True, "role": "teacher"}


@router.post("/demo/role")
async def demo_role(request: Request, response: Response, role: str = Query(pattern="^(teacher|parent)$")):
    """Переключение «глазами учителя / глазами родителя» внутри своей песочницы."""
    if request.headers.get("x-peremena") != "1":
        raise HTTPException(403, "Запрос без заголовка X-Peremena")
    token = request.cookies.get(websession.COOKIE)
    user = await websession.user_by_token(token)
    sb = await demo.sandbox_for(user.user_id) if user else None
    if sb is None:
        raise HTTPException(403, "Переключение ролей есть только в демо")
    await websession.logout(token)
    new = await websession.issue(sb.teacher_id if role == "teacher" else sb.parent_id, "demo", days=1)
    _set_session(request, response, new, demo.TTL_HOURS * 3600)
    return {"ok": True, "role": role}


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    await websession.logout(request.cookies.get(websession.COOKIE))
    response.delete_cookie(websession.COOKIE, path="/")
    return {"ok": True}


@router.get("/public/stats")
async def public_stats():
    """Агрегаты без персональных данных — для живого счётчика на главной."""
    async with session() as s:
        real_chat = demo.not_demo_chat(ClassChat.chat_id)  # песочницы демо в счётчик не попадают
        classes = await s.scalar(select(func.count()).select_from(ClassChat).where(ClassChat.active.is_(True), real_chat))
        teachers = await s.scalar(select(func.count(func.distinct(ClassChat.teacher_id)))
                                  .where(ClassChat.active.is_(True), ClassChat.teacher_id.is_not(None), real_chat))
        schools = await s.scalar(select(func.count()).select_from(School).where(demo.not_demo_user(School.created_by)))
        polls_n = await s.scalar(select(func.count()).select_from(Poll).where(demo.not_demo_chat(Poll.chat_id)))
        answers = await s.scalar(select(func.count()).select_from(PollTarget)
                                 .where(PollTarget.answer.is_not(None), demo.not_demo_user(PollTarget.user_id)))
        faq_n = await s.scalar(select(func.count()).select_from(FaqEntry).where(demo.not_demo_user(FaqEntry.teacher_id)))
        minutes = await s.scalar(select(func.coalesce(func.sum(Metric.minutes), 0))
                                 .where(demo.not_demo_user(Metric.teacher_id)))
    c = ctx()
    return {
        "classes": classes or 0, "teachers": teachers or 0, "schools": schools or 0, "polls": polls_n or 0,
        "answers": answers or 0, "faq": faq_n or 0, "minutes_saved": round(float(minutes or 0)),
        "bot_username": c.bot_username, "bot_link": c.start_link() if c.bot_username else "",
    }

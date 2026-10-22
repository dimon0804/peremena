"""API сайта: вход через бота и публичная статистика для главной страницы."""
from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import func, select

from ..core import ctx
from ..db import session
from ..models import ClassChat, FaqEntry, Metric, Poll, PollTarget, School
from ..services import websession

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
    return {"logged_in": user is not None, "name": user.display_name if user else ""}


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    await websession.logout(request.cookies.get(websession.COOKIE))
    response.delete_cookie(websession.COOKIE, path="/")
    return {"ok": True}


@router.get("/public/stats")
async def public_stats():
    """Агрегаты без персональных данных — для живого счётчика на главной."""
    async with session() as s:
        classes = await s.scalar(select(func.count()).select_from(ClassChat).where(ClassChat.active.is_(True)))
        teachers = await s.scalar(select(func.count(func.distinct(ClassChat.teacher_id)))
                                  .where(ClassChat.active.is_(True), ClassChat.teacher_id.is_not(None)))
        schools = await s.scalar(select(func.count()).select_from(School))
        polls_n = await s.scalar(select(func.count()).select_from(Poll))
        answers = await s.scalar(select(func.count()).select_from(PollTarget).where(PollTarget.answer.is_not(None)))
        faq_n = await s.scalar(select(func.count()).select_from(FaqEntry))
        minutes = await s.scalar(select(func.coalesce(func.sum(Metric.minutes), 0)))
    c = ctx()
    return {
        "classes": classes or 0, "teachers": teachers or 0, "schools": schools or 0, "polls": polls_n or 0,
        "answers": answers or 0, "faq": faq_n or 0, "minutes_saved": round(float(minutes or 0)),
        "bot_username": c.bot_username, "bot_link": c.start_link() if c.bot_username else "",
    }

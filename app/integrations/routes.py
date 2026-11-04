"""HTTP-слой интеграций: управление из мини-приложения, лента календаря, печать, «неделя».

* ``/api/integrations/*`` — календарь, ключи API, вебхуки (вход как в мини-приложении);
* ``/cal/<токен>.ics`` — лента для календаря (без входа, по секретной ссылке);
* ``/api/polls/{id}/print/{sheet|blanks}`` — ведомость и бланки (по подписанной ссылке на сутки);
* ``/api/my/week`` — «что важно на неделе».
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .. import ratelimit
from ..models import User
from ..services import polls, week
from ..webapp.api import current_user
from . import calendar, printing, tokens, webhooks

router = APIRouter()

FEED_PER_HOUR = 120


def _dt(v) -> str | None:
    return v.isoformat() + "Z" if v else None


async def _integrator(user: User = Depends(current_user)) -> User:
    if not await tokens.can_integrate(user.user_id):
        raise HTTPException(403, "Ключи API и вебхуки — для классного руководителя и администрации школы")
    return user


def _key_out(k) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "can_write": k.can_write,
            "created_at": _dt(k.created_at), "last_used_at": _dt(k.last_used_at), "uses": k.uses}


def _hook_out(h) -> dict:
    return {"id": h.id, "url": h.url, "events": [e for e in (h.events or "").split(",") if e], "active": h.active,
            "failures": h.failures, "last_status": h.last_status, "last_at": _dt(h.last_at),
            "created_at": _dt(h.created_at)}


async def _calendar(user_id: int, request: Request) -> dict:
    token = await tokens.ical_link(user_id)
    return calendar.links(f"{tokens.public_base(request)}/cal/{token}.ics")


# ------------------------------------------------------------------ обзор
@router.get("/api/integrations")
async def overview(request: Request, user: User = Depends(current_user)):
    can = await tokens.can_integrate(user.user_id)
    out = {"calendar": await _calendar(user.user_id, request), "can_integrate": can,
           "events": webhooks.EVENTS, "api_base": tokens.public_base(request) + "/api/v1",
           "docs_url": "/api/v1/openapi.json", "api_keys": [], "webhooks": []}
    if can:
        out["api_keys"] = [_key_out(k) for k in await tokens.list_api_keys(user.user_id)]
        out["webhooks"] = [_hook_out(h) for h in await webhooks.list_for(user.user_id)]
    return out


@router.post("/api/integrations/calendar/rotate")
async def rotate_calendar(request: Request, user: User = Depends(current_user)):
    await tokens.rotate_ical(user.user_id)
    return await _calendar(user.user_id, request)


# ------------------------------------------------------------------ ключи API
class KeyIn(BaseModel):
    name: str = Field("", max_length=100)
    can_write: bool = False


@router.post("/api/integrations/keys")
async def create_key(body: KeyIn, user: User = Depends(_integrator)):
    res = await tokens.create_api_key(user.user_id, body.name, body.can_write)
    if res is None:
        raise HTTPException(409, f"Не больше {tokens.MAX_API_KEYS} ключей — отзовите ненужный")
    row, raw = res
    return {"key": raw, **_key_out(row)}


@router.delete("/api/integrations/keys/{kid}")
async def revoke_key(kid: int, user: User = Depends(_integrator)):
    if not await tokens.revoke(user.user_id, kid):
        raise HTTPException(404, "Ключ не найден")
    return {"ok": True}


# ------------------------------------------------------------------ вебхуки
class HookIn(BaseModel):
    url: str = Field(min_length=10, max_length=1000)
    events: list[str] = Field(default_factory=list)


class ActiveIn(BaseModel):
    active: bool


async def _hook(user: User, hid: int):
    h = await webhooks.get_own(user.user_id, hid)
    if h is None:
        raise HTTPException(404, "Вебхук не найден")
    return h


@router.post("/api/integrations/webhooks")
async def create_hook(body: HookIn, user: User = Depends(_integrator)):
    if not await ratelimit.hit("hookadd", user.user_id, 20, 3600):
        raise HTTPException(429, "Слишком часто — попробуйте через час")
    try:
        hook, secret = await webhooks.create(user.user_id, body.url, body.events)
    except webhooks.BadUrl as e:
        raise HTTPException(400, str(e))
    return {"webhook": _hook_out(hook), "secret": secret}


@router.get("/api/integrations/webhooks/{hid}/secret")
async def hook_secret(hid: int, user: User = Depends(_integrator)):
    return {"secret": webhooks.secret_for(await _hook(user, hid))}


@router.put("/api/integrations/webhooks/{hid}")
async def hook_active(hid: int, body: ActiveIn, user: User = Depends(_integrator)):
    h = await webhooks.set_active(user.user_id, hid, body.active)
    if h is None:
        raise HTTPException(404, "Вебхук не найден")
    return _hook_out(h)


@router.delete("/api/integrations/webhooks/{hid}")
async def delete_hook(hid: int, user: User = Depends(_integrator)):
    if not await webhooks.remove(user.user_id, hid):
        raise HTTPException(404, "Вебхук не найден")
    return {"ok": True}


@router.post("/api/integrations/webhooks/{hid}/test")
async def test_hook(hid: int, user: User = Depends(_integrator)):
    h = await _hook(user, hid)
    if not await ratelimit.hit("hooktest", user.user_id, 10, 60):
        raise HTTPException(429, "Слишком часто — подождите минуту")
    return await webhooks.deliver(h, "ping", webhooks.SAMPLE)


@router.get("/api/integrations/webhooks/{hid}/deliveries")
async def hook_log(hid: int, user: User = Depends(_integrator)):
    await _hook(user, hid)
    return [{"id": d.id, "event": d.event, "status": d.status, "code": d.code, "error": d.error,
             "payload": d.payload, "created_at": _dt(d.created_at)} for d in await webhooks.deliveries(hid)]


# ------------------------------------------------------------------ лента календаря
@router.get("/cal/{token}.ics", include_in_schema=False)
async def ical_feed(token: str):
    row = await tokens.resolve(token, "ical")
    if row is None:
        raise HTTPException(404, "Ссылка на календарь не найдена или заменена новой")
    if not await ratelimit.hit("ical", row.id, FEED_PER_HOUR, 3600):
        raise HTTPException(429, "Слишком часто", headers={"Retry-After": "3600"})
    from ..services import chats

    user = await chats.get_user(row.owner_id)
    if user is None:
        raise HTTPException(404, "Ссылка на календарь не найдена")
    body = await calendar.feed_for(user)
    return Response(body, media_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": 'inline; filename="peremena.ics"', "Cache-Control": "private, max-age=900",
        "X-Robots-Tag": "noindex, nofollow"})


# ------------------------------------------------------------------ печать
async def _own_poll(pid: int, user: User):
    p = await polls.get_poll(pid)
    if p is None or p.teacher_id != user.user_id:
        raise HTTPException(404, "Сбор не найден")
    return p


@router.get("/api/polls/{pid}/print-links")
async def print_links(pid: int, user: User = Depends(current_user)):
    p = await _own_poll(pid, user)
    return {"sheet": printing.url(p, "sheet"), "blanks": printing.url(p, "blanks"),
            "blanks_missing": printing.url(p, "blanks", only_missing=True)}


@router.get("/api/polls/{pid}/print/{kind}", response_class=HTMLResponse)
async def print_page(pid: int, kind: str, sig: str = Query(max_length=64), exp: int = 0, only: str = ""):
    p = await polls.get_poll(pid)
    if not printing.check(p, kind, exp, sig):
        raise HTTPException(404, "Ссылка устарела — откройте сбор и нажмите «Печать» заново")
    page = await (printing.sheet(p) if kind == "sheet" else printing.blanks(p, only_missing=only == "missing"))
    return HTMLResponse(page, headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"})


# ------------------------------------------------------------------ неделя
@router.get("/api/my/week")
async def my_week(user: User = Depends(current_user)):
    return await week.collect(user)

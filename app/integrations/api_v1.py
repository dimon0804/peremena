"""Открытый REST API v1 для школьных систем: портал школы, электронный журнал, 1С, таблицы, BI.

Доступ — по ключу (``Authorization: Bearer prm_…`` или ``X-Api-Key``), который классный руководитель или
администрация школы создаёт в мини-приложении («Ещё → Интеграции»). Ключ видит только классы владельца
(у администрации — все классы школы). Ключ «только чтение» по умолчанию; создавать сборы может ключ
с правом записи. Лимит: 120 запросов в минуту на ключ, 30 новых сборов в сутки.

Описание в формате OpenAPI: ``GET /api/v1/openapi.json``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import __version__, ratelimit
from ..db import session
from ..models import IntegrationToken, Member, Poll
from ..services import attendance, board, chats, polls
from ..timeutil import local_now, preset_deadline, to_utc, utcnow, zone
from . import tokens, webhooks

router = APIRouter(prefix="/api/v1", tags=["Открытый API"])

PER_MINUTE = 120
POLLS_PER_DAY = 30


class Key:
    def __init__(self, row: IntegrationToken, chats_: dict):
        self.row, self.chats = row, chats_

    @property
    def owner_id(self) -> int:
        return self.row.owner_id


async def api_key(authorization: str | None = Header(default=None, description="Bearer prm_…"),
                  x_api_key: str | None = Header(default=None)) -> Key:
    raw = x_api_key
    if not raw and authorization and authorization[:7].lower() == "bearer ":
        raw = authorization[7:].strip()
    row = await tokens.resolve(raw or "", "api")
    if row is None:
        raise HTTPException(401, "Нужен действующий ключ API: заголовок Authorization: Bearer prm_…",
                            headers={"WWW-Authenticate": "Bearer"})
    if not await ratelimit.hit("apiv1", row.id, PER_MINUTE, 60):
        raise HTTPException(429, "Слишком много запросов: не больше 120 в минуту", headers={"Retry-After": "60"})
    scope = await tokens.scope_chats(row.owner_id)
    if not scope and not await tokens.can_integrate(row.owner_id):
        raise HTTPException(403, "У владельца ключа больше нет классов")
    return Key(row, scope)


def _class(key: Key, chat_id: int) -> dict:
    c = key.chats.get(chat_id)
    return {"id": chat_id, "title": c.title if c else ""}


async def _poll(key: Key, pid: int) -> Poll:
    p = await polls.get_poll(pid)
    if p is None or p.chat_id not in key.chats:
        raise HTTPException(404, "Сбор не найден")
    return p


def _brief(key: Key, p: Poll) -> dict:
    answered, total, per = polls.counts(p)
    out = {"id": p.id, "title": p.title, "text": p.text, "template": p.template, "options": p.options,
           "class": _class(key, p.chat_id), "created_at": p.created_at.isoformat() + "Z",
           "deadline": p.deadline.isoformat() + "Z" if p.deadline else None, "closed": p.closed,
           "answered": answered, "total": total, "per_option": per, "amount": p.amount}
    m = polls.money(p)
    if m:
        out["money"] = {k: m[k] for k in ("amount", "expected", "collected", "participants", "paid")}
    return out


# ------------------------------------------------------------------ чтение
@router.get("/me", summary="Чей ключ и какие классы он видит")
async def me(key: Key = Depends(api_key)):
    owner = await chats.get_user(key.owner_id)
    return {"owner": owner.display_name if owner else "", "key": {"name": key.row.name, "prefix": key.row.prefix,
                                                                     "can_write": key.row.can_write},
            "classes": [{"id": c.chat_id, "title": c.title} for c in key.chats.values()],
            "events": list(webhooks.EVENTS), "version": __version__}


@router.get("/classes", summary="Классы")
async def classes(key: Key = Depends(api_key)):
    out = []
    for c in key.chats.values():
        out.append({"id": c.chat_id, "title": c.title, "bot_is_admin": c.bot_is_admin,
                    "members": len(await chats.class_list(c.chat_id, {c.teacher_id} if c.teacher_id else set()))})
    return out


@router.get("/classes/{class_id}/members", summary="Список класса: родитель и ребёнок")
async def class_members(class_id: int, key: Key = Depends(api_key)):
    c = key.chats.get(class_id)
    if c is None:
        raise HTTPException(404, "Класс не найден")
    rows = await chats.class_list(class_id, {c.teacher_id} if c.teacher_id else set())
    return [{"name": m.name, "child": m.child_name} for m in rows]


@router.get("/polls", summary="Сборы")
async def list_polls(status: str = Query("all", pattern="^(open|closed|all)$"), class_id: int | None = None,
                     limit: int = Query(50, ge=1, le=200), key: Key = Depends(api_key)):
    ids = [class_id] if class_id is not None else list(key.chats)
    ids = [i for i in ids if i in key.chats]
    if not ids:
        return []
    q = select(Poll).where(Poll.chat_id.in_(ids))
    if status != "all":
        q = q.where(Poll.closed.is_(status == "closed"))
    async with session() as s:
        rows = (await s.scalars(q.order_by(Poll.created_at.desc()).limit(limit))).all()
    return [_brief(key, p) for p in rows]


@router.get("/polls/{poll_id}", summary="Сбор с ответами каждого участника")
async def get_poll(poll_id: int, key: Key = Depends(api_key)):
    p = await _poll(key, poll_id)
    async with session() as s:
        kids = {m.user_id: m.child_name for m in (await s.scalars(
            select(Member).where(Member.chat_id == p.chat_id))).all()}
    out = _brief(key, p)
    out["results"] = [{"name": t.name, "child": kids.get(t.user_id),
                       "answer_index": t.answer,
                       "answer": p.options[t.answer] if t.answer is not None and t.answer < len(p.options) else None,
                       "answered_at": t.answered_at.isoformat() + "Z" if t.answered_at else None,
                       "reminders": t.reminders, "paid": t.paid if p.amount else None} for t in p.targets]
    return out


@router.get("/attendance", summary="Кто не придёт (по дням)")
async def get_attendance(day: date | None = Query(None, description="YYYY-MM-DD, по умолчанию сегодня"),
                         days: int = Query(1, ge=1, le=14), key: Key = Depends(api_key)):
    owner = await chats.get_user(key.owner_id)
    start = day or local_now(owner.tz if owner else None).date()
    want = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    rows = await attendance.for_days(list(key.chats), want)
    return [{"class": _class(key, a.chat_id), "child": a.child_name or None, "day": a.day, "reason": a.reason,
             "reason_text": attendance.REASONS.get(a.reason, "")} for a in rows]


@router.get("/board", summary="Домашние задания и объявления")
async def get_board(days: int = Query(14, ge=1, le=60), kind: str | None = Query(None, pattern="^(hw|news)$"),
                    key: Key = Depends(api_key)):
    items = await board.list_for_chats(list(key.chats), days=days, limit=300)
    return [{"id": a.id, "class": _class(key, a.chat_id), "kind": a.kind, "subject": a.subject or None,
             "text": a.text, "for_date": a.for_date, "created_at": a.created_at.isoformat() + "Z"}
            for a in items if board.is_visible(a) and (kind is None or a.kind == kind)]


@router.get("/schedule", summary="Расписание уроков (из выгрузки журнала или «Дневник.ру»)")
async def get_schedule(day: date | None = Query(None, description="YYYY-MM-DD, по умолчанию сегодня"),
                       days: int = Query(7, ge=1, le=31), class_id: int | None = None,
                       key: Key = Depends(api_key)):
    from .journals import service as journal

    owner = await chats.get_user(key.owner_id)
    start = day or local_now(owner.tz if owner else None).date()
    ids = [i for i in ([class_id] if class_id is not None else list(key.chats)) if i in key.chats]
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        for cid, lessons in (await journal.lessons_for(ids, d)).items():
            out += [{"class": _class(key, cid), "day": d.isoformat(), "number": x.number or None,
                     "subject": x.subject, "time": x.time or None, "room": x.room or None} for x in lessons]
    return out


@router.get("/export/{kind}.csv", summary="Выгрузка для журнала: absences — отсутствия, polls — итоги сборов",
            response_class=Response)
async def export_csv(kind: str, start: date = Query(alias="from"), end: date = Query(alias="to"),
                     class_id: int | None = None, key: Key = Depends(api_key)):
    """CSV в UTF-8 с BOM, разделитель «;» — открывается в Excel и загружается в импорт журналов и таблиц."""
    from .journals import export

    if kind not in ("absences", "polls"):
        raise HTTPException(404, "Такой выгрузки нет: absences или polls")
    if end < start or (end - start).days > 400:
        raise HTTPException(422, "Период — от from до to, не длиннее 400 дней")
    ids = [i for i in ([class_id] if class_id is not None else list(key.chats)) if i in key.chats]
    titles = {cid: c.title for cid, c in key.chats.items()}
    owner = await chats.get_user(key.owner_id)
    data = await (export.absences_csv(ids, titles, start, end) if kind == "absences"
                  else export.polls_csv(ids, titles, start, end, owner.tz if owner else None))
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{kind}_{start:%Y%m%d}-{end:%Y%m%d}.csv"'})


# ------------------------------------------------------------------ запись
class PollCreate(BaseModel):
    class_id: int
    text: str = Field(min_length=3, max_length=3000, description="Первая строка станет заголовком")
    template: str = Field("consent", description="consent | certificate | ack | money | choice")
    options: list[str] = Field(default_factory=list, description="Для choice: 2–10 вариантов")
    deadline: str | None = Field(None, description="ISO 8601 (без пояса — время учителя) или today|tomorrow|friday|week")
    amount: int | None = Field(None, ge=1, le=100_000, description="Для money: рублей с каждого")


@router.post("/polls", summary="Запустить сбор (ключ с правом записи)", status_code=201)
async def create_poll(body: PollCreate, key: Key = Depends(api_key)):
    if not key.row.can_write:
        raise HTTPException(403, "Ключ только для чтения — создайте ключ с правом записи")
    c = key.chats.get(body.class_id)
    if c is None or c.teacher_id != key.owner_id:
        raise HTTPException(404, "Класс не найден: сбор запускает классный руководитель своего класса")
    if body.template not in polls.TEMPLATES:
        raise HTTPException(422, "Неизвестный шаблон")
    if body.template == "choice" and not 2 <= len([o for o in body.options if o.strip()]) <= 10:
        raise HTTPException(422, "Нужно от 2 до 10 вариантов")
    if body.template == "money" and not body.amount:
        raise HTTPException(422, "Укажите amount")
    owner = await chats.get_user(key.owner_id)
    tz = owner.tz if owner else None
    deadline = None
    if body.deadline:
        d = body.deadline.strip()
        if d in ("today", "tomorrow", "friday", "week"):
            deadline = preset_deadline(d, tz)
        else:
            try:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(422, "deadline: нужен ISO 8601, например 2026-11-20T18:00")
            deadline = to_utc(dt.replace(tzinfo=zone(tz)) if dt.tzinfo is None else dt)
            if deadline <= utcnow():
                raise HTTPException(422, "Срок уже прошёл")
    if not await ratelimit.hit("apiv1poll", key.row.id, POLLS_PER_DAY, 86400):
        raise HTTPException(429, "Не больше 30 сборов в сутки на ключ")
    p = await polls.create_poll(key.owner_id, c.chat_id, body.template, body.text, body.options, deadline,
                                amount=body.amount)
    report = await polls.send_poll(p.id)
    p = await polls.get_poll(p.id)
    return {"poll": _brief(key, p), "report": report}


# ------------------------------------------------------------------ описание
@router.get("/openapi.json", include_in_schema=False)
async def openapi():
    schema = get_openapi(title="Перемена — открытый API", version="1.0", routes=router.routes,
                         description=__doc__)
    schema["components"] = schema.get("components", {}) | {"securitySchemes": {
        "bearer": {"type": "http", "scheme": "bearer", "description": "Ключ prm_… из «Ещё → Интеграции»"}}}
    schema["security"] = [{"bearer": []}]
    return JSONResponse(schema)


__all__ = ["router"]

"""HTTP-слой электронного журнала (кабинет и мини-приложение).

* ``POST /api/journals/preview`` — файл выгрузки → что в нём нашлось (ничего ещё не записано);
* ``POST /api/journals/import`` — записать предпросмотр в класс;
* ``GET  /api/journals/roster/{класс}`` — кто из списка класса ещё не в чате;
* ``GET  /api/journals/export-links`` → подписанные ссылки на CSV (открываются без входа, живут сутки);
* ``GET  /api/journals/template.xlsx`` и ``/api/journals/template/{раздел}.csv`` — шаблон для импорта;
* ``/api/journals/dnevnik/{класс}`` — подключение «Дневник.ру» (только при DNEVNIK_ENABLED).

Импортировать и выгружать может только классный руководитель своего класса.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from ... import ratelimit
from ...core import ctx
from ...models import User
from ...services import chats
from ...timeutil import local_now
from ...webapp.api import current_user
from . import BadFile, describe, drop_draft, load_draft, parse_bytes, save_draft
from . import dnevnik, export, service
from .parse import MAX_BYTES

router = APIRouter(prefix="/api/journals", tags=["Электронный журнал"])

EXPORT_TTL = 24 * 3600
PREVIEWS_PER_HOUR = 30
MAX_PERIOD = 400  # дней в одной выгрузке


async def _teacher(user: User = Depends(current_user)) -> User:
    if not await chats.is_teacher(user.user_id):
        raise HTTPException(403, "Загрузка и выгрузка журнала — для классного руководителя")
    return user


async def _own_class(chat_id: int, user: User):
    c = await chats.get_chat(chat_id)
    if c is None or c.teacher_id != user.user_id or not c.active:
        raise HTTPException(404, "Класс не найден")
    return c


def _sample(p) -> dict:
    return {"students": [s["name"] for s in p.students[:5]],
            "lessons": [{"day": x["day"], "weekday": x["weekday"], "number": x["number"], "subject": x["subject"]}
                        for x in p.lessons[:5]],
            "homework": [{"day": x["day"], "subject": x["subject"], "text": x["text"][:200]} for x in p.homework[:5]]}


# ------------------------------------------------------------------ обзор
@router.get("")
async def overview(user: User = Depends(_teacher)):
    my = await chats.teacher_chats(user.user_id)
    classes = []
    for c in my:
        link = await dnevnik.get_link(c.chat_id)
        check = await service.roster_check(c.chat_id)
        classes.append({
            "chat_id": c.chat_id, "title": c.title, "roster": check["total"], "in_chat": check["in_chat"],
            "has_schedule": await service.has_schedule([c.chat_id]),
            "dnevnik": {"school_ref": link.school_ref, "group_ref": link.group_ref, "own_token": bool(link.token_enc),
                        "last_sync_at": link.last_sync_at.isoformat() + "Z" if link.last_sync_at else None,
                        "last_status": link.last_status} if link else None,
        })
    return {"classes": classes, "dnevnik_enabled": dnevnik.enabled(),
            "school_token": bool(ctx().settings.dnevnik_token), "max_mb": MAX_BYTES // 1024 // 1024,
            "template": {"xlsx": "/api/journals/template.xlsx",
                         **{k: f"/api/journals/template/{k}.csv" for k in export.TEMPLATE}}}


# ------------------------------------------------------------------ импорт файла
async def _read_upload(request: Request) -> tuple[bytes, str, dict]:
    try:
        length = int(request.headers.get("content-length", ""))
    except ValueError:
        raise HTTPException(411, "Нужен заголовок Content-Length")
    if length > MAX_BYTES + 64 * 1024:
        raise HTTPException(413, f"Файл больше {MAX_BYTES // 1024 // 1024} МБ — выгрузите один класс")
    try:
        form = await request.form(max_files=1, max_fields=5)
    except Exception:
        raise HTTPException(400, "Не удалось разобрать форму")
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise HTTPException(400, "Нет файла")
    buf = bytearray()
    while chunk := await file.read(256 * 1024):
        buf += chunk
        if len(buf) > MAX_BYTES:
            raise HTTPException(413, f"Файл больше {MAX_BYTES // 1024 // 1024} МБ — выгрузите один класс")
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    return bytes(buf), file.filename or "", fields


@router.post("/preview")
async def preview(request: Request, user: User = Depends(_teacher)):
    """Разбирает файл и показывает, что нашлось. В базу ничего не пишется до /import."""
    if not await ratelimit.hit("jrprev", user.user_id, PREVIEWS_PER_HOUR, 3600):
        raise HTTPException(429, "Слишком много файлов подряд — попробуйте через час")
    data, filename, fields = await _read_upload(request)
    try:
        p = parse_bytes(data, filename, local_now(user.tz).date())
    except BadFile as e:
        raise HTTPException(422, str(e))
    chat_id = None
    if (fields.get("chat_id") or "").lstrip("-").isdigit():
        chat_id = (await _own_class(int(fields["chat_id"]), user)).chat_id
    ref = await save_draft(user.user_id, p, filename, chat_id)
    return {"ref": ref, "filename": filename, "summary": p.summary(), "text": describe(p), "sample": _sample(p)}


class ImportIn(BaseModel):
    ref: str = Field(min_length=4, max_length=40)
    chat_id: int


@router.post("/import")
async def do_import(body: ImportIn, user: User = Depends(_teacher)):
    c = await _own_class(body.chat_id, user)
    d = await load_draft(user.user_id, body.ref)
    if d is None:
        raise HTTPException(404, "Предпросмотр устарел — загрузите файл ещё раз")
    res = await service.apply(c.chat_id, user.user_id, d["parsed"], source="journal")
    await drop_draft(body.ref)
    return {"result": res.as_dict(), "roster": await service.roster_check(c.chat_id)}


@router.get("/roster/{chat_id}")
async def roster(chat_id: int, user: User = Depends(_teacher)):
    c = await _own_class(chat_id, user)
    return await service.roster_check(c.chat_id)


# ------------------------------------------------------------------ шаблон
def _file(data: bytes, name: str, media: str) -> StreamingResponse:
    return StreamingResponse(io.BytesIO(data), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Cache-Control": "public, max-age=3600"})


@router.get("/template.xlsx")
async def template_xlsx():
    return _file(export.template_xlsx(), "peremena_shablon.xlsx",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.get("/template/{kind}.csv")
async def template_csv(kind: str):
    if kind not in export.TEMPLATE:
        raise HTTPException(404, "Такого шаблона нет")
    return _file(export.template_csv(kind), f"peremena_{kind}.csv", "text/csv; charset=utf-8")


# ------------------------------------------------------------------ выгрузки
def _sign(*parts) -> str:
    msg = ":".join(str(p) for p in ("jexp", *parts)).encode()
    return hmac.new(ctx().settings.signing_key, msg, hashlib.sha256).hexdigest()[:32]


def _period(start: date | None, end: date | None, tz: str | None) -> tuple[date, date]:
    today = local_now(tz).date()
    end = end or today
    start = start or end - timedelta(days=30)
    if start > end:
        start, end = end, start
    if (end - start).days > MAX_PERIOD:
        raise HTTPException(422, f"Период — не длиннее {MAX_PERIOD} дней")
    return start, end


def export_url(kind: str, owner_id: int, chat_id: int, start: date, end: date) -> str:
    exp = int(time.time()) + EXPORT_TTL
    sig = _sign(kind, owner_id, chat_id, start, end, exp)
    return (f"/api/journals/export/{kind}.csv?owner={owner_id}&chat={chat_id}&start={start}&end={end}"
            f"&exp={exp}&sig={sig}")


@router.get("/export-links")
async def export_links(chat_id: int = 0, start: date | None = Query(None, alias="from"),
                       end: date | None = Query(None, alias="to"), user: User = Depends(_teacher)):
    """chat_id=0 — все классы учителя."""
    if chat_id:
        await _own_class(chat_id, user)
    start, end = _period(start, end, user.tz)
    return {"from": start.isoformat(), "to": end.isoformat(),
            "absences": export_url("absences", user.user_id, chat_id, start, end),
            "polls": export_url("polls", user.user_id, chat_id, start, end)}


async def classes_for_export(owner_id: int, chat_id: int) -> tuple[list[int], dict[int, str]]:
    my = {c.chat_id: c.title for c in await chats.teacher_chats(owner_id)}
    ids = [chat_id] if chat_id else list(my)
    ids = [i for i in ids if i in my]
    return ids, my


@router.get("/export/{kind}.csv")
async def export_csv(kind: str, owner: int, chat: int, start: date, end: date, exp: int,
                     sig: str = Query(max_length=64)):
    if kind not in ("absences", "polls") or exp < time.time() or \
            not hmac.compare_digest(sig.encode(), _sign(kind, owner, chat, start, end, exp).encode()):
        raise HTTPException(404, "Ссылка устарела — выгрузите заново")
    ids, titles = await classes_for_export(owner, chat)
    user = await chats.get_user(owner)
    tz = user.tz if user else None
    if kind == "absences":
        data = await export.absences_csv(ids, titles, start, end)
    else:
        data = await export.polls_csv(ids, titles, start, end, tz)
    name = f"{'propuski' if kind == 'absences' else 'sbory'}_{start:%Y%m%d}-{end:%Y%m%d}.csv"
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store"})


# ------------------------------------------------------------------ «Дневник.ру»
def _need_dnevnik() -> None:
    if not dnevnik.enabled():
        raise HTTPException(404, "Коннектор «Дневник.ру» выключен на этом сервере")


class DnevnikIn(BaseModel):
    school_ref: str = Field("", max_length=40, pattern=r"^[0-9A-Za-z_-]*$")
    group_ref: str = Field(min_length=1, max_length=40, pattern=r"^[0-9A-Za-z_-]+$")
    token: str | None = Field(None, max_length=500)


class SyncIn(BaseModel):
    start: date | None = Field(None, alias="from")
    end: date | None = Field(None, alias="to")


@router.put("/dnevnik/{chat_id}")
async def dnevnik_link(chat_id: int, body: DnevnikIn, user: User = Depends(_teacher)):
    _need_dnevnik()
    await _own_class(chat_id, user)
    link = await dnevnik.save_link(chat_id, user.user_id, body.school_ref, body.group_ref, body.token)
    return {"school_ref": link.school_ref, "group_ref": link.group_ref, "own_token": bool(link.token_enc)}


@router.delete("/dnevnik/{chat_id}")
async def dnevnik_unlink(chat_id: int, user: User = Depends(_teacher)):
    await _own_class(chat_id, user)
    return {"ok": await dnevnik.remove_link(chat_id)}


@router.post("/dnevnik/{chat_id}/sync")
async def dnevnik_sync(chat_id: int, body: SyncIn, user: User = Depends(_teacher)):
    _need_dnevnik()
    await _own_class(chat_id, user)
    if not await ratelimit.hit("jrsync", user.user_id, 20, 3600):
        raise HTTPException(429, "Слишком часто — попробуйте через час")
    today = local_now(user.tz).date()
    start = body.start or today
    end = body.end or start + timedelta(days=7)
    try:
        parsed, res = await dnevnik.sync(chat_id, user.user_id, start, end)
    except dnevnik.DnevnikError as e:
        raise HTTPException(502, str(e))
    return {"summary": parsed.summary(), "text": describe(parsed), "result": res}

"""REST API мини-приложения. Авторизация — подписанные данные запуска MAX (заголовок X-Max-Init-Data)."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core import ctx
from ..db import session
from ..max_api import MaxApiError
from ..models import ClassChat, InboxItem, User
from ..services import chats, faq, polls, quiet, shelf, stats
from ..timeutil import fmt_dt, local_now, parse_deadline, preset_deadline, to_local, to_utc, zone
from .auth import InitDataError, validate_init_data

router = APIRouter(prefix="/api")


# ====================================================================== авторизация
async def current_user(
    x_max_init_data: str | None = Header(default=None),
    dev_user: int | None = Query(default=None),
) -> User:
    c = ctx()
    if x_max_init_data:
        try:
            data = validate_init_data(x_max_init_data, c.settings.max_bot_token,
                                      c.settings.init_data_ttl_hours * 3600)
        except InitDataError as e:
            raise HTTPException(401, f"Не удалось проверить вход: {e}")
        u = data["user"]
        user = await chats.upsert_user({
            "user_id": int(u["id"]), "first_name": u.get("first_name", ""),
            "last_name": u.get("last_name"), "username": u.get("username"),
        })
        return user
    if c.settings.dev_auth and dev_user:
        user = await chats.get_user(dev_user)
        if user is None:
            user = await chats.upsert_user({"user_id": dev_user, "first_name": f"Dev {dev_user}"})
        return user
    raise HTTPException(401, "Откройте приложение из MAX")


async def teacher_user(user: User = Depends(current_user)) -> User:
    if not await chats.is_teacher(user.user_id):
        raise HTTPException(403, "Только для классного руководителя")
    return user


def _sign(*parts) -> str:
    msg = ":".join(str(p) for p in parts).encode()
    return hmac.new(ctx().settings.signing_key, msg, hashlib.sha256).hexdigest()[:32]


# ====================================================================== сериализация
def chat_out(c: ClassChat) -> dict:
    return {"chat_id": c.chat_id, "title": c.title, "autoanswer": c.autoanswer, "bot_is_admin": c.bot_is_admin}


def poll_out(p, tz: str, with_targets: bool = False, chat_titles: dict | None = None) -> dict:
    answered, total, per = polls.counts(p)
    out = {
        "id": p.id, "title": p.title, "text": p.text, "template": p.template, "options": p.options,
        "chat_id": p.chat_id, "chat_title": (chat_titles or {}).get(p.chat_id, ""),
        "deadline": p.deadline.isoformat() + "Z" if p.deadline else None,
        "deadline_text": fmt_dt(p.deadline, tz), "closed": p.closed,
        "created_at": p.created_at.isoformat() + "Z", "answered": answered, "total": total, "per_option": per,
        "missing": [t.name for t in p.targets if t.answer is None],
    }
    if with_targets:
        out["targets"] = [
            {"user_id": t.user_id, "name": t.name, "answer": t.answer, "delivered": t.delivered,
             "answered_at": t.answered_at.isoformat() + "Z" if t.answered_at else None, "reminders": t.reminders}
            for t in p.targets
        ]
        out["export_url"] = f"/api/polls/{p.id}/export.csv?sig={_sign('poll', p.id, p.teacher_id)}"
    return out


def faq_out(e) -> dict:
    return {"id": e.id, "question": e.question, "answer": e.answer, "keywords": e.keywords, "chat_id": e.chat_id,
            "hits": e.hits, "misses": e.misses, "updated_at": e.updated_at.isoformat() + "Z"}


def material_out(m, titles: dict, tz: str | None) -> dict:
    return {"id": m.id, "title": m.title, "filename": m.filename, "tags": m.tags, "type": m.attach_type,
            "chat_id": m.chat_id, "chat_title": titles.get(m.chat_id, "") if m.chat_id else "",
            "url": m.url, "size": m.size, "source": m.source,
            "sent_at": m.sent_at.isoformat() + "Z", "date": f"{to_local(m.sent_at, tz):%d.%m.%Y}"}


async def _titles() -> dict[int, str]:
    return await shelf.chat_titles()


# ====================================================================== общее
@router.get("/me")
async def me(user: User = Depends(current_user)):
    c = ctx()
    t_chats = await chats.teacher_chats(user.user_id)
    m_chats = await chats.member_chats(user.user_id)
    return {
        "user": {"id": user.user_id, "name": user.display_name, "first_name": user.first_name},
        "is_teacher": bool(t_chats),
        "chats": [chat_out(c_) for c_ in t_chats],
        "member_chats": [{"chat_id": c_.chat_id, "title": c_.title} for c_ in m_chats],
        "bot_username": c.bot_username,
        "bot_link": c.start_link() if c.bot_username else "",
        "settings": settings_out(user),
    }


# ====================================================================== «Перекличка»
@router.get("/templates")
async def templates(_: User = Depends(current_user)):
    return [{"key": k, "label": v["label"], "hint": v["hint"], "options": v["options"]}
            for k, v in polls.TEMPLATES.items()]


@router.get("/polls")
async def list_polls(all: bool = True, user: User = Depends(teacher_user)):
    items = await polls.list_polls(user.user_id, include_closed=all, limit=50)
    titles = await _titles()
    return [poll_out(p, user.tz, chat_titles=titles) for p in items]


class PollIn(BaseModel):
    chat_id: int
    template: str = "consent"
    text: str = Field(min_length=3, max_length=3000)
    options: list[str] = []
    deadline: str | None = None  # today|tomorrow|friday|week|ISO-время в поясе учителя|«28.09 18:00»


@router.post("/polls")
async def create_poll(body: PollIn, user: User = Depends(teacher_user)):
    chat = await chats.get_chat(body.chat_id)
    if not chat or chat.teacher_id != user.user_id:
        raise HTTPException(404, "Чат не найден")
    if body.template not in polls.TEMPLATES:
        raise HTTPException(400, "Неизвестный шаблон")
    if body.template == "choice" and not 2 <= len([o for o in body.options if o.strip()]) <= 10:
        raise HTTPException(400, "Нужно от 2 до 10 вариантов")
    deadline = None
    if body.deadline:
        d = body.deadline.strip()
        if d in ("today", "tomorrow", "friday", "week"):
            deadline = preset_deadline(d, user.tz)
        else:
            try:
                dt = datetime.fromisoformat(d.replace("Z", ""))
                deadline = to_utc(dt.replace(tzinfo=zone(user.tz))) if dt.tzinfo is None else to_utc(dt)
            except ValueError:
                deadline = parse_deadline(d, user.tz)
            if deadline is None:
                raise HTTPException(400, "Не понял срок")
            if deadline <= to_utc(local_now(user.tz)):
                raise HTTPException(400, "Срок уже прошёл")
    p = await polls.create_poll(user.user_id, chat.chat_id, body.template, body.text, body.options, deadline)
    report = await polls.send_poll(p.id)
    p = await polls.get_poll(p.id)
    return {"poll": poll_out(p, user.tz, with_targets=True, chat_titles=await _titles()), "report": report}


async def _own(pid: int, user: User):
    p = await polls.get_poll(pid)
    if p is None or p.teacher_id != user.user_id:
        raise HTTPException(404, "Сбор не найден")
    return p


@router.get("/polls/{pid}")
async def get_poll(pid: int, user: User = Depends(teacher_user)):
    return poll_out(await _own(pid, user), user.tz, with_targets=True, chat_titles=await _titles())


@router.post("/polls/{pid}/remind")
async def remind(pid: int, user: User = Depends(teacher_user)):
    await _own(pid, user)
    return await polls.remind(pid)


@router.post("/polls/{pid}/close")
async def close(pid: int, user: User = Depends(teacher_user)):
    await _own(pid, user)
    p = await polls.close_poll(pid)
    return poll_out(p, user.tz, with_targets=True, chat_titles=await _titles())


@router.get("/polls/{pid}/export.csv")
async def export_csv(pid: int, sig: str):
    p = await polls.get_poll(pid)
    if p is None or not hmac.compare_digest(sig, _sign("poll", p.id, p.teacher_id)):
        raise HTTPException(404, "Не найдено")
    teacher = await chats.get_user(p.teacher_id)
    tz = teacher.tz if teacher else None
    buf = io.StringIO()
    buf.write("﻿")  # BOM, чтобы Excel открыл кириллицу
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Участник", "Ответ", "Когда ответил", "Напоминаний"])
    for t in p.targets:
        ans = p.options[t.answer] if t.answer is not None and t.answer < len(p.options) else "не ответил"
        when = f"{to_local(t.answered_at, tz):%d.%m.%Y %H:%M}" if t.answered_at else ""
        w.writerow([t.name, ans, when, t.reminders])
    data = buf.getvalue().encode("utf-8")
    fname = f"sbor_{p.id}.csv"
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------- для родителя
@router.get("/my/polls")
async def my_polls(user: User = Depends(current_user)):
    out = []
    titles = await _titles()
    for p in await polls.pending_for_user(user.user_id):
        teacher = await chats.get_user(p.teacher_id)
        tz = teacher.tz if teacher else user.tz
        d = poll_out(p, tz, chat_titles=titles)
        d.pop("missing", None)
        d["my_answer"] = await polls.user_answer(p, user.user_id)
        d["teacher"] = teacher.display_name if teacher else ""
        out.append(d)
    return out


class AnswerIn(BaseModel):
    option: int


@router.post("/my/polls/{pid}/answer")
async def answer_poll(pid: int, body: AnswerIn, user: User = Depends(current_user)):
    p = await polls.get_poll(pid)
    if p is None:
        raise HTTPException(404, "Сбор не найден")
    allowed = any(t.user_id == user.user_id for t in p.targets) or \
        p.chat_id in {c.chat_id for c in await chats.member_chats(user.user_id)}
    if not allowed:
        raise HTTPException(403, "Этот сбор не для вас")
    u = {"user_id": user.user_id, "first_name": user.first_name, "last_name": user.last_name}
    p, status = await polls.record_answer(pid, u, body.option)
    if status == "closed":
        raise HTTPException(409, "Сбор уже закрыт")
    if status == "bad":
        raise HTTPException(400, "Неверный вариант")
    await polls.after_answer(p, user.user_id, body.option, status)
    return {"ok": True, "status": status}


@router.get("/ask")
async def ask(q: str = Query(min_length=2, max_length=500), user: User = Depends(current_user)):
    is_t = await chats.is_teacher(user.user_id)
    if is_t:
        entries = await faq.list_entries(user.user_id)
    else:
        entries = await faq.candidates_for_chats([c.chat_id for c in await chats.member_chats(user.user_id)])
    hits = faq.rank(q, entries)
    titles = await _titles()
    mats = await shelf.find(user.user_id, q, teacher_view=is_t, limit=5)
    answer = None
    if hits and hits[0].score >= faq.ANSWER_SCORE:
        e = hits[0].item
        answer = faq_out(e)
        if not is_t:
            await faq.mark_hit(e.id)
            await stats.add(e.teacher_id, "faq_answer")
    return {
        "answer": answer,
        "suggestions": [faq_out(h.item) | {"score": h.score} for h in hits if not answer or h.item.id != answer["id"]],
        "materials": [material_out(h.item, titles, user.tz) for h in mats],
    }


class ForwardIn(BaseModel):
    text: str = Field(min_length=2, max_length=1000)
    chat_id: int | None = None


@router.post("/ask/forward")
async def ask_forward(body: ForwardIn, user: User = Depends(current_user)):
    from ..bot import dialog

    chat_ids = [c.chat_id for c in await chats.member_chats(user.user_id)]
    if not chat_ids:
        raise HTTPException(400, "Вы пока не состоите в чате класса с ботом")
    await ctx().kv.set(f"q:{user.user_id}", {"text": body.text, "chats": chat_ids}, 3600)
    await dialog.forward_question(user.user_id, body.chat_id if body.chat_id in chat_ids else None)
    return {"ok": True}


@router.post("/faq/{fid}/not-that")
async def not_that(fid: int, user: User = Depends(current_user)):
    e = await faq.mark_miss(fid)
    if e:
        name = await chats.member_name(e.chat_id, user.user_id)
        await quiet.notify_teacher(e.teacher_id, f"🙅 Ответ из справочника не подошёл: «{e.question[:150]}» ({name})",
                                   kind="not_that", from_user_id=user.user_id, from_name=name,
                                   raw_text=f"Не подошло: «{e.question[:150]}»")
    return {"ok": True}


# ====================================================================== «Справочник»
class FaqIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    answer: str = Field(min_length=1, max_length=3500)
    keywords: str | None = None
    chat_id: int | None = None


@router.get("/faq")
async def list_faq(user: User = Depends(teacher_user)):
    return [faq_out(e) for e in await faq.list_entries(user.user_id)]


@router.post("/faq")
async def add_faq(body: FaqIn, user: User = Depends(teacher_user)):
    e = await faq.add_entry(user.user_id, body.question, body.answer, body.keywords or None, body.chat_id)
    return faq_out(e)


@router.put("/faq/{fid}")
async def edit_faq(fid: int, body: FaqIn, user: User = Depends(teacher_user)):
    e = await faq.update_entry(fid, user.user_id, question=body.question, answer=body.answer,
                               keywords=body.keywords if body.keywords is not None else None, chat_id=body.chat_id or 0)
    if not e:
        raise HTTPException(404, "Карточка не найдена")
    return faq_out(e)


@router.delete("/faq/{fid}")
async def del_faq(fid: int, user: User = Depends(teacher_user)):
    if not await faq.delete_entry(fid, user.user_id):
        raise HTTPException(404, "Карточка не найдена")
    return {"ok": True}


@router.get("/faq/test")
async def test_faq(q: str, user: User = Depends(teacher_user)):
    hits = faq.rank(q, await faq.list_entries(user.user_id), limit=3)
    return [{"entry": faq_out(h.item), "score": h.score, "auto": h.score >= faq.ANSWER_SCORE,
             "group_auto": h.score >= faq.GROUP_SCORE} for h in hits]


# ====================================================================== «Полка»
@router.get("/materials")
async def list_materials(q: str = "", user: User = Depends(current_user)):
    titles = await _titles()
    if await chats.is_teacher(user.user_id):
        items = await shelf.list_for_teacher(user.user_id, q)
    elif q:
        items = [h.item for h in await shelf.find(user.user_id, q, teacher_view=False, limit=30)]
    else:
        items = sorted(await shelf.visible_materials(user.user_id, False), key=lambda m: m.sent_at, reverse=True)[:50]
    return [material_out(m, titles, user.tz) for m in items]


class MaterialIn(BaseModel):
    title: str | None = None
    tags: str | None = None


@router.put("/materials/{mid}")
async def edit_material(mid: int, body: MaterialIn, user: User = Depends(teacher_user)):
    m = await shelf.update(mid, user.user_id, body.title, body.tags)
    if not m:
        raise HTTPException(404, "Материал не найден")
    return material_out(m, await _titles(), user.tz)


@router.delete("/materials/{mid}")
async def del_material(mid: int, user: User = Depends(teacher_user)):
    if not await shelf.delete(mid, user.user_id):
        raise HTTPException(404, "Материал не найден")
    return {"ok": True}


class SendIn(BaseModel):
    chat_id: int | None = None  # None — прислать себе в личку


@router.post("/materials/{mid}/send")
async def send_material(mid: int, body: SendIn, user: User = Depends(current_user)):
    m = await shelf.get(mid)
    if m is None:
        raise HTTPException(404, "Материал не найден")
    if body.chat_id:
        chat = await chats.get_chat(body.chat_id)
        if m.teacher_id != user.user_id or not chat or chat.teacher_id != user.user_id:
            raise HTTPException(403, "Пересылать в чат может только учитель")
        ok = await shelf.send_material(m, chat_id=body.chat_id, caption=m.title or None)
    else:
        visible = m.teacher_id == user.user_id or \
            (m.chat_id in {c.chat_id for c in await chats.member_chats(user.user_id)})
        if not visible:
            raise HTTPException(403, "Материал недоступен")
        ok = await shelf.send_material(m, user_id=user.user_id)
    if not ok:
        raise HTTPException(502, "MAX не принял файл — попробуйте позже")
    await stats.add(m.teacher_id, "material_found")
    return {"ok": True}


UPLOAD_KIND = {"image": ("image/",), "video": ("video/",), "audio": ("audio/",)}


@router.post("/materials/upload")
async def upload_material(
    file: UploadFile = File(...),
    title: str = Form(""),
    tags: str = Form(""),
    user: User = Depends(teacher_user),
):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 100 МБ — отправьте его боту в личку")
    ctype = file.content_type or ""
    kind = next((k for k, prefixes in UPLOAD_KIND.items() if ctype.startswith(prefixes)), "file")
    try:
        payload = await ctx().api.upload(kind, file.filename or "file", content, ctype)
    except MaxApiError as e:
        raise HTTPException(502, f"MAX не принял файл: {e.message or e.code}")
    key = hashlib.sha256(content).hexdigest()[:40]
    caption = (title + " " + " ".join("#" + t.strip().lstrip("#") for t in tags.split(",") if t.strip())).strip()
    m, _ = await shelf.save(user.user_id, {"type": kind, "payload": payload, "filename": file.filename or "Файл",
                                           "url": None, "size": len(content), "key": key},
                            caption, source="upload")
    return material_out(m, await _titles(), user.tz)


# ====================================================================== настройки, классы, польза
def settings_out(u: User) -> dict:
    return {"tz": u.tz, "work_start": u.work_start, "work_end": u.work_end, "weekends_off": u.weekends_off,
            "quiet_enabled": u.quiet_enabled, "urgent_words": u.urgent_words, "suggest_save": u.suggest_save}


class SettingsIn(BaseModel):
    tz: str | None = None
    work_start: int | None = Field(default=None, ge=0, le=1440)
    work_end: int | None = Field(default=None, ge=0, le=1440)
    weekends_off: bool | None = None
    quiet_enabled: bool | None = None
    urgent_words: str | None = Field(default=None, max_length=300)
    suggest_save: bool | None = None


@router.put("/settings")
async def put_settings(body: SettingsIn, user: User = Depends(teacher_user)):
    data = body.model_dump(exclude_none=True)
    if "tz" in data:
        zone(data["tz"])  # неизвестный пояс заменится на Москву
    async with session() as s:
        u = await s.get(User, user.user_id)
        for k, v in data.items():
            setattr(u, k, v)
        if u.work_end <= u.work_start:
            raise HTTPException(400, "Конец рабочего дня должен быть позже начала")
        return settings_out(u)


class ChatIn(BaseModel):
    autoanswer: bool | None = None


@router.put("/chats/{chat_id}")
async def put_chat(chat_id: int, body: ChatIn, user: User = Depends(teacher_user)):
    async with session() as s:
        c = await s.get(ClassChat, chat_id)
        if not c or c.teacher_id != user.user_id:
            raise HTTPException(404, "Чат не найден")
        if body.autoanswer is not None:
            c.autoanswer = body.autoanswer
        return chat_out(c)


@router.post("/chats/{chat_id}/sync")
async def sync_chat(chat_id: int, user: User = Depends(teacher_user)):
    c = await chats.get_chat(chat_id)
    if not c or c.teacher_id != user.user_id:
        raise HTTPException(404, "Чат не найден")
    n = await chats.sync_members(chat_id)
    admin = await chats.refresh_admin_status(chat_id)
    members = await chats.class_list(chat_id, exclude={user.user_id})
    return {"members": n, "bot_is_admin": admin, "class_list": [m.name for m in members]}


@router.post("/chats/{chat_id}/history")
async def import_history(chat_id: int, user: User = Depends(teacher_user)):
    c = await chats.get_chat(chat_id)
    if not c or c.teacher_id != user.user_id:
        raise HTTPException(404, "Чат не найден")
    return {"added": await shelf.import_history(chat_id, user.user_id)}


@router.get("/stats")
async def get_stats(user: User = Depends(teacher_user)):
    week = await stats.summary(user.user_id, days=7)
    total = await stats.summary(user.user_id)
    return {"week": week, "total": total, "labels": stats.LABELS, "minutes_per": stats.MINUTES}


@router.get("/inbox")
async def inbox(user: User = Depends(teacher_user)):
    async with session() as s:
        items = (await s.scalars(
            select(InboxItem).where(InboxItem.teacher_id == user.user_id).order_by(InboxItem.created_at.desc()).limit(50)
        )).all()
    labels = quiet.KIND_LABEL
    return [{"id": i.id, "kind": i.kind, "label": labels.get(i.kind, ""), "text": i.text, "from_name": i.from_name,
             "from_user_id": i.from_user_id, "delivered": i.delivered,
             "time": f"{to_local(i.created_at, user.tz):%d.%m %H:%M}"} for i in items]

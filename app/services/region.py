"""Регион и округ: уровень выше школы.

* Регион создаёт администратор платформы (ADMIN_MAX_IDS); методист региона может завести округ
  (муниципалитет) внутри него. У каждого — два кода: для школ и для сотрудников управления.
* Школа присоединяется к округу или региону по коду — так же, как учитель вступает в школу.
  Код вводит администратор школы (завуч).
* Методист запускает сбор или объявление на весь регион, выбранные школы или параллели. Сбор
  раскладывается в школьные сборы (их видит завуч, как сбор на параллель), а те — в классные:
  каждый ведёт свой классный руководитель. Рассылка идёт обычной механикой сборов — отложенной
  отправкой через досылку (resume_dispatch), поэтому десятки тысяч сообщений растягиваются
  по лимиту MAX, а не уходят разом.
* Методист видит только цифры по школам и классам. ФИО родителей и их ответы остаются у классного
  руководителя (и у завуча по своей школе) — так безопаснее для персональных данных.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import re
from datetime import timedelta

from sqlalchemy import func, select

from .. import keyboards as kb
from ..core import ctx
from ..db import session
from ..demo_ids import is_demo
from ..integrations.journals.export import safe_cell
from ..models import (
    Campaign, ClassChat, Poll, PollTarget, Region, RegionCampaign, RegionMember, School, SchoolMember, User,
)
from ..timeutil import fmt_dt, utcnow
from . import polls
from .school import _code

log = logging.getLogger(__name__)

HOLD_MINUTES = 30        # окно, чтобы классный руководитель успел добавить приписку до рассылки родителям
NOTE_MAX = 500
JOIN_TRIES_PER_HOUR = 10
INLINE_NOTIFY = 50       # больше учителей — уведомляем в фоне, не задерживая ответ методисту
REGION_TEMPLATES = ("consent", "certificate", "ack", "signup", "choice")  # без «денег»: сумма у каждого своя

_background: set[asyncio.Task] = set()


def is_platform_admin(user_id: int) -> bool:
    return user_id in ctx().settings.platform_admins


def can_create_region(user_id: int) -> bool:
    """Регион заводит администратор платформы. В демо — любой гость: песочница отделена от настоящих данных."""
    return is_platform_admin(user_id) or is_demo(user_id)


_GRADE_RE = re.compile(r"(?<!\d)(1[01]|[1-9])(?!\d)")


def grade_of(title: str | None) -> int | None:
    """Параллель по названию чата: «9Б Родители» → 9, «Родители 11-А» → 11."""
    m = _GRADE_RE.search(title or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- регионы и роли
async def create_region(user_id: int, name: str, parent_id: int | None = None) -> Region:
    async with session() as s:
        reg = Region(name=name.strip()[:300] or "Управление образования", kind="district" if parent_id else "region",
                     parent_id=parent_id, code=_code(), staff_code=_code(8), created_by=user_id)
        s.add(reg)
        await s.flush()
        if parent_id is None:
            await _set_member(s, reg.id, user_id)
        return reg


async def create_district(user_id: int, name: str) -> Region | None:
    """Методист региона заводит округ (муниципалитет). Сам он остаётся в регионе и видит округ целиком."""
    reg, _ = await membership(user_id)
    if reg is None or reg.kind != "region":
        return None
    return await create_region(user_id, name, parent_id=reg.id)


async def _set_member(s, region_id: int, user_id: int, role: str = "methodist") -> None:
    # человек работает в одном управлении: при переходе в другое старое членство заменяется
    for old in (await s.scalars(select(RegionMember).where(RegionMember.user_id == user_id))).all():
        if old.region_id != region_id:
            await s.delete(old)
    m = await s.scalar(select(RegionMember).where(RegionMember.region_id == region_id, RegionMember.user_id == user_id))
    if m is None:
        s.add(RegionMember(region_id=region_id, user_id=user_id, role=role))


async def membership(user_id: int) -> tuple[Region | None, str | None]:
    async with session() as s:
        m = await s.scalar(select(RegionMember).where(RegionMember.user_id == user_id))
        if m is None:
            return None, None
        return await s.get(Region, m.region_id), m.role


async def get_region(region_id: int) -> Region | None:
    async with session() as s:
        return await s.get(Region, region_id)


async def assign_methodist(admin_id: int, region_id: int, user_id: int) -> bool:
    """Назначение администратором платформы — без кода, например по заявке из управления."""
    if not is_platform_admin(admin_id):
        return False
    async with session() as s:
        reg = await s.get(Region, region_id)
        if reg is None or is_demo(reg.created_by) != is_demo(user_id):
            return False
        await _set_member(s, region_id, user_id)
    return True


async def join_by_code(user_id: int, code: str) -> tuple[Region | None, str]:
    """Код для школ → школа пользователя (он должен быть её администратором) присоединяется к округу;
    код для сотрудников → пользователь становится методистом. Возвращает (регион, итог):
    school | methodist | not_admin | limit | "" (не нашли)."""
    from .. import ratelimit

    if not await ratelimit.hit("rjoin", user_id, JOIN_TRIES_PER_HOUR, 3600):
        return None, "limit"
    code = code.strip().upper()
    async with session() as s:
        reg = await s.scalar(select(Region).where(Region.code == code))
        kind = "school"
        if reg is None:
            reg = await s.scalar(select(Region).where(Region.staff_code == code))
            kind = "methodist"
        # демо и настоящие данные не смешиваются: песочница не попадает в настоящий регион и наоборот
        if reg is None or is_demo(reg.created_by) != is_demo(user_id):
            return None, ""
        if kind == "methodist":
            await _set_member(s, reg.id, user_id)
            return reg, kind
        m = await s.scalar(select(SchoolMember).where(SchoolMember.user_id == user_id, SchoolMember.role == "admin"))
        if m is None:
            return reg, "not_admin"
        sch = await s.get(School, m.school_id)
        sch.region_id = reg.id
        return reg, kind


async def leave_region(admin_id: int) -> bool:
    """Администратор школы отключает школу от округа."""
    async with session() as s:
        m = await s.scalar(select(SchoolMember).where(SchoolMember.user_id == admin_id, SchoolMember.role == "admin"))
        if m is None:
            return False
        sch = await s.get(School, m.school_id)
        sch.region_id = None
    return True


async def tree_ids(region_id: int) -> list[int]:
    """Регион и все его округа."""
    async with session() as s:
        kids = list((await s.scalars(select(Region.id).where(Region.parent_id == region_id))).all())
    return [region_id, *kids]


async def districts(region_id: int) -> list[Region]:
    async with session() as s:
        return list((await s.scalars(select(Region).where(Region.parent_id == region_id).order_by(Region.name))).all())


async def schools(region_id: int) -> list[School]:
    ids = await tree_ids(region_id)
    async with session() as s:
        return list((await s.scalars(select(School).where(School.region_id.in_(ids)).order_by(School.name))).all())


async def classes(region_id: int, school_ids: list[int] | None = None,
                  grades: list[int] | None = None) -> list[tuple[School, ClassChat]]:
    """Классы региона одним запросом: школа → классные руководители → их активные чаты."""
    allowed = {s_.id: s_ for s_ in await schools(region_id)}
    ids = [i for i in (school_ids or allowed) if i in allowed]
    if not ids:
        return []
    async with session() as s:
        rows = (await s.execute(
            select(SchoolMember.school_id, ClassChat)
            .join(ClassChat, ClassChat.teacher_id == SchoolMember.user_id)
            .where(SchoolMember.school_id.in_(ids), ClassChat.active.is_(True))
            .order_by(ClassChat.title)
        )).all()
    want = set(grades or [])
    return [(allowed[sid], c) for sid, c in rows if not want or grade_of(c.title) in want]


async def can_see(user_id: int, rc: RegionCampaign) -> bool:
    reg, _ = await membership(user_id)
    return reg is not None and rc.region_id in await tree_ids(reg.id)


# ---------------------------------------------------------------- сбор сверху
async def launch(author_id: int, region_id: int, template: str, text: str, options: list[str], deadline,
                 school_ids: list[int] | None = None, grades: list[int] | None = None,
                 hold_minutes: int = HOLD_MINUTES) -> tuple[RegionCampaign | None, dict]:
    """Раскладывает сбор управления на школы и классы. Ничего не отправляет сам: каждый классный сбор
    ждёт своего времени (окно на приписку учителя, ночью — до утра у родителей) и уходит через досылку
    сборов — по одному, с общим ограничителем запросов к MAX."""
    reg = await get_region(region_id)
    if reg is None:
        return None, {"schools": 0, "classes": 0}
    template = template if template in REGION_TEMPLATES else "consent"
    pairs = [(sc, c) for sc, c in await classes(region_id, school_ids, grades) if c.teacher_id]
    if not pairs:
        return None, {"schools": 0, "classes": 0}
    title = (text.strip().splitlines() or ["Сбор"])[0][:300]
    now = utcnow()
    teacher_ids = {c.teacher_id for _, c in pairs if c.teacher_id}
    async with session() as s:
        tzs = {u.user_id: u.tz for u in (await s.scalars(select(User).where(User.user_id.in_(teacher_ids)))).all()} \
            if teacher_ids else {}
        rc = RegionCampaign(region_id=region_id, author_id=author_id, title=title, text=text.strip()[:3000],
                            template=template,
                            options=[o.strip()[:60] for o in (options or polls.TEMPLATES[template]["options"])
                                     if o.strip()][:10],
                            deadline=deadline, school_ids=sorted(set(school_ids or [])), grades=sorted(set(grades or [])))
        s.add(rc)
        await s.flush()
        camps: dict[int, Campaign] = {}
        for sch, _ in pairs:
            if sch.id not in camps:
                camps[sch.id] = Campaign(school_id=sch.id, author_id=author_id, title=title, region_campaign_id=rc.id)
                s.add(camps[sch.id])
        await s.flush()
        created: list[Poll] = []
        for sch, chat in pairs:
            if not chat.teacher_id:
                continue
            tz = tzs.get(chat.teacher_id)
            send_at = polls.next_daytime(tz, now + timedelta(minutes=hold_minutes))
            if deadline and send_at >= deadline:
                send_at = now  # срок близко — ждать окна нельзя, уходит при ближайшем проходе досылки
            p = polls.build_poll(chat.teacher_id, chat.chat_id, template, rc.text, rc.options, deadline,
                                 send_at=send_at, campaign_id=camps[sch.id].id, origin=reg.name)
            s.add(p)
            created.append(p)
        await s.flush()
        jobs = [(p.id, p.teacher_id, p.chat_id, p.send_at) for p in created]
        rc_id = rc.id
    titles = {c.chat_id: c.title for _, c in pairs}
    coro = _notify_teachers(reg.name, title, jobs, titles, tzs)
    if len(jobs) <= INLINE_NOTIFY:
        await coro
    else:
        task = asyncio.create_task(coro)
        _background.add(task)
        task.add_done_callback(_background.discard)
    async with session() as s:
        rc = await s.get(RegionCampaign, rc_id)
    return rc, {"schools": len(camps), "classes": len(jobs)}


async def _notify_teachers(origin: str, title: str, jobs: list, titles: dict, tzs: dict) -> None:
    from .quiet import notify_teacher

    for pid, teacher_id, chat_id, send_at in jobs:
        try:
            when = fmt_dt(send_at, tzs.get(teacher_id))
            await notify_teacher(
                teacher_id,
                f"🏛 <b>{html.escape(origin)}</b> прислало сбор «{html.escape(title)}» для класса "
                f"{html.escape(titles.get(chat_id, ''))}.\n\n"
                f"Родителям он уйдёт {when} — разошлю сам, напомню молчащим и пришлю вам итог. "
                "Текст и варианты ответа менять нельзя, но можно добавить свою приписку для родителей.",
                kind="report", chat_id=chat_id, raw_text=f"Сбор от управления: «{title}»",
                buttons=[[kb.cb("✍️ Добавить приписку", f"rg:note:{pid}")],
                         [kb.cb("📤 Разослать сейчас", f"rg:now:{pid}")]],
            )
        except Exception:
            log.exception("Уведомление учителю о сборе сверху %s", pid)


async def own_origin_poll(poll_id: int, teacher_id: int) -> Poll | None:
    p = await polls.get_poll(poll_id)
    if p is None or p.teacher_id != teacher_id or not p.origin:
        return None
    return p


async def set_note(poll_id: int, teacher_id: int, note: str) -> tuple[Poll | None, str]:
    """Приписка классного руководителя. Итог: ok | sent (уже разослан — приписку увидят в напоминании)
    | closed | missing."""
    p = await own_origin_poll(poll_id, teacher_id)
    if p is None:
        return None, "missing"
    if p.closed:
        return p, "closed"
    async with session() as s:
        row = await s.get(Poll, poll_id)
        row.note = (note or "").strip()[:NOTE_MAX] or None
    p = await polls.get_poll(poll_id)
    return p, "sent" if p.dispatched else "ok"


async def send_now(poll_id: int, teacher_id: int) -> dict | None:
    p = await own_origin_poll(poll_id, teacher_id)
    if p is None or p.closed:
        return None
    if p.dispatched:
        return {"already": True}
    return await polls.send_poll(poll_id)


# ---------------------------------------------------------------- сводка снизу вверх
def _pct(a: int, t: int) -> int:
    return round(100 * a / t) if t else 0


async def summary(rc: RegionCampaign) -> dict:
    """Только цифры: регион → школы → классы. Ни имён родителей, ни их ответов здесь нет."""
    async with session() as s:
        camps = {c.id: c for c in (await s.scalars(
            select(Campaign).where(Campaign.region_campaign_id == rc.id))).all()}
        rows = (await s.execute(
            select(Poll.id, Poll.chat_id, Poll.campaign_id, Poll.closed, Poll.dispatched,
                   func.count(PollTarget.answer), func.count(PollTarget.id))
            .outerjoin(PollTarget, PollTarget.poll_id == Poll.id)
            .where(Poll.campaign_id.in_(list(camps)))
            .group_by(Poll.id, Poll.chat_id, Poll.campaign_id, Poll.closed, Poll.dispatched)
        )).all() if camps else []
        per = (await s.execute(
            select(PollTarget.answer, func.count(PollTarget.id))
            .join(Poll, Poll.id == PollTarget.poll_id)
            .where(Poll.campaign_id.in_(list(camps)), PollTarget.answer.is_not(None))
            .group_by(PollTarget.answer)
        )).all() if camps else []
        school_names = {sc.id: sc.name for sc in (await s.scalars(
            select(School).where(School.id.in_([c.school_id for c in camps.values()])))).all()} if camps else {}
        titles = {c.chat_id: c.title for c in (await s.scalars(
            select(ClassChat).where(ClassChat.chat_id.in_([r[1] for r in rows])))).all()} if rows else {}
    by_school: dict[int, dict] = {}
    for _pid, chat_id, cmp_id, closed, dispatched, answered, total in rows:
        sid = camps[cmp_id].school_id
        sc = by_school.setdefault(sid, {"school_id": sid, "name": school_names.get(sid, ""), "answered": 0,
                                        "total": 0, "classes": []})
        sc["answered"] += int(answered)
        sc["total"] += int(total)
        sc["classes"].append({"title": titles.get(chat_id, ""), "grade": grade_of(titles.get(chat_id)),
                              "answered": int(answered), "total": int(total), "pct": _pct(answered, total),
                              "dispatched": bool(dispatched), "closed": bool(closed)})
    out_schools = []
    for sc in by_school.values():
        sc["pct"] = _pct(sc["answered"], sc["total"])
        sc["classes"].sort(key=lambda c: (c["grade"] or 0, c["title"]))
        out_schools.append(sc)
    out_schools.sort(key=lambda x: (x["pct"], x["name"]))  # сначала те, где затык
    answered = sum(x["answered"] for x in out_schools)
    total = sum(x["total"] for x in out_schools)
    counts = dict((int(a), int(n)) for a, n in per)
    return {
        "id": rc.id, "title": rc.title, "text": rc.text, "template": rc.template, "options": rc.options or [],
        "deadline_text": fmt_dt(rc.deadline, None) if rc.deadline else "",
        "created_at": rc.created_at.isoformat() + "Z",
        "answered": answered, "total": total, "pct": _pct(answered, total),
        "per_option": [counts.get(i, 0) for i in range(len(rc.options or []))],
        "schools": out_schools,
        "classes": sum(len(x["classes"]) for x in out_schools),
        "waiting_dispatch": sum(1 for x in out_schools for c in x["classes"] if not c["dispatched"]),
    }


def render_summary_text(d: dict) -> str:
    lines = [f"🏛 <b>{html.escape(d['title'])}</b>",
             (f"Ответили <b>{d['pct']} %</b> ({d['answered']} из {d['total']})" if d["total"]
              else "Родителям ещё не разослан") + f" · школ: {len(d['schools'])}, "
             f"классов: {d['classes']}"]
    if d["waiting_dispatch"]:
        lines.append(f"Ещё рассылается: {d['waiting_dispatch']} класс(ов).")
    if d["schools"]:
        lines.append("")
        for sc in d["schools"][:15]:
            lines.append(f"• {html.escape(sc['name'])} — " + (f"{sc['pct']} % ({sc['answered']}/{sc['total']})"
                                                               if sc["total"] else "ещё не разослан"))
        if len(d["schools"]) > 15:
            lines.append(f"…и ещё {len(d['schools']) - 15}. Полная таблица — в приложении, с выгрузкой в CSV.")
    return "\n".join(lines)


def _csv_cell(value):
    """Защита от CSV-инъекции — та же, что у выгрузок в журнал (ловит и ведущие пробелы)."""
    return safe_cell(value)


def summary_csv(d: dict) -> bytes:
    """Таблица для отчёта: строка региона, строки школ и строки классов. Только цифры."""
    buf = io.StringIO()
    buf.write("﻿")  # BOM, чтобы Excel открыл кириллицу
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Уровень", "Школа", "Класс", "Ответили", "Всего", "Ответили, %"])
    w.writerow(["Регион", "", "", d["answered"], d["total"], d["pct"]])
    for sc in d["schools"]:
        w.writerow(["Школа", _csv_cell(sc["name"]), "", sc["answered"], sc["total"], sc["pct"]])
        for c in sc["classes"]:
            w.writerow(["Класс", _csv_cell(sc["name"]), _csv_cell(c["title"]), c["answered"], c["total"], c["pct"]])
    return buf.getvalue().encode("utf-8")


async def list_campaigns(region_id: int, limit: int = 20) -> list[RegionCampaign]:
    ids = await tree_ids(region_id)
    async with session() as s:
        return list((await s.scalars(select(RegionCampaign).where(RegionCampaign.region_id.in_(ids))
                                     .order_by(RegionCampaign.created_at.desc()).limit(limit))).all())


async def get_campaign(rc_id: int) -> RegionCampaign | None:
    async with session() as s:
        return await s.get(RegionCampaign, rc_id)


async def dashboard(region_id: int) -> dict:
    reg = await get_region(region_id)
    sch = await schools(region_id)
    cls = await classes(region_id)
    per_school: dict[int, int] = {}
    grades: set[int] = set()
    for sc, c in cls:
        per_school[sc.id] = per_school.get(sc.id, 0) + 1
        g = grade_of(c.title)
        if g:
            grades.add(g)
    camps = [await summary(rc) for rc in await list_campaigns(region_id, 10)]
    for c in camps:
        c.pop("text", None)
    return {
        "region": {"id": reg.id, "name": reg.name, "kind": reg.kind} if reg else None,
        "districts": [{"id": d.id, "name": d.name, "code": d.code} for d in await districts(region_id)],
        "schools": [{"id": sc.id, "name": sc.name, "classes": per_school.get(sc.id, 0)} for sc in sch],
        "grades": sorted(grades),
        "campaigns": camps,
    }


async def render_dashboard_text(reg: Region) -> str:
    d = await dashboard(reg.id)
    lines = [f"🏛 <b>{html.escape(reg.name)}</b>", ""]
    if not d["schools"]:
        lines.append("Пока ни одна школа не подключилась. Отправьте завучам код для школ — ниже.")
    else:
        total = sum(s_["classes"] for s_ in d["schools"])
        lines.append(f"Школ: <b>{len(d['schools'])}</b>, классов с ботом: <b>{total}</b>.")
    for c in d["campaigns"][:5]:
        worst = c["schools"][:1]
        tail = f", {html.escape(worst[0]['name'])} — {worst[0]['pct']} %" if worst and len(c["schools"]) > 1 else ""
        lines.append(f"• {html.escape(c['title'])}: " + (f"ответили {c['pct']} %{tail}" if c["total"] else "ещё не разослан"))
    return "\n".join(lines)


__all__ = ["launch", "summary", "summary_csv", "join_by_code", "create_region", "membership", "set_note", "grade_of"]

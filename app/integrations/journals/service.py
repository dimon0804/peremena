"""Импорт разобранной выгрузки в «Перемену»: доска, расписание, список класса.

* Домашние задания ложатся на доску класса «тихо» (без сообщения в чат) с предметом и датой —
  бот сразу отвечает по ним на «что задали?». Повторный импорт того же файла ничего не дублирует:
  такое же задание пропускается, изменённое — обновляется, а записи, сделанные учителем руками, не трогаются.
* Расписание заменяет уроки тех же дней (или тех же дней недели, если в файле нет дат).
* Список класса заменяет прежний и сверяется с участниками чата: кого из учеников ещё нет в чате.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select

from ...db import session
from ...models import Announcement, Lesson, Member, RosterEntry
from ...services import board, chats
from .parse import Parsed

SOURCES = ("journal", "dnevnik")


@dataclass
class Result:
    hw_added: int = 0
    hw_updated: int = 0
    hw_same: int = 0
    lessons: int = 0
    students: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").split()).lower() == " ".join((b or "").split()).lower()


async def apply(chat_id: int, author_id: int, parsed: Parsed, source: str = "journal") -> Result:
    res = Result()
    source = source if source in SOURCES else "journal"
    async with session() as s:
        # ---- домашние задания → доска
        for hw in parsed.homework:
            subject = board.canonical_subject(hw.get("subject") or "")[:100]
            text = (hw.get("text") or "").strip()[:3500]
            day = hw.get("day")
            if not text or not subject:
                continue
            q = select(Announcement).where(Announcement.chat_id == chat_id, Announcement.kind == "hw",
                                           Announcement.subject == subject)
            q = q.where(Announcement.for_date == day) if day else q.where(Announcement.for_date.is_(None))
            existing = list((await s.scalars(q)).all())
            if any(_same(a.text, text) for a in existing):
                res.hw_same += 1
                continue
            own = next((a for a in existing if a.source == source), None)
            if own is not None:
                own.text = text
                res.hw_updated += 1
                continue
            s.add(Announcement(chat_id=chat_id, author_id=author_id, kind="hw", subject=subject, text=text,
                               for_date=day, publish_mode="silent", published=True, source=source))
            res.hw_added += 1

        # ---- расписание: заменяем уроки тех же дат / тех же дней недели
        dated = {x["day"] for x in parsed.lessons if x.get("day")}
        weekly = {int(x["weekday"]) for x in parsed.lessons if not x.get("day")}
        if dated:
            await s.execute(delete(Lesson).where(Lesson.chat_id == chat_id, Lesson.day.in_(dated)))
        if weekly:
            await s.execute(delete(Lesson).where(Lesson.chat_id == chat_id, Lesson.day.is_(None),
                                                 Lesson.weekday.in_(weekly)))
        seen = set()
        for x in parsed.lessons:
            subject = board.canonical_subject(x.get("subject") or "")[:100]
            key = (x.get("day"), int(x.get("weekday") or 0), int(x.get("number") or 0), subject)
            if not subject or key in seen:
                continue
            seen.add(key)
            s.add(Lesson(chat_id=chat_id, day=key[0], weekday=key[1], number=key[2], subject=subject,
                         time=(x.get("time") or "")[:20], room=(x.get("room") or "")[:30], source=source))
            res.lessons += 1

        # ---- список класса: файл — это весь список, прежний заменяется
        if parsed.students:
            await s.execute(delete(RosterEntry).where(RosterEntry.chat_id == chat_id))
            names = set()
            for st in parsed.students:
                name = (st.get("name") or "").strip()[:200]
                if not name or name.lower() in names:
                    continue
                names.add(name.lower())
                s.add(RosterEntry(chat_id=chat_id, name=name, parents="; ".join(st.get("parents") or [])[:500]))
                res.students += 1
    return res


# ------------------------------------------------------------------ сверка списка класса с чатом
def _words(s: str) -> list[str]:
    return re.findall(r"[a-zа-я]+", (s or "").lower().replace("ё", "е"))


def _short(s: str) -> str:
    """«Иванова Анна» и «Иванова А.» → «иванова а»."""
    ws = _words(s)
    return f"{ws[0]} {ws[1][0]}" if len(ws) >= 2 else (ws[0] if ws else "")


async def roster_check(chat_id: int) -> dict:
    """Кто из списка класса уже в чате (сам родитель назвал ребёнка или родитель узнан по имени), а кого нет."""
    chat = await chats.get_chat(chat_id)
    teacher = chat.teacher_id if chat else None
    async with session() as s:
        roster = list((await s.scalars(select(RosterEntry).where(RosterEntry.chat_id == chat_id)
                                       .order_by(RosterEntry.name))).all())
        members = [m for m in (await s.scalars(select(Member).where(
            Member.chat_id == chat_id, Member.present.is_(True), Member.is_bot.is_(False)))).all()
            if m.user_id != teacher]
    firsts: dict[str, int] = {}
    for r in roster:
        ws = _words(r.name)
        if len(ws) > 1:
            firsts[ws[1]] = firsts.get(ws[1], 0) + 1
    parents = {_short(m.name): m for m in members if m.name}
    out_in, out_missing = [], []
    for r in roster:
        ws = _words(r.name)
        surname, first = (ws + ["", ""])[:2]
        via = None
        for m in members:
            cw = set(_words(m.child_name or ""))
            if not cw:
                continue
            if surname in cw and (not first or first in cw):
                via = "child"
            elif cw == {first} and firsts.get(first) == 1:
                via = "child"  # родитель написал только имя, а в классе оно одно
            if via:
                break
        if via is None:
            for p in (r.parents or "").split(";"):
                if p.strip() and _short(p) in parents:
                    via = "parent"
                    break
        (out_in if via else out_missing).append({"name": r.name, "via": via} if via else r.name)
    return {"total": len(roster), "in_chat": len(out_in), "missing": out_missing, "matched": out_in,
            "members": len(members)}


def render_check(check: dict, title: str = "") -> str:
    """Текст для бота: «В чате 24 из 27. Ещё не в чате: …»."""
    import html

    if not check["total"]:
        return "Списка класса пока нет — пришлите выгрузку из журнала со столбцом ФИО."
    head = f"👥 <b>Список класса{(' · ' + html.escape(title)) if title else ''}</b>: в чате {check['in_chat']} из {check['total']}."
    if not check["missing"]:
        return head + " Все ученики на месте."
    names = ", ".join(html.escape(n) for n in check["missing"][:40])
    more = f" и ещё {len(check['missing']) - 40}" if len(check["missing"]) > 40 else ""
    return (f"{head}\n\nЕщё не в чате (или родитель не назвал ребёнка): {names}{more}.\n\n"
            "<i>Узнаю ученика, когда родитель отметит имя ребёнка (например, при отметке «не придёт») "
            "или если его имя совпадает с родителем из списка.</i>")


# ------------------------------------------------------------------ расписание для ответов бота
async def lessons_for(chat_ids: list[int], day: date) -> dict[int, list[Lesson]]:
    """Уроки на день по классам: сначала уроки с этой датой, если их нет — расписание по дню недели."""
    if not chat_ids:
        return {}
    async with session() as s:
        rows = list((await s.scalars(select(Lesson).where(
            Lesson.chat_id.in_(chat_ids),
            (Lesson.day == day.isoformat()) | (Lesson.day.is_(None) & (Lesson.weekday == day.weekday())),
        ))).all())
    out: dict[int, list[Lesson]] = {}
    for cid in chat_ids:
        mine = [x for x in rows if x.chat_id == cid]
        dated = [x for x in mine if x.day]
        chosen = dated or mine
        if chosen:
            out[cid] = sorted(chosen, key=lambda x: (x.number or 99, x.time, x.subject))
    return out


async def has_schedule(chat_ids: list[int]) -> bool:
    if not chat_ids:
        return False
    async with session() as s:
        return await s.scalar(select(Lesson.id).where(Lesson.chat_id.in_(chat_ids)).limit(1)) is not None

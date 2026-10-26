"""«Кто сегодня не придёт».

Родитель отмечает отсутствие ребёнка кнопкой — вместо сообщения «мы сегодня не придём» в общий чат,
которое тонет среди остальных. Учитель в начале рабочего дня получает готовый список,
а по понедельникам — пропуски за прошлую неделю. Ручного учёта больше нет.
"""
from __future__ import annotations

import html
import re
from datetime import date, timedelta

from sqlalchemy import select

from ..db import session
from ..models import Absence, ClassChat, Member, User
from ..timeutil import WEEKDAYS, local_now
from . import chats, stats

REASONS = {"sick": "болеет", "family": "семейные обстоятельства", "other": "другая причина"}

# «заболел», «не придём», «температура» — родитель пишет об отсутствии своими словами
ABSENCE_RE = re.compile(
    r"\b(заболе\w*|болеет|приболе\w*|температур\w*|не\s+прид[её]\w*|не\s+будет\s+в\s+школе|пропуст\w*\s+школ\w*|"
    r"к\s+врачу|в\s+больниц\w*|на\s+больничн\w*)",
    re.IGNORECASE,
)


def looks_like_absence(text: str) -> bool:
    return bool(ABSENCE_RE.search(text or ""))


def day_label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


async def child_name(chat_id: int, user_id: int) -> str | None:
    async with session() as s:
        m = await s.scalar(select(Member).where(Member.chat_id == chat_id, Member.user_id == user_id))
        return m.child_name if m and m.child_name else None


async def set_child_name(chat_id: int, user_id: int, name: str) -> None:
    async with session() as s:
        m = await s.scalar(select(Member).where(Member.chat_id == chat_id, Member.user_id == user_id))
        if m is None:
            m = Member(chat_id=chat_id, user_id=user_id)
            s.add(m)
        m.child_name = " ".join(name.split())[:200]


async def mark(chat_id: int, user_id: int, day: str, reason: str = "sick", child: str = "",
               comment: str = "") -> tuple[Absence, bool]:
    """Отмечает отсутствие. Повторная отметка на тот же день обновляет причину. (запись, новая_ли)."""
    reason = reason if reason in REASONS else "other"
    async with session() as s:
        a = await s.scalar(select(Absence).where(Absence.chat_id == chat_id, Absence.user_id == user_id,
                                                 Absence.day == day, Absence.child_name == child))
        created = a is None
        if created:
            a = Absence(chat_id=chat_id, user_id=user_id, day=day, child_name=child)
            s.add(a)
        a.reason, a.comment = reason, comment[:500]
        await s.flush()
    if created:
        teacher_id = (await chats.get_chat(chat_id) or ClassChat()).teacher_id
        await stats.add(teacher_id, "absence")
    return a, created


async def cancel(absence_id: int, user_id: int) -> bool:
    async with session() as s:
        a = await s.get(Absence, absence_id)
        if a is None or a.user_id != user_id:
            return False
        await s.delete(a)
        return True


async def for_days(chat_ids: list[int], days: list[str]) -> list[Absence]:
    if not chat_ids:
        return []
    async with session() as s:
        return list((await s.scalars(
            select(Absence).where(Absence.chat_id.in_(chat_ids), Absence.day.in_(days)).order_by(Absence.child_name)
        )).all())


async def mine(user_id: int, from_day: str) -> list[Absence]:
    async with session() as s:
        return list((await s.scalars(
            select(Absence).where(Absence.user_id == user_id, Absence.day >= from_day).order_by(Absence.day)
        )).all())


async def who(a: Absence) -> str:
    """Как показать ребёнка учителю: имя ребёнка, а если его не назвали — через родителя."""
    if a.child_name:
        return a.child_name
    parent = await chats.member_name(a.chat_id, a.user_id)
    return f"ребёнок ({parent})"


async def render_day(teacher: User, day: str | None = None, with_title: bool = True) -> tuple[str, int]:
    """Список отсутствующих по классам учителя. (текст, сколько человек)."""
    tz = teacher.tz
    day = day or local_now(tz).date().isoformat()
    my = await chats.teacher_chats(teacher.user_id)
    items = await for_days([c.chat_id for c in my], [day])
    titles = {c.chat_id: c.title for c in my}
    lines = [f"🤒 <b>Не придут — {day_label(day)}</b>"] if with_title else []
    by_chat: dict[int, list[str]] = {}
    for a in items:
        by_chat.setdefault(a.chat_id, []).append(
            f"{html.escape(await who(a))} — {REASONS.get(a.reason, a.reason)}"
            + (f" ({html.escape(a.comment)})" if a.comment else ""))
    for cid, rows in by_chat.items():
        if len(my) > 1:
            lines.append(f"<b>{html.escape(titles.get(cid, ''))}</b>")
        lines += [f"• {r}" for r in rows]
    if not items and with_title:
        lines.append("Все на месте — никто не отметил отсутствие.")
    return "\n".join(lines), len(items)


async def week_summary(teacher: User) -> str:
    """Пропуски за прошлые 7 дней — для понедельничной сводки."""
    today = local_now(teacher.tz).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(1, 8)]
    my = await chats.teacher_chats(teacher.user_id)
    items = await for_days([c.chat_id for c in my], days)
    if not items:
        return ""
    count: dict[str, int] = {}
    for a in items:
        name = await who(a)
        count[name] = count.get(name, 0) + 1
    top = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))
    return "🗓 <b>Пропуски за неделю:</b> " + ", ".join(f"{html.escape(n)} — {c} дн." for n, c in top[:15])

"""Выгрузка обратно в журнал: справки об отсутствии и итоги сборов в CSV.

Формат — тот, что без вопросов открывает Excel и принимают импорт-мастера журналов и таблиц:
UTF-8 с BOM, разделитель «;», первая строка — заголовки, даты ДД.ММ.ГГГГ.
Ячейка, которую Excel принял бы за формулу (=, +, -, @, табуляция), начинается с апострофа.

Шаблон для импорта (XLSX с тремя листами или CSV на каждый раздел) — тоже здесь.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from sqlalchemy import select

from ...db import session
from ...models import Absence, Member, Poll
from ...services import attendance
from ...timeutil import to_local

FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value):
    """Защита от CSV-инъекции: '=HYPERLINK(…)' превращается в текст, а не в формулу."""
    if isinstance(value, str) and value and (value.startswith(FORMULA_START)
                                             or value.lstrip().startswith(FORMULA_START[:4])):
        return "'" + value
    return value


def to_csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    buf.write("﻿")  # BOM: Excel под Windows иначе покажет кракозябры вместо кириллицы
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([safe_cell(x) for x in r])
    return buf.getvalue().encode("utf-8")


def _ru(day: str) -> str:
    try:
        return f"{date.fromisoformat(day):%d.%m.%Y}"
    except ValueError:
        return day


async def absences_csv(chat_ids: list[int], titles: dict[int, str], start: date, end: date) -> bytes:
    """Отсутствия за период: дата, класс, ученик, причина, комментарий, кто сообщил."""
    if chat_ids:
        async with session() as s:
            rows = list((await s.scalars(select(Absence).where(
                Absence.chat_id.in_(chat_ids), Absence.day >= start.isoformat(), Absence.day <= end.isoformat(),
            ).order_by(Absence.day, Absence.chat_id, Absence.child_name))).all())
            names = {(m.chat_id, m.user_id): m.name for m in (await s.scalars(select(Member).where(
                Member.chat_id.in_(chat_ids)))).all()}
    else:
        rows, names = [], {}
    out = []
    for a in rows:
        parent = names.get((a.chat_id, a.user_id), "")
        out.append([_ru(a.day), titles.get(a.chat_id, ""), a.child_name or f"ребёнок ({parent})",
                    attendance.REASONS.get(a.reason, a.reason), a.comment or "", parent])
    return to_csv(["Дата", "Класс", "Ученик", "Причина", "Комментарий", "Сообщил"], out)


async def polls_csv(chat_ids: list[int], titles: dict[int, str], start: date, end: date, tz: str | None) -> bytes:
    """Итоги сборов, созданных за период: по строке на участника — кто что ответил и когда."""
    from datetime import datetime, timedelta

    lo = datetime.combine(start, datetime.min.time()) - timedelta(days=1)
    hi = datetime.combine(end, datetime.min.time()) + timedelta(days=2)
    if chat_ids:
        async with session() as s:
            polls = list((await s.scalars(select(Poll).where(
                Poll.chat_id.in_(chat_ids), Poll.created_at >= lo, Poll.created_at < hi,
            ).order_by(Poll.created_at))).all())
            kids = {(m.chat_id, m.user_id): m.child_name for m in (await s.scalars(select(Member).where(
                Member.chat_id.in_(chat_ids)))).all()}
    else:
        polls, kids = [], {}
    out = []
    for p in polls:
        created = to_local(p.created_at, tz).date()
        if not start <= created <= end:
            continue
        for t in p.targets:
            ans = p.options[t.answer] if t.answer is not None and t.answer < len(p.options) else "не ответил"
            out.append([
                f"{created:%d.%m.%Y}", titles.get(p.chat_id, ""), p.title,
                f"{to_local(p.deadline, tz):%d.%m.%Y %H:%M}" if p.deadline else "",
                "закрыт" if p.closed else "идёт", t.name, kids.get((p.chat_id, t.user_id)) or "", ans,
                f"{to_local(t.answered_at, tz):%d.%m.%Y %H:%M}" if t.answered_at else "",
                (p.amount if t.paid else 0) if p.amount else "",
            ])
    return to_csv(["Создан", "Класс", "Сбор", "Срок", "Статус", "Участник", "Ребёнок", "Ответ", "Когда ответил",
                   "Сдано, ₽"], out)


# ------------------------------------------------------------------ шаблон для импорта
TEMPLATE = {
    "roster": ("Ученики", ["ФИО ученика", "Родители"],
               [["Иванов Иван Сергеевич", "Иванова Анна Петровна; Иванов Сергей Олегович"],
                ["Петрова Мария", "Петрова Елена"]]),
    "schedule": ("Расписание", ["Дата", "№ урока", "Время", "Предмет", "Кабинет"],
                 [["10.11.2026", "1", "08:30-09:15", "Алгебра", "21"],
                  ["10.11.2026", "2", "09:25-10:10", "Русский язык", "14"]]),
    "homework": ("Домашние задания", ["Дата", "Предмет", "Домашнее задание"],
                 [["10.11.2026", "Алгебра", "№ 245, 247"], ["10.11.2026", "Русский язык", "Упр. 112, правило на с. 63"]]),
}


def template_csv(kind: str) -> bytes:
    _, header, rows = TEMPLATE[kind]
    return to_csv(header, rows)


def template_xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, header, rows in TEMPLATE.values():
        ws = wb.create_sheet(title)
        ws.append(header)
        for r in rows:
            ws.append(r)
        for col, width in zip("ABCDE", (16, 12, 14, 22, 40)):
            ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

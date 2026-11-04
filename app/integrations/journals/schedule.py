"""Ответы по расписанию: «какие уроки завтра?», «что взять в среду?».

Расписание приходит из выгрузки журнала или из «Дневник.ру». На «что взять» бот перечисляет уроки
дня и домашку к ним с доски класса — то, что родитель обычно выясняет в чате по вечерам.
"""
from __future__ import annotations

import html
from datetime import date, timedelta

from ...services import board, search
from ...timeutil import WEEKDAYS, local_now
from . import service

LESSON_WORDS = ("расписан", "какие урок", "сколько урок", "уроки завтра", "уроки сегодня", "уроки в ",
                "уроки на ", "во сколько урок", "первый урок", "какой первый")
BRING_WORDS = ("что взять", "что брать", "что с собой", "собрать портфель", "собрать рюкзак", "что положить в портфель",
               "какие учебники", "что нести")


def is_schedule_question(text: str) -> bool:
    t = search.normalize(text)
    return any(w in t for w in LESSON_WORDS) or is_bring_question(text)


def is_bring_question(text: str) -> bool:
    t = search.normalize(text)
    return any(w in t for w in BRING_WORDS)


def target_day(text: str, tz: str | None) -> date:
    """День из вопроса, а без дня — завтра (в субботу и воскресенье — понедельник)."""
    d = board.date_in(text, tz)
    if d:
        return d
    d = local_now(tz).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


async def answer(text: str, chat_ids: list[int], tz: str | None, titles: dict[int, str]) -> tuple[str, bool]:
    """(текст ответа, нашлось ли расписание)."""
    day = target_day(text, tz)
    label = f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"
    by_chat = await service.lessons_for(chat_ids, day)
    if not by_chat:
        return f"🗓 На {label} расписания у меня нет.", False
    bring = is_bring_question(text)
    hw: dict[tuple[int, str], str] = {}
    if bring:
        items = [a for a in await board.list_for_chats(chat_ids, days=30)
                 if a.kind == "hw" and board.is_visible(a) and a.for_date == day.isoformat()]
        for a in sorted(items, key=lambda x: x.created_at):
            hw[(a.chat_id, a.subject)] = a.text
    lines = [f"🗓 <b>{'Что взять' if bring else 'Уроки'} на {label}</b>"]
    for cid, lessons in by_chat.items():
        if len(by_chat) > 1:
            lines.append(f"\n<b>{html.escape(titles.get(cid, 'Класс'))}</b>")
        for x in lessons:
            num = f"{x.number}. " if x.number else "• "
            tm = f"{x.time.split('–')[0]} " if x.time else ""
            room = f" (каб. {html.escape(x.room)})" if x.room else ""
            line = f"{num}{tm}{html.escape(x.subject)}{room}"
            if bring and (cid, x.subject) in hw:
                line += f" — д/з: {html.escape(hw[(cid, x.subject)][:200])}"
            lines.append(line)
    if bring:
        lines.append("\n<i>Учебники и тетради по этим предметам; задания — с доски класса.</i>")
    lines.append("<i>📌 Расписание из электронного журнала.</i>")
    return "\n".join(lines), True

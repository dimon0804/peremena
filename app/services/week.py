"""«Что важно на неделе» — одна карточка вместо пролистывания чата класса.

Родителю: сборы, где ждут его ответа (со сроком), домашка по дням на 7 дней вперёд, объявления за неделю,
его отметки «не придёт». Учителю: сроки открытых сборов с числом молчащих, отсутствующие сегодня и домашка.
Только то, что уже есть в базе, — бот ничего не сочиняет.
"""
from __future__ import annotations

import html
from datetime import date, timedelta

from ..models import User
from ..timeutil import WEEKDAYS, fmt_dt, local_now, to_local
from . import attendance, board, chats, polls, school

DAYS = 7


def _day(d: str) -> str:
    dd = date.fromisoformat(d)
    return f"{WEEKDAYS[dd.weekday()]} {dd:%d.%m}"


async def collect(user: User) -> dict:
    tz = user.tz
    today = local_now(tz).date()
    horizon = today + timedelta(days=DAYS)
    r = await school.roles(user.user_id)
    member = [c for c in await chats.member_chats(user.user_id) if c.teacher_id != user.user_id]
    titles = {c.chat_id: c.title for c in member} | {c.chat_id: c.title for c, _ in r.board_chats()}

    # сборы
    poll_items = []
    if r.is_class_teacher:
        for p in await polls.list_polls(user.user_id, include_closed=False, limit=50):
            answered, total, _ = polls.counts(p)
            poll_items.append({"id": p.id, "title": p.title, "chat_title": titles.get(p.chat_id, ""),
                               "deadline_text": fmt_dt(p.deadline, tz), "deadline": p.deadline,
                               "missing": total - answered, "total": total, "mine": True})
    for p in await polls.pending_for_user(user.user_id):
        if p.deadline and to_local(p.deadline, tz).date() > horizon:
            continue
        poll_items.append({"id": p.id, "title": p.title, "chat_title": titles.get(p.chat_id, ""),
                           "deadline_text": fmt_dt(p.deadline, tz), "deadline": p.deadline,
                           "amount": p.amount, "mine": False})
    poll_items.sort(key=lambda x: (x["deadline"] is None, x["deadline"] or 0))
    for x in poll_items:
        x.pop("deadline")

    # домашка по дням и объявления
    chat_ids = sorted(titles)
    anns = await board.list_for_chats(chat_ids, days=21)
    hw = [a for a in anns if a.kind == "hw" and board.is_visible(a) and a.for_date
          and today.isoformat() <= a.for_date <= horizon.isoformat()]
    hw.sort(key=lambda a: (a.for_date, a.subject))
    news = [a for a in anns if a.kind == "news" and board.is_visible(a)
            and (today - to_local(a.created_at, tz).date()).days <= DAYS][:5]

    # отсутствия: родителю — свои отметки, учителю — кто не придёт сегодня
    absences = []
    if r.is_class_teacher:
        for a in await attendance.for_days([c.chat_id for c in r.class_chats], [today.isoformat()]):
            absences.append({"who": await attendance.who(a), "day_text": _day(a.day),
                             "reason_text": attendance.REASONS.get(a.reason, "")})
    for a in await attendance.mine(user.user_id, today.isoformat()):
        absences.append({"who": a.child_name or "ребёнок", "day_text": _day(a.day),
                         "reason_text": attendance.REASONS.get(a.reason, ""), "mine": True})

    return {
        "from": today.isoformat(), "to": horizon.isoformat(),
        "polls": poll_items,
        "homework": [{"day": a.for_date, "day_text": _day(a.for_date), "subject": a.subject or "Задание",
                      "text": a.text, "chat_title": titles.get(a.chat_id, "")} for a in hw],
        "news": [{"text": a.text, "chat_title": titles.get(a.chat_id, ""),
                  "date": f"{to_local(a.created_at, tz):%d.%m}"} for a in news],
        "absences": absences,
        "classes": len(titles),
    }


def render(w: dict) -> str:
    """Текст для лички с ботом (HTML-разметка MAX)."""
    e = html.escape
    parts = [f"🗓 <b>Что важно на неделе</b> · {_day(w['from'])} — {_day(w['to'])}"]
    if w["polls"]:
        lines = []
        for p in w["polls"][:10]:
            if p["mine"]:
                tail = f"не ответили {p['missing']} из {p['total']}" if p["missing"] else "ответили все"
                lines.append(f"• {e(p['title'])} — до {e(p['deadline_text'])}, {tail}")
            else:
                money = f", {polls.rub(p['amount'])}" if p.get("amount") else ""
                lines.append(f"• {e(p['title'])}{money} — ответить до {e(p['deadline_text'])}")
        parts.append("📋 <b>Сборы</b>\n" + "\n".join(lines))
    if w["homework"]:
        lines, day = [], None
        for h in w["homework"][:20]:
            if h["day"] != day:
                day = h["day"]
                lines.append(f"<b>{e(h['day_text'])}</b>")
            lines.append(f"• {e(h['subject'])}: {e(h['text'])}")
        parts.append("📚 <b>Домашка</b>\n" + "\n".join(lines))
    if w["news"]:
        parts.append("📣 <b>Объявления</b>\n" + "\n".join(f"• {e(n['date'])}: {e(n['text'])}" for n in w["news"]))
    if w["absences"]:
        parts.append("🤒 <b>Не придут</b>\n" + "\n".join(
            f"• {e(a['who'])} — {e(a['day_text'])}, {e(a['reason_text'])}" for a in w["absences"]))
    if len(parts) == 1:
        parts.append("На этой неделе ничего срочного: сборов со сроком нет, домашку ещё не записали.")
    return "\n\n".join(parts)

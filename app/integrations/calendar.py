"""Подписка на календарь (iCalendar, RFC 5545) по секретной ссылке.

Учитель видит в своём Яндекс/Google/Apple Календаре сроки сборов («ответили 18 из 24») и домашку
своих классов, родитель — сроки сборов, где ждут его ответа (с напоминанием), и домашние задания.
Календарь сам перечитывает ленту, поэтому цифры в описании свежие без единого действия учителя.

В ленте нет чужих имён: у учителя — только счётчики, у родителя — только его собственный ответ.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select

from ..core import ctx
from ..db import session
from ..models import Announcement, Poll, User
from ..services import board, chats, polls, school
from ..timeutil import local_now, utcnow

PRODID = "-//CLV-DIGITAL//Peremena//RU"
PAST_DAYS = 30      # насколько назад показываем сроки и домашку
AHEAD_DAYS = 60


# ------------------------------------------------------------------ формат iCalendar
def _esc(text: str) -> str:
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", ""))


def _fold(line: str) -> str:
    """Строки длиннее 75 байт переносятся (RFC 5545, 3.1), не разрывая символы UTF-8."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > 75:
            out.append(cur)
            cur, size = " " + ch, 1 + n
        else:
            cur += ch
            size += n
    out.append(cur)
    return "\r\n".join(out)


def _utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _event(uid: str, summary: str, description: str, *, start: datetime | None = None,
           day: date | None = None, minutes: int = 30, alarm_before: int | None = None,
           url: str = "", stamp: datetime | None = None) -> list[str]:
    host = "peremena"
    lines = ["BEGIN:VEVENT", f"UID:{uid}@{host}", f"DTSTAMP:{_utc(stamp or utcnow())}"]
    if day is not None:
        lines += [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
                  "TRANSP:TRANSPARENT"]
    else:
        lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(start + timedelta(minutes=minutes))}"]
    lines += [f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(description)}"]
    if url:
        lines.append(f"URL:{url}")
    if alarm_before:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(summary)}",
                  f"TRIGGER:-PT{alarm_before}M", "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def render(name: str, events: list[list[str]], tz: str) -> str:
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
            f"X-WR-CALNAME:{_esc(name)}", f"X-WR-TIMEZONE:{tz}",
            "X-WR-CALDESC:Сроки сборов и домашние задания из «Перемены»",
            # как часто перечитывать ленту (Apple, Outlook; Google и Яндекс решают сами)
            "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    body = [line for ev in events for line in ev]
    return "\r\n".join(_fold(x) for x in head + body + ["END:VCALENDAR"]) + "\r\n"


# ------------------------------------------------------------------ данные
def _poll_link(poll_id: int) -> str:
    c = ctx()
    return c.start_link(f"poll_{poll_id}") if c.bot_username else ""


async def _hw_events(chat_ids: list[int], titles: dict[int, str], tz: str) -> list[list[str]]:
    if not chat_ids:
        return []
    today = local_now(tz).date()
    lo, hi = (today - timedelta(days=PAST_DAYS)).isoformat(), (today + timedelta(days=AHEAD_DAYS)).isoformat()
    async with session() as s:
        rows = (await s.scalars(select(Announcement).where(
            Announcement.chat_id.in_(chat_ids), Announcement.kind == "hw",
            Announcement.for_date.is_not(None), Announcement.for_date >= lo, Announcement.for_date <= hi,
        ).order_by(Announcement.for_date))).all()
    out = []
    for a in rows:
        if not board.is_visible(a):
            continue
        cls = titles.get(a.chat_id, "")
        out.append(_event(f"hw-{a.id}", f"📚 {a.subject or 'Домашнее задание'}" + (f" · {cls}" if cls else ""),
                          a.text, day=date.fromisoformat(a.for_date), stamp=a.created_at))
    return out


async def _poll_rows(chat_ids: list[int]) -> list[Poll]:
    if not chat_ids:
        return []
    since = utcnow() - timedelta(days=PAST_DAYS)
    async with session() as s:
        return list((await s.scalars(select(Poll).where(
            Poll.chat_id.in_(chat_ids), Poll.deadline.is_not(None), Poll.deadline >= since,
        ).order_by(Poll.deadline))).all())


async def feed_for(user: User) -> str:
    """Лента пользователя: всё, что касается его как учителя, и всё, что касается его как родителя."""
    tz = user.tz or ctx().settings.default_tz
    r = await school.roles(user.user_id)
    staff_chats = {c.chat_id: c for c, _ in r.board_chats()}
    if r.is_admin and r.school:
        for c in await school.school_chats(r.school.id):
            staff_chats.setdefault(c.chat_id, c)
    member = {c.chat_id: c for c in await chats.member_chats(user.user_id) if c.chat_id not in staff_chats}
    titles = {cid: c.title for cid, c in {**staff_chats, **member}.items()}
    events: list[list[str]] = []
    seen: set[int] = set()

    # учитель: сроки сборов своих классов (у администрации — всей школы), только счётчики
    own = {c.chat_id for c in r.class_chats} | (set(staff_chats) if r.is_admin else set())
    for p in await _poll_rows(sorted(own)):
        answered, total, _ = polls.counts(p)
        state = "закрыт" if p.closed else ("ответили все" if total and answered == total else
                                           f"не ответили: {total - answered}")
        desc = f"{titles.get(p.chat_id, '')}\nОтветили {answered} из {total} — {state}."
        m = polls.money(p)
        if m:
            desc += f"\nСобрано {polls.rub(m['collected'])} из {polls.rub(m['expected'])}."
        mark = "✅" if p.closed or (total and answered == total) else "📋"
        events.append(_event(f"poll-{p.id}", f"{mark} Срок сбора: {p.title} ({answered}/{total})", desc,
                             start=p.deadline, stamp=p.created_at))
        seen.add(p.id)

    # родитель: сборы, адресованные ему, — с его собственным ответом и напоминанием, если ответа нет
    for p in await _poll_rows(sorted(member)):
        if p.id in seen or p.teacher_id == user.user_id:
            continue
        target = next((t for t in p.targets if t.user_id == user.user_id), None)
        if target is None and not await chats.is_present(p.chat_id, user.user_id):
            continue
        ans = target.answer if target else None
        link = _poll_link(p.id)
        if ans is not None and ans < len(p.options):
            summary = f"✅ {p.title}: {p.options[ans]}"
            alarm = None
        elif p.closed:
            summary = f"⏹ {p.title} (сбор закрыт)"
            alarm = None
        else:
            summary = f"📋 Ответить до срока: {p.title}"
            alarm = 180
        desc = p.text + (f"\nСумма: {polls.rub(p.amount)}" if p.amount else "") + \
            f"\n{titles.get(p.chat_id, '')}" + (f"\nОтветить в MAX: {link}" if link else "")
        events.append(_event(f"poll-{p.id}-u", summary, desc, start=p.deadline, alarm_before=alarm,
                             url=link, stamp=p.created_at))

    events += await _hw_events(sorted(set(staff_chats) | set(member)), titles, tz)
    name = "Перемена"
    if len(titles) == 1:
        name += " · " + next(iter(titles.values()))
    return render(name, events, tz)


# ------------------------------------------------------------------ ссылки для подписки
def links(feed_url: str) -> dict:
    """Как подписаться: Google — готовая ссылка, Apple — webcal://, Яндекс — вставить адрес в «Подписки»."""
    webcal = "webcal://" + feed_url.split("://", 1)[-1]
    return {
        "url": feed_url,
        "webcal": webcal,
        "google": "https://calendar.google.com/calendar/r?cid=" + quote(webcal, safe=""),
        "yandex": "https://calendar.yandex.ru/",
    }

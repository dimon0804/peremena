"""«Доска класса»: домашние задания и объявления.

* «Что задали по алгебре на завтра?» — бот отвечает сам, по последним записям доски.
  Распознавание — по словам-маркерам и словарю предметов, без ИИ.
* Отложенная публикация: написали вечером — в чат уйдёт утром в начале рабочего дня.
  Граница рабочего дня работает в обе стороны: родителям тоже не приходят сообщения в 23:00.
* «Тихо на доску»: запись не публикуется в чат, но бот отвечает по ней на вопросы.
"""
from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..core import ctx
from ..db import session
from ..models import Announcement, ClassChat, User
from ..timeutil import WEEKDAYS, local_now, to_local, to_utc, utcnow
from . import search

log = logging.getLogger(__name__)

SUBJECTS: dict[str, tuple[str, ...]] = {
    "Алгебра": ("алгебр",),
    "Геометрия": ("геометр",),
    "Математика": ("математ", "матем", "матеш"),
    "Русский язык": ("русск", "рус"),
    "Литература": ("литер", "литра", "чтени"),
    "Английский язык": ("английск", "англ", "инглиш"),
    "Немецкий язык": ("немецк",),
    "Французский язык": ("французск",),
    "Китайский язык": ("китайск",),
    "История": ("истори",),
    "Обществознание": ("обществ",),
    "Физика": ("физик",),
    "Химия": ("хими",),
    "Биология": ("биолог",),
    "География": ("географ",),
    "Информатика": ("информат", "инфа", "программир"),
    "Физкультура": ("физкульт", "физр", "физ-р"),
    "Музыка": ("музык",),
    "ИЗО": ("изо", "рисован"),
    "Труд": ("технолог", "труд"),
    "ОБЗР": ("обж", "обзр"),
    "Окружающий мир": ("окружающ", "окруж"),
    "Вероятность и статистика": ("вероятн", "статист"),
}

HW_MARKERS = ("задал", "задан", "дз", "домашк", "домашн", "д/з", "упражнен")
NEWS_MARKERS = ("объявлен", "новост", "что нового")
_DAY_WORDS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6}


def canonical_subject(text: str) -> str:
    """«англ» → «Английский язык». Неизвестный предмет возвращается как есть (с заглавной)."""
    found = subject_in(text)
    if found:
        return found
    t = (text or "").strip()
    return (t[:1].upper() + t[1:])[:100] if t else ""


ABBREVIATIONS = {
    "гео": "География", "физ": "Физика", "мат": "Математика", "лит": "Литература", "общ": "Обществознание",
    "био": "Биология", "инф": "Информатика", "хим": "Химия", "ист": "История", "алг": "Алгебра",
    "геом": "Геометрия", "рус": "Русский язык", "англ": "Английский язык", "нем": "Немецкий язык",
}


def subject_in(text: str) -> str | None:
    ws = search.words(text.replace("-", "")) + search.words(text)
    for w in ws:  # полные формы и устойчивые сокращения: «физ-ра» → «физра», «английскому»
        for name, aliases in SUBJECTS.items():
            if any(w.startswith(a) for a in aliases):
                return name
    for w in ws:  # короткие школьные сокращения: «хим», «гео», «физ»
        if w in ABBREVIATIONS:
            return ABBREVIATIONS[w]
    return None


def is_homework_question(text: str) -> bool:
    t = search.normalize(text)
    return any(m in t for m in ("д/з",)) or any(w.startswith(HW_MARKERS) for w in search.words(t))


def is_news_question(text: str) -> bool:
    t = search.normalize(text)
    return any(m in t for m in NEWS_MARKERS)


def date_in(text: str, tz: str | None) -> date | None:
    t = search.normalize(text)
    today = local_now(tz).date()
    if "послезавтра" in t:
        return today + timedelta(days=2)
    if "завтра" in t:
        return today + timedelta(days=1)
    if "сегодня" in t:
        return today
    for word, wd in _DAY_WORDS.items():
        if word in t:
            return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    return None


def fmt_day(d: str | None) -> str:
    if not d:
        return ""
    dd = date.fromisoformat(d)
    return f"{WEEKDAYS[dd.weekday()]} {dd:%d.%m}"


def next_morning(user: User | None) -> datetime:
    """Ближайшее начало рабочего дня автора (UTC). Выходные пропускаются, если они у учителя нерабочие."""
    tz = user.tz if user else ctx().settings.default_tz
    start = user.work_start if user else 8 * 60
    now = local_now(tz)
    cand = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    if user is None or user.weekends_off:
        while cand.weekday() >= 5:
            cand += timedelta(days=1)
    return to_utc(cand)


def in_quiet_hours(user: User | None) -> bool:
    from .quiet import in_work_window

    return bool(user) and user.quiet_enabled and not in_work_window(user)


# ---------------------------------------------------------------- запись
async def create(chat_id: int, author_id: int, kind: str, text: str, subject: str = "", for_date: str | None = None,
                 publish_mode: str = "now") -> Announcement:
    author = await _user(author_id)
    publish_at = None
    if publish_mode == "later":
        publish_at = next_morning(author)
    async with session() as s:
        a = Announcement(
            chat_id=chat_id, author_id=author_id, kind=kind if kind in ("hw", "news") else "hw",
            subject=canonical_subject(subject) if kind == "hw" else "", text=text.strip()[:3500],
            for_date=for_date, publish_mode=publish_mode, publish_at=publish_at,
            published=publish_mode == "silent",
        )
        s.add(a)
        await s.flush()
    if publish_mode == "now":
        await publish(a.id)
    return await get(a.id)


async def _user(uid: int) -> User | None:
    async with session() as s:
        return await s.get(User, uid)


async def get(ann_id: int) -> Announcement | None:
    async with session() as s:
        return await s.get(Announcement, ann_id)


def render(a: Announcement, author_name: str = "") -> str:
    if a.kind == "hw":
        head = "📚 <b>Домашнее задание" + (f" · {html.escape(a.subject)}" if a.subject else "") + "</b>"
        if a.for_date:
            head += f" (на {fmt_day(a.for_date)})"
    else:
        head = "📣 <b>Объявление</b>"
    foot = f"\n\n<i>— {html.escape(author_name)}</i>" if author_name else ""
    return f"{head}\n\n{html.escape(a.text)}{foot}"


async def publish(ann_id: int) -> bool:
    a = await get(ann_id)
    if a is None or (a.published and a.publish_mode != "silent" and a.message_id):
        return False
    author = await _user(a.author_id)
    msg = await ctx().safe_send(chat_id=a.chat_id, text=render(a, author.display_name if author else ""))
    async with session() as s:
        row = await s.get(Announcement, ann_id)
        if msg:
            row.published = True
            row.message_id = (msg.get("body") or {}).get("mid")
        elif utcnow() - row.created_at < timedelta(days=2):
            # не получилось (сеть, бот временно не в чате) — повторим через 10 минут
            row.publish_at = utcnow() + timedelta(minutes=10)
        else:
            row.publish_at = None  # двое суток без успеха — больше не пытаемся
    return bool(msg)


async def tick() -> None:
    """Раз в минуту: публикуем отложенные записи, время которых пришло."""
    async with session() as s:
        due = (await s.scalars(select(Announcement).where(
            Announcement.published.is_(False), Announcement.publish_at.is_not(None), Announcement.publish_at <= utcnow(),
        ))).all()
    for a in due:
        try:
            await publish(a.id)
        except Exception:
            log.exception("Публикация записи доски %s", a.id)


async def delete(ann_id: int, user_id: int) -> bool:
    async with session() as s:
        a = await s.get(Announcement, ann_id)
        if a is None:
            return False
        chat = await s.get(ClassChat, a.chat_id)
        if a.author_id != user_id and not (chat and chat.teacher_id == user_id):
            return False
        mid = a.message_id
        await s.delete(a)
    if mid:
        try:
            await ctx().api.delete_message(mid)
        except Exception:  # сообщение могли уже удалить руками
            pass
    return True


async def list_for_chats(chat_ids: list[int], days: int = 21, limit: int = 100) -> list[Announcement]:
    if not chat_ids:
        return []
    async with session() as s:
        return list((await s.scalars(
            select(Announcement).where(
                Announcement.chat_id.in_(chat_ids), Announcement.created_at >= utcnow() - timedelta(days=days),
            ).order_by(Announcement.created_at.desc()).limit(limit)
        )).all())


def is_visible(a: Announcement) -> bool:
    # скрываем только запланированное на утро, пока оно не вышло; запись «сейчас» видна,
    # даже если публикация в чат не удалась — бот всё равно отвечает по ней
    return a.published or a.publish_mode != "later"


# ---------------------------------------------------------------- ответы на вопросы
async def find_homework(query: str, chat_ids: list[int], tz: str | None) -> list[Announcement]:
    subj = subject_in(query)
    day = date_in(query, tz)
    today = local_now(tz).date()
    items = [a for a in await list_for_chats(chat_ids) if a.kind == "hw" and is_visible(a)]
    if subj:
        items = [a for a in items if a.subject == subj or subject_in(a.subject or "") == subj]
    if day:
        exact = [a for a in items if a.for_date == day.isoformat()]
        # если дату не указали в записи — берём выданное за последние дни до нужного дня
        undated = [a for a in items if not a.for_date and to_local(a.created_at, tz).date() < day
                   and (day - to_local(a.created_at, tz).date()).days <= 7]
        items = exact or undated
    else:
        items = [a for a in items if (a.for_date and a.for_date >= today.isoformat())
                 or (not a.for_date and (today - to_local(a.created_at, tz).date()).days <= 7)]
    # по каждому предмету — самое свежее
    latest: dict[tuple[int, str, str | None], Announcement] = {}
    for a in sorted(items, key=lambda x: x.created_at, reverse=True):
        key = (a.chat_id, a.subject, a.for_date if day else None)
        latest.setdefault(key, a)
    return sorted(latest.values(), key=lambda a: (a.for_date or "9999", a.subject))


async def recent_news(chat_ids: list[int], days: int = 7) -> list[Announcement]:
    return [a for a in await list_for_chats(chat_ids, days=days) if a.kind == "news" and is_visible(a)][:5]


async def render_answer(items: list[Announcement], query: str, tz: str | None) -> str:
    subj = subject_in(query)
    day = date_in(query, tz)
    head = "📚 <b>Домашнее задание"
    if subj:
        head += f" · {html.escape(subj)}"
    if day:
        head += f" на {WEEKDAYS[day.weekday()]} {day:%d.%m}"
    head += "</b>"
    lines = [head, ""]
    for a in items[:10]:
        title = html.escape(a.subject or "Без предмета")
        when = f" (на {fmt_day(a.for_date)})" if a.for_date and not day else ""
        lines.append(f"<b>{title}</b>{when}: {html.escape(a.text)}")
    lines.append("")
    lines.append("<i>📌 С доски класса — записано учителем.</i>")
    return "\n".join(lines)


async def render_news(items: list[Announcement], tz: str | None) -> str:
    lines = ["📣 <b>Последние объявления</b>", ""]
    for a in items:
        lines.append(f"• {to_local(a.created_at, tz):%d.%m}: {html.escape(a.text[:300])}")
    return "\n".join(lines)


def parse_for_date(key: str, tz: str | None) -> str | None:
    today = local_now(tz).date()
    if key == "tomorrow":
        d = today + timedelta(days=1)
    elif key == "after":
        d = today + timedelta(days=2)
    elif key == "monday":
        d = today + timedelta(days=(0 - today.weekday()) % 7 or 7)
    elif key and len(key) == 10:
        return key
    else:
        return None
    if key == "tomorrow" and d.weekday() == 6:  # «завтра» в субботу — значит к понедельнику
        d += timedelta(days=1)
    return d.isoformat()


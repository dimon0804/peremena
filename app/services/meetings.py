"""«Запись на встречу»: родительские консультации по слотам.

Учитель задаёт окна («12.11 15:00–17:00 по 15 минут»), формат (очно или звонок) и, если нужно, место.
Бот нарезает окна на слоты и рассылает родителям класса кнопки со свободным временем. Родитель занимает
один слот, может отменить запись или перенести её на другое время. За 2 часа до встречи бот напоминает
родителю (планировщик, только лидер, пачками). Учитель видит расписание в боте и в приложении,
а в день встреч — строку в утренней сводке.

Гонки решает база: слот занимается запросом «UPDATE … WHERE user_id IS NULL» — из двух родителей,
нажавших одновременно, выиграет ровно один; второй слот одному родителю не даёт уникальность
(meeting_id, user_id).
"""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import noload

from .. import keyboards as kb
from ..core import ctx
from ..db import session, utcnow
from ..demo_ids import DEMO_BASE, DEMO_SPAN
from ..models import ClassChat, Meeting, MeetingSlot, User
from ..timeutil import WEEKDAYS, local_now, to_local, to_utc, zone
from . import chats

DAY_START, DAY_END = 8, 21  # как у напоминаний сборов: родителям пишем только днём


def _is_daytime(tz: str) -> bool:
    return DAY_START <= local_now(tz).hour < DAY_END

log = logging.getLogger(__name__)

FORMATS = {"office": "очно", "call": "звонок"}
DEFAULT_MINUTES = 15
MIN_MINUTES, MAX_MINUTES = 5, 120
MAX_SLOTS = 60            # слотов в одной записи: больше родителей в классе не бывает
BUTTONS = 24              # кнопок со свободным временем в одном сообщении
REMIND_BEFORE = timedelta(hours=2)
REMIND_BATCH = 500
TICK_BUDGET = 40          # секунд на проход напоминаний (tick раз в минуту)
KEEP_DAYS = 30            # запись удаляется через 30 дней после последнего слота (app/services/retention.py)

WINDOW_RE = re.compile(
    r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*,?\s*(?:с\s*)?(\d{1,2})[:.](\d{2})\s*(?:-|–|—|до)\s*"
    r"(\d{1,2})[:.](\d{2})(?:\D{0,12}?(\d{1,3})\s*(?:мин|м\b))?",
    re.IGNORECASE,
)


@dataclass
class Window:
    day: date
    start: int      # минуты от полуночи, местное время учителя
    end: int
    minutes: int


# ====================================================================== окна и слоты
def parse_windows(text: str, tz: str | None, default_minutes: int = DEFAULT_MINUTES,
                  today: date | None = None) -> tuple[list[Window], list[str], list[str]]:
    """Текст учителя → (окна, остальные строки, ошибки). Строки без окна — тема и место встречи."""
    today = today or local_now(tz).date()
    windows, rest, errors = [], [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = WINDOW_RE.search(line)
        if not m:
            rest.append(line)
            continue
        d, mo, y, h1, m1, h2, m2, mins = m.groups()
        try:
            year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
            day = date(year, int(mo), int(d))
            if not y and day < today:
                day = date(year + 1, int(mo), int(d))
        except ValueError:
            errors.append(f"не понял дату в строке «{line}»")
            continue
        start, end = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        minutes = int(mins) if mins else default_minutes
        if not (0 <= start < end <= 24 * 60) or int(m1) > 59 or int(m2) > 59:
            errors.append(f"время «{line}»: конец должен быть позже начала")
            continue
        if not MIN_MINUTES <= minutes <= MAX_MINUTES:
            errors.append(f"длительность встречи — от {MIN_MINUTES} до {MAX_MINUTES} минут")
            continue
        if end - start < minutes:
            errors.append(f"в окно «{line}» не помещается ни одна встреча по {minutes} мин")
            continue
        windows.append(Window(day, start, end, minutes))
    return windows, rest, errors


def build_slots(windows: list[Window], tz: str | None, now: datetime | None = None) -> list[tuple[datetime, int]]:
    """Окна → слоты (начало UTC, минуты): только будущие, без повторов, не больше MAX_SLOTS."""
    now = now or utcnow()
    out: dict[datetime, int] = {}
    for w in windows:
        t = w.start
        while t + w.minutes <= w.end:
            local = datetime(w.day.year, w.day.month, w.day.day, tzinfo=zone(tz)) + timedelta(minutes=t)
            at = to_utc(local)
            if at > now:
                out.setdefault(at, w.minutes)
            t += w.minutes
    return sorted(out.items())[:MAX_SLOTS]


def fmt_slot(at: datetime, tz: str | None) -> str:
    d = to_local(at, tz)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m} {d:%H:%M}"


def fmt_format(m: Meeting) -> str:
    what = FORMATS.get(m.format, "очно")
    return f"{what}, {m.place}" if m.place else what


# ====================================================================== создание и рассылка
async def create(teacher_id: int, chat_id: int, title: str, fmt: str, place: str,
                 slots: list[tuple[datetime, int]]) -> Meeting:
    from . import metrics

    await metrics.touch(teacher_id)  # активный день учителя (удержание)
    async with session() as s:
        m = Meeting(teacher_id=teacher_id, chat_id=chat_id, title=(title or "Консультация").strip()[:300],
                    format=fmt if fmt in FORMATS else "office", place=(place or "").strip()[:300])
        s.add(m)
        await s.flush()
        s.add_all(MeetingSlot(meeting_id=m.id, starts_at=at, minutes=mins) for at, mins in slots[:MAX_SLOTS])
        mid = m.id
    return await get(mid)


async def get(meeting_id: int) -> Meeting | None:
    async with session() as s:
        return await s.get(Meeting, meeting_id)


async def _teacher(m: Meeting) -> User | None:
    return await chats.get_user(m.teacher_id)


def free_slots(m: Meeting, now: datetime | None = None) -> list[MeetingSlot]:
    now = now or utcnow()
    return [s for s in m.slots if s.user_id is None and s.starts_at > now]


def slot_of(m: Meeting, user_id: int) -> MeetingSlot | None:
    return next((s for s in m.slots if s.user_id == user_id), None)


def slot_buttons(m: Meeting, tz: str | None) -> list[list[dict]]:
    free = free_slots(m)[:BUTTONS]
    rows, row = [], []
    for s in free:
        row.append(kb.cb(fmt_slot(s.starts_at, tz), f"mt:b:{s.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def parent_text(m: Meeting, tz: str | None, chat_title: str, teacher_name: str) -> str:
    free = free_slots(m)
    lines = [f"🗓 <b>Запись на встречу</b> · {html.escape(chat_title or 'класс')}", "",
             f"<b>{html.escape(m.title)}</b>",
             f"Учитель: {html.escape(teacher_name)}" if teacher_name else "",
             f"Формат: {html.escape(fmt_format(m))}", ""]
    if free:
        lines.append("Выберите удобное время — оно закрепится за вами. Отменить или перенести можно в любой момент.")
        if len(free) > BUTTONS:
            lines.append(f"<i>Показаны ближайшие {BUTTONS} из {len(free)} свободных.</i>")
    else:
        lines.append("Свободного времени не осталось. Если очень нужно — напишите учителю.")
    return "\n".join(x for x in lines if x is not None).replace("\n\n\n", "\n\n")


async def dispatch(meeting_id: int) -> dict:
    """Рассылка родителям класса: каждому в личку кнопки свободных слотов. Повторно не рассылает."""
    from .polls import send_many

    c = ctx()
    m = await get(meeting_id)
    if m is None or m.closed:
        return {"total": 0, "delivered": 0}
    if not await c.kv.add_once(f"mtd:{meeting_id}", ttl=7 * 24 * 3600):
        return {"total": 0, "delivered": 0, "busy": True}
    chat = await chats.get_chat(m.chat_id)
    teacher = await _teacher(m)
    tz = teacher.tz if teacher else c.settings.default_tz
    await chats.sync_members(m.chat_id)
    members = await chats.class_list(m.chat_id, exclude={m.teacher_id})
    text = parent_text(m, tz, chat.title if chat else "", teacher.display_name if teacher else "")
    att = kb.kb_list(slot_buttons(m, tz))
    res = await send_many([(u.user_id, {"user_id": u.user_id, "text": text, "attachments": att}) for u in members])
    return {"total": len(members), "delivered": sum(1 for _, msg in res if msg)}


# ====================================================================== запись родителя
class _Taken(Exception):
    pass


async def book(slot_id: int, user_id: int, name: str = "") -> tuple[str, MeetingSlot | None, Meeting | None]:
    """Родитель занимает слот. Статусы: ok — записан; moved — перенёс запись сюда; same — это уже его время;
    taken — слот занят; closed — запись закрыта; past — время прошло; forbidden — не из этого класса; bad."""
    async with session() as s:
        slot = await s.get(MeetingSlot, slot_id)
        m = await s.get(Meeting, slot.meeting_id) if slot else None
    if slot is None or m is None:
        return "bad", None, None
    if m.closed:
        return "closed", slot, m
    now = utcnow()
    if slot.starts_at <= now:
        return "past", slot, m
    if user_id == m.teacher_id or not await chats.is_present(m.chat_id, user_id):
        return "forbidden", slot, m
    if slot.user_id == user_id:
        return "same", slot, m
    name = name or await chats.member_name(m.chat_id, user_id)
    status = "ok"
    try:
        async with session() as s:
            # прежняя запись этого родителя освобождается в той же транзакции: не удалось занять новый слот —
            # откат вернёт старое время, и родитель не останется ни с чем
            freed = await s.execute(update(MeetingSlot).where(
                MeetingSlot.meeting_id == m.id, MeetingSlot.user_id == user_id,
            ).values(user_id=None, name="", booked_at=None, reminded=False))
            if freed.rowcount:
                status = "moved"
            res = await s.execute(update(MeetingSlot).where(
                MeetingSlot.id == slot_id, MeetingSlot.user_id.is_(None),
            ).values(user_id=user_id, name=name[:300], booked_at=now,
                     reminded=slot.starts_at - now <= REMIND_BEFORE))  # записался впритык — напоминать незачем
            if res.rowcount != 1:
                raise _Taken()
    except (_Taken, IntegrityError):
        return "taken", slot, await get(m.id)
    m = await get(m.id)
    return status, next((x for x in m.slots if x.id == slot_id), slot), m


@dataclass
class Freed:
    """Освобождённая запись: когда была встреча и чья."""
    slot_id: int
    starts_at: datetime
    name: str


async def cancel(meeting_id: int, user_id: int) -> Freed | None:
    """Отмена записи родителем. Возвращает, какое время освободилось, или None, если записи не было."""
    async with session() as s:
        slot = await s.scalar(select(MeetingSlot).where(
            MeetingSlot.meeting_id == meeting_id, MeetingSlot.user_id == user_id))
        if slot is None:
            return None
        was = Freed(slot.id, slot.starts_at, slot.name)
        res = await s.execute(update(MeetingSlot).where(MeetingSlot.id == slot.id, MeetingSlot.user_id == user_id)
                              .values(user_id=None, name="", booked_at=None, reminded=False))
        if not res.rowcount:
            return None
    m = await get(meeting_id)
    now = utcnow()
    if m and now < was.starts_at <= now + timedelta(hours=24):
        # отмена в последние сутки — учителю стоит знать сразу (вне рабочего времени — в утренней сводке)
        from .quiet import notify_teacher

        teacher = await _teacher(m)
        when = fmt_slot(was.starts_at, teacher.tz if teacher else None)
        await notify_teacher(
            m.teacher_id, f"🗓 {html.escape(was.name or 'Родитель')} отменил(а) встречу {when} — время снова свободно.",
            kind="message", chat_id=m.chat_id, from_user_id=user_id, from_name=was.name,
            raw_text=f"Отменена встреча {when}")
    return was


async def close(meeting_id: int, teacher_id: int) -> bool:
    async with session() as s:
        res = await s.execute(update(Meeting).where(Meeting.id == meeting_id, Meeting.teacher_id == teacher_id)
                              .values(closed=True))
        return bool(res.rowcount)


# ====================================================================== списки и расписание
async def for_teacher(teacher_id: int, with_closed: bool = False, limit: int = 20) -> list[Meeting]:
    q = select(Meeting).where(Meeting.teacher_id == teacher_id)
    if not with_closed:
        q = q.where(Meeting.closed.is_(False))
    async with session() as s:
        rows = list((await s.scalars(q.order_by(Meeting.created_at.desc()).limit(limit))).all())
    now = utcnow()
    # прошедшие (все слоты позади) — в конец списка
    return sorted(rows, key=lambda m: (not any(x.starts_at > now for x in m.slots), -m.id))


async def for_parent(user_id: int) -> list[Meeting]:
    """Открытые записи в классах родителя, где ещё есть будущие слоты (или его собственная запись)."""
    chat_ids = [c.chat_id for c in await chats.member_chats(user_id)]
    if not chat_ids:
        return []
    async with session() as s:
        rows = (await s.scalars(select(Meeting).where(
            Meeting.chat_id.in_(chat_ids), Meeting.closed.is_(False), Meeting.teacher_id != user_id,
        ).order_by(Meeting.created_at.desc()).limit(20))).all()
    now = utcnow()
    return [m for m in rows if any(x.starts_at > now for x in m.slots)]


def counts(m: Meeting) -> tuple[int, int]:
    """(занято, всего) среди будущих слотов."""
    now = utcnow()
    future = [x for x in m.slots if x.starts_at > now]
    return sum(1 for x in future if x.user_id), len(future)


def schedule_text(m: Meeting, tz: str | None, chat_title: str = "") -> str:
    busy, total = counts(m)
    lines = [f"🗓 <b>{html.escape(m.title)}</b>" + (f" · {html.escape(chat_title)}" if chat_title else ""),
             f"Формат: {html.escape(fmt_format(m))}. Записались: {busy} из {total}"
             + (" (запись закрыта)" if m.closed else ""), ""]
    day = None
    for x in m.slots:
        d = to_local(x.starts_at, tz)
        if d.date() != day:
            day = d.date()
            lines.append(f"<b>{WEEKDAYS[d.weekday()]} {d:%d.%m}</b>")
        who = html.escape(x.name) if x.user_id else "<i>свободно</i>"
        lines.append(f"{d:%H:%M} — {who}")
    return "\n".join(lines).strip()


async def today_lines(teacher: User) -> list[str]:
    """Для утренней сводки: встречи учителя сегодня (местный день)."""
    now = local_now(teacher.tz)
    lo = to_utc(now.replace(hour=0, minute=0, second=0, microsecond=0))
    hi = lo + timedelta(days=1)
    async with session() as s:
        rows = (await s.execute(
            select(MeetingSlot.starts_at, MeetingSlot.name).join(Meeting, Meeting.id == MeetingSlot.meeting_id)
            .where(Meeting.teacher_id == teacher.user_id, Meeting.closed.is_(False),
                   MeetingSlot.user_id.is_not(None), MeetingSlot.starts_at >= lo, MeetingSlot.starts_at < hi)
            .order_by(MeetingSlot.starts_at))).all()
    return [f"{to_local(at, teacher.tz):%H:%M} — {html.escape(name or 'родитель')}" for at, name in rows]


# ====================================================================== напоминание за 2 часа
def reminder_text(m: Meeting, slot: MeetingSlot, tz: str | None, teacher_name: str) -> str:
    return (f"⏰ Напоминаю: сегодня в <b>{to_local(slot.starts_at, tz):%H:%M}</b> встреча с учителем"
            + (f" ({html.escape(teacher_name)})" if teacher_name else "")
            + f" — «{html.escape(m.title)}», {html.escape(fmt_format(m))}.\n\nНе получается — отмените или перенесите запись, "
              "чтобы время досталось другим.")


def parent_buttons(m: Meeting) -> list[list[dict]]:
    return [[kb.cb("🔁 Перенести", f"mt:mv:{m.id}"), kb.cb("✖️ Отменить", f"mt:c:{m.id}")],
            [kb.cb("📆 В календарь", "m:cal")]]


async def remind_tick() -> int:
    """Раз в минуту (только лидер): родителям, чья встреча через 2 часа или раньше, — одно напоминание.
    Выборка пачками по курсору id, демо-песочницы пропускаются; при сбое MAX повтор не чаще раза в 15 минут."""
    started = time.monotonic()
    now = utcnow()
    cond = and_(MeetingSlot.reminded.is_(False), MeetingSlot.user_id.is_not(None),
                MeetingSlot.starts_at > now, MeetingSlot.starts_at <= now + REMIND_BEFORE,
                ~MeetingSlot.user_id.between(DEMO_BASE, DEMO_BASE + DEMO_SPAN))
    sent, after = 0, 0
    c = ctx()
    tz_cache: dict[int, tuple[str, str]] = {}
    while True:
        async with session() as s:
            rows = (await s.scalars(select(MeetingSlot).options(noload(MeetingSlot.meeting))
                                    .where(cond, MeetingSlot.id > after).order_by(MeetingSlot.id)
                                    .limit(REMIND_BATCH))).all()
        for slot in rows:
            if time.monotonic() - started > TICK_BUDGET:
                log.warning("Напоминания о встречах не уложились в %s с — продолжим через минуту", TICK_BUDGET)
                return sent
            try:
                m = await get(slot.meeting_id)
                if m is None or m.closed:
                    continue
                if m.teacher_id not in tz_cache:
                    t = await _teacher(m)
                    tz_cache[m.teacher_id] = (t.tz if t else c.settings.default_tz, t.display_name if t else "")
                tz, tname = tz_cache[m.teacher_id]
                # ночью родителям не пишем: на встречу в 9:00 напомним в 8:00, а не в 7:00
                if not _is_daytime(tz):
                    continue
                if not await c.kv.add_once(f"mtr:{slot.id}", ttl=15 * 60):
                    continue
                if await c.safe_send(user_id=slot.user_id, text=reminder_text(m, slot, tz, tname),
                                     attachments=kb.kb_list(parent_buttons(m))):
                    sent += 1
                    async with session() as s:
                        await s.execute(update(MeetingSlot).where(
                            MeetingSlot.id == slot.id, MeetingSlot.user_id == slot.user_id).values(reminded=True))
            except Exception:
                log.exception("Напоминание о встрече, слот %s", slot.id)
        if len(rows) < REMIND_BATCH:
            return sent
        after = rows[-1].id


# ====================================================================== сроки хранения и «удалить мои данные»
async def cleanup(now: datetime | None = None) -> int:
    """Записи, у которых последний слот прошёл больше KEEP_DAYS назад, удаляются вместе со слотами."""
    edge = (now or utcnow()) - timedelta(days=KEEP_DAYS)
    async with session() as s:
        latest = dict((await s.execute(select(MeetingSlot.meeting_id, func.max(MeetingSlot.starts_at))
                                       .group_by(MeetingSlot.meeting_id))).all())
        empty = (await s.scalars(select(Meeting.id).where(Meeting.created_at < edge))).all()
        old = {mid for mid, at in latest.items() if at < edge} | {mid for mid in empty if mid not in latest}
        if not old:
            return 0
        await s.execute(delete(MeetingSlot).where(MeetingSlot.meeting_id.in_(old)))
        await s.execute(delete(Meeting).where(Meeting.id.in_(old)))
        return len(old)


async def forget(user_id: int) -> int:
    """Будущие записи родителя отменяются, прошедшие обезличиваются."""
    from .retention import FORGOTTEN_NAME

    now = utcnow()
    async with session() as s:
        rows = (await s.scalars(select(MeetingSlot).where(MeetingSlot.user_id == user_id))).all()
        for x in rows:
            if x.starts_at > now:
                x.user_id, x.name, x.booked_at, x.reminded = None, "", None, False
            else:
                x.user_id, x.name = -x.id, FORGOTTEN_NAME  # отрицательный id строки ни с кем не совпадёт
        return len(rows)


async def chat_title(chat_id: int) -> str:
    async with session() as s:
        c = await s.get(ClassChat, chat_id)
        return c.title if c else ""

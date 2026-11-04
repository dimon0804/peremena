"""«Тихий режим» и «Утро»: граница рабочего дня учителя.

Всё, что бот хочет передать учителю (вопрос родителя, «нужно уточнить», «это не то»,
итог сбора), проходит через ``notify_teacher``. В рабочее окно — сразу,
вне его — копится во «входящих» и приходит утром одной карточкой.
Срочное (по слову-маркеру) проходит всегда.
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update

from .. import keyboards as kb
from ..core import ctx
from ..db import session
from ..demo_ids import DEMO_BASE, DEMO_SPAN
from ..models import ClassChat, InboxItem, Poll, User
from ..timeutil import fmt_dt, fmt_minutes, local_now, to_local, to_utc, utcnow
from . import stats

log = logging.getLogger(__name__)

KIND_LABEL = {
    "question": "Вопрос родителя",
    "clarify": "Нужно уточнить",
    "not_that": "Ответ не подошёл",
    "chat_question": "Вопрос в чате",
    "report": "Итог сбора",
    "faq_hint": "Частый вопрос без ответа",
    "absence": "Отсутствие",
    "message": "Сообщение",
}


def in_work_window(user: User) -> bool:
    if not user.quiet_enabled:
        return True
    now = local_now(user.tz)
    if user.weekends_off and now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return user.work_start <= minutes < user.work_end


# «в понедельник», «во вторник» — когда учитель увидит вопрос
WEEKDAYS_ACC = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")


def next_work_start(user: User, now: datetime | None = None) -> datetime:
    """Ближайшее начало рабочего окна учителя (местное время) — с учётом выходных."""
    now = now or local_now(user.tz)
    for d in range(8):
        day = (now + timedelta(days=d)).date()
        if user.weekends_off and day.weekday() >= 5:
            continue
        start = datetime(day.year, day.month, day.day, tzinfo=now.tzinfo) + timedelta(minutes=user.work_start)
        if start > now:
            return start
    return now


def when_back_text(user: User, now: datetime | None = None) -> str:
    """«завтра с 8:00», «в понедельник с 8:00» — честно, когда учитель увидит вопрос (сценарий «Вечер пятницы»)."""
    now = now or local_now(user.tz)
    start = next_work_start(user, now)
    at = fmt_minutes(user.work_start)
    days = (start.date() - now.date()).days
    if days <= 0:
        return f"сегодня с {at}"
    if days == 1:
        return f"завтра с {at}"
    return f"{WEEKDAYS_ACC[start.weekday()]} с {at}"


def is_urgent(user: User, text: str) -> bool:
    t = (text or "").lower()
    return any(w.strip() and w.strip().lower() in t for w in (user.urgent_words or "").split(","))


async def notify_teacher(
    teacher_id: int,
    text: str,
    *,
    kind: str = "message",
    chat_id: int | None = None,
    from_user_id: int | None = None,
    from_name: str = "",
    raw_text: str = "",
    buttons: list[list[dict]] | None = None,
    urgent: bool = False,
    ref: str | None = None,
) -> str:
    """Доставка учителю. Возвращает:
    * "sent"   — сообщение ушло сразу;
    * "night"  — вне рабочего времени, лежит во «входящих» до утренней сводки;
    * "queued" — доставить не удалось (учитель не запускал бота или заблокировал его),
                 сообщение сохранено во «входящих» и придёт в сводке, когда учитель откроет бота.
    Ничего не теряется: всё, что не ушло сразу, сохраняется."""
    async with session() as s:
        teacher = await s.get(User, teacher_id)
    if kind in ("question", "chat_question") and not urgent:
        # вопрос, который бот не закрыл сам, — для метрики пилота «закрыто ботом без учителя»
        await stats.add(teacher_id, "question_forwarded")
    status = "queued"
    if teacher is not None:
        if urgent or in_work_window(teacher) or is_urgent(teacher, raw_text):
            if await ctx().safe_send(user_id=teacher_id, text=text, attachments=kb.kb_list(buttons or [])):
                return "sent"
        else:
            status = "night"
    async with session() as s:
        s.add(InboxItem(
            teacher_id=teacher_id, chat_id=chat_id, from_user_id=from_user_id, from_name=from_name,
            kind=kind, text=raw_text or text, ref=ref,
        ))
    return status


async def has_pending(teacher_id: int) -> bool:
    async with session() as s:
        return bool(await s.scalar(select(InboxItem.id).where(
            InboxItem.teacher_id == teacher_id, InboxItem.delivered.is_(False)).limit(1)))


DIGEST_ITEMS = 15     # сколько пунктов «пока вы отдыхали» показываем в одной сводке
DIGEST_REPLIES = 8    # кнопок «Ответить» под сводкой


async def build_digest(teacher: User, include_empty: bool = False) -> tuple[str, list[list[dict]], list[int]] | None:
    """(текст, кнопки, id показанных пунктов «входящих»). Доставленными потом отмечаются только показанные:
    остальные и пришедшие после сборки останутся до следующей сводки."""
    c = ctx()
    async with session() as s:
        items = (await s.scalars(
            select(InboxItem).where(InboxItem.teacher_id == teacher.user_id, InboxItem.delivered.is_(False))
            .order_by(InboxItem.created_at)
        )).all()
        polls = (await s.scalars(
            select(Poll).where(Poll.teacher_id == teacher.user_id, Poll.closed.is_(False)).order_by(Poll.deadline)
        )).all()
        since = digest_since(teacher)
        closed_recent = (await s.scalars(
            select(Poll).where(
                Poll.teacher_id == teacher.user_id, Poll.closed.is_(True),
                Poll.closed_at >= min(since, utcnow() - timedelta(hours=18)),
            )
        )).all()

    now_local = local_now(teacher.tz)
    # сводка приходит и не утром — когда учитель сам открывает бота; «Доброе утро» в полночь звучит странно
    if 5 <= now_local.hour < 12:
        head = f"☀️ <b>Доброе утро!</b> Сводка на {now_local:%d.%m}"
    else:
        head = f"📋 <b>Сводка</b> на {now_local:%d.%m}, {now_local:%H:%M}"
    lines = [head, ""]
    buttons: list[list[dict]] = []

    hot = [p for p in polls if p.deadline and to_local(p.deadline, teacher.tz).date() <= now_local.date()]
    if polls:
        lines.append("📋 <b>Сборы</b>")
        for p in polls[:8]:
            answered = sum(1 for t in p.targets if t.answer is not None)
            total = len(p.targets)
            missing = [t.name for t in p.targets if t.answer is None]
            fire = "🔥 " if p in hot else ""
            lines.append(f"{fire}{html.escape(p.title)} — {answered}/{total}, срок {fmt_dt(p.deadline, teacher.tz)}")
            if missing and p in hot:
                lines.append("   Не ответили: " + html.escape(", ".join(missing[:10])) + (" …" if len(missing) > 10 else ""))
        buttons.append([kb.cb("📋 Статус сборов", "st:list")])
        lines.append("")
    if closed_recent:
        lines.append("✅ <b>Закрылись</b>")
        for p in closed_recent[:5]:
            answered = sum(1 for t in p.targets if t.answer is not None)
            lines.append(f"{html.escape(p.title)} — ответили {answered}/{len(p.targets)}")
        lines.append("")

    # «4 закрыты автоматически»: сколько вопросов бот закрыл сам, пока учителя не было
    auto_n = await stats.count_since(teacher.user_id, "faq_answer", since)
    if auto_n:
        lines.append(f"🤖 Пока вас не было, бот сам ответил на вопросы: <b>{auto_n}</b>.")
        lines.append("")

    shown = list(items[:DIGEST_ITEMS])
    if items:
        lines.append(f"🌙 <b>Пока вы отдыхали</b> — {len(items)}")
        replies: list[list[dict]] = []
        for it in shown:
            when = to_local(it.created_at, teacher.tz)
            who = f"{html.escape(it.from_name)}: " if it.from_name else ""
            label = KIND_LABEL.get(it.kind, "Сообщение")
            body = html.escape(it.text[:200])
            if it.from_user_id:
                who = f'<a href="max://user/{it.from_user_id}">{html.escape(it.from_name or "родитель")}</a>: '
            lines.append(f"• {when:%H:%M} · {label} · {who}{body}")
            if it.ref and it.kind == "question" and len(replies) < DIGEST_REPLIES:
                # вопрос, переданный родителем ночью, можно ответить прямо отсюда — ответ уйдёт ему в личку
                replies.append([kb.cb(f"✍️ Ответить: {it.from_name or 'родитель'}"[:60], f"fr:{it.ref}")])
        if len(items) > len(shown):
            lines.append(f"…и ещё {len(items) - len(shown)} — пришлю следующей сводкой: /morning")
        lines.append("")
        buttons += replies

    # кто сегодня не придёт — список, который раньше учитель собирал из сообщений в чате
    from . import attendance, faq_learning

    absent_text, absent_n = await attendance.render_day(teacher, with_title=False)
    if absent_n:
        lines.append(f"🤒 <b>Сегодня не придут ({absent_n})</b>")
        lines.append(absent_text)
        lines.append("")

    weekly = False
    if now_local.weekday() == 0:
        week_absences = await attendance.week_summary(teacher)
        if week_absences:
            weekly = True
            lines += [week_absences, ""]
        # частые вопросы без ответа — предложение пополнить справочник
        hint = await faq_learning.digest_hint(teacher.user_id)
        if hint:
            weekly = True
            lines += [hint, ""]
            buttons.append([kb.cb("📖 Ответить на частые вопросы", "fq:open")])
        # понедельник — итоги прошлой недели из «Счётчика пользы»
        week = await stats.summary(teacher.user_id, days=7)
        if week["minutes"] >= 1:
            weekly = True
            lines.append(f"⏱ <b>Итоги недели:</b> бот сэкономил вам {stats.fmt_minutes(week['minutes'])}.")
            for kind, label in stats.LABELS.items():
                v = week["by_kind"].get(kind)
                if v:
                    lines.append(f"   {label}: {v['count']}")
            lines.append("")

    if not items and not polls and not closed_recent and not weekly and not absent_n and not auto_n \
            and not include_empty:
        return None
    if not items and not polls and not closed_recent:
        lines.append("Ничего срочного. Хорошего дня!")
    if c.bot_username:
        buttons.append([kb.app("Открыть «Перемену»", c.bot_username, "home")])
    return "\n".join(lines).strip(), buttons, [it.id for it in shown]


def digest_since(teacher: User, now: datetime | None = None):
    """«Пока вас не было» — с конца последнего рабочего дня учителя (UTC): в понедельник это вечер пятницы,
    во вторник — вечер понедельника. Не дальше 96 часов назад."""
    now = now or local_now(teacher.tz)
    end = now - timedelta(hours=18)
    for d in range(8):
        day = (now - timedelta(days=d)).date()
        if teacher.weekends_off and day.weekday() >= 5:
            continue
        cand = datetime(day.year, day.month, day.day, tzinfo=now.tzinfo) + timedelta(minutes=teacher.work_end)
        if cand <= now:
            end = cand
            break
    return to_utc(max(end, now - timedelta(hours=96)))


async def deliver_digest(teacher: User, include_empty: bool = False) -> str:
    """Собирает и отправляет сводку: "sent" — ушла, "empty" — слать нечего, "failed" — не доставлена."""
    built = await build_digest(teacher, include_empty)
    if not built:
        return "empty"
    text, buttons, shown_ids = built
    sent = await ctx().safe_send(user_id=teacher.user_id, text=text, attachments=kb.kb_list(buttons))
    if not sent:
        return "failed"
    if shown_ids:
        async with session() as s:
            await s.execute(update(InboxItem).where(InboxItem.id.in_(shown_ids)).values(delivered=True))
        await stats.add(teacher.user_id, "quiet_held", len(shown_ids))
    return "sent"


async def send_digest(teacher: User, include_empty: bool = False) -> bool:
    return await deliver_digest(teacher, include_empty) == "sent"


MORNING_WINDOW = 120   # минут после начала рабочего дня, в которые сводка ещё уместна
MORNING_BATCH = 500    # учителей за один запрос
_TZ_TTL = 300.0
_tz_cache: tuple[int, float, list[str]] | None = None


async def _teacher_zones() -> list[str]:
    """Часовые пояса классных руководителей. Их единицы, а учителей — тысячи: окно «утро» считаем
    один раз на пояс, а не для каждого учителя. Список обновляется раз в 5 минут."""
    global _tz_cache
    from .. import db

    now, eid = time.monotonic(), id(db.engine())
    if _tz_cache and _tz_cache[0] == eid and now - _tz_cache[1] < _TZ_TTL:
        return _tz_cache[2]
    async with session() as s:
        zones = [z for z in (await s.scalars(
            select(User.tz).where(User.user_id.in_(_active_teacher_ids())).distinct()
        )).all() if z]
    _tz_cache = (eid, now, zones)
    return zones


def _active_teacher_ids():
    return select(ClassChat.teacher_id).where(ClassChat.active.is_(True), ClassChat.teacher_id.is_not(None))


async def due_for_digest(batch: int = MORNING_BATCH):
    """Учителя, у которых прямо сейчас утреннее окно и сводки сегодня ещё не было.
    Окно и «сегодня» считаются на часовой пояс, отбор — в базе, пачками по курсору user_id."""
    for zone in await _teacher_zones():
        now = local_now(zone)
        today = now.strftime("%Y-%m-%d")
        minutes = now.hour * 60 + now.minute
        cond = [
            User.tz == zone,
            User.user_id.in_(_active_teacher_ids()),
            ~User.user_id.between(DEMO_BASE, DEMO_BASE + DEMO_SPAN),  # демо-учителям сводка не нужна
            User.work_start <= minutes, User.work_start > minutes - MORNING_WINDOW,
            or_(User.last_digest_on.is_(None), User.last_digest_on != today),
        ]
        if now.weekday() >= 5:
            cond.append(User.weekends_off.is_(False))
        after = None
        while True:
            q = select(User).where(*cond)
            if after is not None:
                q = q.where(User.user_id > after)
            async with session() as s:
                rows = (await s.scalars(q.order_by(User.user_id).limit(batch))).all()
            for u in rows:
                yield u
            if len(rows) < batch:
                break
            after = rows[-1].user_id


async def morning_tick() -> None:
    """Раз в минуту: кому из учителей пора получить утреннюю сводку.
    Сводку получают все классные руководители — и с выключенным тихим режимом: в ней ещё
    «кто сегодня не придёт», сборы со сроком сегодня и итоги недели."""
    async for t in due_for_digest():
        now = local_now(t.tz)
        today = now.strftime("%Y-%m-%d")
        # не доставилась (сбой MAX) — повторим, но не чаще раза в 20 минут
        if not await ctx().kv.add_once(f"dg:{t.user_id}:{today}", ttl=20 * 60):
            continue
        try:
            status = await deliver_digest(t)
        except Exception:  # одна ошибка не должна сорвать сводки остальным
            log.exception("Утренняя сводка для %s", t.user_id)
            continue
        if status == "failed":
            continue  # день не отмечаем: иначе сводки не было бы до завтра
        async with session() as s:
            await s.execute(update(User).where(User.user_id == t.user_id).values(last_digest_on=today))

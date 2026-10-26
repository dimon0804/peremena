"""«Тихий режим» и «Утро»: граница рабочего дня учителя.

Всё, что бот хочет передать учителю (вопрос родителя, «нужно уточнить», «это не то»,
итог сбора), проходит через ``notify_teacher``. В рабочее окно — сразу,
вне его — копится во «входящих» и приходит утром одной карточкой.
Срочное (по слову-маркеру) проходит всегда.
"""
from __future__ import annotations

import html
import logging
from datetime import timedelta

from sqlalchemy import select

from .. import keyboards as kb
from ..core import ctx
from ..db import session
from ..models import ClassChat, InboxItem, Poll, User
from ..timeutil import fmt_dt, local_now, to_local, utcnow
from . import stats

log = logging.getLogger(__name__)

KIND_LABEL = {
    "question": "Вопрос родителя",
    "clarify": "Нужно уточнить",
    "not_that": "Ответ не подошёл",
    "chat_question": "Вопрос в чате",
    "report": "Итог сбора",
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
) -> str:
    """Доставка учителю. Возвращает:
    * "sent"   — сообщение ушло сразу;
    * "night"  — вне рабочего времени, лежит во «входящих» до утренней сводки;
    * "queued" — доставить не удалось (учитель не запускал бота или заблокировал его),
                 сообщение сохранено во «входящих» и придёт в сводке, когда учитель откроет бота.
    Ничего не теряется: всё, что не ушло сразу, сохраняется."""
    async with session() as s:
        teacher = await s.get(User, teacher_id)
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
            kind=kind, text=raw_text or text,
        ))
    return status


async def has_pending(teacher_id: int) -> bool:
    async with session() as s:
        return bool(await s.scalar(select(InboxItem.id).where(
            InboxItem.teacher_id == teacher_id, InboxItem.delivered.is_(False)).limit(1)))


async def build_digest(teacher: User, include_empty: bool = False) -> tuple[str, list[list[dict]]] | None:
    c = ctx()
    async with session() as s:
        items = (await s.scalars(
            select(InboxItem).where(InboxItem.teacher_id == teacher.user_id, InboxItem.delivered.is_(False))
            .order_by(InboxItem.created_at)
        )).all()
        polls = (await s.scalars(
            select(Poll).where(Poll.teacher_id == teacher.user_id, Poll.closed.is_(False)).order_by(Poll.deadline)
        )).all()
        closed_recent = (await s.scalars(
            select(Poll).where(
                Poll.teacher_id == teacher.user_id, Poll.closed.is_(True),
                Poll.closed_at >= utcnow() - timedelta(hours=18),
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

    if items:
        lines.append(f"🌙 <b>Пока вы отдыхали</b> — {len(items)}")
        for it in items[:15]:
            when = to_local(it.created_at, teacher.tz)
            who = f"{html.escape(it.from_name)}: " if it.from_name else ""
            label = KIND_LABEL.get(it.kind, "Сообщение")
            body = html.escape(it.text[:200])
            if it.from_user_id:
                who = f'<a href="max://user/{it.from_user_id}">{html.escape(it.from_name or "родитель")}</a>: '
            lines.append(f"• {when:%H:%M} · {label} · {who}{body}")
        if len(items) > 15:
            lines.append(f"…и ещё {len(items) - 15}")
        lines.append("")

    weekly = False
    if now_local.weekday() == 0:
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

    if not items and not polls and not closed_recent and not weekly and not include_empty:
        return None
    if not items and not polls and not closed_recent:
        lines.append("Ничего срочного. Хорошего дня!")
    if c.bot_username:
        buttons.append([kb.app("Открыть «Перемену»", c.bot_username, "home")])
    return "\n".join(lines).strip(), buttons


async def send_digest(teacher: User, include_empty: bool = False) -> bool:
    built = await build_digest(teacher, include_empty)
    if not built:
        return False
    text, buttons = built
    sent = await ctx().safe_send(user_id=teacher.user_id, text=text, attachments=kb.kb_list(buttons))
    if sent:
        async with session() as s:
            items = (await s.scalars(
                select(InboxItem).where(InboxItem.teacher_id == teacher.user_id, InboxItem.delivered.is_(False))
            )).all()
            for it in items:
                it.delivered = True
        await stats.add(teacher.user_id, "quiet_held", len(items))
    return bool(sent)


async def morning_tick() -> None:
    """Раз в минуту: кому из учителей пора получить утреннюю сводку."""
    async with session() as s:
        # только классные руководители: родителей в базе в десятки раз больше, перебирать их каждую минуту незачем
        teacher_ids = select(ClassChat.teacher_id).where(ClassChat.active.is_(True), ClassChat.teacher_id.is_not(None))
        teachers = (await s.scalars(
            select(User).where(User.quiet_enabled.is_(True), User.user_id.in_(teacher_ids))
        )).all()
    for t in teachers:
        now = local_now(t.tz)
        today = now.strftime("%Y-%m-%d")
        if t.last_digest_on == today:
            continue
        if t.weekends_off and now.weekday() >= 5:
            continue
        minutes = now.hour * 60 + now.minute
        if not (t.work_start <= minutes < t.work_start + 120):
            continue
        from .chats import is_teacher

        if not await is_teacher(t.user_id):
            continue
        try:
            await send_digest(t)
        except Exception:  # одна ошибка не должна сорвать сводки остальным
            log.exception("Утренняя сводка для %s", t.user_id)
        async with session() as s:
            u = await s.get(User, t.user_id)
            if u:
                u.last_digest_on = today

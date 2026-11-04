"""Сроки хранения и «удалить мои данные».

Бот держит переписку родителей ровно столько, сколько она нужна для дела:
* «входящие» учителя, уже показанные в сводке, — 7 дней (недоставленные — 30);
* вопросы без ответа для подсказок справочнику — 30 дней (дальше они и так не учитываются);
* отметки «не придёт» — 14 дней (хватает на сводку за прошлую неделю);
* уроки расписания с конкретной датой из выгрузки журнала — 60 дней после этой даты.
* замеры времени ответа родителю (без текста и без того, кто спросил) — 180 дней;
* активные дни учителя (одна строка в день, для удержания) — 400 дней;
* запись на встречу — 30 дней после последнего слота.
Закрытые сборы хранятся — это история класса, но в них только имя и факт ответа.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select, update

from ..core import ctx
from ..db import session, utcnow
from . import meetings, metrics
from ..models import (Absence, InboxItem, Lesson, Member, OpenQuestion, Poll, PollTarget, QuestionTiming,
                      TeacherActivity, User)

log = logging.getLogger(__name__)

INBOX_DELIVERED_DAYS = 7
INBOX_PENDING_DAYS = 30
OPEN_QUESTION_DAYS = 30
ABSENCE_DAYS = 14
LESSON_DAYS = 60
FORGOTTEN_NAME = "Родитель (данные удалены)"


async def cleanup() -> dict:
    """Раз в сутки: удаляет то, что пережило свой срок. Возвращает, сколько записей удалено."""
    now = utcnow()
    out = {}
    async with session() as s:
        out["inbox"] = (await s.execute(delete(InboxItem).where(
            ((InboxItem.delivered.is_(True)) & (InboxItem.created_at < now - timedelta(days=INBOX_DELIVERED_DAYS)))
            | (InboxItem.created_at < now - timedelta(days=INBOX_PENDING_DAYS))))).rowcount
        out["open_questions"] = (await s.execute(delete(OpenQuestion).where(
            OpenQuestion.created_at < now - timedelta(days=OPEN_QUESTION_DAYS)))).rowcount
        out["absences"] = (await s.execute(delete(Absence).where(
            Absence.created_at < now - timedelta(days=ABSENCE_DAYS)))).rowcount
        out["lessons"] = (await s.execute(delete(Lesson).where(
            Lesson.day.is_not(None), Lesson.day < (now - timedelta(days=LESSON_DAYS)).date().isoformat()))).rowcount
        out["timings"] = (await s.execute(delete(QuestionTiming).where(
            QuestionTiming.asked_at < now - timedelta(days=metrics.TIMING_DAYS)))).rowcount
        out["activity"] = (await s.execute(delete(TeacherActivity).where(
            TeacherActivity.day < (now - timedelta(days=metrics.ACTIVITY_DAYS)).date().isoformat()))).rowcount
    out["meetings"] = await meetings.cleanup(now)
    if any(out.values()):
        log.info("Очистка по срокам хранения: %s", out)
    return out


async def forget(user_id: int) -> dict:
    """«Удалить мои данные»: вопросы, отметки отсутствия, имя ребёнка и сообщения во «входящих» учителя
    удаляются; ответы на закрытых сборах обезличиваются (учитель видит, что ответ был, но не чей).
    На открытых сборах ответ остаётся до закрытия — он нужен учителю; после закрытия повторите команду."""
    out = {}
    async with session() as s:
        out["questions"] = (await s.execute(delete(OpenQuestion).where(OpenQuestion.user_id == user_id))).rowcount
        out["inbox"] = (await s.execute(delete(InboxItem).where(InboxItem.from_user_id == user_id))).rowcount
        out["absences"] = (await s.execute(delete(Absence).where(Absence.user_id == user_id))).rowcount
        await s.execute(delete(TeacherActivity).where(TeacherActivity.user_id == user_id))
        await s.execute(update(Member).where(Member.user_id == user_id).values(child_name=None))
        closed = (await s.scalars(select(PollTarget).join(Poll, Poll.id == PollTarget.poll_id).where(
            PollTarget.user_id == user_id, Poll.closed.is_(True)))).all()
        for t in closed:
            # user_id уникален в пределах сбора — отрицательный id строки не совпадёт ни с кем из живых
            t.user_id, t.name, t.message_id = -t.id, FORGOTTEN_NAME, None
        out["answers"] = len(closed)
        u = await s.get(User, user_id)
        if u:
            u.first_name, u.last_name, u.username = "", None, None
    out["meetings"] = await meetings.forget(user_id)
    await ctx().kv.delete(f"q:{user_id}")
    return out

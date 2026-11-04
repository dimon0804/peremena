"""«Счётчик пользы»: сколько работы бот сделал вместо учителя.

Минуты — из прозрачной модели расчёта проекта (слайд «Польза»; это расчёт, а не опрос):
ответ на повторяющийся вопрос ≈ 2 мин, сбор с ручной сверкой ≈ 25 мин,
поиск материала ≈ 4 мин, вечернее сообщение, отложенное до утра ≈ 3 мин.

Плюс метрики пилота из анкеты проекта (``pilot``): доля ответивших до срока (цель ≥ 90 %)
и доля вопросов, закрытых ботом без учителя (цель ≥ 60 %).
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from ..db import session, utcnow
from ..models import Metric, Poll, PollTarget

MINUTES = {
    "faq_answer": 2.0,
    "poll_done": 25.0,
    "reminder": 0.5,      # за одно персональное напоминание
    "material_found": 4.0,
    "quiet_held": 3.0,
    "absence": 1.0,       # отметка отсутствия вместо сообщения в чат и ручного учёта
    # служебная отметка без минут: вопрос ушёл учителю — для доли «закрыто ботом без учителя»
    "question_forwarded": 0.0,
}

PILOT_ON_TIME_GOAL = 90   # % участников, ответивших до срока
PILOT_SELF_GOAL = 60      # % вопросов, закрытых ботом без учителя

LABELS = {
    "faq_answer": "Ответов на типовые вопросы",
    "poll_done": "Сборов без ручного подсчёта",
    "reminder": "Напоминаний вместо учителя",
    "material_found": "Найдено материалов",
    "quiet_held": "Сообщений перенесено на утро",
    "absence": "Отсутствий отмечено родителями",
}


async def add(teacher_id: int | None, kind: str, count: int = 1) -> None:
    if not teacher_id or count <= 0:
        return
    async with session() as s:
        s.add_all(Metric(teacher_id=teacher_id, kind=kind, minutes=MINUTES.get(kind, 0)) for _ in range(count))


async def summary(teacher_id: int, days: int | None = None) -> dict:
    q = select(Metric.kind, func.count(Metric.id), func.coalesce(func.sum(Metric.minutes), 0)).where(
        Metric.teacher_id == teacher_id
    )
    if days:
        q = q.where(Metric.created_at >= utcnow() - timedelta(days=days))
    q = q.group_by(Metric.kind)
    async with session() as s:
        rows = (await s.execute(q)).all()
    by_kind = {k: {"count": int(c), "minutes": float(m)} for k, c, m in rows}
    total = sum(v["minutes"] for v in by_kind.values())
    return {"by_kind": by_kind, "minutes": round(total)}


async def count_since(teacher_id: int, kind: str, since) -> int:
    """Сколько раз с момента since (UTC) случилось событие kind — для утренней сводки."""
    async with session() as s:
        return int(await s.scalar(select(func.count(Metric.id)).where(
            Metric.teacher_id == teacher_id, Metric.kind == kind, Metric.created_at >= since)) or 0)


async def pilot(teacher_id: int) -> dict:
    """Метрики пилота по данным самого бота — без опросов и ручного подсчёта."""
    async with session() as s:
        closed = (await s.execute(
            select(Poll.id, Poll.deadline, Poll.closed_at).where(Poll.teacher_id == teacher_id, Poll.closed.is_(True))
        )).all()
        on_time = total = 0
        if closed:
            limit = {pid: dl or cl for pid, dl, cl in closed}  # без срока — «до закрытия сбора»
            rows = (await s.execute(select(PollTarget.poll_id, PollTarget.answered_at)
                                    .where(PollTarget.poll_id.in_(list(limit))))).all()
            total = len(rows)
            on_time = sum(1 for pid, at in rows if at is not None and (limit[pid] is None or at <= limit[pid]))
        kinds = dict((await s.execute(
            select(Metric.kind, func.count(Metric.id)).where(
                Metric.teacher_id == teacher_id, Metric.kind.in_(("faq_answer", "question_forwarded")))
            .group_by(Metric.kind))).all())
    by_bot = int(kinds.get("faq_answer", 0))
    forwarded = int(kinds.get("question_forwarded", 0))
    asked = by_bot + forwarded
    return {
        "polls_closed": len(closed),
        "on_time_pct": round(100 * on_time / total) if total else None, "on_time_goal": PILOT_ON_TIME_GOAL,
        "self_pct": round(100 * by_bot / asked) if asked else None, "self_goal": PILOT_SELF_GOAL,
        "answered_by_bot": by_bot, "forwarded": forwarded,
    }


def fmt_minutes(total: float) -> str:
    total = int(round(total))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


async def render(teacher_id: int) -> str:
    week = await summary(teacher_id, days=7)
    total = await summary(teacher_id)
    lines = ["📊 <b>Счётчик пользы</b>", ""]
    lines.append(f"За 7 дней бот сэкономил вам <b>{fmt_minutes(week['minutes'])}</b>.")
    lines.append(f"За всё время — <b>{fmt_minutes(total['minutes'])}</b>.")
    lines.append("")
    for kind, label in LABELS.items():
        v = total["by_kind"].get(kind)
        if v:
            lines.append(f"• {label}: {v['count']} (≈ {fmt_minutes(v['minutes'])})")
    if not any(k in LABELS for k in total["by_kind"]):
        lines.append("Пока пусто — запустите первый сбор командой /poll.")
    p = await pilot(teacher_id)
    if p["polls_closed"] or p["self_pct"] is not None:
        lines += ["", "📈 <b>Метрики пилота</b>"]
        if p["polls_closed"]:
            lines.append(f"• Сборов закрыто без ручного подсчёта: {p['polls_closed']} (100 %)")
        if p["on_time_pct"] is not None:
            lines.append(f"• Ответили до срока: {p['on_time_pct']} % (цель — не меньше {p['on_time_goal']} %)")
        if p["self_pct"] is not None:
            lines.append(f"• Вопросов бот закрыл сам, без вас: {p['self_pct']} % "
                         f"({p['answered_by_bot']} из {p['answered_by_bot'] + p['forwarded']}; "
                         f"цель — не меньше {p['self_goal']} %)")
    return "\n".join(lines)

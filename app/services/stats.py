"""«Счётчик пользы»: сколько работы бот сделал вместо учителя.

Минуты — из модели расчёта проекта (опрос 25 педагогов):
ответ на повторяющийся вопрос ≈ 2 мин, сбор с ручной сверкой ≈ 25 мин,
поиск материала ≈ 4 мин, вечернее сообщение, отложенное до утра ≈ 3 мин.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from ..db import session, utcnow
from ..models import Metric

MINUTES = {
    "faq_answer": 2.0,
    "poll_done": 25.0,
    "reminder": 0.5,      # за одно персональное напоминание
    "material_found": 4.0,
    "quiet_held": 3.0,
    "absence": 1.0,       # отметка отсутствия вместо сообщения в чат и ручного учёта
}

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
    if not total["by_kind"]:
        lines.append("Пока пусто — запустите первый сбор командой /poll.")
    return "\n".join(lines)

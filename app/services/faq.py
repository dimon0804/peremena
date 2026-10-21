"""«Справочник класса» — бот отвечает тем, что сохранил учитель, и ничего не сочиняет.

Под каждым ответом — подпись «сохранено учителем такого-то числа» и кнопка «Это не то».
Бот никогда не выдаёт себя за учителя.
"""
from __future__ import annotations

import html

from sqlalchemy import or_, select

from ..db import session, utcnow
from ..models import ClassChat, FaqEntry, User
from ..timeutil import fmt_date
from . import search

# Пороги уверенности
ANSWER_SCORE = 0.55   # отвечаем сами в личке
SUGGEST_SCORE = 0.30  # предлагаем варианты «возможно, вы имели в виду»
GROUP_SCORE = 0.70    # отвечаем в общем чате (только если учитель включил автоответ)


async def add_entry(teacher_id: int, question: str, answer: str, keywords: str | None = None,
                    chat_id: int | None = None) -> FaqEntry:
    async with session() as s:
        e = FaqEntry(
            teacher_id=teacher_id, chat_id=chat_id, question=question.strip()[:1000],
            answer=answer.strip()[:3500], keywords=(keywords if keywords is not None else search.auto_keywords(question)),
        )
        s.add(e)
        await s.flush()
        return e


async def update_entry(entry_id: int, teacher_id: int, **fields) -> FaqEntry | None:
    async with session() as s:
        e = await s.get(FaqEntry, entry_id)
        if e is None or e.teacher_id != teacher_id:
            return None
        for k in ("question", "answer", "keywords", "chat_id"):
            if k in fields and fields[k] is not None:
                setattr(e, k, fields[k] if k != "chat_id" else (fields[k] or None))
        e.updated_at = utcnow()
        return e


async def delete_entry(entry_id: int, teacher_id: int) -> bool:
    async with session() as s:
        e = await s.get(FaqEntry, entry_id)
        if e is None or e.teacher_id != teacher_id:
            return False
        await s.delete(e)
        return True


async def list_entries(teacher_id: int) -> list[FaqEntry]:
    async with session() as s:
        return list((await s.scalars(
            select(FaqEntry).where(FaqEntry.teacher_id == teacher_id).order_by(FaqEntry.updated_at.desc())
        )).all())


async def get_entry(entry_id: int) -> FaqEntry | None:
    async with session() as s:
        return await s.get(FaqEntry, entry_id)


async def candidates_for_chats(chat_ids: list[int]) -> list[FaqEntry]:
    """Карточки, видимые в данных чатах: общие карточки учителя этих чатов + карточки конкретного чата."""
    if not chat_ids:
        return []
    async with session() as s:
        teacher_ids = [t for t in (await s.scalars(
            select(ClassChat.teacher_id).where(ClassChat.chat_id.in_(chat_ids))
        )).all() if t]
        if not teacher_ids:
            return []
        return list((await s.scalars(
            select(FaqEntry).where(
                FaqEntry.teacher_id.in_(teacher_ids),
                or_(FaqEntry.chat_id.is_(None), FaqEntry.chat_id.in_(chat_ids)),
            )
        )).all())


def rank(query: str, entries: list[FaqEntry], limit: int = 3) -> list[search.Hit[FaqEntry]]:
    return search.rank(query, entries, lambda e: e.keywords, lambda e: e.question, limit=limit,
                       min_score=SUGGEST_SCORE)


async def mark_hit(entry_id: int) -> None:
    async with session() as s:
        e = await s.get(FaqEntry, entry_id)
        if e:
            e.hits += 1


async def mark_miss(entry_id: int) -> FaqEntry | None:
    async with session() as s:
        e = await s.get(FaqEntry, entry_id)
        if e:
            e.misses += 1
        return e


async def render_answer(e: FaqEntry) -> str:
    async with session() as s:
        teacher = await s.get(User, e.teacher_id)
    who = teacher.display_name if teacher else "учителем"
    tz = teacher.tz if teacher else None
    return (
        f"{html.escape(e.answer)}\n\n"
        f"<i>📖 Из справочника класса · сохранено учителем ({html.escape(who)}) {fmt_date(e.updated_at, tz)}</i>"
    )


async def render_list(teacher_id: int) -> tuple[str, list[FaqEntry]]:
    entries = await list_entries(teacher_id)
    if not entries:
        return ("📖 <b>Справочник пуст</b>\n\nДобавьте первую карточку: вопрос, который вам задают чаще всего, "
                "и ваш ответ. Бот будет отвечать им родителям в любое время."), []
    lines = [f"📖 <b>Что я отвечаю от вашего имени</b> — {len(entries)} карточек", ""]
    for i, e in enumerate(entries[:30], 1):
        lines.append(f"<b>{i}. {html.escape(e.question[:80])}</b>")
        lines.append(f"→ {html.escape(e.answer[:150])}" + ("…" if len(e.answer) > 150 else ""))
        stat = f"ответов: {e.hits}" + (f", «не то»: {e.misses}" if e.misses else "")
        lines.append(f"<i>{stat}</i>")
        lines.append("")
    if len(entries) > 30:
        lines.append(f"…и ещё {len(entries) - 30}. Полный список — в приложении.")
    return "\n".join(lines).strip(), entries

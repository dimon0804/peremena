"""Справочник, который подсказывает сам — без ИИ.

Каждый вопрос родителя, на который в справочнике не нашлось ответа, запоминается (только текст вопроса).
Похожие вопросы группируются по основам слов (стемминг + коэффициент Жаккара). Когда одну и ту же тему
спросили уже несколько раз, бот предлагает учителю сохранить ответ одной кнопкой — справочник растёт
сам собой, и доля вопросов, которые бот закрывает без учителя, увеличивается от недели к неделе.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select, update

from ..core import ctx
from ..db import session, utcnow
from ..models import ClassChat, OpenQuestion
from . import search

NUDGE_AT = 3          # столько похожих вопросов без ответа — повод подсказать учителю сразу
WINDOW_DAYS = 30      # старые вопросы не учитываем
SIMILAR = 0.5         # порог похожести по Жаккару


@dataclass
class Cluster:
    question: str                      # самый короткий из похожих — обычно самый ясный
    ids: list[int] = field(default_factory=list)
    stems: set[str] = field(default_factory=set)
    users: set[int] = field(default_factory=set)
    members: list[set[str]] = field(default_factory=list)  # основы слов каждого вопроса группы

    def accepts(self, st: set[str]) -> bool:
        # сравниваем с каждым вопросом, а не с объединением слов группы: иначе случайные слова
        # («брать», «завтра») размывают сходство и одна тема распадается на несколько
        return any(_similar(st, m) for m in self.members)

    @property
    def count(self) -> int:
        return len(self.ids)


def _stems(text: str) -> set[str]:
    return set(search.content_stems(text))


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    # похожи по Жаккару или короткий вопрос целиком входит в длинный («форма физкультура» ⊂ «какая форма на физкультуру завтра»)
    return inter / len(a | b) >= SIMILAR or (inter >= 2 and inter == min(len(a), len(b)))


async def record(chat_ids: list[int], user_id: int | None, text: str) -> list[Cluster]:
    """Запоминает вопрос без ответа для учителей этих чатов. Возвращает кластеры, которые
    именно сейчас дошли до порога подсказки (чтобы сразу сказать учителю)."""
    stems = _stems(text)
    if len(stems) < 2 or len(text) > 500:
        return []  # «срочно», «?» и длинные простыни — не вопросы для справочника
    async with session() as s:
        teacher_ids = {t for t in (await s.scalars(
            select(ClassChat.teacher_id).where(ClassChat.chat_id.in_(chat_ids))
        )).all() if t} if chat_ids else set()
        for tid in teacher_ids:
            s.add(OpenQuestion(teacher_id=tid, chat_id=chat_ids[0] if len(chat_ids) == 1 else None,
                               user_id=user_id, text=text.strip()[:500], stems=" ".join(sorted(stems))))
    reached = []
    for tid in teacher_ids:
        for c in await clusters(tid, min_count=NUDGE_AT):
            if stems & c.stems and c.count == NUDGE_AT:
                reached.append((tid, c))
    for tid, c in reached:
        await _nudge(tid, c)
    return [c for _, c in reached]


async def clusters(teacher_id: int, min_count: int = 2) -> list[Cluster]:
    async with session() as s:
        rows = (await s.scalars(
            select(OpenQuestion).where(
                OpenQuestion.teacher_id == teacher_id, OpenQuestion.resolved.is_(False),
                OpenQuestion.created_at >= utcnow() - timedelta(days=WINDOW_DAYS),
            ).order_by(OpenQuestion.created_at)
        )).all()
    found: list[Cluster] = []
    for q in rows:
        st = set(q.stems.split())
        target = next((c for c in found if c.accepts(st)), None)
        if target is None:
            target = Cluster(question=q.text)
            found.append(target)
        target.ids.append(q.id)
        target.stems |= st
        target.members.append(st)
        if q.user_id:
            target.users.add(q.user_id)
        if len(q.text) < len(target.question):
            target.question = q.text
    result = [c for c in found if c.count >= min_count]
    result.sort(key=lambda c: -c.count)
    return result


async def resolve(ids: list[int]) -> None:
    if ids:
        async with session() as s:
            await s.execute(update(OpenQuestion).where(OpenQuestion.id.in_(ids)).values(resolved=True))


async def resolve_matching(teacher_id: int, question: str, keywords: str = "") -> int:
    """Учитель сохранил карточку — снимаем вопросы, на которые она теперь отвечает."""
    async with session() as s:
        rows = (await s.scalars(select(OpenQuestion).where(
            OpenQuestion.teacher_id == teacher_id, OpenQuestion.resolved.is_(False)))).all()
    from .faq import ANSWER_SCORE

    ids = [q.id for q in rows if search.score_text(q.text, keywords, question) >= ANSWER_SCORE]
    await resolve(ids)
    return len(ids)


async def _nudge(teacher_id: int, c: Cluster) -> None:
    """Подсказка учителю, когда одну тему спросили уже NUDGE_AT раз (не чаще раза в неделю на тему)."""
    from .. import keyboards as kb
    from .quiet import notify_teacher

    key = "oqn:" + "-".join(sorted(c.stems))[:200]
    if not await ctx().kv.add_once(key, ttl=7 * 24 * 3600):
        return
    await ctx().kv.set(f"oqc:{c.ids[0]}", {"q": c.question, "ids": c.ids}, 14 * 24 * 3600)
    await notify_teacher(
        teacher_id,
        f"📖 Про «{html.escape(c.question)}» спросили уже {c.count} раза, а в справочнике ответа нет.\n\n"
        "Сохраните ответ один раз — дальше я буду отвечать на такие вопросы сам.",
        kind="faq_hint", raw_text=f"Частый вопрос без ответа: «{c.question}» ({c.count}×)",
        buttons=[[kb.cb("💾 Добавить ответ", f"fq:oq:{c.ids[0]}")]],
    )


async def digest_hint(teacher_id: int) -> str:
    top = (await clusters(teacher_id, min_count=2))[:3]
    if not top:
        return ""
    lines = ["📖 <b>Чаще всего спрашивали без ответа:</b>"]
    lines += [f"• {html.escape(c.question)} — {c.count}×" for c in top]
    return "\n".join(lines)


async def remember_cluster(c: Cluster) -> str:
    """Кладёт кластер в KV под коротким ключом для кнопки. Возвращает ключ."""
    key = str(c.ids[0])
    await ctx().kv.set(f"oqc:{key}", {"q": c.question, "ids": c.ids}, 14 * 24 * 3600)
    return key

"""Ответ на вопрос в личке — одна логика для бота и мини-приложения (/api/ask).

Порядок: «ребёнок заболел» → отметка отсутствия; «что задали?» и «какие объявления?» → доска класса;
дальше справочник (уверенно — отвечаем, неуверенно — предлагаем варианты), полка, и если ничего нет —
вопрос запоминается для подсказок учителю и его можно передать учителю.

Один человек бывает сразу учителем и родителем (ребёнок учится в другом классе): тогда он видит
и свой справочник «глазами родителей», и ответы по классу ребёнка.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from ..core import ctx
from ..models import FaqEntry
from . import attendance, board, chats, faq, faq_learning, school, shelf, stats

ASK_TTL = 3600 * 6  # сколько помним вопрос для кнопки «Передать учителю»


@dataclass
class Answer:
    # board — по доске; board_missing — на доске нет; news; faq — уверенный ответ справочника;
    # suggest — «возможно, вы имели в виду»; shelf — материалы; absence — похоже на отсутствие;
    # onboard — человек ещё ни в одном классе; schedule — расписание из журнала; none — ничего не нашлось
    kind: str
    text: str = ""                      # готовый текст ответа (HTML: только <b>, <i>)
    entry: FaqEntry | None = None       # kind=faq
    hits: list = field(default_factory=list)       # карточки справочника: search.Hit[FaqEntry]
    materials: list = field(default_factory=list)  # материалы полки: search.Hit[Material]
    as_teacher: bool = False            # ответ «как увидят родители» для самого учителя
    can_forward: bool = False           # можно передать вопрос учителю (вопрос запомнен)
    chat_ids: list[int] = field(default_factory=list)  # классы спрашивающего-родителя


@dataclass
class Who:
    parent_chats: list[int]   # классы, где человек — родитель/ученик (не свой класс)
    staff_chats: list[int]    # классы, где он учитель или предметник
    is_staff: bool
    is_class_teacher: bool


async def who(uid: int) -> Who:
    r = await school.roles(uid)
    own = {c.chat_id for c in r.class_chats}
    parent = [c.chat_id for c in await chats.member_chats(uid) if c.chat_id not in own]
    staff = [c.chat_id for c, _ in r.board_chats()]
    return Who(parent_chats=parent, staff_chats=staff, is_staff=r.is_staff, is_class_teacher=r.is_class_teacher)


async def _remember(uid: int, text: str, chat_ids: list[int]) -> None:
    await ctx().kv.set(f"q:{uid}", {"text": text, "chats": chat_ids}, ASK_TTL)


async def _board(uid: int, text: str, chat_ids: list[int], tz: str | None, is_staff: bool,
                 parent_chats: list[int]) -> Answer | None:
    from ..integrations.journals import schedule, service as journal

    # «какие уроки завтра?», «что взять в среду?» — по расписанию из электронного журнала
    if schedule.is_schedule_question(text) and (schedule.is_bring_question(text)
                                                or not board.is_homework_question(text)):
        if await journal.has_schedule(chat_ids):
            titles = await board._chat_titles(set(chat_ids)) if len(chat_ids) > 1 else {}
            reply, found = await schedule.answer(text, chat_ids, tz, titles)
            if found:
                return Answer("schedule", reply, chat_ids=parent_chats, as_teacher=is_staff and not parent_chats)
            if not board.is_homework_question(text):
                return Answer("schedule", reply + (" Загрузить его из журнала — /journal" if is_staff and not
                                                   parent_chats else ""), chat_ids=parent_chats)
        elif is_staff and not parent_chats and not board.is_homework_question(text):
            return Answer("schedule", "🗓 Расписания пока нет. Его можно загрузить из выгрузки электронного журнала — "
                                      "/journal", as_teacher=True)
    if board.is_homework_question(text):
        items = await board.find_homework(text, chat_ids, tz)
        subj = board.subject_in(text)
        day = board.date_in(text, tz)
        prefix, query = "", text
        if not items and day:
            # на запрошенный день ничего нет — покажем ближайшее, иначе родитель решит, что не задано вовсе
            items = await board.find_homework(f"задали {subj or ''}", chat_ids, tz)
            if items:
                prefix = (f"На {board.WEEKDAYS[day.weekday()]} {day:%d.%m} на доске ничего не записано. "
                          f"Ближайшее:\n\n")
                query = f"задали {subj or ''}"
        if items:
            teacher_ids = {c.teacher_id for c in [await chats.get_chat(a.chat_id) for a in items] if c}
            for t in teacher_ids:
                if t != uid:
                    await stats.add(t, "faq_answer")
            return Answer("board", prefix + await board.render_answer(items, query, tz), chat_ids=parent_chats)
        what = "📚 На доске класса пока нет задания" + (f" по предмету «{html.escape(subj)}»" if subj else "")
        if is_staff and not parent_chats:
            return Answer("board_missing", what + ". Записать — /hw", as_teacher=True)
        await _remember(uid, text, parent_chats)
        return Answer("board_missing", what + ". Если задание давали устно — могу передать вопрос учителю.",
                      can_forward=bool(parent_chats), chat_ids=parent_chats)
    if board.is_news_question(text):
        items = await board.recent_news(chat_ids)
        if items:
            return Answer("news", await board.render_news(items, tz), chat_ids=parent_chats)
    return None


async def answer(uid: int, text: str, tz: str | None = None, *, learn: bool = True) -> Answer:
    """Ищет ответ и делает побочные действия бота: счётчики «ответил сам», запоминание вопроса
    для «Передать учителю» и для подсказок учителю. learn=False — без записи вопроса в «частые»."""
    if tz is None:
        user = await chats.get_user(uid)
        tz = user.tz if user else ctx().settings.default_tz
    w = await who(uid)
    parent_chats = w.parent_chats
    board_chats = list(dict.fromkeys(parent_chats + w.staff_chats))
    as_teacher = w.is_staff and not parent_chats
    if not board_chats:
        return Answer("onboard")

    # «Заболел, сегодня не придём» — предлагаем отметить отсутствие кнопкой
    if parent_chats and attendance.looks_like_absence(text):
        return Answer("absence", chat_ids=parent_chats)

    found = await _board(uid, text, board_chats, tz, w.is_staff, parent_chats)
    if found:
        return found

    # справочник: своё (как увидят родители) + справочник классов ребёнка
    entries: list[FaqEntry] = []
    if w.is_class_teacher:
        entries += await faq.list_entries(uid, with_school=True)
    if parent_chats:
        seen = {e.id for e in entries}
        entries += [e for e in await faq.candidates_for_chats(parent_chats) if e.id not in seen]
    hits = await faq.rank_smart(text, entries)  # при EMBEDDINGS=on — ещё и по смыслу
    if hits and hits[0].score >= faq.ANSWER_SCORE:
        e = hits[0].item
        own = e.teacher_id == uid
        if not own:
            await faq.mark_hit(e.id)
            await stats.add(e.teacher_id, "faq_answer")
            await _remember(uid, text, parent_chats)
        return Answer("faq", await faq.render_answer(e, parent_chats), entry=e, hits=hits, as_teacher=own,
                      chat_ids=parent_chats)

    mats = await shelf.find(uid, text, teacher_view=w.is_staff, limit=3)
    if hits:
        if not as_teacher:
            await _remember(uid, text, parent_chats)
        return Answer("suggest", "🤔 Возможно, вы имели в виду:", hits=hits, materials=mats, as_teacher=as_teacher,
                      can_forward=not as_teacher, chat_ids=parent_chats)
    if mats:
        if not as_teacher:
            await _remember(uid, text, parent_chats)
        return Answer("shelf", "🗂 Нашёл на полке:", materials=mats, as_teacher=as_teacher,
                      can_forward=not as_teacher, chat_ids=parent_chats)
    if as_teacher:
        return Answer("none", "В справочнике и на полке такого нет. Добавить ответ — /add, найти файл — /shelf.",
                      as_teacher=True)
    await _remember(uid, text, parent_chats)
    if learn:
        # запоминаем вопрос без ответа: когда тему спросят несколько человек, бот подскажет учителю сохранить ответ
        await faq_learning.record(parent_chats, uid, text)
    return Answer("none", "В справочнике класса пока нет готового ответа. Передать вопрос учителю?",
                  can_forward=True, chat_ids=parent_chats)


def plain(text: str) -> str:
    """Текст ответа без разметки — для клиентов, которые не показывают HTML."""
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))

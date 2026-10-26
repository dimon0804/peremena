"""Демо-режим для жюри и знакомства с продуктом: «попробовать без регистрации».

Каждый посетитель получает свою песочницу — вымышленную школу с двумя классами, родителями,
сборами (в том числе денег), отсутствиями, справочником, доской и полкой. Все id песочницы лежат
в особом диапазоне (app/demo_ids.py): клиент MAX API по нему ничего никуда не отправляет.
Песочницы удаляются через сутки.
"""
from __future__ import annotations

import random
import secrets
from datetime import timedelta

from sqlalchemy import delete, or_, select

from ..db import session, utcnow
from ..demo_ids import DEMO_BASE, DEMO_SPAN, SANDBOX_SIZE, is_demo, sandbox_base, sandbox_id_of
from ..models import (
    Absence, Announcement, Campaign, ClassChat, DemoSandbox, FaqEntry, InboxItem, Material, Member, Metric,
    OpenQuestion, Poll, PollTarget, School, SchoolMember, SubjectTeacher, User, WebSession,
)
from ..timeutil import local_now, to_utc
from . import search
from .stats import MINUTES

TZ = "Europe/Moscow"
TTL_HOURS = 24

PARENTS_9B = [
    ("Анна", "Иванова", "Иванов Пётр"), ("Мария", "Петрова", "Петрова Вера"), ("Олег", "Сидоров", "Сидоров Илья"),
    ("Ирина", "Козлова", "Козлова Ульяна"), ("Дмитрий", "Морозов", "Морозов Артём"), ("Елена", "Волкова", "Волков Глеб"),
    ("Павел", "Соколов", "Соколова Ника"), ("Наталья", "Лебедева", "Лебедев Марк"), ("Сергей", "Новиков", "Новикова Ева"),
    ("Татьяна", "Фёдорова", "Фёдоров Лев"), ("Кирилл", "Михайлов", "Михайлова Софья"), ("Вера", "Егорова", "Егоров Тимур"),
    ("Андрей", "Павлов", "Павлова Алиса"), ("Юлия", "Семёнова", "Семёнов Матвей"), ("Роман", "Голубев", "Голубева Мила"),
    ("Дарья", "Виноградова", "Виноградов Ян"), ("Игорь", "Богданов", "Богданова Кира"), ("Светлана", "Воробьёва", "Воробьёв Савва"),
    ("Максим", "Фролов", "Фролова Злата"), ("Ольга", "Зайцева", "Зайцев Мирон"), ("Антон", "Киселёв", "Киселёва Ася"),
    ("Людмила", "Макарова", "Макаров Фёдор"), ("Георгий", "Орлов", "Орлова Таисия"), ("Алина", "Андреева", "Андреев Данил"),
]
PARENTS_9A = [("Ксения", "Белова"), ("Виктор", "Комаров"), ("Полина", "Захарова"), ("Степан", "Титов"),
              ("Нина", "Медведева"), ("Глеб", "Ершов")]

OPEN_QUESTIONS = [
    "Во сколько заканчиваются уроки в пятницу?", "в пятницу во сколько уроки заканчиваются?",
    "Подскажите, когда в пятницу заканчиваются уроки", "Во сколько забирать детей в пятницу после уроков?",
    "Нужна ли сменная обувь на экскурсию?", "сменку на экскурсию брать?", "Сменная обувь на экскурсию нужна?",
]


def _evening(days: int, hour: int = 18):
    """Срок «через N дней в 18:00» по Москве — как поставил бы учитель, а не «в 00:19»."""
    day = local_now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return to_utc(day)


def _name(first: str, last: str) -> str:
    return f"{last} {first[0]}."


async def create_sandbox() -> DemoSandbox:
    async with session() as s:
        sb = DemoSandbox(teacher_id=0, parent_id=0)
        s.add(sb)
        await s.flush()
        sid = sb.id
    base = sandbox_base(sid)
    teacher, teacher2 = base + 1, base + 2
    chat, chat2 = -(base + 1), -(base + 2)
    parents = [base + 11 + i for i in range(len(PARENTS_9B))]
    parents2 = [base + 51 + i for i in range(len(PARENTS_9A))]
    now = utcnow()
    today = local_now(TZ).date()
    rnd = random.Random(sid)

    async with session() as s:
        s.add_all([
            User(user_id=teacher, first_name="Мария", last_name="Петрова", tz=TZ, dialog_open=True),
            User(user_id=teacher2, first_name="Игорь", last_name="Смирнов", tz=TZ, dialog_open=True),
            *[User(user_id=uid, first_name=f, last_name=l, tz=TZ, dialog_open=True)
              for uid, (f, l, _) in zip(parents, PARENTS_9B)],
            *[User(user_id=uid, first_name=f, last_name=l, tz=TZ) for uid, (f, l) in zip(parents2, PARENTS_9A)],
            ClassChat(chat_id=chat, title="9Б · Родители", teacher_id=teacher, bot_is_admin=True, autoanswer=True),
            ClassChat(chat_id=chat2, title="9А · Родители", teacher_id=teacher2, bot_is_admin=True),
            *[Member(chat_id=chat, user_id=uid, name=_name(f, l), child_name=child)
              for uid, (f, l, child) in zip(parents, PARENTS_9B)],
            *[Member(chat_id=chat2, user_id=uid, name=_name(f, l)) for uid, (f, l) in zip(parents2, PARENTS_9A)],
        ])
        school = School(name="Демо-школа № 1", code="D" + secrets.token_hex(3).upper(),
                        admin_code="DA" + secrets.token_hex(4).upper(), created_by=teacher)
        s.add(school)
        await s.flush()
        s.add_all([SchoolMember(school_id=school.id, user_id=teacher, role="admin"),
                   SchoolMember(school_id=school.id, user_id=teacher2, role="teacher")])

        # --- сборы
        excursion = Poll(teacher_id=teacher, chat_id=chat, template="consent",
                         title="Экскурсия в Музей космонавтики, 7 ноября",
                         text="Экскурсия в Музей космонавтики, 7 ноября\nВыезд в 9:00 от школы, возвращение к 15:00.",
                         options=["Согласен", "Не согласен", "Нужно уточнить"], clarify_option=2,
                         deadline=_evening(3), created_at=now - timedelta(days=1))
        gift = Poll(teacher_id=teacher, chat_id=chat, template="money", title="Подарок ко Дню учителя",
                    text="Подарок ко Дню учителя\nСобираем на общий подарок от класса.", amount=200,
                    options=["Перевёл(а)", "Сдам наличными", "Не участвуем", "Нужно уточнить"], clarify_option=3,
                    deadline=_evening(2), created_at=now - timedelta(days=2))
        rules = Poll(teacher_id=teacher, chat_id=chat, template="ack", title="Правила поведения на каникулах",
                     text="Правила поведения на каникулах", options=["Ознакомлен(а)", "Есть вопрос"],
                     clarify_option=1, closed=True, closed_at=now - timedelta(days=4),
                     created_at=now - timedelta(days=7))
        campaign = Campaign(school_id=school.id, author_id=teacher, title="Анкета по школьному питанию")
        s.add_all([excursion, gift, rules, campaign])
        await s.flush()
        food = [Poll(teacher_id=t, chat_id=c, template="consent", title="Анкета по школьному питанию",
                     text="Анкета по школьному питанию\nСогласны ли вы на горячее питание со следующей четверти?",
                     options=["Согласен", "Не согласен", "Нужно уточнить"], clarify_option=2,
                     deadline=_evening(5), created_at=now - timedelta(hours=20), campaign_id=campaign.id)
                for t, c in ((teacher, chat), (teacher2, chat2))]
        s.add_all(food)
        await s.flush()

        exc_answers = [0] * 16 + [1, 1, 2] + [None] * 5
        gift_answers = [0] * 14 + [1] * 5 + [2] + [None] * 4
        gift_paid = set(range(10)) | {14, 15}
        for i, (uid, (f, l, _)) in enumerate(zip(parents, PARENTS_9B)):
            name = _name(f, l)
            for poll, ans, paid in ((excursion, exc_answers[i], False), (gift, gift_answers[i], i in gift_paid),
                                    (rules, 0 if i != 7 else 1, False), (food[0], 0 if i % 3 else None, False)):
                s.add(PollTarget(poll_id=poll.id, user_id=uid, name=name, delivered=True, answer=ans,
                                 answered_at=now - timedelta(hours=rnd.randint(1, 40)) if ans is not None else None,
                                 paid=paid, paid_at=now if paid else None))
        for i, (uid, (f, l)) in enumerate(zip(parents2, PARENTS_9A)):
            s.add(PollTarget(poll_id=food[1].id, user_id=uid, name=_name(f, l), delivered=True,
                             answer=0 if i < 2 else None, answered_at=now if i < 2 else None))

        # --- кто сегодня не придёт
        d = today.isoformat()
        s.add_all([Absence(chat_id=chat, user_id=parents[2], child_name="Сидоров Илья", day=d, reason="sick"),
                   Absence(chat_id=chat, user_id=parents[9], child_name="Фёдоров Лев", day=d, reason="family",
                           comment="уезжаем к бабушке"),
                   Absence(chat_id=chat, user_id=parents[16], child_name="Богданова Кира", day=d, reason="sick")])

        # --- справочник, вопросы без ответа, доска, полка
        s.add_all([
            FaqEntry(teacher_id=teacher, question="Во сколько родительское собрание?", keywords="собрание, родительское",
                     answer="В четверг в 18:30, кабинет 214.", hits=17),
            FaqEntry(teacher_id=teacher, question="Какая форма на физкультуру?", keywords="форма, физкультура",
                     answer="Белая футболка, тёмные шорты, кроссовки на светлой подошве.", hits=11, misses=1),
            FaqEntry(teacher_id=teacher, school_id=school.id, question="Расписание звонков", keywords="звонки",
                     answer="1 урок 8:30–9:15, 2 урок 9:25–10:10, 3 урок 10:30–11:15, 4 урок 11:35–12:20.", hits=23),
            FaqEntry(teacher_id=teacher, school_id=school.id, question="Где взять справку 086/у?",
                     keywords="справка, 086", answer="В медкабинете школы, каб. 105, с 9:00 до 13:00.", hits=6),
        ])
        for i, q in enumerate(OPEN_QUESTIONS):
            s.add(OpenQuestion(teacher_id=teacher, chat_id=chat, user_id=parents[i], text=q,
                               stems=" ".join(sorted(set(search.content_stems(q)))),
                               created_at=now - timedelta(hours=10 * (i + 1))))
        s.add_all([
            Announcement(chat_id=chat, author_id=teacher, kind="hw", subject="Алгебра",
                         text="№245, 247; выучить правило на с. 63", published=True,
                         for_date=(today + timedelta(days=1)).isoformat()),
            Announcement(chat_id=chat, author_id=teacher, kind="hw", subject="Английский язык",
                         text="Unit 5, ex. 3, 4 (письменно)", publish_mode="silent", published=True,
                         for_date=(today + timedelta(days=1)).isoformat()),
            Announcement(chat_id=chat, author_id=teacher, kind="news", text="В субботу субботник, форма свободная.",
                         publish_mode="later", publish_at=_evening(1, 8)),
        ])
        s.add(Material(teacher_id=teacher, chat_id=chat, title="Памятка по пожарной безопасности",
                       filename="Памятка_ПБ.pdf", tags="безопасность", attach_type="file",
                       attach_payload={"token": "demo"}, file_key=f"demo-{sid}", source="chat"))
        s.add_all([InboxItem(teacher_id=teacher, chat_id=chat, from_user_id=parents[4], from_name=_name("Дмитрий", "Морозов"),
                             kind="question", text="Можно забрать ребёнка в пятницу после 3 урока?",
                             created_at=now - timedelta(hours=9))])
        for kind, n in (("faq_answer", 34), ("poll_done", 3), ("reminder", 18), ("material_found", 6),
                        ("quiet_held", 11), ("absence", 7)):
            s.add_all(Metric(teacher_id=teacher, kind=kind, minutes=MINUTES[kind],
                             created_at=now - timedelta(days=rnd.randint(0, 6))) for _ in range(n))

        row = await s.get(DemoSandbox, sid)
        row.teacher_id, row.parent_id = teacher, parents[21]  # у этого «родителя» неотвеченные сборы
    async with session() as s:
        return await s.get(DemoSandbox, sid)


async def sandbox_for(user_id: int) -> DemoSandbox | None:
    if not is_demo(user_id):
        return None
    async with session() as s:
        return await s.get(DemoSandbox, sandbox_id_of(user_id))


def _ranges(sid: int):
    base = sandbox_base(sid)
    return (base, base + SANDBOX_SIZE - 1), (-(base + SANDBOX_SIZE - 1), -base)


async def delete_sandbox(sid: int) -> None:
    (u_lo, u_hi), (c_lo, c_hi) = _ranges(sid)
    async with session() as s:
        users = lambda col: col.between(u_lo, u_hi)  # noqa: E731
        chats = lambda col: col.between(c_lo, c_hi)  # noqa: E731
        poll_ids = select(Poll.id).where(chats(Poll.chat_id))
        school_ids = select(School.id).where(users(School.created_by))
        await s.execute(delete(PollTarget).where(or_(PollTarget.poll_id.in_(poll_ids), users(PollTarget.user_id))))
        for model, cond in (
            (Poll, chats(Poll.chat_id)), (Absence, chats(Absence.chat_id)), (Announcement, chats(Announcement.chat_id)),
            (Member, chats(Member.chat_id)), (SubjectTeacher, chats(SubjectTeacher.chat_id)),
            (ClassChat, chats(ClassChat.chat_id)), (FaqEntry, users(FaqEntry.teacher_id)),
            (Material, users(Material.teacher_id)), (InboxItem, users(InboxItem.teacher_id)),
            (Metric, users(Metric.teacher_id)), (OpenQuestion, users(OpenQuestion.teacher_id)),
            (Campaign, Campaign.school_id.in_(school_ids)), (SchoolMember, SchoolMember.school_id.in_(school_ids)),
            (School, users(School.created_by)), (WebSession, users(WebSession.user_id)), (User, users(User.user_id)),
            (DemoSandbox, DemoSandbox.id == sid),
        ):
            await s.execute(delete(model).where(cond))


async def cleanup(ttl_hours: int = TTL_HOURS) -> int:
    async with session() as s:
        old = (await s.scalars(select(DemoSandbox.id).where(DemoSandbox.created_at < utcnow() - timedelta(hours=ttl_hours)))).all()
    for sid in old:
        await delete_sandbox(sid)
    return len(old)


def not_demo_chat(col):
    """Условие «чат не из демо» для публичной статистики (реальные id чатов — около −10¹³)."""
    return ~col.between(-(DEMO_BASE + DEMO_SPAN), -DEMO_BASE)


def not_demo_user(col):
    return ~col.between(DEMO_BASE, DEMO_BASE + DEMO_SPAN)


__all__ = ["create_sandbox", "sandbox_for", "delete_sandbox", "cleanup", "not_demo_chat", "not_demo_user"]

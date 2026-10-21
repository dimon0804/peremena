"""Доска класса, предметник по приглашению, школа: общая база ответов и сбор на параллель."""
from datetime import timedelta

from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.models import Announcement, Poll, User
from app.services import board, faq, school
from app.timeutil import utcnow

from .conftest import CHAT2_ID, CHAT_ID, PARENT2, PARENTS, SUBJ, TEACHER, TEACHER2, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop, chat_id, teacher):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": teacher,
                             "is_channel": False}))


def test_homework_board(env):
    loop, api, c = env
    added(loop, CHAT_ID, TEACHER)
    run(loop, handle_update(msg_update(TEACHER, "/дз", mid="h1")))
    run(loop, handle_update(msg_update(TEACHER, "алгебра", mid="h2")))
    run(loop, handle_update(msg_update(TEACHER, "№245, 247; правило на с. 63", mid="h3")))
    run(loop, handle_update(cb_update(TEACHER, "bd:fd:tomorrow", "b1")))
    run(loop, handle_update(cb_update(TEACHER, "bd:pub:now", "b2")))
    assert any("№245" in t and "Алгебра" in t for t in api.texts_to(chat_id=CHAT_ID))

    # родитель спрашивает «что задали» — бот отвечает по доске
    run(loop, handle_update(msg_update(PARENTS[0], "Что задали по алгебре на завтра?", mid="h4")))
    assert "№245" in api.texts_to(user_id=PARENTS[0]["user_id"])[-1]
    # по другому предмету задания нет — честно говорим и предлагаем спросить учителя
    run(loop, handle_update(msg_update(PARENTS[0], "а по физике дз есть?", mid="h5")))
    assert "нет задания" in api.texts_to(user_id=PARENTS[0]["user_id"])[-1]


def test_delayed_publication(env):
    loop, api, c = env
    added(loop, CHAT_ID, TEACHER)
    a = run(loop, board.create(CHAT_ID, TEACHER["user_id"], "news", "Завтра субботник, форма свободная",
                               publish_mode="later"))
    assert not a.published and a.publish_at > utcnow()
    assert not any("субботник" in t for t in api.texts_to(chat_id=CHAT_ID))

    async def make_due():
        async with session() as s:
            (await s.get(Announcement, a.id)).publish_at = utcnow() - timedelta(minutes=1)
    run(loop, make_due())
    run(loop, board.tick())
    assert any("субботник" in t for t in api.texts_to(chat_id=CHAT_ID))


def test_subject_teacher_invite(env):
    loop, api, c = env
    added(loop, CHAT_ID, TEACHER)
    run(loop, handle_update(msg_update(TEACHER, "/invite", mid="i1")))
    invite = api.texts_to(user_id=TEACHER["user_id"])[-1]
    code = invite.split("start=subj_")[1].split()[0]

    run(loop, handle_update({"update_type": "bot_started", "timestamp": 2, "chat_id": 1, "user": SUBJ,
                             "payload": f"subj_{code}"}))
    run(loop, handle_update(msg_update(SUBJ, "англ", mid="i2")))
    roles = run(loop, school.roles(SUBJ["user_id"]))
    assert roles.is_subject_teacher and roles.subject_chats[0][1] == "Английский язык"
    # предметник публикует домашку — предмет подставлен сам
    run(loop, handle_update(msg_update(SUBJ, "/hw", mid="i3")))
    run(loop, handle_update(msg_update(SUBJ, "Unit 5, ex. 3", mid="i4")))
    run(loop, handle_update(cb_update(SUBJ, "bd:fd:none", "i5")))
    run(loop, handle_update(cb_update(SUBJ, "bd:pub:silent", "i6")))
    run(loop, handle_update(msg_update(PARENTS[1], "что задали по английскому?", mid="i7")))
    assert "Unit 5" in api.texts_to(user_id=PARENTS[1]["user_id"])[-1]


def test_school_shared_faq_and_campaign(env):
    loop, api, c = env
    added(loop, CHAT_ID, TEACHER)
    added(loop, CHAT2_ID, TEACHER2)

    async def no_quiet():
        async with session() as s:
            for uid in (TEACHER["user_id"], TEACHER2["user_id"]):
                (await s.get(User, uid)).quiet_enabled = False
    run(loop, no_quiet())

    # первый учитель создаёт школу, второй вступает по коду
    run(loop, handle_update(msg_update(TEACHER, "/school", mid="s1")))
    run(loop, handle_update(msg_update(TEACHER, "Школа № 7", mid="s2")))
    r1 = run(loop, school.roles(TEACHER["user_id"]))
    assert r1.is_admin and r1.school.name == "Школа № 7"
    run(loop, handle_update({"update_type": "bot_started", "timestamp": 3, "chat_id": 2, "user": TEACHER2,
                             "payload": f"school_{r1.school.code}"}))
    r2 = run(loop, school.roles(TEACHER2["user_id"]))
    assert r2.school and r2.school.id == r1.school.id and not r2.is_admin

    # карточка в базе школы видна родителю из класса второго учителя
    run(loop, faq.add_entry(TEACHER["user_id"], "Расписание звонков", "1 урок 8:30–9:15, 2 урок 9:25–10:10",
                            school_id=r1.school.id))
    run(loop, handle_update(msg_update(PARENT2, "Подскажите расписание звонков", mid="s3")))
    last = api.texts_to(user_id=PARENT2["user_id"])[-1]
    assert "8:30" in last and "базы ответов школы" in last

    # сбор на параллель: два класса, каждый ведёт свой классный руководитель
    cmp, reports = run(loop, school.launch_campaign(TEACHER["user_id"], r1.school.id, [CHAT_ID, CHAT2_ID],
                                                    "consent", "Анкета по питанию", [], None))
    assert len(reports) == 2

    async def polls_of():
        async with session() as s:
            return (await s.scalars(select(Poll).where(Poll.campaign_id == cmp.id))).all()
    ps = run(loop, polls_of())
    assert {p.teacher_id for p in ps} == {TEACHER["user_id"], TEACHER2["user_id"]}
    assert any("Администрация" in t for t in api.texts_to(user_id=TEACHER2["user_id"]))

    p2 = next(p for p in ps if p.chat_id == CHAT2_ID)
    run(loop, handle_update(cb_update(PARENT2, f"pa:{p2.id}:0", "s4")))
    summary = run(loop, school.campaign_summary(cmp))
    assert summary["answered"] == 1 and summary["total"] == 4
    dash = run(loop, school.dashboard(r1.school.id))
    assert len(dash["classes"]) == 2 and dash["campaigns"][0]["answered"] == 1


def test_subject_detection():
    assert board.subject_in("что по англ задали") == "Английский язык"
    assert board.subject_in("дз по физ-ре") == "Физкультура"
    assert board.is_homework_question("а что на завтра задали?")
    assert not board.is_homework_question("во сколько собрание?")

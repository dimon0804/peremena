"""«Кто сегодня не придёт» и справочник, который подсказывает сам."""
from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.models import Absence, FaqEntry, OpenQuestion, User
from app.services import attendance, faq_learning, quiet
from app.webapp import api

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def setup_class(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))

    async def working_hours():
        async with session() as s:
            (await s.get(User, TEACHER["user_id"])).quiet_enabled = False
    run(loop, working_hours())


async def absences():
    async with session() as s:
        return (await s.scalars(select(Absence))).all()


def test_parent_marks_absence_in_bot_and_teacher_sees_it(env):
    loop, api_, c = env
    setup_class(loop)
    parent = PARENTS[0]
    run(loop, handle_update(cb_update(parent, "ab:start", "a1")))
    assert "Как зовут ребёнка" in api_.texts_to(user_id=parent["user_id"])[-1]
    run(loop, handle_update(msg_update(parent, "Иванов Пётр", mid="a2")))
    run(loop, handle_update(cb_update(parent, "ab:d:today", "a3")))
    run(loop, handle_update(cb_update(parent, "ab:r:sick", "a4")))
    rows = run(loop, absences())
    assert len(rows) == 1 and rows[0].child_name == "Иванов Пётр" and rows[0].reason == "sick"
    assert "Иванов Пётр" in api_.texts_to(user_id=parent["user_id"])[-1]
    # учитель в рабочее время узнаёт сразу и видит список по /absent
    assert any("Иванов Пётр сегодня не придёт" in t for t in api_.texts_to(user_id=TEACHER["user_id"]))
    run(loop, handle_update(msg_update(TEACHER, "/absent", mid="a5")))
    assert "Иванов Пётр — болеет" in api_.texts_to(user_id=TEACHER["user_id"])[-1]
    # имя ребёнка запомнено — второй раз бот его не спрашивает
    run(loop, handle_update(cb_update(parent, "ab:start", "a6")))
    assert "Иванов Пётр" in api_.texts_to(user_id=parent["user_id"])[-1]
    # и оно же в утренней сводке
    teacher = run(loop, api.chats.get_user(TEACHER["user_id"]))
    text, _ = run(loop, quiet.build_digest(teacher))
    assert "Сегодня не придут (1)" in text and "Иванов Пётр" in text


def test_absence_from_group_message_only_author_can_confirm(env):
    loop, api_, c = env
    setup_class(loop)
    author, other = PARENTS[1], PARENTS[2]
    run(loop, handle_update(msg_update(author, "Мы сегодня не придём, заболел", chat_id=CHAT_ID, mid="g1")))
    offer = api_.sent[-1]
    assert offer.get("chat_id") == CHAT_ID and "Отметить" in offer["text"]
    payload = offer["attachments"][0]["payload"]["buttons"][0][0]["payload"]
    run(loop, handle_update(cb_update(other, payload, "g2", in_group=True)))
    assert run(loop, absences()) == [] and "только автор" in api_.answers[-1]["notification"]
    run(loop, handle_update(cb_update(author, payload, "g3", in_group=True)))
    rows = run(loop, absences())
    assert len(rows) == 1 and rows[0].user_id == author["user_id"]


def test_parent_writes_sick_in_dm_gets_offer(env):
    loop, api_, c = env
    setup_class(loop)
    run(loop, handle_update(msg_update(PARENTS[0], "Здравствуйте, у нас температура, в школу не пойдём", mid="d1")))
    last = api_.sent[-1]
    assert "Отметить" in last["text"] and last["attachments"][0]["payload"]["buttons"][0][0]["payload"] == "ab:start"


def test_frequent_unanswered_question_becomes_card(env):
    loop, api_, c = env
    setup_class(loop)
    variants = ["Какая форма на физкультуру?", "какую форму брать на физкультуру завтра?",
                "Подскажите, форма на физкультуру какая нужна?"]
    for i, (p, q) in enumerate(zip(PARENTS, variants)):
        run(loop, handle_update(msg_update(p, q, mid=f"q{i}")))
    # разная тема не смешивается с физкультурой
    run(loop, handle_update(msg_update(PARENTS[0], "Когда сдавать деньги на подарки учителям?", mid="q9")))
    clusters = run(loop, faq_learning.clusters(TEACHER["user_id"]))
    assert len(clusters) == 1 and clusters[0].count == 3
    # на третьем вопросе учитель получил подсказку с кнопкой
    nudge = [s for s in api_.sent if s.get("user_id") == TEACHER["user_id"] and "спросили уже 3 раза" in s["text"]]
    assert len(nudge) == 1
    button = nudge[0]["attachments"][0]["payload"]["buttons"][0][0]["payload"]
    run(loop, handle_update(cb_update(TEACHER, button, "n1")))
    run(loop, handle_update(msg_update(TEACHER, "Белая футболка, тёмные шорты, кроссовки.", mid="n2")))

    async def state():
        async with session() as s:
            cards = (await s.scalars(select(FaqEntry))).all()
            open_q = (await s.scalars(select(OpenQuestion).where(OpenQuestion.resolved.is_(False)))).all()
            return cards, open_q
    cards, open_q = run(loop, state())
    assert len(cards) == 1 and [q.text for q in open_q] == ["Когда сдавать деньги на подарки учителям?"]
    # четвёртый родитель получает ответ сразу
    run(loop, handle_update(msg_update(PARENTS[2], "в какой форме на физкультуру?", mid="q10")))
    assert "Белая футболка" in api_.texts_to(user_id=PARENTS[2]["user_id"])[-1]


def test_open_questions_api(env):
    loop, api_, c = env
    setup_class(loop)
    for i, p in enumerate(PARENTS[:2]):
        run(loop, handle_update(msg_update(p, "Во сколько заканчивается шестой урок?", mid=f"o{i}")))
    teacher = run(loop, api.chats.get_user(TEACHER["user_id"]))
    items = run(loop, api.faq_open(teacher))
    assert items and items[0]["count"] == 2
    res = run(loop, api.faq_open_answer(api.OpenAnswerIn(key=items[0]["key"], answer="В 13:25"), teacher))
    assert res["resolved"] == 2 and run(loop, api.faq_open(teacher)) == []
    parent = run(loop, api.chats.get_user(PARENTS[0]["user_id"]))
    marked = run(loop, api.add_absence(api.AbsenceIn(chat_id=CHAT_ID, day="tomorrow", child_name="Иванов Пётр"), parent))
    assert marked["child_name"] == "Иванов Пётр" and run(loop, attendance.child_name(CHAT_ID, PARENTS[0]["user_id"]))

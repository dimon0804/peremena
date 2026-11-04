"""Диалоги в личке: ответ учителя на вопрос, школа по коду, предметник и полка, роли, общий ответ бота и API."""
from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.models import School, SubjectTeacher, User
from app.services import board, faq, school, shelf
from app.webapp import api

from .conftest import CHAT2_ID, CHAT_ID, PARENT2, PARENTS, SUBJ, TEACHER, TEACHER2, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop, chat_id=CHAT_ID, teacher=TEACHER):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": teacher,
                             "is_channel": False}))


def no_quiet(loop, *uids):
    async def go():
        async with session() as s:
            for uid in uids:
                (await s.get(User, uid)).quiet_enabled = False
    run(loop, go())


def ask_and_open_reply(loop, api_):
    run(loop, handle_update(msg_update(PARENTS[0], "Во сколько начинается родительское собрание?", mid="q1")))
    run(loop, handle_update(cb_update(PARENTS[0], "fw:ask", "q2")))
    qkey = next(k for k in api_.sent[-2]["attachments"][0]["payload"]["buttons"][0] if k["payload"].startswith("fr:"))
    run(loop, handle_update(cb_update(TEACHER, qkey["payload"], "q3")))
    return qkey["payload"]


def test_empty_teacher_reply_is_not_sent_and_question_kept(env):
    loop, api_, c = env
    added(loop)
    no_quiet(loop, TEACHER["user_id"])
    ask_and_open_reply(loop, api_)
    photo = msg_update(TEACHER, "", mid="q4", attachments=[{"type": "image", "payload": {"token": "img"}}])
    run(loop, handle_update(photo))
    assert not any("Ответ учителя" in t for t in api_.texts_to(user_id=PARENTS[0]["user_id"]))
    assert "текстом" in api_.texts_to(user_id=TEACHER["user_id"])[-1]
    run(loop, handle_update(msg_update(TEACHER, "В 18:00 в кабинете 21", mid="q5")))
    assert "В 18:00 в кабинете 21" in api_.texts_to(user_id=PARENTS[0]["user_id"])[-1]


def test_undelivered_reply_keeps_question(env):
    loop, api_, c = env
    added(loop)
    no_quiet(loop, TEACHER["user_id"])
    payload = ask_and_open_reply(loop, api_)
    api_.blocked.add(PARENTS[0]["user_id"])
    run(loop, handle_update(msg_update(TEACHER, "В 18:00", mid="q6")))
    assert run(loop, c.kv.get(f"qq:{payload[3:]}"))  # вопрос не снят — можно ответить ещё раз


def test_lowercase_code_joins_and_typo_does_not_create_school(env):
    loop, api_, c = env
    added(loop)
    sch = run(loop, school.create_school(TEACHER2["user_id"], "Лицей № 1"))
    run(loop, handle_update(msg_update(TEACHER, "/school", mid="s1")))
    run(loop, handle_update(msg_update(TEACHER, sch.code.lower(), mid="s2")))
    r = run(loop, school.roles(TEACHER["user_id"]))
    assert r.school and r.school.id == sch.id and not r.is_admin
    # опечатка в коде у другого человека: школа не создаётся, роль администратора не выдаётся
    run(loop, handle_update(msg_update(PARENT2, "/school", mid="s3")))
    run(loop, handle_update(msg_update(PARENT2, "AB3K7Z", mid="s4")))
    assert not run(loop, school.roles(PARENT2["user_id"])).school
    assert "не найден" in api_.texts_to(user_id=PARENT2["user_id"])[-1]

    async def count():
        async with session() as s:
            return len((await s.scalars(select(School))).all())
    assert run(loop, count()) == 1


def test_subject_teacher_finds_own_file_and_sends_to_class(env):
    loop, api_, c = env
    added(loop)

    async def make_subject():
        async with session() as s:
            s.add(SubjectTeacher(chat_id=CHAT_ID, user_id=SUBJ["user_id"], subject="Физика"))
    run(loop, make_subject())
    upd = msg_update(SUBJ, "Лабораторная по оптике", mid="f1",
                     attachments=[{"type": "file", "filename": "Оптика.pdf", "payload": {"token": "tok-opt"}}])
    run(loop, handle_update(upd))
    run(loop, handle_update(msg_update(SUBJ, "/shelf оптика", mid="f2")))
    assert any("Оптика.pdf" in t for t in api_.texts_to(user_id=SUBJ["user_id"])[-1:])
    m = run(loop, shelf.find(SUBJ["user_id"], "оптика", teacher_view=True))[0].item
    run(loop, handle_update(cb_update(SUBJ, f"sh:to:{m.id}:{CHAT_ID}", "f3")))
    assert any(s.get("chat_id") == CHAT_ID and s.get("attachments") for s in api_.sent)


def test_parent_of_two_classes_sees_class_labels(env):
    loop, api_, c = env
    added(loop)
    added(loop, CHAT2_ID, TEACHER2)
    # родитель есть в обоих классах
    run(loop, handle_update(msg_update(PARENTS[0], "привет", chat_id=CHAT2_ID, mid="x2")))
    run(loop, board.create(CHAT_ID, TEACHER["user_id"], "hw", "№1-5", "Алгебра", None, "silent"))
    run(loop, board.create(CHAT2_ID, TEACHER2["user_id"], "hw", "№7-9", "Алгебра", None, "silent"))
    run(loop, handle_update(msg_update(PARENTS[0], "Что задали по алгебре?", mid="x3")))
    last = api_.texts_to(user_id=PARENTS[0]["user_id"])[-1]
    assert "9Б Родители" in last and "9А Родители" in last


def test_teacher_with_child_in_other_class_asks_as_parent(env):
    loop, api_, c = env
    added(loop)
    added(loop, CHAT2_ID, TEACHER2)
    # учитель 9Б — родитель в 9А
    run(loop, handle_update(msg_update(TEACHER, "привет", chat_id=CHAT2_ID, mid="t1")))
    run(loop, faq.add_entry(TEACHER2["user_id"], "Когда сдавать деньги на экскурсию?", "До пятницы, 500 ₽"))
    run(loop, handle_update(msg_update(TEACHER, "Когда сдавать деньги на экскурсию?", mid="t2")))
    last = api_.texts_to(user_id=TEACHER["user_id"])[-1]
    assert "До пятницы" in last and "Так я отвечу" not in last
    # «заболел» — предлагаем отметить отсутствие в классе ребёнка
    run(loop, handle_update(msg_update(TEACHER, "Сын заболел, не придёт", mid="t3")))
    assert "Отметить" in api_.texts_to(user_id=TEACHER["user_id"])[-1]


def test_api_ask_answers_like_bot(env):
    loop, api_, c = env
    added(loop)
    run(loop, board.create(CHAT_ID, TEACHER["user_id"], "hw", "Параграф 12", "История", None, "silent"))
    parent = run(loop, api.chats.upsert_user(PARENTS[0], dialog_open=True))
    res = run(loop, api.ask("Что задали по истории?", parent))
    assert res["kind"] == "board" and "Параграф 12" in res["text"]
    assert {"answer", "suggestions", "materials"} <= set(res)  # старые поля на месте
    res = run(loop, api.ask("Ваня заболел, сегодня не придёт", parent))
    assert res["kind"] == "absence"
    res = run(loop, api.ask("Где взять справку для бассейна?", parent))
    assert res["kind"] == "none" and res["can_forward"]

    async def open_q():
        from app.models import OpenQuestion
        async with session() as s:
            return (await s.scalars(select(OpenQuestion))).all()
    assert run(loop, open_q())  # вопрос без ответа копится для подсказок учителю, как в боте

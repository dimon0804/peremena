"""Сквозной сценарий на поддельном MAX API: подключение → сбор → ответы → статус → справочник."""
from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.models import FaqEntry, Poll, User
from app.services import polls

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def test_rollcall_end_to_end(env):
    loop, api, c = env
    # бот добавлен в чат учителем
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    assert any("Перемена" in t for t in api.texts_to(chat_id=CHAT_ID))

    # учитель всегда в рабочем окне для теста
    async def no_quiet():
        async with session() as s:
            (await s.get(User, TEACHER["user_id"])).quiet_enabled = False
    run(loop, no_quiet())

    # родитель Сидоров не запускал бота — личка не доставится
    api.blocked.add(202)

    # учитель создаёт сбор пошагово
    run(loop, handle_update(msg_update(TEACHER, "/сбор", mid="t1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:consent", "c1")))
    run(loop, handle_update(msg_update(TEACHER, "Экскурсия в музей, 28 сентября, 300 ₽.\nНужно согласие.", mid="t2")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:friday", "c2")))
    run(loop, handle_update(cb_update(TEACHER, "f:send", "c3")))

    async def get_poll():
        async with session() as s:
            return (await s.scalars(select(Poll))).first()
    poll = run(loop, get_poll())
    assert poll is not None and len(poll.targets) == 3
    assert sum(t.delivered for t in poll.targets) == 2
    assert any("Экскурсия" in t for t in api.texts_to(user_id=201))

    # ответы: Иванова в личке, Сидоров — кнопкой в чате
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{poll.id}:0", "c4")))
    run(loop, handle_update(cb_update(PARENTS[1], f"pa:{poll.id}:2", "c5", in_group=True)))
    poll = run(loop, polls.get_poll(poll.id))
    answered, total, per = polls.counts(poll)
    assert (answered, total) == (2, 3) and per == [1, 0, 1]
    # «Нужно уточнить» дошло до учителя
    assert any("Нужно уточнить" in t for t in api.texts_to(user_id=TEACHER["user_id"]))

    text, _ = run(loop, polls.status_card(poll))
    assert "2/3" in text and "Козлова И." in text

    # напоминание — только тем, кто молчит
    before = len(api.texts_to(user_id=203))
    res = run(loop, polls.remind(poll.id))
    assert res["sent"] == 1 and len(api.texts_to(user_id=203)) == before + 1

    run(loop, handle_update(cb_update(PARENTS[2], f"pa:{poll.id}:0", "c6")))
    assert any("Ответили все" in t for t in api.texts_to(user_id=TEACHER["user_id"]))

    run(loop, polls.close_poll(poll.id))
    poll = run(loop, polls.get_poll(poll.id))
    assert poll.closed


def test_faq_answer_and_forward(env):
    loop, api, c = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    run(loop, handle_update(msg_update(TEACHER, "/add", mid="a1")))
    run(loop, handle_update(msg_update(TEACHER, "Во сколько родительское собрание?", mid="a2")))
    run(loop, handle_update(msg_update(TEACHER, "В четверг в 18:30, кабинет 214.", mid="a3")))

    async def count():
        async with session() as s:
            return len((await s.scalars(select(FaqEntry))).all())
    assert run(loop, count()) == 1

    # родитель спрашивает — бот отвечает из справочника с подписью
    run(loop, handle_update(msg_update(PARENTS[0], "Подскажите, собрание во сколько?", mid="p1")))
    last = api.texts_to(user_id=201)[-1]
    assert "18:30" in last and "сохранено учителем" in last

    # неизвестный вопрос — предлагаем передать учителю
    run(loop, handle_update(msg_update(PARENTS[0], "Когда сдавать деньги на подарки?", mid="p2")))
    assert "Передать" in api.texts_to(user_id=201)[-1] or "передать" in api.texts_to(user_id=201)[-1]
    run(loop, handle_update(cb_update(PARENTS[0], "fw:ask", "f1")))
    teacher_msgs = api.texts_to(user_id=TEACHER["user_id"])
    inbox_or_sent = any("подарки" in t for t in teacher_msgs)

    async def inbox():
        from app.models import InboxItem
        async with session() as s:
            return (await s.scalars(select(InboxItem))).all()
    assert inbox_or_sent or run(loop, inbox())


def test_teacher_reply_suggests_save(env):
    loop, api, c = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    run(loop, handle_update(msg_update(PARENTS[1], "А какая форма завтра?", chat_id=CHAT_ID, mid="g1")))
    link = {"type": "reply", "sender": PARENTS[1], "message": {"mid": "g1", "text": "А какая форма завтра?"}}
    run(loop, handle_update(msg_update(TEACHER, "Парадная: белый верх, тёмный низ.", chat_id=CHAT_ID, mid="g2", link=link)))
    assert any("Сохранить этот ответ" in t for t in api.texts_to(user_id=TEACHER["user_id"]))

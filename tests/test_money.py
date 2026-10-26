"""Сбор денег с учётом: сумма, «перевёл / наличные / не участвуем», подтверждение учителем, итог в рублях."""
from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.models import Poll
from app.services import polls
from app.webapp import api

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def test_money_collection_in_bot(env):
    loop, api_, c = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="m1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:money", "m2")))
    run(loop, handle_update(msg_update(TEACHER, "Экскурсия в музей 28 октября", mid="m3")))
    assert "Сколько сдаёт каждый" in api_.texts_to(user_id=TEACHER["user_id"])[-1]
    run(loop, handle_update(msg_update(TEACHER, "300 руб", mid="m4")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:week", "m5")))
    assert "Сумма: <b>300 ₽</b> с каждого" in api_.texts_to(user_id=TEACHER["user_id"])[-1]
    run(loop, handle_update(cb_update(TEACHER, "f:send", "m6")))

    async def poll():
        async with session() as s:
            return (await s.scalars(select(Poll))).first()
    p = run(loop, poll())
    assert p.amount == 300 and p.template == "money"
    assert any("Сумма: <b>300 ₽</b>" in t for t in api_.texts_to(user_id=PARENTS[0]["user_id"]))

    # перевёл, наличные, не участвуем
    for parent, opt in zip(PARENTS, (polls.PAY_TRANSFER, polls.PAY_CASH, polls.PAY_SKIP)):
        run(loop, handle_update(cb_update(parent, f"pa:{p.id}:{opt}", f"a{opt}")))
    p = run(loop, polls.get_poll(p.id))
    m = polls.money(p)
    assert (m["expected"], m["collected"], m["participants"]) == (600, 0, 2)
    assert len(m["waiting"]) == 2

    # учитель отмечает сдавшего — родитель получает подтверждение
    run(loop, handle_update(cb_update(TEACHER, f"pm:{p.id}", "t1")))
    target = next(t for t in p.targets if t.user_id == PARENTS[0]["user_id"])
    run(loop, handle_update(cb_update(TEACHER, f"pp:{p.id}:{target.id}", "t2")))
    assert "Собрано 300 ₽ из 600 ₽" in api_.answers[-1]["message"]["text"]
    assert "подтвердил получение 300 ₽" in api_.texts_to(user_id=PARENTS[0]["user_id"])[-1]
    p = run(loop, polls.get_poll(p.id))
    text, buttons = run(loop, polls.status_card(p))
    assert "Собрано 300 ₽ из 600 ₽" in text and "Ждут подтверждения" in text
    assert "✅" in polls.full_list(p)


def test_money_api(env):
    loop, api_, c = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    teacher = run(loop, api.chats.get_user(TEACHER["user_id"]))
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        run(loop, api.create_poll(api.PollIn(chat_id=CHAT_ID, template="money", text="Без суммы"), teacher))
    res = run(loop, api.create_poll(api.PollIn(chat_id=CHAT_ID, template="money", text="Подарок на 8 марта",
                                               amount=150), teacher))
    pid = res["poll"]["id"]
    assert res["poll"]["money"]["expected"] == 450
    tid = res["poll"]["targets"][0]["id"]
    out = run(loop, api.mark_paid(pid, api.PaidIn(target_id=tid, paid=True), teacher))
    assert out["money"]["collected"] == 150 and any(t["paid"] for t in out["targets"])

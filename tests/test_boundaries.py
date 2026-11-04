"""Граница рабочего дня для родителей, постраничный /faq, «спросили трое» — по людям."""
from app.bot.router import handle_update
from app.services import faq, faq_learning, polls

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))


def buttons(sent: dict) -> list[str]:
    return [b["payload"] for att in sent.get("attachments") or [] for row in att["payload"]["buttons"] for b in row]


def test_night_poll_can_wait_until_morning(env, monkeypatch):
    loop, api, c = env
    added(loop)
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: False)
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="n1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:consent", "n2")))
    run(loop, handle_update(msg_update(TEACHER, "Согласие на поездку", mid="n3")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:week", "n4")))
    assert "f:send:am" in buttons(api.sent[-1])
    run(loop, handle_update(cb_update(TEACHER, "f:send:am", "n5")))
    assert not api.texts_to(user_id=PARENTS[0]["user_id"])  # ночью родителям ничего
    assert "уйдёт родителям" in api.texts_to(user_id=TEACHER["user_id"])[-1]
    poll = run(loop, polls.list_polls(TEACHER["user_id"]))[0]
    assert poll.send_at and not poll.dispatched


def test_night_manual_remind_asks_and_defers(env, monkeypatch):
    loop, api, c = env
    added(loop)

    async def make():
        p = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия", [], None)
        await polls.send_poll(p.id)
        return p.id
    pid = run(loop, make())
    before = len(api.texts_to(user_id=PARENTS[0]["user_id"]))
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: False)
    run(loop, handle_update(cb_update(TEACHER, f"pr:{pid}", "r1")))
    assert f"pr:{pid}:am" in buttons(api.sent[-1])
    run(loop, handle_update(cb_update(TEACHER, f"pr:{pid}:am", "r2")))
    run(loop, polls.tick())
    assert len(api.texts_to(user_id=PARENTS[0]["user_id"])) == before  # ночь — ждём
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: True)
    run(loop, polls.tick())
    assert len(api.texts_to(user_id=PARENTS[0]["user_id"])) == before + 1


def test_faq_delete_buttons_paginate(env):
    loop, api, c = env
    added(loop)
    for i in range(23):
        run(loop, faq.add_entry(TEACHER["user_id"], f"Вопрос номер {i} про тему {i}", f"Ответ {i}"))
    run(loop, handle_update(msg_update(TEACHER, "/faq", mid="f1")))
    first = next(s for s in reversed(api.sent) if any(p.startswith("fq:del:") for p in buttons(s)))
    assert "fq:page:1" in buttons(first)
    run(loop, handle_update(cb_update(TEACHER, "fq:page:2", "f2")))
    msg = api.answers[-1]["message"]
    payloads = [b["payload"] for row in msg["attachments"][0]["payload"]["buttons"] for b in row]
    assert sum(p.startswith("fq:del:") for p in payloads) == 3


def test_frequent_question_counts_people_not_messages(env):
    loop, api, c = env
    added(loop)
    for i in range(3):  # один родитель спросил трижды — это не «трое»
        run(loop, faq_learning.record([CHAT_ID], PARENTS[0]["user_id"], "Где купить форму для физкультуры?"))
    top = run(loop, faq_learning.clusters(TEACHER["user_id"], min_count=1))
    assert top[0].count == 1 and "спросили" not in "".join(api.texts_to(user_id=TEACHER["user_id"]))
    run(loop, faq_learning.record([CHAT_ID], PARENTS[1]["user_id"], "Где купить форму для физкультуры?"))
    run(loop, faq_learning.record([CHAT_ID], PARENTS[2]["user_id"], "Где купить форму для физкультуры?"))
    assert faq_learning.people(3) == "3 человека"
    assert run(loop, faq_learning.clusters(TEACHER["user_id"], min_count=1))[0].count == 3

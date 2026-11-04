"""Сбор видят и отвечают на него только адресаты: ссылка ?start=poll_N у постороннего не работает."""
from app.bot.router import handle_update
from app.services import polls

from .conftest import CHAT_ID, PARENT2, PARENTS, TEACHER, cb_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def setup_poll(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))

    async def go():
        p = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия в музей", [], None)
        await polls.send_poll(p.id)
        return p.id
    return run(loop, go())


def start(user, payload, ts):
    return {"update_type": "bot_started", "timestamp": ts, "chat_id": 1, "user": user, "payload": payload}


def test_outsider_cannot_see_or_answer_poll_by_link(env):
    loop, api, c = env
    pid = setup_poll(loop)
    run(loop, handle_update(start(PARENT2, f"poll_{pid}", 50)))
    assert not any("Экскурсия в музей" in t for t in api.texts_to(user_id=PARENT2["user_id"]))
    run(loop, handle_update(cb_update(PARENT2, f"pa:{pid}:0", "out1")))
    assert "другого класса" in (api.answers[-1]["notification"] or "")
    poll = run(loop, polls.get_poll(pid))
    assert polls.counts(poll)[:2] == (0, 3)


def test_class_member_still_sees_poll_by_link_and_group_button_counts(env):
    loop, api, c = env
    pid = setup_poll(loop)
    run(loop, handle_update(start(PARENTS[0], f"poll_{pid}", 51)))
    assert any("Экскурсия в музей" in t for t in api.texts_to(user_id=PARENTS[0]["user_id"]))
    # новичок, которого ещё нет в нашем списке, нажал кнопку под сообщением в чате класса — засчитываем
    newbie = {"user_id": 260, "first_name": "Пётр", "last_name": "Новый", "is_bot": False}
    run(loop, handle_update(cb_update(newbie, f"pa:{pid}:0", "in1", in_group=True)))
    poll = run(loop, polls.get_poll(pid))
    assert polls.counts(poll)[:2] == (1, 4)

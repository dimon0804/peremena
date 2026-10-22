"""Регрессии по найденным ошибкам: каждая проверка падала на прежнем коде."""
import asyncio

from sqlalchemy import select

from app import keyboards as kb
from app.bot.router import handle_update
from app.db import session
from app.max_api import TEXT_LIMIT, split_text
from app.models import InboxItem, Material, Poll, User
from app.services import polls

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))


def make_poll(loop):
    async def go():
        p = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия", [], None)
        await polls.send_poll(p.id)
        return p.id
    return run(loop, go())


def test_teacher_button_in_group_is_not_counted(env):
    loop, api, c = env
    added(loop)
    pid = make_poll(loop)
    run(loop, handle_update(cb_update(TEACHER, f"pa:{pid}:0", "t1", in_group=True)))
    poll = run(loop, polls.get_poll(pid))
    assert all(t.user_id != TEACHER["user_id"] for t in poll.targets)
    assert polls.counts(poll)[:2] == (0, 3)


def test_question_is_kept_when_teacher_unreachable(env):
    loop, api, c = env
    added(loop)

    async def working_hours():
        async with session() as s:
            (await s.get(User, TEACHER["user_id"])).quiet_enabled = False
    run(loop, working_hours())
    api.blocked.add(TEACHER["user_id"])  # учитель ещё не запускал бота
    run(loop, handle_update(msg_update(PARENTS[0], "Когда сдавать деньги на подарки?", mid="q1")))
    run(loop, handle_update(cb_update(PARENTS[0], "fw:ask", "q2")))

    async def inbox():
        async with session() as s:
            return (await s.scalars(select(InboxItem))).all()
    items = run(loop, inbox())
    assert len(items) == 1 and "подарки" in items[0].text
    last = api.texts_to(user_id=PARENTS[0]["user_id"])[-1]
    assert "Передал" not in last and "записан" in last


def test_long_text_is_split_under_limit():
    text = "\n".join(f"<b>{i}. Вопрос номер {i}</b>\n→ " + "ответ " * 40 for i in range(40))
    parts = split_text(text)
    assert len(parts) > 1 and all(len(p) <= TEXT_LIMIT for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
    huge = "слово " * 2000  # одна строка длиннее лимита
    assert all(len(p) <= TEXT_LIMIT for p in split_text(huge))


def test_forwarded_file_goes_to_shelf(env):
    loop, api, c = env
    added(loop)
    upd = msg_update(TEACHER, "", mid="f1")
    upd["message"]["link"] = {"type": "forward", "message": {
        "mid": "orig", "text": "Памятка по ПБ #безопасность",
        "attachments": [{"type": "file", "filename": "Памятка_ПБ.pdf", "size": 100,
                         "payload": {"url": "https://x/y", "token": "tok123"}}]}}
    run(loop, handle_update(upd))

    async def mats():
        async with session() as s:
            return (await s.scalars(select(Material))).all()
    m = run(loop, mats())
    assert len(m) == 1 and m[0].filename == "Памятка_ПБ.pdf" and m[0].tags == "безопасность"


def test_double_tap_send_creates_one_poll(env):
    loop, api, c = env
    added(loop)
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="d1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:consent", "d2")))
    run(loop, handle_update(msg_update(TEACHER, "Согласие на поездку", mid="d3")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:none", "d4")))
    async def double_tap():
        await asyncio.gather(handle_update(cb_update(TEACHER, "f:send", "d5")),
                             handle_update(cb_update(TEACHER, "f:send", "d6")))
    run(loop, double_tap())

    async def count():
        async with session() as s:
            return len((await s.scalars(select(Poll))).all())
    assert run(loop, count()) == 1


def test_stale_board_button_is_handled(env):
    loop, api, c = env
    added(loop)
    run(loop, handle_update(msg_update(TEACHER, "/hw", mid="b1")))
    run(loop, handle_update(cb_update(TEACHER, "bd:pub:now", "b2")))  # кнопка до ввода текста
    assert "старого черновика" in api.texts_to(user_id=TEACHER["user_id"])[-1]


def test_open_app_buttons_dropped_without_miniapp(env):
    loop, api, c = env
    assert not c.settings.miniapp
    assert kb.app("Открыть", "bot") is None
    assert kb.kb_list([[kb.app("Открыть", "bot")], [kb.cb("Ок", "x")]]) == \
        [{"type": "inline_keyboard", "payload": {"buttons": [[kb.cb("Ок", "x")]]}}]
    assert kb.kb_list([[kb.app("Открыть", "bot")]]) == []


# ---------------------------------------------------------------- найдено на живом тесте 22.10
def test_every_button_press_gets_valid_answer(env):
    loop, api, c = env
    added(loop)
    for i, payload in enumerate(["m:board", "m:shelf", "m:stats", "m:quiet", "st:list", "m:ask"]):
        run(loop, handle_update(cb_update(TEACHER, payload, f"btn{i}")))
    assert api.answer_errors == 0


def test_late_joiner_is_added_and_all_answered_not_premature(env):
    loop, api, c = env
    added(loop)

    async def working_hours():
        async with session() as s:
            (await s.get(User, TEACHER["user_id"])).quiet_enabled = False
    run(loop, working_hours())
    pid = make_poll(loop)  # в чате трое родителей
    newbie = {"user_id": 250, "first_name": "Пётр", "last_name": "Новый", "is_bot": False}
    run(loop, handle_update({"update_type": "user_added", "timestamp": 9, "chat_id": CHAT_ID, "user": newbie,
                             "is_channel": False}))
    poll = run(loop, polls.get_poll(pid))
    assert any(t.user_id == 250 for t in poll.targets) and len(poll.targets) == 4
    for i, p in enumerate(PARENTS):
        run(loop, handle_update(cb_update(p, f"pa:{pid}:0", f"a{i}")))
    assert not any("Ответили все" in t for t in api.texts_to(user_id=TEACHER["user_id"]))
    run(loop, handle_update(cb_update(newbie, f"pa:{pid}:0", "a9")))
    assert any("Ответили все" in t for t in api.texts_to(user_id=TEACHER["user_id"]))

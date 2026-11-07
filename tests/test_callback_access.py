"""Кнопки бота нельзя подделать: id из payload сверяется с правами того, кто нажал."""
from sqlalchemy import func, select

from app.bot.router import handle_update
from app.db import session
from app.models import Absence, InboxItem, WebSession
from app.services import chats, faq, retention, websession

from .conftest import CHAT2_ID, CHAT_ID, PARENT2, PARENTS, TEACHER, TEACHER2, cb_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop, chat_id=CHAT_ID, teacher=TEACHER):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": teacher,
                             "is_channel": False}))


async def count(model, *where):
    async with session() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))


def test_absence_button_cannot_join_foreign_class(env):
    loop, api, c = env
    added(loop)
    added(loop, CHAT2_ID, TEACHER2)
    p = PARENTS[0]
    # кнопка нажата в чате 9Б, а в payload подставлен чужой класс
    run(loop, handle_update(cb_update(p, f"ab:g:{CHAT2_ID}:{p['user_id']}:today", "a1", in_group=True)))
    assert "не для этого чата" in api.answers[-1]["notification"]
    assert not run(loop, chats.is_present(CHAT2_ID, p["user_id"]))
    assert run(loop, count(Absence, Absence.chat_id == CHAT2_ID)) == 0
    # своя кнопка в своём чате работает
    run(loop, handle_update(cb_update(p, f"ab:g:{CHAT_ID}:{p['user_id']}:today", "a2", in_group=True)))
    assert run(loop, count(Absence, Absence.chat_id == CHAT_ID)) == 1


def test_faq_card_of_other_class_is_not_shown(env):
    loop, api, c = env
    added(loop)
    added(loop, CHAT2_ID, TEACHER2)
    e = run(loop, faq.add_entry(TEACHER2["user_id"], "Код от домофона?", "Секретный ответ 9А", chat_id=CHAT2_ID))
    p = PARENTS[0]
    run(loop, handle_update(cb_update(p, f"fq:show:{e.id}", "f1")))
    assert not any("Секретный ответ" in t for t in api.texts_to(user_id=p["user_id"]))
    # родитель 9А карточку своего класса видит
    run(loop, handle_update(cb_update(PARENT2, f"fq:show:{e.id}", "f2")))
    assert any("Секретный ответ" in t for t in api.texts_to(user_id=PARENT2["user_id"]))


def test_not_that_is_limited_and_only_for_visible_cards(env):
    loop, api, c = env
    added(loop)
    added(loop, CHAT2_ID, TEACHER2)
    foreign = run(loop, faq.add_entry(TEACHER2["user_id"], "Чужой вопрос?", "Чужой ответ", chat_id=CHAT2_ID))
    own = run(loop, faq.add_entry(TEACHER["user_id"], "Во сколько собрание?", "В 18:30", chat_id=CHAT_ID))
    p = PARENTS[0]
    run(loop, handle_update(cb_update(p, f"fq:no:{foreign.id}", "n1")))
    assert run(loop, faq.get_entry(foreign.id)).misses == 0
    for i in range(3):  # повторные нажатия — одна отметка в сутки
        run(loop, handle_update(cb_update(p, f"fq:no:{own.id}", f"n{i + 2}")))
    assert run(loop, faq.get_entry(own.id)).misses == 1


def test_question_goes_only_to_own_class_teacher(env):
    loop, api, c = env
    added(loop)
    added(loop, CHAT2_ID, TEACHER2)
    p = PARENTS[0]
    run(loop, c.kv.set(f"q:{p['user_id']}", {"text": "Когда каникулы?", "chats": [CHAT_ID]}, 3600))
    run(loop, handle_update(cb_update(p, f"fw:{CHAT2_ID}", "w1")))
    assert not any("Когда каникулы" in t for t in api.texts_to(user_id=TEACHER2["user_id"]))
    assert run(loop, count(InboxItem, InboxItem.teacher_id == TEACHER2["user_id"])) == 0


def test_forget_also_ends_site_sessions(env):
    loop, api, c = env
    uid = PARENTS[0]["user_id"]
    run(loop, websession.issue(uid))
    run(loop, retention.forget(uid))
    assert run(loop, count(WebSession, WebSession.user_id == uid)) == 0


def test_child_name_is_erased_when_parent_leaves(env):
    loop, api, c = env
    added(loop)
    p = PARENTS[0]
    from app.models import Member
    from app.services import attendance

    run(loop, attendance.set_child_name(CHAT_ID, p["user_id"], "Иванов Пётр"))
    run(loop, handle_update({"update_type": "user_removed", "timestamp": 5, "chat_id": CHAT_ID, "user": p}))

    async def name():
        async with session() as s:
            return await s.scalar(select(Member.child_name).where(Member.chat_id == CHAT_ID,
                                                                  Member.user_id == p["user_id"]))
    assert run(loop, name()) is None

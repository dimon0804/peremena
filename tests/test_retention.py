"""Сроки хранения и «удалить мои данные»."""
from datetime import timedelta

from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session, utcnow
from app.models import Absence, InboxItem, OpenQuestion, PollTarget
from app.services import polls, retention

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def all_of(loop, model):
    async def go():
        async with session() as s:
            return (await s.scalars(select(model))).all()
    return run(loop, go())


def test_cleanup_removes_expired_records(env):
    loop, api, c = env
    old = utcnow() - timedelta(days=40)

    async def seed():
        async with session() as s:
            s.add_all([
                InboxItem(teacher_id=1, text="старый доставленный", delivered=True, created_at=utcnow() - timedelta(days=8)),
                InboxItem(teacher_id=1, text="свежий", delivered=True),
                InboxItem(teacher_id=1, text="недоставленный", delivered=False, created_at=utcnow() - timedelta(days=8)),
                OpenQuestion(teacher_id=1, text="старый вопрос", created_at=old),
                OpenQuestion(teacher_id=1, text="новый вопрос"),
                Absence(chat_id=1, user_id=2, day="2020-01-01", created_at=old),
                Absence(chat_id=1, user_id=2, day="2099-01-01"),
            ])
    run(loop, seed())
    run(loop, retention.cleanup())
    assert sorted(i.text for i in all_of(loop, InboxItem)) == ["недоставленный", "свежий"]
    assert [q.text for q in all_of(loop, OpenQuestion)] == ["новый вопрос"]
    assert [a.day for a in all_of(loop, Absence)] == ["2099-01-01"]


def test_forget_command_with_confirmation(env):
    loop, api, c = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    p = PARENTS[0]

    async def make_closed_poll():
        poll = await polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "Экскурсия", [], None)
        await polls.send_poll(poll.id)
        await polls.record_answer(poll.id, p, 0)
        await polls.close_poll(poll.id)
        return poll.id
    pid = run(loop, make_closed_poll())
    run(loop, handle_update(msg_update(p, "Где купить тетради для контрольных работ?", mid="f1")))
    assert all_of(loop, OpenQuestion)
    run(loop, handle_update(msg_update(p, "/forget", mid="f2")))
    assert all_of(loop, OpenQuestion)  # без подтверждения ничего не удаляется
    run(loop, handle_update(cb_update(p, "fg:yes", "f3")))
    assert not all_of(loop, OpenQuestion)
    targets = [t for t in all_of(loop, PollTarget) if t.poll_id == pid]
    assert all(t.user_id != p["user_id"] for t in targets)
    assert any(t.name == retention.FORGOTTEN_NAME and t.answer == 0 for t in targets)
    poll = run(loop, polls.get_poll(pid))
    assert polls.counts(poll)[:2] == (1, 3)  # итог сбора не изменился

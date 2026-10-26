"""Найдено при ручной проверке в веб-версии MAX 26.10: каждая проверка падала на прежнем коде."""
from datetime import datetime, timedelta

from app.bot import dialog
from app.bot.router import handle_update
from app.services import board, quiet, shelf
from app.state import DbKV
from app.timeutil import zone

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update


def run(loop, coro):
    return loop.run_until_complete(coro)


def added(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))


def test_state_survives_restart_without_redis(env):
    loop, api, c = env
    run(loop, DbKV().set("subj:ABC", {"chat_id": 1}, ttl=7 * 24 * 3600))
    # «перезапуск»: новый объект хранилища видит то же приглашение
    assert run(loop, DbKV().get("subj:ABC")) == {"chat_id": 1}
    kv = DbKV()
    assert run(loop, kv.add_once("dd:x")) is True and run(loop, kv.add_once("dd:x")) is False
    run(loop, kv.set("short", 1, ttl=-1))  # уже истёк
    assert run(loop, kv.get("short")) is None
    run(loop, kv.cleanup())


def test_homework_asked_for_wrong_day_shows_nearest(env):
    loop, api, c = env
    added(loop)
    day_after = (datetime.now(zone("Europe/Moscow")).date() + timedelta(days=2)).isoformat()
    run(loop, board.create(CHAT_ID, TEACHER["user_id"], "hw", "№245", "Алгебра", day_after, "silent"))
    run(loop, handle_update(msg_update(PARENTS[0], "что задали по алгебре на завтра?", mid="h1")))
    last = api.texts_to(user_id=PARENTS[0]["user_id"])[-1]
    assert "ничего не записано" in last and "№245" in last


def test_teacher_is_not_offered_to_ask_teacher(env):
    loop, api, c = env
    added(loop)
    run(loop, handle_update(msg_update(TEACHER, "а по физике что задали?", mid="t1")))
    sent = api.sent[-1]
    assert "/hw" in sent["text"] and not sent.get("attachments")


def test_caption_button_extracts_tags(env):
    loop, api, c = env
    added(loop)
    media = {"type": "file", "payload": {"token": "t"}, "filename": "Памятка_ПБ.txt", "url": None, "size": 1,
             "key": "k1"}
    m, _ = run(loop, shelf.save(TEACHER["user_id"], media, ""))
    run(loop, handle_update(cb_update(TEACHER, f"sh:cap:{m.id}", "c1")))
    run(loop, handle_update(msg_update(TEACHER, "Памятка по пожарной безопасности #безопасность", mid="c2")))
    m = run(loop, shelf.get(m.id))
    assert m.title == "Памятка по пожарной безопасности" and m.tags == "безопасность"


def test_night_digest_is_not_good_morning(env, monkeypatch):
    loop, api, c = env
    added(loop)
    night = datetime(2026, 10, 27, 0, 5, tzinfo=zone("Europe/Moscow"))
    monkeypatch.setattr(quiet, "local_now", lambda tz=None: night)
    teacher = run(loop, dialog.chats.get_user(TEACHER["user_id"]))
    text, _ = run(loop, quiet.build_digest(teacher, include_empty=True))
    assert "Доброе утро" not in text and "Сводка" in text


def test_evening_deadline_has_no_today_button(env, monkeypatch):
    loop, api, c = env
    added(loop)
    evening = datetime(2026, 10, 26, 23, 16, tzinfo=zone("Europe/Moscow"))
    monkeypatch.setattr(dialog, "local_now", lambda tz=None: evening)
    run(loop, dialog.ask_deadline(TEACHER["user_id"]))
    buttons = [b["text"] for row in api.sent[-1]["attachments"][0]["payload"]["buttons"] for b in row]
    assert "Сегодня 18:00" not in buttons and "Завтра 18:00" in buttons
    assert "28.09" not in api.sent[-1]["text"]

"""Открытая библиотека шаблонов: стартовый набор, поиск, публикация с проверкой на персональные данные,
взять к себе в один тап, счётчик «взяли», жалобы и модерация, демо отдельно, чтение через API v1."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.bot.router import handle_update
from app.db import session
from app.integrations import api_v1
from app.models import FaqEntry, LibraryItem, Poll, User
from app.services import chats, faq, library, polls
from app.webapp import platform_api

from .conftest import CHAT2_ID, CHAT_ID, TEACHER, TEACHER2, cb_update, msg_update

ADMIN = {"user_id": 130, "first_name": "Павел", "last_name": "Админов", "is_bot": False}


def run(loop, coro):
    return loop.run_until_complete(coro)


def last_button(api_, prefix: str) -> str:
    for s in reversed(api_.sent):
        for att in s.get("attachments") or []:
            for row in att.get("payload", {}).get("buttons", []):
                for b in row:
                    if b.get("payload", "").startswith(prefix):
                        return b["payload"]
    raise AssertionError(f"нет кнопки {prefix}")


def setup(loop, c):
    for chat_id, t in ((CHAT_ID, TEACHER), (CHAT2_ID, TEACHER2)):
        run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": t,
                                 "is_channel": False}))

    async def no_quiet():
        async with session() as s:
            for uid in (TEACHER["user_id"], TEACHER2["user_id"]):
                (await s.get(User, uid)).quiet_enabled = False
    run(loop, no_quiet())
    c.settings.admin_max_ids = str(ADMIN["user_id"])


def test_starter_search_publish_take_and_moderation(env):
    loop, fake, c = env
    setup(loop, c)
    t1 = TEACHER["user_id"]
    t2 = TEACHER2["user_id"]

    # стартовый набор от команды появляется сам и не дублируется
    items = run(loop, library.search_items(t1, limit=100))
    assert 15 <= len(items) == len(library.STARTER) and all(it.official for it in items)
    assert run(loop, library.ensure_seed()) == 0
    found = run(loop, library.search_items(t1, q="экскурсия"))
    assert found and "музей" in found[0].text
    assert all(it.topic == "health" for it in run(loop, library.search_items(t1, topic="health")))
    assert all(it.kind == "faq" for it in run(loop, library.search_items(t1, kind="faq")))

    # телефон не пропускаем, имя из своего класса — предупреждаем
    it, blocks, warns = run(loop, library.publish(t1, "poll", "", "Сдача макулатуры\nЗвоните: +7 (912) 345-67-89"))
    assert it is None and blocks
    it, blocks, warns = run(loop, library.publish(t1, "poll", "", "Сдача макулатуры\nОтветственная — Иванова, "
                                                                     "сдаём во вторник"))
    assert it is None and not blocks and any("Иванова" in w for w in warns)
    res = run(loop, platform_api.library_publish(platform_api.LibraryIn(
        text="Сдача макулатуры\nВо вторник с 8:00 до 8:30 у спортзала. Класс-победитель едет в аквапарк.",
        template="signup"), run(loop, chats.get_user(t1))))
    assert res["ok"] and res["item"]["topic"] and res["item"]["mine"]
    item_id = res["item"]["id"]
    assert "author" not in res["item"]  # публикация обезличена

    # коллега находит и берёт к себе в один тап; второй раз счётчик не растёт
    user2 = run(loop, chats.get_user(t2))
    lst = run(loop, platform_api.library_list(q="макулатуры", kind=None, topic=None, grades=None, sort="popular",
                                              user=user2))
    assert [x["id"] for x in lst["items"]] == [item_id] and not lst["items"][0]["mine"]
    took = run(loop, platform_api.library_take(item_id, user2))
    assert took["draft"]["template"] == "signup" and "макулатуры" in took["draft"]["text"]
    run(loop, platform_api.library_take(item_id, user2))
    assert run(loop, library.get(item_id)).uses == 1
    # самое популярное — первым
    assert run(loop, library.search_items(t2, limit=1))[0].id == item_id

    # карточка справочника — сразу в справочник учителя
    card = run(loop, library.search_items(t2, q="справку после болезни", kind="faq"))[0]
    took = run(loop, platform_api.library_take(card.id, user2))
    assert took["faq"]["question"] == card.title
    assert any(e.question == card.title for e in run(loop, faq.list_entries(t2)))

    # три жалобы — шаблон скрыт до решения модератора; модератору пришло уведомление
    for uid in (t2, 501, 502):
        run(loop, chats.upsert_user({"user_id": uid, "first_name": f"П{uid}"}))
        assert run(loop, library.report(uid, item_id, "реклама")) in ("ok", "hidden")
    assert run(loop, library.report(t2, item_id)) == "missing"  # уже скрыт
    assert run(loop, library.get(item_id)).hidden
    assert not any(x.id == item_id for x in run(loop, library.search_items(t2, limit=100)))
    assert any("Жалоба" in t for t in fake.texts_to(user_id=ADMIN["user_id"]))
    # обычный учитель модерировать не может, администратор платформы — может (в том числе кнопкой в боте)
    assert run(loop, library.moderate(t2, item_id, "show")) is None
    with pytest.raises(HTTPException):
        run(loop, platform_api.library_moderation(user2))
    run(loop, chats.upsert_user(ADMIN))
    run(loop, handle_update(cb_update(ADMIN, f"lb:mod:show:{item_id}", "m1")))
    back = run(loop, library.get(item_id))
    assert not back.hidden and back.reports == 0

    # автор удаляет свой шаблон; чужой — нельзя
    with pytest.raises(HTTPException):
        run(loop, platform_api.library_delete(item_id, user2))
    assert run(loop, platform_api.library_delete(item_id, run(loop, chats.get_user(t1))))["ok"]


def test_demo_isolation_bot_flow_and_api_v1(env):
    loop, fake, c = env
    setup(loop, c)
    t1 = TEACHER["user_id"]

    # из демо можно смотреть и брать, но опубликованное в демо видно только в демо,
    # а взятия из демо не накручивают счётчик настоящей библиотеки
    demo_uid = 9_100_000_777
    run(loop, chats.upsert_user({"user_id": demo_uid, "first_name": "Гость"}))
    real = run(loop, library.search_items(demo_uid, q="музей"))[0]
    run(loop, library.take(demo_uid, real.id))
    assert run(loop, library.get(real.id)).uses == 0
    it, _, _ = run(loop, library.publish(demo_uid, "poll", "", "Демо-сбор\nПроверяем, как работает библиотека"))
    assert it.demo
    assert not any(x.id == it.id for x in run(loop, library.search_items(t1, limit=200)))
    assert any(x.id == it.id for x in run(loop, library.search_items(demo_uid, limit=200)))
    assert run(loop, library.report(demo_uid, real.id)) == "ok" and run(loop, library.get(real.id)).reports == 0

    # в боте: «Новый сбор» → «Взять из библиотеки» → шаблон → текст подходит → срок → разослать
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="l1")))
    assert last_button(fake, "lb:list")
    run(loop, handle_update(cb_update(TEACHER, "lb:list", "l2")))
    run(loop, handle_update(cb_update(TEACHER, f"lb:show:{real.id}", "l3")))
    assert "Взять к себе" in str(fake.sent[-1]["attachments"])
    run(loop, handle_update(cb_update(TEACHER, f"lb:take:{real.id}", "l4")))
    assert "Текст подходит" in str(fake.sent[-1]["attachments"])
    run(loop, handle_update(cb_update(TEACHER, "lb:keep", "l5")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:week", "l6")))
    run(loop, handle_update(cb_update(TEACHER, "f:send", "l7")))
    mine = run(loop, polls.list_polls(t1))
    assert mine and mine[0].title == real.title and mine[0].chat_id == CHAT_ID
    assert run(loop, library.get(real.id)).uses == 1

    # поиск командой и публикация своего сбора из бота
    run(loop, handle_update(msg_update(TEACHER, "/library справка", mid="l8")))
    assert "справка" in fake.texts_to(user_id=t1)[-1]
    run(loop, handle_update(cb_update(TEACHER, "lb:share", "l9")))
    run(loop, handle_update(cb_update(TEACHER, last_button(fake, "lb:pp:"), "l10")))
    assert "Опубликовано" in fake.texts_to(user_id=t1)[-1]

    async def own_items():
        async with session() as s:
            return list((await s.scalars(select(LibraryItem).where(LibraryItem.author_id == t1))).all())
    assert len(run(loop, own_items())) == 1

    # карточку справочника с телефоном опубликовать нельзя
    e = run(loop, faq.add_entry(t1, "Как связаться с медсестрой?", "Звоните 8 912 345 67 89 с 8 до 15"))
    it, blocks, _ = run(loop, library.publish_faq(t1, e.id))
    assert it is None and blocks

    # API v1: чтение библиотеки по ключу
    key = SimpleNamespace(owner_id=t1)
    rows = run(loop, api_v1.get_library(q="", kind="faq", topic=None, grades=None, sort="popular", limit=5, key=key))
    assert rows and all(r["kind"] == "faq" for r in rows) and "author_id" not in rows[0]
    one = run(loop, api_v1.get_library_item(rows[0]["id"], key=key))
    assert one["title"] == rows[0]["title"]
    with pytest.raises(HTTPException):
        run(loop, api_v1.get_library_item(10_000, key=key))


def test_pii_check_heuristics(env):
    loop, fake, c = env
    blocks, warns = run(loop, library.pii_check(1, "Пишите на teacher@school.ru"))
    assert blocks
    blocks, warns = run(loop, library.pii_check(1, "Ответственный — Петров Иван"))
    assert not blocks and warns
    blocks, warns = run(loop, library.pii_check(1, "Новогодний праздник в актовом зале, приходите всей семьёй"))
    assert not blocks and not warns

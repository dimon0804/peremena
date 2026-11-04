"""Регион и округ: школа присоединяется по коду, сбор сверху раскладывается на школы и классы,
учитель добавляет приписку, сводка снизу вверх — только цифры, методист не видит ФИО родителей."""
import asyncio
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.bot.router import handle_update
from app.db import session
from app.models import Campaign, ClassChat, Poll, RegionCampaign, School, SchoolMember, User
from app.services import chats, polls, region, school
from app.timeutil import utcnow
from app.webapp import api, platform_api

from .conftest import CHAT2_ID, CHAT_ID, PARENT2, PARENTS, TEACHER, TEACHER2, cb_update, msg_update

ADMIN = {"user_id": 130, "first_name": "Павел", "last_name": "Админов", "is_bot": False}
METH = {"user_id": 140, "first_name": "Нина", "last_name": "Методистова", "is_bot": False}
STRANGER = {"user_id": 150, "first_name": "Пётр", "last_name": "Чужой", "is_bot": False}


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


def setup_two_schools(loop, c):
    """Два класса в двух школах: «Школа № 5» (TEACHER) и «Лицей № 1» (TEACHER2)."""
    for chat_id, t in ((CHAT_ID, TEACHER), (CHAT2_ID, TEACHER2)):
        run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": t,
                                 "is_channel": False}))

    async def no_quiet():
        async with session() as s:
            for uid in (TEACHER["user_id"], TEACHER2["user_id"]):
                (await s.get(User, uid)).quiet_enabled = False
    run(loop, no_quiet())
    s1 = run(loop, school.create_school(TEACHER["user_id"], "Школа № 5"))
    s2 = run(loop, school.create_school(TEACHER2["user_id"], "Лицей № 1"))
    c.settings.admin_max_ids = str(ADMIN["user_id"])
    return s1, s2


def make_due(loop):
    async def go():
        async with session() as s:
            await s.execute(update(Poll).values(send_at=utcnow() - timedelta(minutes=1),
                                                created_at=utcnow() - timedelta(minutes=5)))
    run(loop, go())


def test_join_by_code_launch_note_and_summary(env):
    loop, fake, c = env
    s1, s2 = setup_two_schools(loop, c)
    admin = run(loop, chats.upsert_user(ADMIN))
    teacher = run(loop, chats.get_user(TEACHER["user_id"]))
    teacher2 = run(loop, chats.get_user(TEACHER2["user_id"]))

    # регион заводит только администратор платформы
    with pytest.raises(HTTPException):
        run(loop, platform_api.region_create(platform_api.RegionIn(name="Управление образования"), teacher))
    info = run(loop, platform_api.region_create(platform_api.RegionIn(name="Управление образования Энска"), admin))
    reg = run(loop, region.get_region(info["id"]))

    # завуч первой школы подключает школу в боте, вторая — через приложение
    run(loop, handle_update(msg_update(TEACHER, "/region", mid="r1")))
    run(loop, handle_update(msg_update(TEACHER, reg.code.lower(), mid="r2")))
    assert "подключена" in fake.texts_to(user_id=TEACHER["user_id"])[-1]
    run(loop, platform_api.region_join(platform_api.RegionJoinIn(code=reg.code), teacher2))

    async def region_ids():
        async with session() as s:
            return {sc.id: sc.region_id for sc in (await s.scalars(select(School))).all()}
    assert run(loop, region_ids()) == {s1.id: reg.id, s2.id: reg.id}

    # учитель (не завуч) не может подключить школу кодом для школ
    run(loop, school.join_by_code(TEACHER2["user_id"], s1.code))  # TEACHER2 теперь учитель школы № 5, не админ
    assert run(loop, region.join_by_code(TEACHER2["user_id"], reg.code))[1] == "not_admin"
    run(loop, school.join_by_code(TEACHER2["user_id"], s2.admin_code))  # вернули: завуч лицея

    # методист — по коду для сотрудников
    run(loop, handle_update(msg_update(METH, "/region", mid="r3")))
    run(loop, handle_update(msg_update(METH, reg.staff_code, mid="r4")))
    meth = run(loop, chats.get_user(METH["user_id"]))
    assert run(loop, region.membership(METH["user_id"]))[0].id == reg.id

    # сбор сверху: раскладывается на 2 школы и 2 класса, родителям сразу ничего не уходит
    sent_before = len(fake.sent)
    res = run(loop, platform_api.region_launch(platform_api.RegionCampaignIn(
        template="consent", text="Согласие на экскурсию в планетарий\nВ четверг, транспорт от школы.",
        deadline="week"), (meth, reg)))
    assert res["schools"] == 2 and res["classes"] == 2

    async def region_polls():
        async with session() as s:
            return list((await s.scalars(select(Poll).order_by(Poll.chat_id))).all())
    ps = run(loop, region_polls())
    assert {p.teacher_id for p in ps} == {TEACHER["user_id"], TEACHER2["user_id"]}
    assert all(p.origin == reg.name and not p.dispatched and p.send_at > utcnow() for p in ps)
    for p_ in PARENTS + [PARENT2]:
        assert not fake.texts_to(user_id=p_["user_id"])  # до окна приписки родителям не пишем
    assert any("прислало сбор" in t for t in fake.texts_to(user_id=TEACHER2["user_id"]))
    assert len(fake.sent) - sent_before == 2  # только уведомления двум учителям

    # завуч видит школьную часть как сбор на параллель
    async def school_camps():
        async with session() as s:
            return list((await s.scalars(select(Campaign).where(Campaign.region_campaign_id.is_not(None)))).all())
    camps = run(loop, school_camps())
    assert {cm.school_id for cm in camps} == {s1.id, s2.id}

    # учитель добавляет приписку кнопкой; суть сбора не меняется
    p1 = next(p for p in ps if p.chat_id == CHAT_ID)
    run(loop, handle_update(cb_update(TEACHER, f"rg:note:{p1.id}", "n1")))
    run(loop, handle_update(msg_update(TEACHER, "Бумажное согласие можно сдать в кабинет 12", mid="n2")))
    p1 = run(loop, polls.get_poll(p1.id))
    assert p1.note == "Бумажное согласие можно сдать в кабинет 12" and p1.title.startswith("Согласие на экскурсию")
    # чужой сбор — нельзя
    p2 = next(p for p in ps if p.chat_id == CHAT2_ID)
    assert run(loop, region.set_note(p2.id, TEACHER["user_id"], "чужая приписка"))[1] == "missing"

    # пришло время — досылка рассылает обычной механикой сборов
    make_due(loop)
    run(loop, polls.resume_dispatch())
    t = fake.texts_to(user_id=PARENTS[0]["user_id"])[-1]
    assert reg.name in t and "кабинет 12" in t
    assert reg.name in fake.texts_to(user_id=PARENT2["user_id"])[-1]

    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{p1.id}:0", "a1")))
    run(loop, handle_update(cb_update(PARENT2, f"pa:{p2.id}:0", "a2")))

    rc = run(loop, region.get_campaign(res["campaign"]["id"]))
    d = run(loop, platform_api.region_campaign(rc.id, (meth, reg)))
    assert d["answered"] == 2 and d["total"] == 4 and d["pct"] == 50 and d["per_option"][0] == 2
    assert {s_["name"] for s_ in d["schools"]} == {"Школа № 5", "Лицей № 1"}
    lyceum = next(s_ for s_ in d["schools"] if s_["name"] == "Лицей № 1")
    assert lyceum["pct"] == 100 and lyceum["classes"][0]["grade"] == 9

    # методист не видит ФИО родителей: ни в сводке, ни в выгрузке
    dumped = json.dumps(d, ensure_ascii=False)
    csv_text = region.summary_csv(run(loop, region.summary(rc))).decode("utf-8")
    for p_ in PARENTS + [PARENT2]:
        assert p_["last_name"] not in dumped and p_["last_name"] not in csv_text
    assert "missing" not in dumped and "Школа № 5" in csv_text and "Регион" in csv_text
    # и в классные и школьные экраны методисту хода нет
    with pytest.raises(HTTPException):
        run(loop, api.admin_user(meth))
    with pytest.raises(HTTPException):
        run(loop, api._own(p1.id, meth))
    # а завуч по своей школе видит имена, как и раньше
    cmp1 = next(cm for cm in camps if cm.school_id == s1.id)
    assert run(loop, school.campaign_summary(cmp1))["classes"][0]["missing"]

    # выгрузка по подписанной ссылке
    url = d["export_url"]
    q = dict(x.split("=") for x in url.split("?")[1].split("&"))
    resp = run(loop, platform_api.region_export(rc.id, sig=q["sig"], exp=int(q["exp"])))
    assert resp.media_type.startswith("text/csv")
    with pytest.raises(HTTPException):
        run(loop, platform_api.region_export(rc.id, sig="0" * 32, exp=int(q["exp"])))

    # посторонний методист другого региона сводку не видит
    other = run(loop, region.create_region(ADMIN["user_id"], "Соседний район"))
    stranger = run(loop, chats.upsert_user(STRANGER))
    run(loop, region.join_by_code(STRANGER["user_id"], other.staff_code))
    with pytest.raises(HTTPException):
        run(loop, platform_api.region_campaign(rc.id, (stranger, other)))


def test_filters_bot_flow_and_demo_isolation(env):
    loop, fake, c = env
    s1, s2 = setup_two_schools(loop, c)
    reg = run(loop, region.create_region(ADMIN["user_id"], "Управление образования Энска"))
    for t in (TEACHER, TEACHER2):
        assert run(loop, region.join_by_code(t["user_id"], reg.code))[1] == "school"
    run(loop, region.join_by_code(METH["user_id"], reg.staff_code))
    meth = run(loop, chats.get_user(METH["user_id"])) or run(loop, chats.upsert_user(METH))

    # выбранная школа — один класс; параллель, которой нет, — ни одного
    rc, info = run(loop, region.launch(METH["user_id"], reg.id, "ack", "Новые правила пропуска в школу", [], None,
                                       school_ids=[s2.id]))
    assert info == {"schools": 1, "classes": 1}
    with pytest.raises(HTTPException):
        run(loop, platform_api.region_launch(platform_api.RegionCampaignIn(text="Анкета для 10 классов", grades=[10]),
                                             (meth, reg)))
    assert region.grade_of("9Б Родители") == 9 and region.grade_of("Родители 11-А") == 11
    assert region.grade_of("Чат 2024 года") is None

    # методист запускает сбор кнопками в боте
    run(loop, handle_update(msg_update(METH, "/region", mid="b1")))
    run(loop, handle_update(cb_update(METH, "rg:new", "b2")))
    run(loop, handle_update(cb_update(METH, "rg:tpl:certificate", "b3")))
    run(loop, handle_update(msg_update(METH, "Справка о прививках\nДо конца месяца, в медкабинет.", mid="b4")))
    run(loop, handle_update(cb_update(METH, "rg:gr:5-9", "b5")))
    run(loop, handle_update(cb_update(METH, "rg:dl:week", "b6")))
    assert "2 класс" in fake.texts_to(user_id=METH["user_id"])[-1]
    run(loop, handle_update(cb_update(METH, "rg:go", "b7")))

    async def rcs():
        async with session() as s:
            return list((await s.scalars(select(RegionCampaign).order_by(RegionCampaign.id))).all())
    items = run(loop, rcs())
    assert len(items) == 2 and items[-1].template == "certificate" and items[-1].grades == [5, 6, 7, 8, 9]
    run(loop, handle_update(cb_update(METH, f"rg:sum:{items[-1].id}", "b8")))
    # классные сборы ещё ждут окна для приписки и родителям не ушли — «0 %» вводил бы в заблуждение
    assert "Родителям ещё не разослан" in fake.texts_to(user_id=METH["user_id"])[-1]

    # учитель сразу рассылает сам, не дожидаясь окна
    p = run(loop, polls.list_polls(TEACHER["user_id"]))[0]
    run(loop, handle_update(cb_update(TEACHER, f"rg:now:{p.id}", "b9")))
    assert run(loop, polls.get_poll(p.id)).dispatched
    assert reg.name in fake.texts_to(user_id=PARENTS[1]["user_id"])[-1]

    # демо отдельно: код настоящего региона из песочницы не работает, и наоборот
    demo_uid = 9_100_000_501
    assert run(loop, region.join_by_code(demo_uid, reg.staff_code)) == (None, "")
    demo_reg = run(loop, region.create_region(demo_uid, "Демо-управление"))
    assert region.can_create_region(demo_uid) and not region.can_create_region(TEACHER["user_id"])
    assert run(loop, region.join_by_code(TEACHER["user_id"], demo_reg.code)) == (None, "")
    # назначить методиста может только админ платформы
    assert not run(loop, region.assign_methodist(TEACHER["user_id"], reg.id, STRANGER["user_id"]))
    assert run(loop, region.assign_methodist(ADMIN["user_id"], reg.id, STRANGER["user_id"]))


def test_fan_out_to_many_schools_and_classes(env):
    """20 школ × 3 класса: 20 школьных сборов, 60 классных, ни одного сообщения родителям при запуске;
    уведомления учителям уходят в фоне, чтобы не держать запрос методиста."""
    loop, fake, c = env
    reg = run(loop, region.create_region(ADMIN["user_id"], "Большой регион"))

    async def build():
        async with session() as s:
            for i in range(20):
                sc = School(name=f"Школа № {i + 1}", code=f"S{i:05d}", admin_code=f"A{i:07d}", created_by=1000 + i,
                            region_id=reg.id)
                s.add(sc)
                await s.flush()
                for j, letter in enumerate("АБВ"):
                    tid = 10_000 + i * 10 + j
                    s.add(User(user_id=tid, first_name=f"Учитель{tid}"))
                    s.add(SchoolMember(school_id=sc.id, user_id=tid, role="teacher"))
                    s.add(ClassChat(chat_id=-(70_000 + i * 10 + j), title=f"{5 + j}{letter} Родители", teacher_id=tid,
                                    bot_is_admin=True))
    run(loop, build())
    sent_before = len(fake.sent)
    rc, info = run(loop, region.launch(METH["user_id"], reg.id, "consent", "Анкета по питанию", [], None))
    assert info == {"schools": 20, "classes": 60}
    run(loop, asyncio.gather(*list(region._background)))

    async def counts():
        async with session() as s:
            n_polls = len((await s.scalars(select(Poll).where(Poll.dispatched.is_(False)))).all())
            n_camps = len((await s.scalars(select(Campaign).where(Campaign.region_campaign_id == rc.id))).all())
        return n_polls, n_camps
    assert run(loop, counts()) == (60, 20)
    assert all(m.get("user_id") and m["user_id"] >= 10_000 for m in fake.sent[sent_before:])  # только учителям

    d = run(loop, region.summary(rc))
    assert len(d["schools"]) == 20 and d["classes"] == 60 and d["waiting_dispatch"] == 60
    # только 6-е классы
    rc2, info2 = run(loop, region.launch(METH["user_id"], reg.id, "ack", "Для шестых классов", [], None, grades=[6]))
    assert info2 == {"schools": 20, "classes": 20}

"""API мини-приложения для доски и школы: обработчики вызываются напрямую в офлайн-окружении."""
import pytest
from fastapi import HTTPException

from app.bot.router import handle_update
from app.services import chats
from app.webapp import api

from .conftest import CHAT2_ID, CHAT_ID, PARENTS, TEACHER, TEACHER2


def run(loop, coro):
    return loop.run_until_complete(coro)


def test_board_and_campaign_api(env):
    loop, fake, c = env
    for chat_id, t in ((CHAT_ID, TEACHER), (CHAT2_ID, TEACHER2)):
        run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": t,
                                 "is_channel": False}))
    teacher = run(loop, chats.get_user(TEACHER["user_id"]))
    teacher2 = run(loop, chats.get_user(TEACHER2["user_id"]))
    parent = run(loop, chats.upsert_user(PARENTS[0]))

    me = run(loop, api.me(teacher))
    assert me["board_chats"][0]["chat_id"] == CHAT_ID and me["school"] is None

    a = run(loop, api.add_board(api.BoardIn(chat_id=CHAT_ID, kind="hw", subject="хим", text="§12, задача 3",
                                            for_date="tomorrow", publish_mode="later"), teacher))
    assert a["subject"] == "Химия" and not a["published"] and a["publish_at_text"]
    # запланированное видит учитель, но не родитель
    assert any(x["id"] == a["id"] for x in run(loop, api.list_board(teacher)))
    assert not any(x["id"] == a["id"] for x in run(loop, api.list_board(parent)))
    with pytest.raises(HTTPException):
        run(loop, api.add_board(api.BoardIn(chat_id=CHAT_ID, text="чужой класс"), teacher2))

    # школа: создать, вступить, запустить сбор на параллель
    run(loop, api.create_school(api.SchoolIn(name="Лицей № 1"), teacher))
    me = run(loop, api.me(teacher))
    assert me["is_admin"] and me["school"]["code"]
    run(loop, api.join_school(api.JoinIn(code=me["school"]["code"]), teacher2))
    ar = run(loop, api.admin_user(teacher))
    res = run(loop, api.launch_campaign(api.CampaignIn(chat_ids=[CHAT_ID, CHAT2_ID], text="Согласие на вакцинацию",
                                                       deadline="week"), ar))
    assert len(res["reports"]) == 2 and res["campaign"]["total"] == 4
    dash = run(loop, api.school_dashboard(ar))
    assert {c_["chat_id"] for c_ in dash["classes"]} == {CHAT_ID, CHAT2_ID}
    with pytest.raises(HTTPException):
        run(loop, api.admin_user(teacher2))  # обычный учитель не видит сводку школы

    # общая карточка школы
    e = run(loop, api.add_faq(api.FaqIn(question="Где взять справку 086?", answer="В медкабинете, каб. 105",
                                        shared=True), teacher))
    assert e["shared"]
    assert any(x["id"] == e["id"] for x in run(loop, api.school_faq(teacher2)))

    inv = run(loop, api.invite_subject(CHAT_ID, teacher))
    assert "subj_" in inv["link"]

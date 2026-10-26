"""Демо-режим: песочница с вымышленным классом, переключение ролей, ничего не уходит в MAX, уборка через сутки."""
import asyncio
import os
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.demo_ids import is_demo
from app.max_api import MaxClient

H = {"X-Peremena": "1"}


def test_demo_sandbox_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("BOT_USERNAME", "peremena_test_bot")
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"
    if "postgres" in url:  # те же проверки на настоящем PostgreSQL
        from .conftest import _reset_postgres

        asyncio.run(_reset_postgres(url))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("DEMO_PER_IP_HOUR", "2")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/api/demo/start").status_code == 403  # без заголовка — как любой изменяющий запрос
        assert client.post("/api/demo/start", headers=H).json()["role"] == "teacher"
        assert client.get("/api/auth/session").json()["demo"] is True

        me = client.get("/api/me").json()
        assert me["demo"] and me["is_teacher"] and is_demo(me["user"]["id"]) and me["school"]["role"] == "admin"
        polls = client.get("/api/polls").json()
        polls = polls.get("polls", polls) if isinstance(polls, dict) else polls
        money = next(p for p in polls if p["template"] == "money")
        assert money["amount"] == 200 and 0 < money["money"]["collected"] < money["money"]["expected"]
        att = client.get("/api/attendance").json()
        assert "Сидоров Илья" in str(att)
        open_q = client.get("/api/faq/open").json()
        assert open_q and max(c["count"] for c in open_q) >= 3, open_q

        # учитель создаёт сбор — сообщения «уходят» в заглушку, реальный MAX не вызывается
        chat_id = me["chats"][0]["chat_id"]
        r = client.post("/api/polls", headers=H, json={"chat_id": chat_id, "template": "ack", "text": "Проверка демо"})
        assert r.status_code == 200, r.text
        assert r.json()["poll"]["total"] == 24
        # загрузка файлов в демо закрыта
        up = client.post("/api/materials/upload", headers=H, files={"file": ("a.txt", b"hi", "text/plain")})
        assert up.status_code == 403

        # глазами родителя — и обратно
        assert client.post("/api/demo/role?role=parent", headers=H).status_code == 200
        parent = client.get("/api/me").json()
        assert parent["demo"] and not parent["is_teacher"] and parent["member_chats"]
        mine = client.get("/api/my/polls").json()
        assert any(p.get("answer") is None for p in (mine.get("polls", mine) if isinstance(mine, dict) else mine))
        assert client.post("/api/demo/role?role=teacher", headers=H).status_code == 200
        assert client.get("/api/me").json()["is_teacher"]

        # публичный счётчик на главной песочницы не видит
        stats = client.get("/api/public/stats").json()
        assert stats["classes"] == 0 and stats["polls"] == 0 and stats["minutes_saved"] == 0

        # ограничение по адресу
        client.post("/api/demo/start", headers=H)
        assert client.post("/api/demo/start", headers=H).status_code == 429

        # через сутки песочницы удаляются целиком
        from app.db import session
        from app.models import ClassChat, DemoSandbox, Member, Poll, PollTarget, User, WebSession
        from app.services import demo

        async def age_and_clean():
            async with session() as s:
                await s.execute(update(DemoSandbox).values(created_at=DemoSandbox.created_at - timedelta(days=2)))
            removed = await demo.cleanup()
            async with session() as s:
                left = [await s.scalar(select(func.count()).select_from(m))
                        for m in (User, ClassChat, Member, Poll, PollTarget, WebSession, DemoSandbox)]
            return removed, left

        removed, left = client.portal.call(age_and_clean) if hasattr(client, "portal") and client.portal else \
            asyncio.run(age_and_clean())
        assert removed == 2 and left == [0] * 7
        assert client.get("/api/me").status_code == 401
    get_settings.cache_clear()


def test_max_client_never_sends_demo():
    async def go():
        c = MaxClient("token", "https://example.invalid")
        try:
            r = await c.send_message(user_id=9_100_001_011, text="x")
            assert r["body"]["mid"].startswith("demo.")
            assert (await c.edit_message(r["body"]["mid"], text="y"))["success"]
            assert (await c.get_messages(-9_100_001_001))["messages"] == []
        finally:
            await c.close()
    asyncio.run(go())

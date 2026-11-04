"""Интеграции: календарь iCal, открытый API v1, исходящие вебхуки с подписью, печать, «неделя».

Первая часть — на поддельном MAX API (настоящий класс, бот, кнопки), вторая — демо-песочница через HTTP.
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app import updates
from app.bot.router import handle_update
from app.db import session
from app.integrations import calendar, routes, tokens, webhooks
from app.integrations import api_v1
from app.models import IntegrationToken, Poll, User, Webhook, WebhookDelivery
from app.services import attendance, polls

from .conftest import CHAT2_ID, CHAT_ID, PARENTS, TEACHER, TEACHER2, cb_update, msg_update

H = {"X-Peremena": "1"}
CRLF = "\r\n"


def unfold(ics: str) -> str:
    """Склеивает перенесённые строки iCalendar (RFC 5545, 3.1)."""
    return ics.replace(CRLF + " ", "")


def run(loop, coro):
    return loop.run_until_complete(coro)


def settle(loop):
    """Доводим фоновые доставки вебхуков до конца."""
    async def go():
        for _ in range(50):
            pending = [t for t in updates._tasks if not t.done()]
            if not pending:
                return
            await asyncio.wait(pending, timeout=2)
    run(loop, go())


def setup_class(loop, api):
    for chat_id, t in ((CHAT_ID, TEACHER), (CHAT2_ID, TEACHER2)):
        run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": t,
                                 "is_channel": False}))

    async def no_quiet():
        async with session() as s:
            (await s.get(User, TEACHER["user_id"])).quiet_enabled = False
    run(loop, no_quiet())
    run(loop, handle_update(msg_update(TEACHER, "/сбор", mid="t1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:consent", "c1")))
    run(loop, handle_update(msg_update(TEACHER, "Экскурсия в планетарий\nВыезд в 9:00.", mid="t2")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:week", "c2")))
    run(loop, handle_update(cb_update(TEACHER, "f:send", "c3")))

    async def first():
        async with session() as s:
            return (await s.scalars(select(Poll))).first()
    return run(loop, first())


class Receiver:
    """Приёмник вебхуков вместо внешней системы: httpx.MockTransport, сеть не нужна."""

    def __init__(self, status=200):
        self.requests: list[httpx.Request] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"ok": True})

    def install(self, monkeypatch):
        real = httpx.AsyncClient
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
        monkeypatch.setattr(webhooks, "RETRY_DELAYS", (0, 0, 0))


def test_webhooks_signed_delivery_and_events(env, monkeypatch):
    loop, api, c = env
    rx = Receiver()
    rx.install(monkeypatch)
    # SSRF: внутренние адреса и не-https не принимаются
    for bad in ("ftp://example.ru/x", "https://localhost/x", "https://10.0.0.5/hook", "https://169.254.169.254/x",
                "https://user:pw@example.ru/x"):
        with pytest.raises(webhooks.BadUrl):
            run(loop, webhooks.check_url(bad))

    hook, secret = run(loop, webhooks.create(TEACHER["user_id"], "http://127.0.0.1:9/hook", []))
    assert secret.startswith("whsec_") and secret == webhooks.secret_for(hook)
    async def stored():
        async with session() as s:
            return (await s.get(Webhook, hook.id)).salt
    assert secret not in run(loop, stored())  # секрета в базе нет — только соль

    poll = setup_class(loop, api)
    settle(loop)
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{poll.id}:0", "c4")))
    settle(loop)
    run(loop, attendance.mark(CHAT_ID, PARENTS[1]["user_id"], "2026-11-05", "sick", "Сидоров Илья"))
    settle(loop)
    run(loop, polls.close_poll(poll.id))
    settle(loop)

    events = [r.headers["X-Peremena-Event"] for r in rx.requests]
    assert events == ["poll.created", "poll.answered", "absence.created", "poll.closed"], events
    for r in rx.requests:  # подпись проверяется так, как это сделала бы внешняя система
        ts = r.headers["X-Peremena-Timestamp"]
        expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + r.content, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(r.headers["X-Peremena-Signature"], expected)
        assert abs(int(ts) - time.time()) < 60
    answered = json.loads(rx.requests[1].content)
    assert answered["data"]["answer"]["text"] == "Согласен" and answered["data"]["poll"]["answered"] == 1
    assert answered["data"]["participant"]["name"] == "Иванова А."
    absent = json.loads(rx.requests[2].content)["data"]
    assert absent["child"] == "Сидоров Илья" and absent["reason_text"] == "болеет"
    closed = json.loads(rx.requests[3].content)["data"]
    assert closed["poll"]["closed"] and len(closed["results"]) == 3

    # журнал доставок и выключение после череды ошибок
    log = run(loop, webhooks.deliveries(hook.id))
    assert len(log) == 4 and all(d.status == "ok" for d in log)
    rx.status = 500
    res = run(loop, webhooks.deliver(hook, "ping", webhooks.SAMPLE))
    assert res["status"] == "error" and res["code"] == 500
    assert len(rx.requests) == 4 + 3  # три попытки на 5xx
    rx.status = 410
    run(loop, webhooks.deliver(hook, "ping", webhooks.SAMPLE))
    assert len(rx.requests) == 8  # на осознанный отказ (4xx) не повторяем

    # чужой учитель не видит событий этого класса
    other, _ = run(loop, webhooks.create(TEACHER2["user_id"], "http://127.0.0.1:9/other", ["poll.answered"]))
    run(loop, handle_update(cb_update(PARENTS[2], f"pa:{poll.id}:1", "c9")))  # сбор закрыт — события нет
    settle(loop)
    assert run(loop, webhooks.deliveries(other.id)) == []


def test_calendar_feed_and_bot(env):
    loop, api, c = env
    c.settings.public_url = "https://peremena.test"
    poll = setup_class(loop, api)
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{poll.id}:0", "c4")))
    from app.services import chats

    teacher = run(loop, chats.get_user(TEACHER["user_id"]))
    feed = run(loop, calendar.feed_for(teacher))
    assert feed.startswith("BEGIN:VCALENDAR\r\n") and feed.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in feed.split(CRLF))
    feed = unfold(feed)
    assert "Экскурсия в планетарий (1/3)" in feed and "Ответили 1 из 3" in feed
    assert "Иванова" not in feed and "Сидоров" not in feed  # у учителя в ленте только счётчики

    parent = run(loop, chats.upsert_user(PARENTS[1]))
    pfeed = unfold(run(loop, calendar.feed_for(parent)))
    assert "Ответить до срока: Экскурсия в планетарий" in pfeed and "TRIGGER:-PT180M" in pfeed
    assert "Иванова" not in pfeed
    answered = unfold(run(loop, calendar.feed_for(run(loop, chats.get_user(PARENTS[0]["user_id"])))))
    assert "✅ Экскурсия в планетарий: Согласен" in answered

    # лента по секретной ссылке; в базе только хеш; смена ссылки отключает старую
    token = run(loop, tokens.ical_link(PARENTS[1]["user_id"]))
    resp = run(loop, routes.ical_feed(token))
    assert resp.media_type.startswith("text/calendar") and b"VEVENT" in resp.body

    async def hashes():
        async with session() as s:
            return [r.token_hash for r in (await s.scalars(select(IntegrationToken))).all()]
    assert token not in str(run(loop, hashes()))
    assert run(loop, tokens.ical_link(PARENTS[1]["user_id"])) == token  # ссылку можно показать снова
    new = run(loop, tokens.rotate_ical(PARENTS[1]["user_id"]))
    assert new != token
    with pytest.raises(HTTPException) as e:
        run(loop, routes.ical_feed(token))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException):
        run(loop, routes.ical_feed("cal_guess"))

    # бот: /календарь и /неделя у родителя
    run(loop, handle_update(msg_update(PARENTS[1], "/календарь", mid="p1")))
    msg = api.sent[-1]
    assert "/cal/cal_" in msg["text"] and "Яндекс" in msg["text"]
    btns = [b for row in msg["attachments"][0]["payload"]["buttons"] for b in row]
    assert btns[0]["type"] == "clipboard" and btns[0]["payload"].startswith("https://peremena.test/cal/")
    assert btns[1]["type"] == "link" and btns[1]["url"].startswith("https://calendar.google.com/")
    run(loop, handle_update(msg_update(PARENTS[1], "/неделя", mid="p2")))
    assert "Что важно на неделе" in api.sent[-1]["text"] and "Экскурсия в планетарий" in api.sent[-1]["text"]
    run(loop, handle_update(cb_update(TEACHER, "m:week", "w1")))
    assert "не ответили 2 из 3" in api.sent[-1]["text"]

    # в карточке статуса — ведомость и бланки с QR
    _, buttons = run(loop, polls.status_card(run(loop, polls.get_poll(poll.id))))
    urls = [b.get("url", "") for row in buttons for b in row if b]
    assert any("/print/sheet?" in u for u in urls) and any("/print/blanks?" in u for u in urls)


def test_api_v1_keys_scope_and_write(env):
    loop, api, c = env
    poll = setup_class(loop, api)
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{poll.id}:0", "c4")))

    async def call(fn, *a, key=None, **kw):
        k = await api_v1.api_key(authorization=f"Bearer {key}" if key else None, x_api_key=None)
        return await fn(*a, key=k, **kw)

    with pytest.raises(HTTPException) as e:
        run(loop, call(api_v1.me, key=None))
    assert e.value.status_code == 401
    row, raw = run(loop, tokens.create_api_key(TEACHER["user_id"], "Электронный журнал"))
    assert raw.startswith("prm_") and row.prefix == raw[:10]
    me = run(loop, call(api_v1.me, key=raw))
    assert me["classes"][0]["id"] == CHAT_ID and not me["key"]["can_write"]
    lst = run(loop, call(api_v1.list_polls, "open", None, 50, key=raw))
    assert lst[0]["answered"] == 1 and lst[0]["total"] == 3
    one = run(loop, call(api_v1.get_poll, poll.id, key=raw))
    assert {r["name"]: r["answer"] for r in one["results"]}["Иванова А."] == "Согласен"
    members = run(loop, call(api_v1.class_members, CHAT_ID, key=raw))
    assert len(members) == 3

    # чужой ключ не видит этот класс
    _, raw2 = run(loop, tokens.create_api_key(TEACHER2["user_id"], "x"))
    with pytest.raises(HTTPException) as e:
        run(loop, call(api_v1.get_poll, poll.id, key=raw2))
    assert e.value.status_code == 404

    # запись — только ключом с правом записи; сбор уходит родителям так же, как из бота
    body = api_v1.PollCreate(class_id=CHAT_ID, text="Справка после каникул\nДо пятницы", template="certificate",
                             deadline="friday")
    with pytest.raises(HTTPException) as e:
        run(loop, call(api_v1.create_poll, body, key=raw))
    assert e.value.status_code == 403
    _, wraw = run(loop, tokens.create_api_key(TEACHER["user_id"], "Портал", can_write=True))
    sent_before = len(api.sent)
    res = run(loop, call(api_v1.create_poll, body, key=wraw))
    assert res["poll"]["title"] == "Справка после каникул" and res["report"]["total"] == 3
    assert len(api.sent) > sent_before
    with pytest.raises(HTTPException):
        run(loop, call(api_v1.create_poll, api_v1.PollCreate(class_id=CHAT2_ID, text="чужой класс"), key=wraw))

    # отзыв ключа
    assert run(loop, tokens.revoke(TEACHER["user_id"], row.id))
    with pytest.raises(HTTPException) as e:
        run(loop, call(api_v1.me, key=raw))
    assert e.value.status_code == 401
    # лимит числа ключей
    for i in range(4):
        run(loop, tokens.create_api_key(TEACHER["user_id"], str(i)))
    assert run(loop, tokens.create_api_key(TEACHER["user_id"], "лишний")) is None
    # родитель ключей не создаёт
    assert not run(loop, tokens.can_integrate(PARENTS[0]["user_id"]))


def test_integrations_in_demo_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("BOT_USERNAME", "peremena_test_bot")
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'i.db'}"
    if "postgres" in url:
        from .conftest import _reset_postgres

        asyncio.run(_reset_postgres(url))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/api/demo/start", headers=H).status_code == 200
        ov = client.get("/api/integrations").json()
        assert ov["can_integrate"] and ov["calendar"]["url"].endswith(".ics")
        assert ov["calendar"]["google"].startswith("https://calendar.google.com/")
        assert ov["webhooks"][0]["url"].startswith("https://journal.example-school.ru")
        assert ov["api_keys"][0]["name"].startswith("Школьный портал")
        hid = ov["webhooks"][0]["id"]
        log = client.get(f"/api/integrations/webhooks/{hid}/deliveries").json()
        assert {d["event"] for d in log} >= {"poll.answered", "absence.created"} and log[0]["status"] == "demo"

        # календарь демо-класса — по ссылке, без входа
        # тот же клиент (один цикл событий — важно для asyncpg), но без cookie входа
        saved = dict(client.cookies)
        client.cookies.clear()
        feed = client.get(ov["calendar"]["url"].replace("http://testserver", ""))
        client.cookies.update(saved)
        assert feed.status_code == 200 and "Экскурсия в Музей космонавтики" in unfold(feed.text)

        # свой ключ → настоящий запрос к открытому API
        k = client.post("/api/integrations/keys", headers=H, json={"name": "Проверка", "can_write": True}).json()
        auth = {"Authorization": f"Bearer {k['key']}"}
        polls_ = client.get("/api/v1/polls?status=open", headers=auth).json()
        money = next(p for p in polls_ if p["template"] == "money")
        assert money["money"]["collected"] > 0 and money["total"] == 24
        assert client.get("/api/v1/polls").status_code == 401
        assert client.get("/api/v1/attendance", headers=auth).json()
        assert client.get("/api/v1/board", headers=auth).json()
        made = client.post("/api/v1/polls", headers=auth, json={"class_id": money["class"]["id"],
                                                                "text": "Анкета из портала", "deadline": "week"})
        assert made.status_code == 201, made.text
        assert "paths" in client.get("/api/v1/openapi.json").json()

        # вебхук: «Проверить» в демо — тело и подпись настоящие, но наружу ничего не уходит
        t = client.post(f"/api/integrations/webhooks/{hid}/test", headers=H).json()
        assert t["status"] == "demo"
        bad = client.post("/api/integrations/webhooks", headers=H, json={"url": "http://x.ru/hook"})
        assert bad.status_code == 400
        new = client.post("/api/integrations/webhooks", headers=H,
                          json={"url": "https://crm.example.ru/hook", "events": ["poll.answered"]}).json()
        assert new["secret"].startswith("whsec_")
        assert client.get(f"/api/integrations/webhooks/{new['webhook']['id']}/secret").json()["secret"] == new["secret"]

        # печать: ведомость и бланки с QR по подписанной ссылке
        pl = client.get(f"/api/polls/{money['id']}/print-links").json()
        sheet = client.get(pl["sheet"])
        assert sheet.status_code == 200 and "Подпись" in sheet.text and "Собрано" in sheet.text
        assert "Иванова А." in sheet.text and "Иванов Пётр" in sheet.text
        blanks = client.get(pl["blanks_missing"])
        assert blanks.status_code == 200 and "<svg" in blanks.text and blanks.text.count("class=blank") == 4
        assert client.get(pl["sheet"].replace("sig=", "sig=0")).status_code == 404

        # глазами родителя: ответ уходит во «внешнюю систему» (журнал демо-доставок), есть «неделя»
        before = len(client.get(f"/api/integrations/webhooks/{hid}/deliveries").json())
        assert client.post("/api/demo/role?role=parent", headers=H).status_code == 200
        mine = client.get("/api/my/polls").json()
        target = next(p for p in mine if p["my_answer"] is None)
        assert client.post(f"/api/my/polls/{target['id']}/answer", headers=H, json={"option": 0}).status_code == 200
        wk = client.get("/api/my/week").json()
        assert wk["homework"] and wk["polls"]
        assert client.post("/api/integrations/keys", headers=H, json={"name": "x"}).status_code == 403
        assert client.get(f"/api/polls/{money['id']}/print-links").status_code == 404
        pcal = client.get("/api/integrations").json()["calendar"]["url"]
        assert "Ответить до срока" in unfold(client.get(pcal.replace("http://testserver", "")).text)
        assert client.post("/api/demo/role?role=teacher", headers=H).status_code == 200
        for _ in range(60):  # доставка идёт в фоне
            log = client.get(f"/api/integrations/webhooks/{hid}/deliveries").json()
            if len(log) > before:
                break
            time.sleep(0.05)
        assert log[0]["event"] == "poll.answered" and log[0]["status"] == "demo"

        # через сутки песочница удаляется вместе с ключами и вебхуками
        from app.services import demo

        async def age_and_clean():
            from app.models import DemoSandbox

            async with session() as s:
                await s.execute(update(DemoSandbox).values(created_at=DemoSandbox.created_at - timedelta(days=2)))
            await demo.cleanup()
            async with session() as s:
                return [await s.scalar(select(func.count()).select_from(m))
                        for m in (IntegrationToken, Webhook, WebhookDelivery)]
        left = client.portal.call(age_and_clean)
        assert left == [0, 0, 0]
    get_settings.cache_clear()

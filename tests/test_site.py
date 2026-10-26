"""Сайт: страницы, публичная статистика и вход через бота (код → подтверждение в боте → cookie)."""
import asyncio

from fastapi.testclient import TestClient

from app.bot.router import handle_update
from app.services import websession

from .conftest import TEACHER, cb_update


def test_site_pages_and_login_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("BOT_USERNAME", "peremena_test_bot")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    monkeypatch.setenv("DEV_AUTH", "true")
    monkeypatch.setenv("REDIS_URL", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200 and "Пусть у учителя тоже будет" in home.text
        assert client.get("/login").status_code == 200
        assert client.get("/site.css").status_code == 200
        assert client.get("/app/").status_code == 200  # кабинет и мини-приложение по-прежнему на месте
        # после обновления браузер должен брать новый код, а не старую копию из кэша
        for path in ("/", "/site.js", "/login", "/app/", "/app/app.js"):
            assert client.get(path).headers.get("cache-control") == "no-cache", path
        assert "cache-control" not in client.get("/api/public/stats").headers
        stats = client.get("/api/public/stats").json()
        assert stats["bot_link"] == "https://max.ru/peremena_test_bot" and stats["classes"] == 0

        client.get("/api/me?dev_user=777")  # пользователь появляется в базе
        start = client.post("/api/auth/start").json()
        assert start["link"].endswith("start=login_" + start["code"]) and start["qr_svg"].startswith("<svg")
        assert client.get(f"/api/auth/status?code={start['code']}").json()["status"] == "pending"
        assert client.get("/api/me").status_code == 401

        asyncio.run(websession.confirm_login(start["code"], 777))  # как будто нажали «Войти» в боте
        assert client.get(f"/api/auth/status?code={start['code']}").json()["status"] == "ok"
        assert websession.COOKIE in client.cookies
        assert client.get("/api/me").json()["user"]["id"] == 777
        # код одноразовый
        assert client.get(f"/api/auth/status?code={start['code']}").json()["status"] == "expired"

        # CSRF: изменяющий запрос по cookie без заголовка X-Peremena отклоняется
        assert client.post("/api/school/join", json={"code": "NOPE42"}).status_code == 403
        assert client.post("/api/school/join", json={"code": "NOPE42"},
                           headers={"X-Peremena": "1"}).status_code == 404

        client.post("/api/auth/logout", headers={"X-Peremena": "1"})
        client.cookies.clear()
        assert client.get("/api/me").status_code == 401
    get_settings.cache_clear()


def test_login_confirmed_in_bot(env):
    loop, api, c = env
    code = loop.run_until_complete(websession.start_login("test"))
    loop.run_until_complete(handle_update({"update_type": "bot_started", "timestamp": 5, "chat_id": 1,
                                           "user": TEACHER, "payload": f"login_{code}"}))
    assert "Вход на сайт" in api.texts_to(user_id=TEACHER["user_id"])[-1]
    loop.run_until_complete(handle_update(cb_update(TEACHER, f"lg:ok:{code}", "l1")))
    status, token = loop.run_until_complete(websession.finish_login(code))
    assert status == "ok" and token
    user = loop.run_until_complete(websession.user_by_token(token))
    assert user.user_id == TEACHER["user_id"]

    # «Это не я» — вход отклоняется
    code2 = loop.run_until_complete(websession.start_login("x"))
    loop.run_until_complete(handle_update(cb_update(TEACHER, f"lg:no:{code2}", "l2")))
    assert loop.run_until_complete(websession.finish_login(code2)) == ("rejected", None)


def test_login_by_pin_without_camera(env):
    from .conftest import msg_update

    loop, api, c = env
    code = loop.run_until_complete(websession.start_login("pc"))
    pin = loop.run_until_complete(websession.make_pin(code))
    # отправили боту цифры как есть, с пробелом посередине
    loop.run_until_complete(handle_update(msg_update(TEACHER, f"{pin[:3]} {pin[3:]}", mid="p1")))
    assert "Вход на сайт" in api.texts_to(user_id=TEACHER["user_id"])[-1]
    loop.run_until_complete(handle_update(cb_update(TEACHER, f"lg:ok:{code}", "p2")))
    assert loop.run_until_complete(websession.finish_login(code))[0] == "ok"
    # неизвестный код — понятный ответ, а не поиск по справочнику
    loop.run_until_complete(handle_update(msg_update(TEACHER, "/login 000000", mid="p3")))
    assert "нет или он устарел" in api.texts_to(user_id=TEACHER["user_id"])[-1]

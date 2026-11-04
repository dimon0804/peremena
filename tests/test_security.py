"""Регрессии по аудиту безопасности: подмена входа, чужие данные, лимиты, заголовки."""
import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException

from app.bot.router import handle_update
from app.services import chats
from app.webapp import api
from app.webapp.auth import InitDataError, validate_init_data

from .conftest import CHAT2_ID, CHAT_ID, PARENTS, TEACHER, TEACHER2, FakeApi

TOKEN = "123:abc"
H = {"X-Peremena": "1"}


class FakeWebApi(FakeApi):
    """Поддельный MAX API для сервиса целиком (TestClient + lifespan)."""

    def __init__(self) -> None:
        super().__init__()
        self.uploads: list[int] = []

    async def upload(self, kind, filename, content, content_type=None):
        self.uploads.append(len(content))
        return {"token": "t"}

    async def get_updates(self, marker=None, timeout=30):
        import asyncio

        await asyncio.sleep(3600)


@pytest.fixture
def web(tmp_path, monkeypatch):
    """Фабрика TestClient: web(MAX_BOT_TOKEN="x", ...) — сервис с нужными переменными окружения."""
    from fastapi.testclient import TestClient

    from app.config import get_settings

    holder = {}

    def make(**env):
        base = {"MAX_BOT_TOKEN": "", "BOT_USERNAME": "peremena_test_bot", "REDIS_URL": "", "DEV_AUTH": "true",
                "UPDATES_MODE": "webhook", "WEBHOOK_URL": "", "PUBLIC_URL": "", "SECRET_KEY": "",
                "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'w.db'}"}
        for k, v in {**base, **env}.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        fake = holder["api"] = FakeWebApi()
        monkeypatch.setattr("app.main.MaxClient", lambda *a, **k: fake)
        from app.main import app

        return TestClient(app)

    make.holder = holder
    yield make
    get_settings.cache_clear()


async def _make_class(chat_id: int, teacher_id: int) -> None:
    from app.db import session
    from app.models import ClassChat

    async with session() as s:
        s.add(ClassChat(chat_id=chat_id, title="9Б", teacher_id=teacher_id))


def _sign(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def _raw(fields: dict, token: str = TOKEN) -> str:
    """Неэнкоденная строка данных запуска — так её иногда отдаёт платформа."""
    return "&".join(f"{k}={v}" for k, v in {**fields, "hash": _sign(fields, token)}.items())


# ---------------------------------------------------------------- данные запуска мини-приложения
def test_init_data_cannot_smuggle_foreign_id():
    # Имя подписано как есть, но после повторного декодирования дало бы второй «id» в JSON
    user = '{"id":5,"first_name":"x%22,%22id%22:100,%22z%22:%22"}'
    fields = {"auth_date": str(int(time.time())), "user": user}
    data = validate_init_data(_raw(fields), TOKEN)
    assert data["user"]["id"] == 5 and data["user"]["first_name"] == 'x%22,%22id%22:100,%22z%22:%22'


def test_init_data_rejects_duplicate_json_keys():
    user = '{"id":5,"first_name":"x","id":100}'
    fields = {"auth_date": str(int(time.time())), "user": user}
    with pytest.raises(InitDataError):
        validate_init_data(_raw(fields), TOKEN)


@pytest.mark.parametrize("auth_date", [None, "0", "abc", str(int(time.time()) + 3600)])
def test_init_data_requires_sane_auth_date(auth_date):
    fields = {"user": json.dumps({"id": 5}, separators=(",", ":"))}
    if auth_date is not None:
        fields["auth_date"] = auth_date
    with pytest.raises(InitDataError):
        validate_init_data(_raw(fields), TOKEN)


def test_init_data_non_ascii_hash_is_rejected_not_crash():
    fields = {"auth_date": str(int(time.time())), "user": '{"id":5}'}
    raw = "&".join(f"{k}={v}" for k, v in fields.items()) + "&hash=ж"
    with pytest.raises(InitDataError):
        validate_init_data(raw, TOKEN)


# ---------------------------------------------------------------- доступ к чужим данным
def run(loop, coro):
    return loop.run_until_complete(coro)


def _two_classes(loop):
    for chat_id, t in ((CHAT_ID, TEACHER), (CHAT2_ID, TEACHER2)):
        run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": t,
                                 "is_channel": False}))
    return run(loop, chats.get_user(TEACHER["user_id"])), run(loop, chats.get_user(TEACHER2["user_id"]))


def test_open_questions_key_belongs_to_teacher(env):
    from app.db import session
    from app.models import OpenQuestion
    from app.services import faq_learning

    loop, fake, c = env
    teacher, teacher2 = _two_classes(loop)

    async def seed():
        async with session() as s:
            for text in ("Во сколько собрание в пятницу?", "собрание в пятницу во сколько?"):
                s.add(OpenQuestion(teacher_id=TEACHER["user_id"], chat_id=CHAT_ID, user_id=PARENTS[0]["user_id"],
                                   text=text, stems=" ".join(faq_learning._stems(text))))
    run(loop, seed())
    key = run(loop, api.faq_open(teacher))[0]["key"]
    # другой учитель угадал ключ (это id первого вопроса) — ни текста вопроса, ни закрытия
    with pytest.raises(HTTPException) as e:
        run(loop, api.faq_open_answer(api.OpenAnswerIn(key=key, answer="В 18:00"), teacher2))
    assert e.value.status_code == 404
    assert run(loop, faq_learning.clusters(TEACHER["user_id"]))
    # хозяин — может
    run(loop, api.faq_open_answer(api.OpenAnswerIn(key=key, answer="В 18:00"), teacher))
    assert not run(loop, faq_learning.clusters(TEACHER["user_id"]))


def test_not_that_requires_access_and_is_limited(env):
    from app.services import faq

    loop, fake, c = env
    teacher, teacher2 = _two_classes(loop)
    # участник чата 9Б (класс TEACHER) с «разметкой» в имени
    parent = run(loop, chats.upsert_user({"user_id": PARENTS[0]["user_id"], "first_name": "<b>Анна</b>"}))
    assert CHAT_ID in {x.chat_id for x in run(loop, chats.member_chats(parent.user_id))}
    e = run(loop, faq.add_entry(TEACHER2["user_id"], "Где <i>форма</i>?", "В шкафу"))
    # чужая карточка (учитель другого класса) — 404 и ни одного сообщения
    before = len(fake.sent)
    with pytest.raises(HTTPException) as err:
        run(loop, api.not_that(e.id, parent))
    assert err.value.status_code == 404 and len(fake.sent) == before

    own = run(loop, faq.add_entry(TEACHER["user_id"], "Во сколько <i>собрание</i>?", "В 18:00"))
    run(loop, api.not_that(own.id, parent))
    msgs = fake.texts_to(user_id=TEACHER["user_id"])
    assert "&lt;i&gt;собрание" in msgs[-1] and "<b>" not in msgs[-1]
    # повторно ту же карточку — без новой рассылки
    n = len(fake.sent)
    run(loop, api.not_that(own.id, parent))
    assert len(fake.sent) == n


def test_parent_sees_only_own_money_status(env):
    loop, fake, c = env
    teacher, _ = _two_classes(loop)
    res = run(loop, api.create_poll(api.PollIn(chat_id=CHAT_ID, template="money", text="Сбор на экскурсию",
                                               amount=300), teacher))
    pid = res["poll"]["id"]
    target = next(t for t in res["poll"]["targets"] if t["user_id"] == PARENTS[1]["user_id"])
    run(loop, api.mark_paid(pid, api.PaidIn(target_id=target["id"], paid=True), teacher))
    parent = run(loop, chats.upsert_user(PARENTS[0]))
    mine = next(p for p in run(loop, api.my_polls(parent)) if p["id"] == pid)
    assert mine["money"] == {"amount": 300, "paid": False}
    dump = str(mine)
    for other in PARENTS[1:]:
        assert other["last_name"] not in dump


def test_demo_and_real_schools_do_not_mix_and_join_is_limited(env):
    from app.services import demo, school

    loop, fake, c = env
    teacher, teacher2 = _two_classes(loop)
    real = run(loop, api.create_school(api.SchoolIn(name="Лицей № 1"), teacher))
    real_code = run(loop, api.me(teacher))["school"]["code"]
    sb = run(loop, demo.create_sandbox())
    demo_teacher = run(loop, chats.get_user(sb.teacher_id))
    demo_code = run(loop, api.me(demo_teacher))["school"]["code"]

    for code, who in ((real_code, demo_teacher), (demo_code, teacher2)):
        with pytest.raises(HTTPException) as err:
            run(loop, api.join_school(api.JoinIn(code=code), who))
        assert err.value.status_code == 404
    assert run(loop, school.school_of_teacher(TEACHER2["user_id"])) is None
    assert run(loop, school.school_of_teacher(sb.teacher_id)) != real["id"]

    # перебор кода упирается в лимит
    statuses = []
    for code in [f"AAAA{a}{b}" for a in "BC" for b in "234567"]:
        try:
            run(loop, api.join_school(api.JoinIn(code=code), teacher2))
        except HTTPException as err:
            statuses.append(err.status_code)
    assert statuses[-1] == 429
    with pytest.raises(HTTPException) as err:  # даже верный код — после лимита
        run(loop, api.join_school(api.JoinIn(code=real_code), teacher2))
    assert err.value.status_code == 429


# ---------------------------------------------------------------- секреты без токена
def test_no_token_disables_webhook_and_bot_login(web):
    import hashlib

    from app.config import Settings

    s = Settings(max_bot_token="", secret_key="")
    assert s.effective_webhook_secret == ""
    assert s.signing_key != hashlib.sha256(b"peremena:").digest()  # не из пустой строки
    assert Settings(max_bot_token="", secret_key="k").signing_key == hashlib.sha256(b"k").digest()
    with web() as client:
        public = hashlib.sha256(b"wh:").hexdigest()[:40]  # прежний «секрет» без токена
        assert client.post("/webhook", json={"update_type": "x"},
                           headers={"X-Max-Bot-Api-Secret": public}).status_code == 503
        assert client.post("/api/auth/start", headers=H).status_code == 503


def test_webhook_non_ascii_secret_is_403_not_500(web):
    with web(MAX_BOT_TOKEN="test-token") as client:
        r = client.post("/webhook", json={}, headers={"X-Max-Bot-Api-Secret": "секрет".encode()})
        assert r.status_code == 403


def test_dev_auth_ignored_on_public_deploy(web):
    with web(PUBLIC_URL="https://peremena.example") as client:
        assert client.get("/api/me?dev_user=100").status_code == 401
    with web() as client:  # локально — работает как раньше
        assert client.get("/api/me?dev_user=100").status_code == 200


# ---------------------------------------------------------------- вход на сайт через бота
def test_login_code_works_only_in_browser_that_started_it(web, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from app.services import websession

    with web(MAX_BOT_TOKEN="test-token") as client:
        assert client.post("/api/auth/start").status_code == 403  # без X-Peremena
        assert client.post("/api/auth/logout").status_code == 403
        client.get("/api/me?dev_user=777")
        start = client.post("/api/auth/start", headers={**H, "User-Agent": "Mozilla/5.0 (Windows NT 10.0) "
                                                        "Chrome/120.0 Safari/537.36"}).json()
        asyncio.run(websession.confirm_login(start["code"], 777))
        # подсмотревший код (другой браузер, без метки) сессию не получает и даже статуса не видит
        other = TestClient(client.app)
        r = other.get(f"/api/auth/status?code={start['code']}")
        assert r.json()["status"] == "expired" and websession.COOKIE not in other.cookies
        # браузер, начавший вход, — получает
        assert client.get(f"/api/auth/status?code={start['code']}").json()["status"] == "ok"
        assert client.get("/api/me").json()["user"]["id"] == 777
        assert client.post("/api/auth/logout", headers=H).status_code == 200

        monkeypatch.setattr("app.webapp.site_api.AUTH_STARTS_PER_HOUR", 2)
        codes = [client.post("/api/auth/start", headers=H).status_code for _ in range(3)]
        assert codes[-1] == 429


def test_parallel_finish_login_gives_one_session(env):
    import asyncio

    from app.services import websession
    from app.state import DbKV

    loop, fake, c = env
    for kv in (c.kv, DbKV()):
        c.kv = kv
        code = run(loop, websession.start_login("ua"))
        run(loop, websession.confirm_login(code, 777))

        async def race():
            return await asyncio.gather(*[websession.finish_login(code) for _ in range(5)])
        tokens = [t for _, t in run(loop, race()) if t]
        assert len(tokens) == 1, type(kv).__name__


def test_login_pin_bruteforce_is_limited_and_device_shown(env):
    from app.services import websession

    from .conftest import msg_update

    loop, fake, c = env
    code = run(loop, websession.start_login("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1"))
    pin = run(loop, websession.make_pin(code))
    wrong = [f"{(int(pin) + i) % 1_000_000:06d}" for i in range(1, 11)]
    for i, p in enumerate(wrong):
        run(loop, handle_update(msg_update(TEACHER, p, mid=f"w{i}")))
    run(loop, handle_update(msg_update(TEACHER, pin, mid="right")))
    assert "нет или он устарел" in fake.texts_to(user_id=TEACHER["user_id"])[-1]
    # другой пользователь со своим лимитом — видит запрос и устройство
    run(loop, handle_update(msg_update(TEACHER2, pin, mid="t2")))
    text = fake.texts_to(user_id=TEACHER2["user_id"])[-1]
    assert "Вход на сайт" in text and "Safari, iPhone" in text and "запрошен в" in text


# ---------------------------------------------------------------- демо-песочницы
def test_demo_ids_never_leave_demo_range(env):
    from app.db import session
    from app.demo_ids import SLOTS, is_demo, sandbox_base
    from app.models import DemoSandbox
    from app.services import demo

    loop, fake, c = env
    with pytest.raises(ValueError):
        sandbox_base(SLOTS)

    async def occupy_last():
        async with session() as s:
            s.add(DemoSandbox(id=SLOTS - 1, teacher_id=0, parent_id=0))
    run(loop, occupy_last())
    sb = run(loop, demo.create_sandbox())  # номера после последнего нет — берётся освободившийся
    assert sb.id == 1 and is_demo(sb.teacher_id) and is_demo(sb.parent_id)
    assert run(loop, demo.create_sandbox()).id == 2


def test_demo_global_limit_and_cleanup_without_token(web):
    from app.main import maintenance_scheduler

    with web(DEMO_MAX_SANDBOXES="1") as client:
        assert client.post("/api/demo/start", headers=H).status_code == 200
        assert client.post("/api/demo/start", headers=H).status_code == 503
        jobs = {j.id for j in maintenance_scheduler(client.app.state.ctx).get_jobs()}
        assert {"demo", "sessions"} <= jobs


# ---------------------------------------------------------------- адрес клиента и прокси
def test_demo_limit_ignores_spoofed_forwarded_for(web):
    with web(DEMO_PER_IP_HOUR="1") as client:
        assert client.post("/api/demo/start", headers=H).status_code == 200
        r = client.post("/api/demo/start", headers={**H, "X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 429


def test_forwarded_headers_trusted_only_from_local_proxy():
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    from app.config import Settings

    trusted = _TrustedHosts(Settings().forwarded_allow_ips)
    assert "127.0.0.1" in trusted and "172.18.0.5" in trusted  # Caddy в сети Docker
    assert "203.0.113.9" not in trusted and "*" not in Settings().forwarded_allow_ips


# ---------------------------------------------------------------- загрузка файлов
def test_upload_checks_auth_and_size_before_reading_body(web):
    import asyncio

    with web() as client:
        fake = web.holder["api"]
        huge = {"content-type": "multipart/form-data; boundary=xx", "content-length": str(500 * 1024 * 1024), **H}
        # без входа и не учитель — отказ до разбора тела
        assert client.post("/api/materials/upload", content=b"--xx--", headers=huge).status_code == 401
        assert client.post("/api/materials/upload?dev_user=7", content=b"--xx--", headers=huge).status_code == 403
        asyncio.run(_make_class(-7000, 8))
        # заявленный размер больше лимита — 413, тело не читается
        assert client.post("/api/materials/upload?dev_user=8", content=b"--xx--", headers=huge).status_code == 413
        # обычная загрузка работает
        ok = client.post("/api/materials/upload?dev_user=8", headers=H, data={"title": "Расписание", "tags": "8 класс"},
                         files={"file": ("a.txt", b"hello", "text/plain")})
        assert ok.status_code == 200, ok.text
        assert ok.json()["title"].startswith("Расписание") and fake.uploads == [5]
        # два файла в одной форме — отказ
        two = client.post("/api/materials/upload?dev_user=8", headers=H,
                          files=[("file", ("a.txt", b"1", "text/plain")), ("file", ("b.txt", b"2", "text/plain"))])
        assert two.status_code == 400

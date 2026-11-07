"""HTTP-сервис: вебхук MAX, API и статика мини-приложения, health-check."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import mimetypes
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db, metrics, updates
from .bot.common import COMMANDS
from .bot.router import handle_update
from .config import BASE_DIR, get_settings
from .core import Ctx, set_ctx
from .max_api import MaxApiError, MaxClient
from .state import make_kv
from .events import EventQueue
from .leader import Leader, make_backend
from .updates import JOB_OPTS, Background, drain, leader_only, make_scheduler, spawn
from .webapp.api import router as api_router
from .webapp.site_api import router as site_api_router
from .webapp.platform_api import router as platform_api_router
from .integrations.api_v1 import router as api_v1_router
from .integrations.routes import router as integrations_router
from .integrations.journals.routes import router as journals_router

log = logging.getLogger("peremena")


def maintenance_scheduler(c: Ctx):
    """Уборка без бота: просроченные демо-песочницы, сессии сайта и записи состояния в базе."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from .services import demo, websession

    sched = AsyncIOScheduler(timezone="UTC")
    opts = JOB_OPTS
    sched.add_job(leader_only(demo.cleanup), "interval", hours=1, id="demo", **opts)
    sched.add_job(leader_only(websession.cleanup), "interval", hours=1, id="sessions", **opts)
    if hasattr(c.kv, "cleanup"):
        sched.add_job(leader_only(c.kv.cleanup), "interval", hours=1, id="kv", **opts)
    updates.add_embeddings_job(sched)
    return sched


async def _start_background(c: Ctx, sched, polling: bool, subscribe_webhook: bool) -> tuple[Background, Leader]:
    """Планировщик и long polling — только в процессе-лидере (замок в Redis / PostgreSQL)."""
    bg = Background(c, sched, polling=polling, subscribe_webhook=subscribe_webhook)
    leader = Leader(make_backend(c.kv, db.engine(), c.settings.max_bot_token), ttl=LEADER_TTL,
                    on_elected=bg.on_elected, on_demoted=bg.on_demoted)
    updates.current_leader = leader
    await leader.step()  # первый шаг сразу: одиночный экземпляр начинает работу без задержки
    leader.start()
    return bg, leader


LEADER_TTL = 30.0


async def startup() -> tuple[Ctx, list]:
    settings = get_settings()
    await db.init_db(settings.database_url)
    # EMBEDDINGS=on — загрузить модель (в каждом процессе: ею же ищем в личке); ошибка — работаем без неё
    from .services import semantic

    await semantic.init(settings)
    kv = await make_kv(settings.redis_url)
    api = MaxClient(settings.max_bot_token, settings.max_api_url, settings.extra_ca_file, rps=settings.max_rps)
    if kv.redis is not None:
        api.use_shared_limiter(kv.redis)  # один лимит MAX API на все экземпляры и воркеры
    c = Ctx(settings=settings, api=api, kv=kv, bot_username=settings.bot_username)
    set_ctx(c)
    background: list = []
    mode = settings.event_queue.strip().lower()
    updates.queue = None
    if kv.redis is not None and mode in ("auto", "redis"):
        updates.queue = EventQueue(kv.redis, max_attempts=settings.event_max_attempts,
                                   reclaim_idle_ms=int(settings.event_reclaim_s * 1000),
                                   backlog_warn=settings.event_backlog_warn,
                                   maxlen=settings.event_stream_maxlen)
    elif mode == "redis":
        log.error("EVENT_QUEUE=redis, но Redis не подключён — события обрабатываются в принявшем процессе")
    if kv.redis is not None:  # снимок метрик процесса — в Redis, чтобы /metrics любого процесса видел все
        background.append(asyncio.create_task(metrics.pusher(kv.redis), name="metrics"))
    role = settings.role.strip().lower() or "all"
    if role not in ("all", "web", "worker"):
        log.error("ROLE=%s не поддерживается (all | web | worker) — работаю как all", settings.role)
    log.info("Роль процесса: %s; база: %s, пул: %s; состояние: %s", role, db.dialect(),
             db.pool_status() or "—", type(kv).__name__)
    if settings.dev_auth and settings.is_public:
        log.error("!!! DEV_AUTH=true при заданном PUBLIC_URL/WEBHOOK_URL — это вход под любым пользователем. "
                  "Режим разработки ИГНОРИРУЕТСЯ; уберите DEV_AUTH из настроек прода.")
    if not settings.max_bot_token:
        log.warning("MAX_BOT_TOKEN не задан — работает только веб-часть (API и мини-приложение)")
        if settings.runs_background:  # демо и сессии убираем и без бота
            background += await _start_background(c, maintenance_scheduler(c), polling=False, subscribe_webhook=False)
        return c, background
    try:
        me = await api.get_me()
        c.bot_user_id = me.get("user_id", 0)
        c.bot_username = settings.bot_username or me.get("username") or ""
        c.bot_name = me.get("name") or me.get("first_name") or c.bot_name
        log.info("Бот: %s (@%s, id=%s)", c.bot_name, c.bot_username, c.bot_user_id)
    except MaxApiError as e:
        log.error("GET /me не удался: %s — проверьте токен и доступ к %s", e, settings.max_api_url)
    if settings.runs_background:
        try:
            await api.set_commands(COMMANDS)
        except MaxApiError as e:
            log.warning("Не удалось обновить меню команд: %s", e)
        background += await _start_background(c, make_scheduler(c), polling=settings.mode == "polling",
                                               subscribe_webhook=settings.mode == "webhook")
        if updates.queue is not None:  # обработчики очереди — в каждом процессе all/worker, не только у лидера
            background.append(asyncio.create_task(
                updates.queue.consume(handle_update, settings.handler_concurrency), name="event-queue"))
    else:
        log.info("ROLE=web: планировщик и long polling работают в другом экземпляре (ROLE=worker или all)")
    c.started = True
    log.info("Режим получения событий: %s", settings.mode)
    return c, background


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    c, background = await startup()
    app.state.ctx = c
    app.state.background = background
    try:
        yield
    finally:
        for b in background:  # сначала лидер отпускает замок — другой экземпляр подхватит задачи сразу
            if isinstance(b, Leader):
                await b.stop()
        for b in background:
            if isinstance(b, Background):
                b.shutdown()
        if updates.queue is not None:
            await updates.queue.drain()
        for b in background:
            if isinstance(b, asyncio.Task):
                b.cancel()
        await drain()  # начатые обработки (рассылка, ответ на кнопку) доводим до конца, пока живы API и база
        await c.api.close()
        await c.kv.close()
        await db.close_db()


# Документация API открыта только в режиме разработки (DEV_AUTH без публичного адреса) — см. ниже
app = FastAPI(title="Перемена", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.include_router(api_router)
app.include_router(site_api_router)
app.include_router(platform_api_router)  # регион (сбор сверху) и библиотека шаблонов
app.include_router(integrations_router)  # календарь, ключи, вебхуки, печать
app.include_router(api_v1_router)        # открытый API для школьных систем
app.include_router(journals_router)      # электронный журнал: импорт выгрузок, экспорт, «Дневник.ру»

# Что разрешено грузить страницам сайта и мини-приложения: свои файлы и мост MAX (st.max.ru).
# Инлайновые стили нужны интерфейсу (style="…" в разметке), инлайновых скриптов нет.
CSP = ("default-src 'self'; script-src 'self' https://st.max.ru; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'")
BODY_LIMIT = 1024 * 1024  # JSON-запросы и вебхук; загрузка файлов проверяет свой лимит сама
OWN_LIMIT = ("/api/materials/upload", "/api/journals/preview")  # здесь лимит проверяет сам обработчик
DEV_ONLY = ("/api/docs", "/openapi.json")


@app.get("/openapi.json", include_in_schema=False)
async def openapi_schema():
    if not get_settings().dev_mode:
        raise HTTPException(404, "Not Found")
    return JSONResponse(app.openapi())


@app.get("/api/docs", include_in_schema=False)
async def api_docs():
    if not get_settings().dev_mode:
        raise HTTPException(404, "Not Found")
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Перемена — API")


def _is_api(path: str) -> bool:
    return path.startswith(("/api/", "/webhook", "/health", "/metrics"))


def _revalidate(path: str) -> bool:
    """Страницы, скрипты и стили сайта и мини-приложения браузер должен сверять с сервером (по ETag),
    иначе после обновления у пользователей остаётся старый код — особенно во встроенном браузере MAX."""
    return not path.startswith("/api/") and (path.endswith((".html", ".js", ".css")) or path.endswith("/")
                                             or path in ("/login", "/app"))


class HttpPolicy:
    """Общие правила HTTP одним ASGI-слоем (раньше — три @app.middleware("http"); каждый такой слой — отдельная
    задача и поток тела ответа, на сотнях запросов в секунду это заметная доля процессора):
    * запрос больше BODY_LIMIT — 413 (загрузка файлов проверяет свой лимит сама);
    * заголовки безопасности: nosniff, Referrer-Policy, CSP (мини-приложению MAX без frame-ancestors —
      его открывают во фрейме/вебвью; Swagger в разработке грузит скрипты с CDN — ему CSP не ставим);
    * Cache-Control: no-cache для страниц, скриптов и стилей;
    * неизвестный /api/... попадает в статику сайта и получил бы HTML-страницу 404 — отвечаем JSON."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        length = 0
        for k, v in scope["headers"]:
            if k == b"content-length":
                try:
                    length = int(v)
                except ValueError:
                    length = 0
                break
        if length > BODY_LIMIT and path not in OWN_LIMIT:
            return await JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)(scope, receive, send)
        extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"strict-origin-when-cross-origin")]
        if path not in DEV_ONLY:
            csp = CSP if path == "/app" or path.startswith("/app/") else CSP + "; frame-ancestors 'self'"
            extra.append((b"content-security-policy", csp.encode()))
        revalidate = _revalidate(path)
        api = _is_api(path)
        swallow = False

        async def wrapped(message):
            nonlocal swallow
            if swallow:
                return
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if api and message["status"] == 404 and any(
                        k.lower() == b"content-type" and v.startswith(b"text/html") for k, v in headers):
                    swallow = True
                    body = json.dumps({"detail": "Не найдено"}, ensure_ascii=False).encode()
                    await send({"type": "http.response.start", "status": 404, "headers": [
                        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                        *extra]})
                    await send({"type": "http.response.body", "body": body})
                    return
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra if k not in present]  # как setdefault
                if revalidate:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


@app.exception_handler(StarletteHTTPException)
async def not_found_page(request: Request, exc: StarletteHTTPException):
    """Человеку, а не программе, на несуществующем адресе показываем страницу в стиле сайта, а не голый JSON."""
    if exc.status_code == 404 and not _is_api(request.url.path):
        return FileResponse(SITE_DIR / "404.html", status_code=404, headers={"Cache-Control": "no-cache"})
    return await http_exception_handler(request, exc)


async def accept_webhook(secret: bytes | None, raw: bytes) -> tuple[int, dict]:
    """Приём вебхука MAX: проверка секрета, разбор, в очередь (или в обработку). Возвращает (статус, тело)."""
    settings = get_settings()
    expected = settings.effective_webhook_secret
    if not expected:  # без токена бота вебхук не принимаем: секрет был бы выведен из пустой строки
        return 503, {"detail": "bot is not configured"}
    # сравниваем байты: строка с не-ASCII символами в compare_digest даёт TypeError (и 500)
    if not secret or not hmac.compare_digest(secret, expected.encode()):
        return 403, {"detail": "bad secret"}
    try:
        update = json.loads(raw)
    except ValueError:
        return 400, {"detail": "bad json"}
    if isinstance(update, dict) and update.get("update_type"):
        metrics.inc("peremena_events_received_total", source="webhook")
        # с очередью — только XADD в Redis: 200 уходит сразу, а событие уже не потеряется при падении процесса.
        # Очередь длиннее EVENT_BACKLOG_WARN — всё равно 200 (повтор доставки MAX только добавил бы нагрузки),
        # предупреждение и метрику пишет сама очередь.
        if not await updates.enqueue(update):
            spawn(updates.handle_local(update))
    return 200, {"ok": True}


class WebhookFastPath:
    """POST /webhook — в обход стека middleware FastAPI (каждый @app.middleware("http") — отдельная задача
    и поток тела ответа). На пике это половина процессора приёма: так один процесс принимает в разы больше
    вебхуков в секунду. Заголовки ответа — те же, что ставит security_headers."""

    HEADERS = [(b"content-type", b"application/json"), (b"x-content-type-options", b"nosniff"),
               (b"referrer-policy", b"strict-origin-when-cross-origin"),
               (b"content-security-policy", (CSP + "; frame-ancestors 'self'").encode())]

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/webhook" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        secret = None
        length = 0
        for k, v in scope["headers"]:
            if k == b"x-max-bot-api-secret":
                secret = v
            elif k == b"content-length":
                try:
                    length = int(v)
                except ValueError:
                    length = 0
        status, body = 413, {"detail": "Слишком большой запрос"}
        if length <= BODY_LIMIT:
            chunks, size, more = [], 0, True
            while more:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                chunk = msg.get("body", b"")
                size += len(chunk)
                if size > BODY_LIMIT:
                    break
                chunks.append(chunk)
                more = msg.get("more_body", False)
            if size <= BODY_LIMIT:
                t0 = time.perf_counter()
                status, body = await accept_webhook(secret, b"".join(chunks))
                metrics.observe("peremena_webhook_seconds", time.perf_counter() - t0)
        raw = json.dumps(body, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": self.HEADERS + [(b"content-length", str(len(raw)).encode())]})
        await send({"type": "http.response.body", "body": raw})


async def _db_ok() -> bool:
    from sqlalchemy import text

    try:
        async with db.session() as s:
            await asyncio.wait_for(s.execute(text("SELECT 1")), timeout=3)
        return True
    except Exception:
        log.exception("health: база недоступна")
        return False


async def _kv_ok(c: Ctx) -> bool:
    try:
        return bool(await asyncio.wait_for(c.kv.ping(), timeout=3))
    except Exception:
        return False


@app.get("/health")
async def health():
    """200 — всё работает, 503 — что-то сломалось (на это смотрит HEALTHCHECK в Docker).
    Подробности — только в режиме разработки: снаружи незачем знать версию и устройство сервиса."""
    c = app.state.ctx
    checks = {"db": await _db_ok(), "kv": await _kv_ok(c)}
    background = getattr(app.state, "background", [])
    leader = next((b for b in background if isinstance(b, Leader)), None)
    for b in background:
        if isinstance(b, Background) and b.polling_ok() is not None:  # long polling упал — события не приходят
            checks["polling"] = b.polling_ok()
    if leader is not None and leader._task is not None and leader._task.done():
        checks["leader"] = False  # цикл выборов умер — процесс не подхватит задачи, если лидер упадёт
    consumers = [b for b in background if isinstance(b, asyncio.Task) and b.get_name() == "event-queue"]
    if consumers:
        checks["queue"] = not any(t.done() for t in consumers)  # обработчик очереди упал
    ok = all(checks.values())
    body: dict = {"status": "ok" if ok else "degraded"}
    if c.settings.dev_mode:
        body |= {"checks": checks, "version": __version__, "mode": c.settings.mode, "bot": c.bot_username,
                 "db": db.dialect(), "kv": type(c.kv).__name__, "bot_ready": c.started,
                 "role": c.settings.role, "leader": bool(leader and leader.is_leader), "pool": db.pool_status(),
                 "queue": (await updates.queue.backlog()) if updates.queue is not None else None,
                 "processed": updates.queue.processed if updates.queue is not None else None}
    return JSONResponse(body, status_code=200 if ok else 503)


async def _queue_stats() -> dict | None:
    if updates.queue is None:
        return None
    try:
        return await asyncio.wait_for(updates.queue.stats(), timeout=3)
    except Exception as e:
        return {"error": str(e)}


@app.get("/health/ready")
async def health_ready():
    """Готовность принимать и обрабатывать события: база, Redis, обработчики очереди живы и очередь
    не длиннее EVENT_READY_BACKLOG. 503 — сигнал мониторингу и выкатке (новая версия не готова или обработчики
    не успевают). Балансировщику — /health: очередь общая, и по длинной очереди он снял бы все экземпляры сразу."""
    c = app.state.ctx
    checks: dict = {"db": await _db_ok(), "kv": await _kv_ok(c)}
    background = getattr(app.state, "background", [])
    consumers = [b for b in background if isinstance(b, asyncio.Task) and b.get_name() == "event-queue"]
    if consumers:
        checks["consumers"] = not any(t.done() for t in consumers)
    q = await _queue_stats()
    backlog = None
    if q is not None:
        if q.get("error"):
            checks["queue"] = False
        else:
            backlog = (q.get("pending") or 0) + (q.get("lag") or 0)
            checks["queue"] = backlog <= c.settings.event_ready_backlog
    ok = all(checks.values())
    body: dict = {"status": "ready" if ok else "not_ready", "checks": checks}
    if backlog is not None:
        body["backlog"] = backlog
    return JSONResponse(body, status_code=200 if ok else 503)


def _client_private(request: Request) -> bool:
    """Адрес клиента после разбора X-Forwarded-For доверенных прокси (uvicorn proxy_headers): запрос из
    интернета через Caddy/nginx получает публичный адрес родителя, внутри сети — приватный."""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _metrics_allowed(request: Request) -> bool:
    token = get_settings().metrics_token
    if token:
        got = request.headers.get("x-metrics-token") or ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            got = got or auth[7:].strip()
        if got and hmac.compare_digest(got.encode(), token.encode()):
            return True
    return _client_private(request)


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request):
    """Метрики Prometheus: события (принято, обработано, упало, dead-letter), время обработки, очередь и lag,
    запросы к MAX и 429, пул базы. Снаружи — только с METRICS_TOKEN; для чужих — 404, как будто адреса нет."""
    if not _metrics_allowed(request):
        raise HTTPException(404, "Not Found")
    c = app.state.ctx
    redis = getattr(c.kv, "redis", None)
    snaps = await metrics.collect(redis)
    q = await _queue_stats()
    gauges = {}
    if q is not None and not q.get("error"):
        gauges = {"peremena_queue_length": q.get("length"), "peremena_queue_pending": q.get("pending"),
                  "peremena_queue_lag": q.get("lag"), "peremena_queue_dead": q.get("dead")}
    return PlainTextResponse(metrics.render(snaps, gauges), media_type="text/plain; version=0.0.4; charset=utf-8")


SITE_DIR = BASE_DIR / "site"
# шрифты сайта лежат рядом (site/fonts); в Windows и slim-образах .woff2 может не быть в таблице типов
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/webp", ".webp")  # в slim-образе нет /etc/mime.types: без этого портреты уходят как text/plain


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(SITE_DIR / "login.html")


@app.get("/cabinet", include_in_schema=False)
async def cabinet():
    return RedirectResponse("/app/")


# Мини-приложение MAX и веб-кабинет сайта — один и тот же интерфейс
app.mount("/app", StaticFiles(directory=BASE_DIR / "webapp", html=True), name="webapp")
# Сайт: главная страница, стили, картинки
app.mount("/", StaticFiles(directory=SITE_DIR, html=True), name="site")

app.add_middleware(HttpPolicy)
# последним — значит, снаружи всех: вебхук не проходит общий стек
app.add_middleware(WebhookFastPath)

"""HTTP-сервис: вебхук MAX, API и статика мини-приложения, health-check."""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db, updates
from .bot.common import COMMANDS
from .bot.router import handle_update
from .config import BASE_DIR, get_settings
from .core import Ctx, set_ctx
from .max_api import MaxApiError, MaxClient
from .state import make_kv
from .leader import Leader, make_backend
from .updates import JOB_OPTS, Background, drain, leader_only, make_scheduler, spawn
from .webapp.api import router as api_router
from .webapp.site_api import router as site_api_router

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
    kv = await make_kv(settings.redis_url)
    api = MaxClient(settings.max_bot_token, settings.max_api_url, settings.extra_ca_file, rps=settings.max_rps)
    if kv.redis is not None:
        api.use_shared_limiter(kv.redis)  # один лимит MAX API на все экземпляры и воркеры
    c = Ctx(settings=settings, api=api, kv=kv, bot_username=settings.bot_username)
    set_ctx(c)
    background: list = []
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
        await drain()  # начатые обработки (рассылка, ответ на кнопку) доводим до конца, пока живы API и база
        await c.api.close()
        await c.kv.close()
        await db.close_db()


# Документация API открыта только в режиме разработки (DEV_AUTH без публичного адреса) — см. ниже
app = FastAPI(title="Перемена", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.include_router(api_router)
app.include_router(site_api_router)

# Что разрешено грузить страницам сайта и мини-приложения: свои файлы и мост MAX (st.max.ru).
# Инлайновые стили нужны интерфейсу (style="…" в разметке), инлайновых скриптов нет.
CSP = ("default-src 'self'; script-src 'self' https://st.max.ru; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'")
BODY_LIMIT = 1024 * 1024  # JSON-запросы и вебхук; загрузка файлов проверяет свой лимит сама
DEV_ONLY = ("/api/docs", "/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    path = request.url.path
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        length = 0
    if length > BODY_LIMIT and path != "/api/materials/upload":
        return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if path not in DEV_ONLY:  # Swagger грузит скрипты с CDN — ему CSP не ставим, он есть только в разработке
        # Мини-приложение MAX открывает во фрейме/вебвью — ему frame-ancestors не ограничиваем;
        # сайт (вход, главная) во фрейм чужого сайта не встраивается.
        csp = CSP if path == "/app" or path.startswith("/app/") else CSP + "; frame-ancestors 'self'"
        response.headers.setdefault("Content-Security-Policy", csp)
    return response


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


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Страницы, скрипты и стили сайта и мини-приложения браузер должен сверять с сервером (по ETag),
    иначе после обновления у пользователей остаётся старый код — особенно во встроенном браузере MAX."""
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and (path.endswith((".html", ".js", ".css")) or path.endswith("/")
                                         or path in ("/login", "/app")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _is_api(path: str) -> bool:
    return path.startswith(("/api/", "/webhook", "/health"))


@app.middleware("http")
async def api_404_json(request: Request, call_next):
    """Неизвестный адрес /api/... попадает в статику сайта и получил бы HTML-страницу 404 — API отвечает JSON."""
    response = await call_next(request)
    if response.status_code == 404 and _is_api(request.url.path)             and response.headers.get("content-type", "").startswith("text/html"):
        return JSONResponse({"detail": "Не найдено"}, status_code=404)
    return response


@app.exception_handler(StarletteHTTPException)
async def not_found_page(request: Request, exc: StarletteHTTPException):
    """Человеку, а не программе, на несуществующем адресе показываем страницу в стиле сайта, а не голый JSON."""
    if exc.status_code == 404 and not _is_api(request.url.path):
        return FileResponse(SITE_DIR / "404.html", status_code=404, headers={"Cache-Control": "no-cache"})
    return await http_exception_handler(request, exc)


@app.post("/webhook")
async def webhook(request: Request, x_max_bot_api_secret: str | None = Header(default=None)):
    settings = get_settings()
    expected = settings.effective_webhook_secret
    if not expected:  # без токена бота вебхук не принимаем: секрет был бы выведен из пустой строки
        raise HTTPException(503, "bot is not configured")
    # сравниваем байты: строка с не-ASCII символами в compare_digest даёт TypeError (и 500)
    if not x_max_bot_api_secret or not hmac.compare_digest(x_max_bot_api_secret.encode(), expected.encode()):
        raise HTTPException(403, "bad secret")
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(400, "bad json")
    if isinstance(update, dict) and update.get("update_type"):
        spawn(handle_update(update))
    return {"ok": True}


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
    ok = all(checks.values())
    body: dict = {"status": "ok" if ok else "degraded"}
    if c.settings.dev_mode:
        body |= {"checks": checks, "version": __version__, "mode": c.settings.mode, "bot": c.bot_username,
                 "db": db.dialect(), "kv": type(c.kv).__name__, "bot_ready": c.started,
                 "role": c.settings.role, "leader": bool(leader and leader.is_leader), "pool": db.pool_status()}
    return JSONResponse(body, status_code=200 if ok else 503)


SITE_DIR = BASE_DIR / "site"


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

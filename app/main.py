"""HTTP-сервис: вебхук MAX, API и статика мини-приложения, health-check."""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db
from .bot.common import COMMANDS
from .bot.router import handle_update
from .config import BASE_DIR, get_settings
from .core import Ctx, set_ctx
from .max_api import MaxApiError, MaxClient
from .state import make_kv
from .updates import ensure_webhook, make_scheduler, polling_loop, spawn
from .webapp.api import router as api_router
from .webapp.site_api import router as site_api_router

log = logging.getLogger("peremena")


async def startup() -> tuple[Ctx, list]:
    settings = get_settings()
    await db.init_db(settings.database_url)
    kv = await make_kv(settings.redis_url)
    api = MaxClient(settings.max_bot_token, settings.max_api_url, settings.extra_ca_file)
    c = Ctx(settings=settings, api=api, kv=kv, bot_username=settings.bot_username)
    set_ctx(c)
    background: list = []
    if not settings.max_bot_token:
        log.warning("MAX_BOT_TOKEN не задан — работает только веб-часть (API и мини-приложение)")
        return c, background
    try:
        me = await api.get_me()
        c.bot_user_id = me.get("user_id", 0)
        c.bot_username = settings.bot_username or me.get("username") or ""
        c.bot_name = me.get("name") or me.get("first_name") or c.bot_name
        log.info("Бот: %s (@%s, id=%s)", c.bot_name, c.bot_username, c.bot_user_id)
    except MaxApiError as e:
        log.error("GET /me не удался: %s — проверьте токен и доступ к %s", e, settings.max_api_url)
    try:
        await api.set_commands(COMMANDS)
    except MaxApiError as e:
        log.warning("Не удалось обновить меню команд: %s", e)

    sched = make_scheduler(c)
    sched.start()
    background.append(sched)
    if settings.mode == "webhook":
        await ensure_webhook(c, force=True)
    else:
        background.append(asyncio.create_task(polling_loop(c)))
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
    try:
        yield
    finally:
        for b in background:
            if isinstance(b, asyncio.Task):
                b.cancel()
            else:
                b.shutdown(wait=False)
        await c.api.close()
        await c.kv.close()
        await db.close_db()


app = FastAPI(title="Перемена", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
app.include_router(api_router)
app.include_router(site_api_router)


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
    if not x_max_bot_api_secret or not hmac.compare_digest(x_max_bot_api_secret, expected):
        raise HTTPException(403, "bad secret")
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(400, "bad json")
    if isinstance(update, dict) and update.get("update_type"):
        spawn(handle_update(update))
    return {"ok": True}


@app.get("/health")
async def health():
    c = app.state.ctx
    return {
        "status": "ok",
        "version": __version__,
        "mode": c.settings.mode,
        "bot": c.bot_username,
        "db": db.dialect(),
        "kv": type(c.kv).__name__,
        "bot_ready": c.started,
    }


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

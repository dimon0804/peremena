"""Настройки сервиса. Всё берётся из переменных окружения (или файла .env)."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
_PROCESS_KEY = secrets.token_bytes(32)  # ключ подписей, если нет ни SECRET_KEY, ни токена


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- MAX Bot API ---
    max_bot_token: str = ""
    max_api_url: str = "https://platform-api2.max.ru"
    # Публичное имя бота (max.ru/<имя>). Если пусто — берётся из GET /me.
    bot_username: str = ""
    # Доп. корневые сертификаты (Минцифры) — MAX просит добавить их в доверенные.
    extra_ca_file: str = str(BASE_DIR / "certs" / "russian_trusted_ca.pem")

    # --- Получение событий ---
    # Если задан WEBHOOK_URL — бот подписывается на вебхук (прод), иначе работает long polling.
    webhook_url: str = ""
    webhook_secret: str = ""
    # Режим: auto | webhook | polling
    updates_mode: str = "auto"

    # --- Хранилища ---
    database_url: str = f"sqlite+aiosqlite:///{(BASE_DIR / 'data' / 'peremena.db').as_posix()}"
    redis_url: str = ""
    # Пул соединений PostgreSQL на один процесс (воркер uvicorn). Итог на базу: экземпляры × воркеры ×
    # (DB_POOL_SIZE + DB_MAX_OVERFLOW) — должен оставаться меньше max_connections сервера.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: float = 30.0

    # --- Масштабирование ---
    # all — всё в одном процессе (как раньше); web — только HTTP (вебхук, API, сайт);
    # worker — планировщик и long polling (HTTP остаётся только для /health).
    # Планировщик и long polling работают ровно в одном процессе — лидере (замок в Redis или PostgreSQL).
    role: str = "all"
    # Процессов uvicorn в одном контейнере. Больше одного — только с Redis или PostgreSQL (нужен общий замок)
    web_concurrency: int = 1
    # Общий потолок запросов к MAX API в секунду на бота — один на все экземпляры (через Redis)
    max_rps: float = 25.0
    # Сколько событий MAX один процесс обрабатывает одновременно; остальные ждут очереди в памяти.
    # Без потолка пик в тысячи событий выбирает весь пул базы, и обработчики падают по таймауту пула.
    handler_concurrency: int = 64

    # --- Мини-приложение ---
    # Публичный https-адрес сервиса (тот же домен, что у вебхука). Нужен для ссылок на выгрузку.
    public_url: str = ""
    # Показывать кнопки «Открыть приложение». Пустое значение — включено, если задан PUBLIC_URL или WEBHOOK_URL
    # (без HTTPS-домена мини-приложение на платформе MAX не подключить, а кнопка на него ломает сообщение).
    miniapp_enabled: bool | None = None
    # Разрешить вход в мини-приложение из обычного браузера по ?dev_user=<id> (только для разработки!)
    dev_auth: bool = False
    init_data_ttl_hours: int = 24
    # Демо-режим «попробовать без регистрации»: песочница с вымышленным классом, ничего не уходит в MAX
    demo_enabled: bool = True
    demo_per_ip_hour: int = 10
    demo_max_sandboxes: int = 1000  # живых песочниц одновременно (каждая — около 150 строк в базе)

    # --- Прочее ---
    default_tz: str = "Europe/Moscow"
    host: str = "0.0.0.0"
    port: int = 8080
    # Чьим заголовкам X-Forwarded-For/-Proto верить (адреса и сети через запятую): локальный прокси
    # и внутренние сети Docker, где живёт Caddy. «*» — верить всем, тогда любой подделает свой IP.
    forwarded_allow_ips: str = "127.0.0.1,::1,172.16.0.0/12"
    log_level: str = "INFO"
    # Ключ подписей сервиса. Пусто — выводится из MAX_BOT_TOKEN (а без токена — случайный при каждом запуске)
    secret_key: str = ""

    @property
    def mode(self) -> str:
        if self.updates_mode in ("webhook", "polling"):
            return self.updates_mode
        return "webhook" if self.webhook_url else "polling"

    @property
    def runs_background(self) -> bool:
        """Этот процесс претендует на роль лидера: планировщик и long polling."""
        return self.role.strip().lower() in ("all", "worker", "")

    @property
    def miniapp(self) -> bool:
        if self.miniapp_enabled is not None:
            return self.miniapp_enabled
        return bool(self.public_url or self.webhook_url)

    @property
    def is_public(self) -> bool:
        """Сервис смотрит в интернет (задан публичный адрес или вебхук)."""
        return bool(self.public_url or self.webhook_url)

    @property
    def dev_mode(self) -> bool:
        """DEV_AUTH действует только без публичного адреса: в проде вход по ?dev_user=<id> — это вход под любым."""
        return self.dev_auth and not self.is_public

    @property
    def signing_key(self) -> bytes:
        """Ключ подписей (ссылки на выгрузку): SECRET_KEY, иначе выводится из токена. Без токена и SECRET_KEY —
        случайный на время работы процесса: из пустой строки вывели бы ключ, известный всем."""
        if self.secret_key:
            raw = self.secret_key
        elif self.max_bot_token:
            raw = "peremena:" + self.max_bot_token
        else:
            return _PROCESS_KEY
        return hashlib.sha256(raw.encode()).digest()

    @property
    def effective_webhook_secret(self) -> str:
        """Секрет вебхука. Пустая строка — вебхук выключен (нет токена: секрет из пустого токена публичен)."""
        if not self.max_bot_token:
            return ""
        if self.webhook_secret and re.fullmatch(r"[\w-]{5,256}", self.webhook_secret, re.ASCII):
            return self.webhook_secret
        if self.webhook_secret:
            logging.getLogger(__name__).error(
                "WEBHOOK_SECRET должен быть 5–256 символов A-Z a-z 0-9 _ - — использую секрет, выведенный из токена")
        # Детерминированный секрет из токена: 5–256 символов [A-Za-z0-9_-]
        return hashlib.sha256(("wh:" + self.max_bot_token).encode()).hexdigest()[:40]


@lru_cache
def get_settings() -> Settings:
    return Settings()

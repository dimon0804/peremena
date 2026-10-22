"""Настройки сервиса. Всё берётся из переменных окружения (или файла .env)."""
from __future__ import annotations

import hashlib
import logging
import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


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

    # --- Мини-приложение ---
    # Публичный https-адрес сервиса (тот же домен, что у вебхука). Нужен для ссылок на выгрузку.
    public_url: str = ""
    # Показывать кнопки «Открыть приложение». Пустое значение — включено, если задан PUBLIC_URL или WEBHOOK_URL
    # (без HTTPS-домена мини-приложение на платформе MAX не подключить, а кнопка на него ломает сообщение).
    miniapp_enabled: bool | None = None
    # Разрешить вход в мини-приложение из обычного браузера по ?dev_user=<id> (только для разработки!)
    dev_auth: bool = False
    init_data_ttl_hours: int = 24

    # --- Прочее ---
    default_tz: str = "Europe/Moscow"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    secret_key: str = ""

    @property
    def mode(self) -> str:
        if self.updates_mode in ("webhook", "polling"):
            return self.updates_mode
        return "webhook" if self.webhook_url else "polling"

    @property
    def miniapp(self) -> bool:
        if self.miniapp_enabled is not None:
            return self.miniapp_enabled
        return bool(self.public_url or self.webhook_url)

    @property
    def signing_key(self) -> bytes:
        raw = self.secret_key or ("peremena:" + self.max_bot_token)
        return hashlib.sha256(raw.encode()).digest()

    @property
    def effective_webhook_secret(self) -> str:
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

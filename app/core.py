"""Общий контекст бота: клиент API, хранилище состояния, настройки и сведения о самом боте."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Settings
from .max_api import MaxApiError, MaxClient
from .state import KV

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    settings: Settings
    api: MaxClient
    kv: KV
    bot_user_id: int = 0
    bot_username: str = ""
    bot_name: str = "Перемена"
    started: bool = field(default=False)

    def start_link(self, payload: str | None = None) -> str:
        base = f"https://max.ru/{self.bot_username}" if self.bot_username else "https://max.ru"
        return f"{base}?start={payload}" if payload else base

    def app_link(self, payload: str | None = None) -> str:
        base = f"https://max.ru/{self.bot_username}" if self.bot_username else "https://max.ru"
        return f"{base}?startapp={payload}" if payload else f"{base}?startapp"

    async def safe_send(self, **kwargs) -> dict | None:
        """send_message, который не роняет обработчик: ошибку пишем в лог и возвращаем None."""
        try:
            return await self.api.send_message(**kwargs)
        except MaxApiError as e:
            log.warning("Не удалось отправить сообщение (%s): %s", {k: kwargs.get(k) for k in ("user_id", "chat_id")}, e)
            return None


_ctx: Ctx | None = None


def set_ctx(ctx: Ctx) -> None:
    global _ctx
    _ctx = ctx


def ctx() -> Ctx:
    assert _ctx is not None, "контекст бота не инициализирован"
    return _ctx

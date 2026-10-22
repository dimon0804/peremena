"""Построение inline-клавиатур MAX (вложение ``inline_keyboard``)."""
from __future__ import annotations

from typing import Any

Button = dict[str, Any]


def cb(text: str, payload: str) -> Button:
    return {"type": "callback", "text": text[:128], "payload": payload[:1024]}


def link(text: str, url: str) -> Button:
    return {"type": "link", "text": text[:128], "url": url}


def app(text: str, bot_username: str, payload: str | None = None) -> Button | None:
    """Кнопка мини-приложения. None, если мини-приложение не подключено, — такие кнопки отбрасываются."""
    from .core import ctx

    if not bot_username or not ctx().settings.miniapp:
        return None
    btn: Button = {"type": "open_app", "text": text[:128], "web_app": bot_username}
    if payload:
        btn["payload"] = payload
    return btn


def _clean(rows) -> list[list[Button]]:
    out = []
    for r in rows:
        r = [b for b in (r or []) if b]
        if r:
            out.append(r)
    return out


def keyboard(*rows: list[Button] | None) -> dict:
    return {"type": "inline_keyboard", "payload": {"buttons": _clean(rows)}}


def kb_list(rows: list[list[Button]]) -> list[dict]:
    """Готовый список attachments для send_message."""
    rows = _clean(rows)
    return [keyboard(*rows)] if rows else []

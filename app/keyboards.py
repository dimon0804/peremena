"""Построение inline-клавиатур MAX (вложение ``inline_keyboard``)."""
from __future__ import annotations

from typing import Any

Button = dict[str, Any]


def cb(text: str, payload: str) -> Button:
    return {"type": "callback", "text": text[:128], "payload": payload[:1024]}


def link(text: str, url: str) -> Button:
    return {"type": "link", "text": text[:128], "url": url}


def app(text: str, bot_username: str, payload: str | None = None) -> Button:
    btn: Button = {"type": "open_app", "text": text[:128], "web_app": bot_username}
    if payload:
        btn["payload"] = payload
    return btn


def keyboard(*rows: list[Button] | None) -> dict:
    clean = [r for r in rows if r]
    return {"type": "inline_keyboard", "payload": {"buttons": clean}}


def kb_list(rows: list[list[Button]]) -> list[dict]:
    """Готовый список attachments для send_message."""
    rows = [r for r in rows if r]
    return [keyboard(*rows)] if rows else []

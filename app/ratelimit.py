"""Простые лимиты частоты: счётчик в KV на окно фиксированной длины.

Не атомарный (get + set), но для защиты от перебора этого хватает: гонка даёт лишние
единицы попыток, а не тысячи.
"""
from __future__ import annotations

import time

from .core import ctx


async def hit(name: str, who: object, limit: int, window_s: int) -> bool:
    """Засчитывает попытку. False — лимит на это окно исчерпан (попытка не засчитана)."""
    bucket = int(time.time() // window_s)
    key = f"rl:{name}:{who}:{bucket}"
    kv = ctx().kv
    used = int(await kv.get(key) or 0)
    if used >= limit:
        return False
    await kv.set(key, used + 1, window_s + 5)
    return True

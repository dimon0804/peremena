"""Диапазон id демо-режима. Вынесен отдельно: им пользуется и клиент MAX API (чтобы ничего не отправлять
демо-«родителям»), и сервисы — без циклических импортов.

Реальные id в MAX: пользователи — до ~10⁹, чаты — около −10¹³. Демо: 9 100 000 000 … 9 199 999 999
(чаты — те же числа со знаком минус). Пересечения нет.
"""
from __future__ import annotations

DEMO_BASE = 9_100_000_000
DEMO_SPAN = 100_000_000
SANDBOX_SIZE = 1_000  # id внутри одной песочницы: base + 0 … base + 999
# Номера песочниц 1 … SLOTS-1. Номера освободившихся песочниц используются снова, поэтому id никогда
# не выходят за демо-диапазон (иначе песочница стала бы «настоящей» и бот писал бы в MAX).
SLOTS = DEMO_SPAN // SANDBOX_SIZE


def is_demo(x) -> bool:
    try:
        return DEMO_BASE <= abs(int(x)) < DEMO_BASE + DEMO_SPAN
    except (TypeError, ValueError):
        return False


def is_demo_mid(mid) -> bool:
    return isinstance(mid, str) and mid.startswith("demo.")


def sandbox_base(sandbox_id: int) -> int:
    if not 0 <= sandbox_id < SLOTS:
        raise ValueError(f"номер песочницы {sandbox_id} вне демо-диапазона")
    return DEMO_BASE + sandbox_id * SANDBOX_SIZE


def sandbox_id_of(x: int) -> int:
    return (abs(int(x)) - DEMO_BASE) // SANDBOX_SIZE

"""Состав классов для нагрузки — по формуле, одинаково в поддельном MAX, засеве базы и генераторе.

Класс i (0, 1, 2, …): чат CHAT0 - i, учитель TEACHER0 + i, родители PARENT0 + i*100 + j (j < PARENTS).
Школа s объединяет классы [s*CLASSES_PER_SCHOOL, (s+1)*CLASSES_PER_SCHOOL); её администратор — ADMIN0 + s.
"""
from __future__ import annotations

BOT_ID = 9_000_000
CHAT0 = -1_000_000
TEACHER0 = 1_000_000
PARENT0 = 10_000_000
ADMIN0 = 5_000_000
PARENTS = 30
CLASSES_PER_SCHOOL = 10


def chat_id(i: int) -> int:
    return CHAT0 - i


def class_index(chat: int) -> int:
    return CHAT0 - chat


def teacher_id(i: int) -> int:
    return TEACHER0 + i


def parent_id(i: int, j: int) -> int:
    return PARENT0 + i * 100 + j


def parent(i: int, j: int) -> dict:
    """Родитель как его присылает MAX в событиях — с тем же именем, что в списке участников."""
    return {"user_id": parent_id(i, j), "first_name": "Родитель", "last_name": f"{i}-{j}", "is_bot": False}


def parent_by_id(uid: int) -> dict:
    i, j = divmod(uid - PARENT0, 100)
    return parent(i, j)


def admin_id(school: int) -> int:
    return ADMIN0 + school


def chat_title(chat: int) -> str:
    i = class_index(chat)
    return f"{i // CLASSES_PER_SCHOOL}-{i % CLASSES_PER_SCHOOL + 1}Б Родители" if i >= 0 else "Чат"


def members(chat: int) -> list[dict]:
    i = class_index(chat)
    if i < 0:
        return []
    out = [{"user_id": teacher_id(i), "first_name": "Учитель", "last_name": f"№{i}", "is_owner": True,
            "is_admin": True, "is_bot": False}]
    out += [{"user_id": parent_id(i, j), "first_name": "Родитель", "last_name": f"{i}-{j}", "is_owner": False,
             "is_admin": False, "is_bot": False} for j in range(PARENTS)]
    out.append({"user_id": BOT_ID, "first_name": "Перемена", "is_bot": True, "is_admin": True, "is_owner": False})
    return out


def route(path: str) -> str:
    """/chats/-1000003/members → /chats/{id}/members — для счётчиков по методам."""
    parts = path.split("/")
    return "/".join("{id}" if p.lstrip("-").isdigit() else p for p in parts)

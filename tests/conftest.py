"""Офлайн-окружение: SQLite во временной папке, состояние в памяти, поддельный MAX API."""
from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import Settings  # noqa: E402
from app.core import Ctx, set_ctx  # noqa: E402
from app.state import MemoryKV  # noqa: E402

TEACHER = {"user_id": 100, "first_name": "Мария", "last_name": "Петрова", "is_bot": False}
PARENTS = [
    {"user_id": 201, "first_name": "Анна", "last_name": "Иванова", "is_bot": False},
    {"user_id": 202, "first_name": "Олег", "last_name": "Сидоров", "is_bot": False},
    {"user_id": 203, "first_name": "Ирина", "last_name": "Козлова", "is_bot": False},
]
BOT = {"user_id": 999, "first_name": "Перемена", "is_bot": True}
CHAT_ID = -5000
TEACHER2 = {"user_id": 110, "first_name": "Игорь", "last_name": "Смирнов", "is_bot": False}
PARENT2 = {"user_id": 301, "first_name": "Ольга", "last_name": "Морозова", "is_bot": False}
SUBJ = {"user_id": 120, "first_name": "Елена", "last_name": "Орлова", "is_bot": False}
CHAT2_ID = -6000


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.answers: list[dict] = []
        self.edits: list[dict] = []
        self._mid = itertools.count(1)
        self.blocked: set[int] = set()

    async def get_me(self):
        return {"user_id": BOT["user_id"], "username": "peremena_test_bot", "name": "Перемена"}

    async def set_commands(self, commands):
        return {}

    async def send_message(self, **kw):
        from app.max_api import MaxApiError

        if kw.get("user_id") in self.blocked:
            raise MaxApiError(403, "chat.denied", "bot not started")
        self.sent.append(kw)
        return {"body": {"mid": f"mid.{next(self._mid)}"}}

    async def edit_message(self, message_id, **kw):
        self.edits.append({"message_id": message_id, **kw})
        return {"success": True}

    async def answer_callback(self, callback_id, notification=None, message=None):
        self.answers.append({"callback_id": callback_id, "notification": notification, "message": message})
        return {"success": True}

    async def get_chat(self, chat_id):
        return {"chat_id": chat_id, "title": "9Б Родители" if chat_id == CHAT_ID else "9А Родители", "type": "chat"}

    async def get_members(self, chat_id, limit=2000):
        if chat_id == CHAT2_ID:
            return [dict(TEACHER2, is_owner=True, is_admin=True), dict(PARENT2, is_owner=False, is_admin=False),
                    dict(BOT, is_admin=True)]
        owner = dict(TEACHER, is_owner=True, is_admin=True)
        return [owner, *[dict(p, is_owner=False, is_admin=False) for p in PARENTS], dict(BOT, is_admin=True)]

    async def delete_message(self, message_id):
        return {"success": True}

    async def get_my_membership(self, chat_id):
        return {"user_id": BOT["user_id"], "is_admin": True, "is_owner": False}

    async def get_messages(self, chat_id, count=100, before=None):
        return {"messages": []}

    async def get_subscriptions(self):
        return []

    async def close(self):
        pass

    def texts_to(self, user_id=None, chat_id=None) -> list[str]:
        return [s.get("text") or "" for s in self.sent
                if (user_id and s.get("user_id") == user_id) or (chat_id and s.get("chat_id") == chat_id)]


@pytest.fixture
def env(tmp_path):
    loop = asyncio.new_event_loop()
    settings = Settings(max_bot_token="test-token", database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
                        dev_auth=True, bot_username="peremena_test_bot")
    api = FakeApi()
    c = Ctx(settings=settings, api=api, kv=MemoryKV(), bot_user_id=BOT["user_id"], bot_username="peremena_test_bot")
    set_ctx(c)
    loop.run_until_complete(db.init_db(settings.database_url))
    yield loop, api, c
    loop.run_until_complete(db.close_db())
    loop.close()


def msg_update(user: dict, text: str, chat_id: int | None = None, mid: str = "x", attachments=None, link=None) -> dict:
    recipient = {"chat_type": "chat", "chat_id": chat_id} if chat_id else {"chat_type": "dialog", "chat_id": 1, "user_id": BOT["user_id"]}
    body = {"mid": mid, "seq": 1, "text": text}
    if attachments:
        body["attachments"] = attachments
    msg = {"sender": user, "recipient": recipient, "timestamp": 1, "body": body}
    if link:
        msg["link"] = link
    return {"update_type": "message_created", "timestamp": 1, "message": msg}


def cb_update(user: dict, payload: str, cid: str, in_group: bool = False) -> dict:
    recipient = {"chat_type": "chat", "chat_id": CHAT_ID} if in_group else {"chat_type": "dialog", "chat_id": 1}
    return {"update_type": "message_callback", "timestamp": 1,
            "callback": {"timestamp": 1, "callback_id": cid, "payload": payload, "user": user},
            "message": {"recipient": recipient, "body": {"mid": "m", "seq": 1}, "timestamp": 1}}

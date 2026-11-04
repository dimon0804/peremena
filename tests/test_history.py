"""Импорт файлов из истории чата: листание через from, без зацикливания на одной странице."""
import asyncio

import httpx

from app.bot.router import handle_update
from app.max_api import MaxClient
from app.services import shelf

from .conftest import CHAT_ID, TEACHER


def run(loop, coro):
    return loop.run_until_complete(coro)


def page(start, n=100):
    return [{"sender": {"user_id": TEACHER["user_id"]}, "timestamp": 10_000 - i,
             "body": {"mid": f"m{i}", "text": "",
                      "attachments": [{"type": "file", "filename": f"f{i}.pdf", "payload": {"token": f"t{i}"}}]}}
            for i in range(start, start + n)]


def test_history_paging_stops_when_page_repeats(env):
    loop, api, c = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    calls = []

    async def same_page(chat_id, count=100, before=None):
        calls.append(before)
        return {"messages": page(0)}  # сервер игнорирует параметр — отдаёт одно и то же
    api.get_messages = same_page
    added = run(loop, shelf.import_history(CHAT_ID, TEACHER["user_id"], pages=5))
    assert added == 100 and len(calls) == 2


def test_get_messages_uses_from_param():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(request.url.params)
        return httpx.Response(200, json={"messages": []})
    c = MaxClient("t", "https://max.test")
    c._http = httpx.AsyncClient(base_url="https://max.test", transport=httpx.MockTransport(handler))
    asyncio.run(c.get_messages(5, count=100, before=123))
    assert seen.get("from") == "123" and "before" not in seen


def test_history_imported_when_bot_becomes_admin(env):
    loop, api, c = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))

    async def one_page(chat_id, count=100, before=None):
        return {"messages": page(0, 3)}
    api.get_messages = one_page
    run(loop, handle_update({"update_type": "bot_admin_permissions_changed", "timestamp": 2, "chat_id": CHAT_ID}))
    assert len(run(loop, shelf.list_for_teacher(TEACHER["user_id"]))) == 3

"""Поддельный MAX Bot API для нагрузочных прогонов — быстрый, в памяти, без сети наружу.

    python loadtest/fake_max.py --port 59100 --limit 30

* Отвечает на всё, чем пользуется бот: /me, /messages, /answers, /chats/{id}, /chats/{id}/members, /subscriptions…
* Списки участников — по формуле (см. loadtest/roster.py): чат класса i — учитель, 30 родителей и бот.
* --limit N — потолок запросов в секунду на бота, как у настоящего MAX: сверх него 429 с Retry-After.
  Так видно, держат ли несколько экземпляров бота общий лимит.
* Служебное: GET /_stats — счётчики, пиковый RPS по секундам, число 429; GET /_sent — что и кому отправлено;
  POST /_reset — обнулить.
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roster  # noqa: E402

app = FastAPI()
LIMIT = 0.0  # 0 — без лимита
LATENCY = 0.0


class Stats:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: Counter = Counter()
        self.too_many = 0
        self.per_second: Counter = Counter()  # секунда (int) → принятых запросов
        self.window: deque = deque()          # моменты принятых запросов за последнюю секунду
        self.sent: list[dict] = []            # POST /messages: кому, payload кнопок
        self.answers: Counter = Counter()     # callback_id → сколько раз ответили
        self.answer_t: dict[str, float] = {}  # callback_id → когда ответили впервые (задержка нажатие → ответ)
        self.edits = 0
        self.started = time.time()


S = Stats()
_mid = itertools.count(1)


def _admit() -> bool:
    """Скользящее окно в 1 с: сверх LIMIT — 429 (как настоящий MAX при превышении)."""
    now = time.monotonic()
    w = S.window
    while w and now - w[0] >= 1.0:
        w.popleft()
    if LIMIT and len(w) >= LIMIT:
        S.too_many += 1
        return False
    w.append(now)
    S.per_second[int(time.time())] += 1
    return True


@app.middleware("http")
async def limiter(request: Request, call_next):
    path = request.url.path
    if path.startswith("/_"):
        return await call_next(request)
    if not _admit():
        return JSONResponse({"code": "too.many.requests", "message": "rate limit"}, status_code=429,
                            headers={"Retry-After": "1"})
    S.calls[f"{request.method} {roster.route(path)}"] += 1
    if LATENCY:
        import asyncio

        await asyncio.sleep(LATENCY)
    return await call_next(request)


@app.get("/me")
async def me():
    return {"user_id": roster.BOT_ID, "username": "peremena_load_bot", "name": "Перемена", "is_bot": True}


@app.patch("/me/commands")
async def commands():
    return {"success": True}


@app.post("/messages")
async def send(request: Request):
    body = await request.json()
    q = request.query_params
    payloads = [b.get("payload") for a in body.get("attachments") or [] if a.get("type") == "inline_keyboard"
                for row in (a.get("payload") or {}).get("buttons") or [] for b in row if b.get("payload")]
    mid = f"mid.{next(_mid)}"
    S.sent.append({"user_id": int(q["user_id"]) if q.get("user_id") else None,
                   "chat_id": int(q["chat_id"]) if q.get("chat_id") else None,
                   "text": (body.get("text") or "")[:80], "payloads": payloads, "t": time.time(), "mid": mid})
    return {"message": {"body": {"mid": mid, "text": body.get("text")}}}


@app.put("/messages")
async def edit():
    S.edits += 1
    return {"success": True}


@app.delete("/messages")
async def delete():
    return {"success": True}


@app.get("/messages")
async def history():
    return {"messages": []}


@app.post("/answers")
async def answer(request: Request):
    cid = request.query_params.get("callback_id", "")
    S.answers[cid] += 1
    S.answer_t.setdefault(cid, time.time())
    return {"success": True}


@app.get("/chats/{chat_id}")
async def chat(chat_id: int):
    return {"chat_id": chat_id, "type": "chat", "title": roster.chat_title(chat_id)}


@app.get("/chats/{chat_id}/members/me")
async def membership(chat_id: int):
    return {"user_id": roster.BOT_ID, "is_admin": True, "is_owner": False}


@app.get("/chats/{chat_id}/members")
async def members(chat_id: int):
    return {"members": roster.members(chat_id), "marker": None}


@app.get("/subscriptions")
async def subs():
    return {"subscriptions": []}


@app.post("/subscriptions")
async def subscribe():
    return {"success": True}


@app.delete("/subscriptions")
async def unsubscribe():
    return {"success": True}


@app.get("/updates")
async def updates(timeout: int = 30):
    import asyncio

    await asyncio.sleep(min(timeout, 5))
    return {"updates": [], "marker": 1}


# ---------------------------------------------------------------- служебное
@app.get("/_stats")
async def stats():
    secs = sorted(S.per_second.items())
    return {"calls": dict(S.calls), "too_many": S.too_many, "sent": len(S.sent), "edits": S.edits,
            "answers_total": sum(S.answers.values()), "answers_unique": len(S.answers),
            "answers_dup": sum(1 for v in S.answers.values() if v > 1),
            "peak_rps": max((n for _, n in secs), default=0), "per_second": secs[-120:],
            "limit": LIMIT}


@app.get("/_sent")
async def sent(since: float = 0):
    return [m for m in S.sent if m["t"] >= since]


@app.get("/_answers")
async def answers():
    return dict(S.answers)


@app.get("/_answer_times")
async def answer_times():
    return S.answer_t


@app.post("/_reset")
async def reset():
    S.reset()
    return {"ok": True}


def main() -> None:
    global LIMIT, LATENCY
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=59100)
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 — в контейнере стенда")
    p.add_argument("--limit", type=float, default=30.0, help="запросов в секунду на бота; 0 — без лимита")
    p.add_argument("--latency", type=float, default=0.01, help="задержка ответа, с (сеть до MAX)")
    a = p.parse_args()
    LIMIT, LATENCY = a.limit, a.latency
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

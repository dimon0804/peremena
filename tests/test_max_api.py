"""Клиент MAX API: повторы без дублей, 429 с Retry-After, ограничитель не тормозит всех из-за одного."""
import asyncio
import time

import httpx
import pytest

from app.max_api import MaxApiError, MaxClient, _RateLimiter


def client_with(handler) -> MaxClient:
    c = MaxClient("token", "https://max.test")
    c._http = httpx.AsyncClient(base_url="https://max.test", transport=httpx.MockTransport(handler))
    return c


def scripted(*steps):
    """Обработчик, который по очереди отдаёт ответы или бросает исключения; считает вызовы."""
    calls = []

    def handler(request: httpx.Request):
        step = steps[min(len(calls), len(steps) - 1)]
        calls.append(request)
        if isinstance(step, Exception):
            raise step
        status, headers = step if isinstance(step, tuple) else (step, {})
        return httpx.Response(status, json={"message": {"body": {"mid": "m1"}}} if status == 200 else {},
                              headers=headers)
    return handler, calls


def send(c: MaxClient):
    return asyncio.run(c.send_message(user_id=1, text="Сбор"))


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), 503, 500])
def test_message_is_not_resent_when_it_may_have_been_delivered(failure):
    handler, calls = scripted(failure, 200)
    with pytest.raises(MaxApiError):
        send(client_with(handler))
    assert len(calls) == 1  # повтор после таймаута/5xx = дубль у родителя


def test_message_is_retried_when_connection_failed():
    handler, calls = scripted(httpx.ConnectError("refused"), 200)
    assert send(client_with(handler))["body"]["mid"] == "m1"
    assert len(calls) == 2


def test_idempotent_get_is_retried_on_5xx():
    handler, calls = scripted(503, 503, 200)
    c = client_with(handler)
    asyncio.run(c.get_chat(5))
    assert len(calls) == 3


def test_429_waits_retry_after_for_everyone():
    handler, calls = scripted((429, {"Retry-After": "0.6"}), 200)
    c = client_with(handler)
    t0 = time.monotonic()
    send(c)
    assert len(calls) == 2 and time.monotonic() - t0 >= 0.55

    async def other_request_waits_too():
        c._limiter.pause(0.4)
        t = time.monotonic()
        await c._limiter.wait("u2")
        return time.monotonic() - t
    assert asyncio.run(other_request_waits_too()) >= 0.35


def test_per_peer_pause_does_not_block_others():
    async def go():
        lim = _RateLimiter()
        await lim.wait("u1")
        slow = asyncio.create_task(lim.wait("u1"))  # второе сообщение тому же — ждёт ~0.55 с
        await asyncio.sleep(0.01)
        t = time.monotonic()
        await lim.wait("u2")  # другому адресату — сразу
        fast = time.monotonic() - t
        await slow
        return fast
    assert asyncio.run(go()) < 0.2

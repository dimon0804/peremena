"""Маршрутизатор событий MAX: одинаково работает для вебхука и long polling."""
from __future__ import annotations

import asyncio
import logging

from .. import keyboards as kb
from ..core import ctx
from ..services import chats, polls
from . import callbacks, dialog, group, staff
from .common import send

log = logging.getLogger(__name__)


def _dedup_key(update: dict) -> str:
    t = update.get("update_type", "")
    if t == "message_callback":
        return f"cb:{(update.get('callback') or {}).get('callback_id')}"
    mid = ((update.get("message") or {}).get("body") or {}).get("mid")
    if mid:
        return f"msg:{t}:{mid}"
    return f"ev:{t}:{update.get('timestamp')}:{update.get('chat_id')}:{(update.get('user') or {}).get('user_id')}"


_gate: tuple[object, asyncio.Semaphore] | None = None


def gate() -> asyncio.Semaphore:
    """Потолок одновременно обрабатываемых событий в процессе (HANDLER_CONCURRENCY): пик в тысячи
    событий ждёт очереди здесь, а не в пуле соединений базы, где обработчик упал бы по таймауту."""
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:
        _gate = (loop, asyncio.Semaphore(max(1, ctx().settings.handler_concurrency)))
    return _gate[1]


def _actor_key(update: dict) -> str:
    """Чьё это событие: пользователь (личка, кнопка) или чат (подключение бота, участники)."""
    uid = ((update.get("callback") or {}).get("user") or {}).get("user_id") \
        or ((update.get("message") or {}).get("sender") or {}).get("user_id") \
        or (update.get("user") or {}).get("user_id")
    if update.get("update_type") in ("bot_added", "bot_removed", "bot_admin_permissions_changed"):
        return f"c{update.get('chat_id')}"
    return f"u{uid}" if uid else f"c{update.get('chat_id')}"


async def handle_update(update: dict) -> None:
    c = ctx()
    key_dd = "dd:" + _dedup_key(update)
    try:
        fresh = await c.kv.add_once(key_dd, ttl=24 * 3600)
    except Exception:
        # хранилище состояния недоступно: лучше обработать событие без дедупликации, чем молча потерять
        # (и оставить кнопку у пользователя «нажатой» без ответа)
        log.exception("Дедупликация недоступна — обрабатываем событие без неё")
        fresh, key_dd = True, None
    if not fresh:
        return  # MAX повторяет доставку, пока не получит 200 — дубликаты пропускаем
    # События одного человека обрабатываем по очереди: иначе двойное нажатие «Разослать»
    # создаёт два сбора, а первое сообщение нового пользователя дублирует запись в базе.
    # Замок общий для всех экземпляров (Redis): два нажатия одного человека могут прийти в разные процессы.
    try:
        async with c.kv.lock("actor:" + _actor_key(update), ttl=60, wait=60):
            async with gate():
                await _dispatch(update)
    except asyncio.CancelledError:
        # остановка сервиса посреди обработки: снимаем отметку «обработано», чтобы повторная доставка
        # после перезапуска не была отброшена как дубликат
        if key_dd:
            try:
                await c.kv.delete(key_dd)
            except Exception:
                pass
        raise


async def _dispatch(update: dict) -> None:
    t = update.get("update_type")
    log.debug("Событие MAX: %s", update)
    try:
        if t == "message_created":
            chat_type = ((update.get("message") or {}).get("recipient") or {}).get("chat_type")
            if chat_type == "dialog":
                await dialog.on_message(update)
            elif chat_type == "chat":
                await group.on_message(update)
        elif t == "message_callback":
            await callbacks.on_callback(update)
        elif t == "bot_started":
            await on_bot_started(update)
        elif t == "bot_added":
            await group.on_bot_added(update)
        elif t == "bot_removed":
            await group.on_bot_removed(update)
        elif t == "user_added":
            await group.on_user_added(update)
        elif t == "user_removed":
            await group.on_user_removed(update)
        elif t == "chat_title_changed":
            await group.on_title_changed(update)
        elif t == "bot_admin_permissions_changed":
            await group.on_admin_changed(update)
    except Exception:
        log.exception("Ошибка обработки события %s", t)


async def on_bot_started(update: dict) -> None:
    """Пользователь нажал «Начать» (в т.ч. по ссылке max.ru/<бот>?start=<payload>)."""
    user = update.get("user") or {}
    uid = user.get("user_id")
    if not uid:
        return
    await chats.upsert_user(user, dialog_open=True)
    payload = update.get("payload") or ""
    if await staff.handle_start_payload(uid, payload):
        return
    if payload.startswith("poll_") and payload[5:].isdigit():
        poll = await polls.get_poll(int(payload[5:]))
        # ссылку могли переслать постороннему — сбор показываем только тем, кому он адресован
        if poll and not poll.closed and await polls.is_addressee(poll, uid):
            chat = await chats.get_chat(poll.chat_id)
            teacher = await chats.get_user(poll.teacher_id)
            tz = teacher.tz if teacher else ctx().settings.default_tz
            await send(uid, polls.parent_text(poll, tz, chat.title if chat else "",
                                              teacher.display_name if teacher else ""),
                       polls.answer_buttons(poll, await polls.user_answer(poll, uid)))
            return
    await dialog.show_start(uid)
    # родителю сразу показываем сборы, которые ждут ответа
    if not await chats.is_teacher(uid):
        pending = await polls.pending_for_user(uid)
        if pending:
            await send(uid, f"📋 Вас ждут сборы: {len(pending)}. Ответить:",
                       [[kb.cb(f"{p.title[:50]}", "m:my")] for p in pending[:1]])


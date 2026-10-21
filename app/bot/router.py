"""Маршрутизатор событий MAX: одинаково работает для вебхука и long polling."""
from __future__ import annotations

import logging

from .. import keyboards as kb
from ..core import ctx
from ..services import chats, polls
from . import callbacks, dialog, group
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


async def handle_update(update: dict) -> None:
    c = ctx()
    if not await c.kv.add_once("dd:" + _dedup_key(update), ttl=24 * 3600):
        return  # MAX повторяет доставку, пока не получит 200 — дубликаты пропускаем
    t = update.get("update_type")
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
    if payload.startswith("poll_") and payload[5:].isdigit():
        poll = await polls.get_poll(int(payload[5:]))
        if poll and not poll.closed:
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


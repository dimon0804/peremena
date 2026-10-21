"""Общие вещи для обработчиков: состояние диалога, меню, ответ на нажатие кнопки."""
from __future__ import annotations

import logging
from typing import Any

from .. import keyboards as kb
from ..core import ctx
from ..max_api import MaxApiError

log = logging.getLogger(__name__)

STATE_TTL = 60 * 60 * 2

COMMANDS = [
    ("start", "Главное меню"),
    ("poll", "Новый сбор (перекличка)"),
    ("status", "Статус сборов"),
    ("faq", "Справочник: что я отвечаю"),
    ("add", "Добавить ответ в справочник"),
    ("shelf", "Полка: найти материал"),
    ("quiet", "Тихий режим и рабочее время"),
    ("morning", "Утренняя сводка сейчас"),
    ("stats", "Счётчик пользы"),
    ("chats", "Мои классы"),
    ("app", "Открыть приложение"),
    ("cancel", "Отменить текущее действие"),
    ("help", "Помощь"),
]

# русские синонимы команд из анкеты проекта
ALIASES = {
    "start": "start", "старт": "start", "меню": "start", "help": "help", "помощь": "help",
    "poll": "poll", "сбор": "poll", "перекличка": "poll", "sbor": "poll",
    "status": "status", "статус": "status",
    "faq": "faq", "справочник": "faq", "что_я_отвечаю": "faq", "удалить": "faq",
    "add": "add", "добавить": "add",
    "shelf": "shelf", "полка": "shelf", "найти": "shelf",
    "quiet": "quiet", "тихий": "quiet", "тихий_режим": "quiet",
    "morning": "morning", "утро": "morning", "сводка": "morning",
    "stats": "stats", "польза": "stats",
    "chats": "chats", "классы": "chats",
    "app": "app", "приложение": "app",
    "cancel": "cancel", "отмена": "cancel",
    "my": "my", "мои": "my",
}


def parse_command(text: str) -> tuple[str | None, str]:
    t = (text or "").strip()
    if not t.startswith("/"):
        return None, t
    head, _, rest = t[1:].partition(" ")
    head = head.split("@", 1)[0].lower()
    return ALIASES.get(head, head), rest.strip()


# ------------------------------------------------------------------ состояние диалога
async def get_state(user_id: int) -> dict | None:
    return await ctx().kv.get(f"st:{user_id}")


async def set_state(user_id: int, flow: str, step: str, data: dict | None = None) -> None:
    await ctx().kv.set(f"st:{user_id}", {"flow": flow, "step": step, "data": data or {}}, STATE_TTL)


async def clear_state(user_id: int) -> None:
    await ctx().kv.delete(f"st:{user_id}")


# ------------------------------------------------------------------ отправка
async def send(user_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict | None:
    return await ctx().safe_send(user_id=user_id, text=text, attachments=kb.kb_list(buttons or []))


async def ack(callback_id: str, notification: str | None = None, message: dict[str, Any] | None = None) -> None:
    try:
        await ctx().api.answer_callback(callback_id, notification=notification, message=message)
    except MaxApiError as e:
        log.debug("answers: %s", e)


def teacher_menu() -> list[list[dict]]:
    c = ctx()
    rows = [
        [kb.cb("📋 Новый сбор", "m:poll"), kb.cb("📊 Статус сборов", "st:list")],
        [kb.cb("📖 Справочник", "m:faq"), kb.cb("🗂 Полка", "m:shelf")],
        [kb.cb("🌙 Тихий режим", "m:quiet"), kb.cb("⏱ Польза", "m:stats")],
        [kb.cb("🏫 Мои классы", "m:chats")],
    ]
    if c.bot_username:
        rows.append([kb.app("📱 Открыть приложение", c.bot_username, "home")])
    return rows


def parent_menu() -> list[list[dict]]:
    c = ctx()
    rows = [
        [kb.cb("📋 Мои сборы", "m:my"), kb.cb("❓ Задать вопрос", "m:ask")],
        [kb.cb("🗂 Материалы класса", "m:shelf")],
    ]
    if c.bot_username:
        rows.append([kb.app("📱 Открыть приложение", c.bot_username, "home")])
    return rows


TEACHER_HELP = (
    "Я <b>«Перемена»</b> — секретарь классного руководителя. Убираю рутину из чатов:\n\n"
    "📋 <b>Перекличка</b> — /poll: сбор согласий, справок, отметок «ознакомлен». Рассылаю каждому "
    "в личку с кнопками, сам напоминаю молчащим, /status покажет «25 из 27, не ответили: …».\n"
    "📖 <b>Справочник</b> — /faq: отвечаю родителям тем, что вы сохранили. Ответили в чате на вопрос — "
    "предложу сохранить ответ.\n"
    "🗂 <b>Полка</b> — пришлите мне файл с подписью (#теги можно), а потом /shelf памятка.\n"
    "🌙 <b>Тихий режим</b> — /quiet: вне рабочего времени копирую всё в утреннюю сводку. "
    "Срочное (слово «срочно») — сразу.\n"
    "⏱ <b>Польза</b> — /stats: сколько времени я вам сэкономил."
)

PARENT_HELP = (
    "Я <b>«Перемена»</b> — помощник вашего классного руководителя.\n\n"
    "• Сборы (согласия, справки) приходят сюда — отвечайте кнопкой, ответ сразу попадёт учителю.\n"
    "• Спросите меня что угодно о классе — отвечу тем, что сохранил учитель, в любое время.\n"
    "• Если готового ответа нет — передам вопрос учителю (в рабочее время).\n\n"
    "Я бот и никогда не выдаю себя за учителя."
)

ONBOARD_HELP = (
    "Здравствуйте! Я <b>«Перемена»</b> — бот-помощник классного руководителя в MAX.\n\n"
    "<b>Если вы учитель:</b> добавьте меня в чат класса (с родителями или учениками) и назначьте "
    "администратором с правом читать сообщения — тогда я смогу сверять ответы со списком класса "
    "и видеть вопросы. Все настройки — здесь, в личке.\n\n"
    "<b>Если вы родитель:</b> как только учитель добавит меня в чат класса, сборы и ответы будут приходить сюда."
)

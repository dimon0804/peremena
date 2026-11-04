"""Бот: подписка на календарь (/calendar), «что важно на неделе» (/week), вход в журнал (m:journal)."""
from __future__ import annotations

import html

from .. import keyboards as kb
from ..integrations import calendar, tokens
from ..services import chats, week
from .common import send


def clipboard(text: str, payload: str) -> dict:
    """Кнопка MAX «скопировать в буфер» (тип clipboard, до 1024 символов)."""
    return {"type": "clipboard", "text": text[:128], "payload": payload[:1024]}


CAL_HELP = (
    "🗓 <b>Календарь «Перемены»</b>\n\n"
    "Сроки сборов и домашка появятся в вашем календаре и будут обновляться сами — "
    "{who}.\n\n"
    "• <b>Google Календарь</b> — кнопка ниже.\n"
    "• <b>Яндекс Календарь</b> — «Подписаться на календарь» → «По ссылке» → вставьте ссылку.\n"
    "• <b>iPhone</b> — Настройки → Календарь → Учётные записи → Новая → Другое → Подписной календарь.\n\n"
    "<code>{url}</code>\n\n"
    "Ссылка личная: по ней видно ваш календарь. Переслали по ошибке — нажмите «Сменить ссылку», старая перестанет работать."
)


async def show_calendar(uid: int, rotate: bool = False) -> None:
    base = tokens.public_base()
    if not base.startswith("https://"):
        await send(uid, "🗓 Подписка на календарь заработает, когда сервис будет опубликован по HTTPS "
                        "(переменная PUBLIC_URL или WEBHOOK_URL).")
        return
    token = await (tokens.rotate_ical(uid) if rotate else tokens.ical_link(uid))
    links = calendar.links(f"{base}/cal/{token}.ics")
    teacher = await chats.is_teacher(uid)
    who = ("у вас — сроки сборов с числом ответивших и домашка ваших классов" if teacher else
           "сборы, где ждут вашего ответа (с напоминанием за 3 часа), и домашние задания")
    text = CAL_HELP.format(who=who, url=html.escape(links["url"]))
    if rotate:
        text = "🔄 Ссылка заменена: старая больше не работает.\n\n" + text
    await send(uid, text, [
        [clipboard("📋 Скопировать ссылку", links["url"])],
        [kb.link("Google Календарь", links["google"])],
        [kb.cb("🔄 Сменить ссылку", "m:calnew")],
    ])


async def ask_rotate(uid: int) -> None:
    await send(uid, "Сменить ссылку на календарь? Календари, подписанные по старой ссылке, перестанут обновляться — "
                    "подпишитесь заново по новой.",
               [[kb.cb("🔄 Да, сменить", "m:calnew!"), kb.cb("Нет", "m:cal")]])


async def show_week(uid: int) -> None:
    user = await chats.get_user(uid)
    if user is None:
        return
    await send(uid, week.render(await week.collect(user)), [[kb.cb("🗓 В мой календарь", "m:cal")]])


async def show_journal(uid: int) -> None:
    from . import journal

    if await chats.is_teacher(uid):
        await journal.show(uid)


MENU = {"cal": show_calendar, "calnew": ask_rotate, "week": show_week, "journal": show_journal}


async def on_menu(uid: int, action: str) -> bool:
    if action == "calnew!":
        await show_calendar(uid, rotate=True)
        return True
    h = MENU.get(action)
    if h:
        await h(uid)
        return True
    return False

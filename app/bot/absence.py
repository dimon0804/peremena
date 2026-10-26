"""Бот: «Ребёнок не придёт» для родителя и «Кто сегодня не придёт» для учителя."""
from __future__ import annotations

import html
from datetime import timedelta

from .. import keyboards as kb
from ..core import ctx
from ..services import attendance, chats, quiet
from ..timeutil import local_now
from .common import ack, clear_state, get_state, send, set_state

REASON_BUTTONS = [[kb.cb("🤒 Болеет", "ab:r:sick"), kb.cb("👪 Семейные обстоятельства", "ab:r:family")],
                  [kb.cb("Другая причина", "ab:r:other")]]


def _day(key: str, tz: str | None) -> str:
    today = local_now(tz).date()
    return (today + timedelta(days=1 if key == "tomorrow" else 0)).isoformat()


async def _tz_of(chat_id: int) -> str:
    chat = await chats.get_chat(chat_id)
    teacher = await chats.get_user(chat.teacher_id) if chat and chat.teacher_id else None
    return teacher.tz if teacher else ctx().settings.default_tz


# ---------------------------------------------------------------- родитель
async def start(uid: int) -> None:
    my = await chats.member_chats(uid)
    if not my:
        await send(uid, "Я пока не вижу вас в чате класса. Как только учитель подключит меня к чату, "
                        "здесь можно будет отметить, что ребёнок не придёт.")
        return
    if len(my) > 1:
        await set_state(uid, "absence", "chat", {})
        await send(uid, "🤒 В каком классе?", [[kb.cb(c.title or "Класс", f"ab:c:{c.chat_id}")] for c in my[:10]])
        return
    await _after_chat(uid, my[0].chat_id)


async def _after_chat(uid: int, chat_id: int) -> None:
    name = await attendance.child_name(chat_id, uid)
    if not name:
        await set_state(uid, "absence", "name", {"chat_id": chat_id})
        await send(uid, "Как зовут ребёнка? Напишите фамилию и имя — спрошу один раз, чтобы учитель видел, "
                        "кого нет.\n<i>Например: Иванов Пётр</i>", [[kb.cb("Отмена", "f:cancel")]])
        return
    await set_state(uid, "absence", "day", {"chat_id": chat_id, "child": name})
    await send(uid, f"🤒 <b>{html.escape(name)}</b> не придёт — когда?",
               [[kb.cb("Сегодня", "ab:d:today"), kb.cb("Завтра", "ab:d:tomorrow")],
                [kb.cb("Отмена", "f:cancel")]])


async def name_step(uid: int, data: dict, text: str) -> bool:
    name = " ".join(text.split())
    if len(name) < 2 or len(name) > 100 or name.startswith("/"):
        await send(uid, "Напишите фамилию и имя ребёнка, например: Иванов Пётр")
        return True
    await attendance.set_child_name(data["chat_id"], uid, name)
    await _after_chat(uid, data["chat_id"])
    return True


async def offer(uid: int) -> None:
    """Родитель написал «заболел» в личку — предлагаем отметить кнопкой."""
    await send(uid, "Похоже, ребёнок не придёт в школу. Отметить? Учитель увидит это в утреннем списке.",
               [[kb.cb("🤒 Отметить отсутствие", "ab:start")]])


async def _finish(uid: int, chat_id: int, child: str, day: str, reason: str) -> None:
    a, created = await attendance.mark(chat_id, uid, day, reason, child)
    who = child or "Ребёнок"
    text = (f"✅ Отметил: <b>{html.escape(who)}</b> не придёт — {attendance.day_label(day)} "
            f"({attendance.REASONS[a.reason]}).\nУчитель увидит это в списке, писать в чат не нужно.")
    if a.reason == "sick":
        text += " Выздоравливайте!"
    await send(uid, text, [[kb.cb("Отменить отметку", f"ab:x:{a.id}")]])
    if created:
        await tell_teacher_now(chat_id, who if child else await attendance.who(a), day, a.reason)


async def tell_teacher_now(chat_id: int, who: str, day: str, reason: str) -> None:
    """В рабочее время — сразу (если это сегодня); вне его отметка придёт в утренней сводке, без ночных сообщений."""
    chat = await chats.get_chat(chat_id)
    teacher = await chats.get_user(chat.teacher_id) if chat and chat.teacher_id else None
    if not teacher or day != local_now(teacher.tz).date().isoformat() or not quiet.in_work_window(teacher):
        return
    await ctx().safe_send(user_id=teacher.user_id,
                          text=f"🤒 {html.escape(who)} сегодня не придёт — {attendance.REASONS.get(reason, reason)}"
                               + (f" ({html.escape(chat.title)})" if chat.title else ""),
                          attachments=kb.kb_list([[kb.cb("Кто сегодня не придёт", "ab:list")]]))


# ---------------------------------------------------------------- учитель
async def show_list(uid: int) -> None:
    teacher = await chats.get_user(uid)
    if not teacher or not await chats.is_teacher(uid):
        await start(uid)  # родитель набрал /absent — ведём в отметку
        return
    today, _ = await attendance.render_day(teacher)
    tomorrow_day = (local_now(teacher.tz).date() + timedelta(days=1)).isoformat()
    tomorrow, n_tomorrow = await attendance.render_day(teacher, tomorrow_day)
    week = await attendance.week_summary(teacher)
    parts = [today]
    if n_tomorrow:
        parts.append(tomorrow)
    if week:
        parts.append(week)
    await send(uid, "\n\n".join(parts))


# ---------------------------------------------------------------- кнопки ab:*
async def on_cb(cid: str, uid: int, user: dict, tail: str) -> None:
    parts = tail.split(":")
    action = parts[0]
    if action == "start":
        await ack(cid)
        await start(uid)
        return
    if action == "list":
        await ack(cid)
        await show_list(uid)
        return
    if action == "x" and len(parts) == 2:
        ok = await attendance.cancel(int(parts[1]), uid)
        await ack(cid, "Отметка снята" if ok else "Уже снято")
        return
    if action == "g" and len(parts) == 4:  # кнопка под сообщением в чате класса
        chat_id, author, key = int(parts[1]), int(parts[2]), parts[3]
        if uid != author:
            await ack(cid, "Эту кнопку может нажать только автор сообщения")
            return
        await chats.touch_member(chat_id, user)
        day = _day(key, await _tz_of(chat_id))
        child = await attendance.child_name(chat_id, uid) or ""
        a, created = await attendance.mark(chat_id, uid, day, "sick", child)
        await ack(cid, f"Отметил: не придёт — {attendance.day_label(day)}. Учитель увидит в списке.")
        if created:
            await tell_teacher_now(chat_id, await attendance.who(a), day, "sick")
        return
    st = await get_state(uid)
    if not st or st["flow"] != "absence":
        await ack(cid, "Начните заново: «Ребёнок не придёт» в меню")
        return
    data = st["data"]
    await ack(cid)
    if action == "c" and len(parts) == 2:
        if int(parts[1]) not in {c.chat_id for c in await chats.member_chats(uid)}:
            await send(uid, "Этот класс вам недоступен.")
            return
        await _after_chat(uid, int(parts[1]))
    elif action == "d" and len(parts) == 2 and data.get("chat_id"):
        data["day"] = _day(parts[1], await _tz_of(data["chat_id"]))
        await set_state(uid, "absence", "reason", data)
        await send(uid, "Причина?", REASON_BUTTONS)
    elif action == "r" and len(parts) == 2 and data.get("day"):
        await clear_state(uid)
        await _finish(uid, data["chat_id"], data.get("child", ""), data["day"], parts[1])

"""Бот: /meet — запись на встречу (родительские консультации по слотам).

Учитель: класс → формат (очно / звонок) → окна одним сообщением → «Разослать». Расписание — /meet.
Родитель: кнопка со временем в личке → записан; «Перенести», «Отменить», «В календарь».
"""
from __future__ import annotations

import html
from datetime import datetime

from .. import keyboards as kb
from ..core import ctx
from ..services import chats, meetings
from ..timeutil import local_now, utcnow
from .common import ack, clear_state, get_state, send, set_state

ASK_WINDOWS = (
    "🗓 Напишите одним сообщением, когда вы готовы встретиться: по строке на каждое окно — дата, время "
    "и длительность одной встречи.\n\n"
    "<code>{example}</code>\n\n"
    "Первая строка без времени станет темой, вторая — местом (кабинет или «позвоню по номеру из чата»). "
    "Без длительности нарежу по {default} минут."
)


async def _tz(uid: int) -> str:
    u = await chats.get_user(uid)
    return u.tz if u else ctx().settings.default_tz


# ====================================================================== вход
async def start(uid: int) -> None:
    if not await chats.is_teacher(uid):
        await show_parent(uid)
        return
    mine = await meetings.for_teacher(uid)
    if not mine:
        await new(uid)
        return
    tz = await _tz(uid)
    lines = ["🗓 <b>Запись на встречу</b>", ""]
    rows = []
    for m in mine[:8]:
        busy, total = meetings.counts(m)
        first = next((x.starts_at for x in m.slots if x.starts_at > utcnow()), None)
        when = meetings.fmt_slot(first, tz) if first else "время прошло"
        lines.append(f"• {html.escape(m.title)} — записались {busy} из {total}, ближайшее: {when}")
        rows.append([kb.cb(f"📋 {m.title[:40]}", f"mt:s:{m.id}")])
    rows.append([kb.cb("➕ Новая запись", "mt:new")])
    await send(uid, "\n".join(lines), rows)


async def new(uid: int) -> None:
    my_chats = await chats.teacher_chats(uid)
    if not my_chats:
        await send(uid, "Сначала добавьте меня в чат класса — тогда я смогу пригласить родителей на встречу.")
        return
    if len(my_chats) == 1:
        await set_state(uid, "meet", "fmt", {"chat_id": my_chats[0].chat_id})
        await ask_format(uid)
        return
    await set_state(uid, "meet", "chat", {})
    rows = [[kb.cb(c.title or str(c.chat_id), f"mt:chat:{c.chat_id}")] for c in my_chats[:20]]
    rows.append([kb.cb("Отмена", "mt:x")])
    await send(uid, "🗓 <b>Запись на встречу.</b> Родителей какого класса пригласить?", rows)


async def ask_format(uid: int) -> None:
    await send(uid, "Как будете встречаться?",
               [[kb.cb("🏫 Очно", "mt:fmt:office"), kb.cb("📞 Звонок", "mt:fmt:call")], [kb.cb("Отмена", "mt:x")]])


async def ask_windows(uid: int) -> None:
    now = local_now(await _tz(uid))
    from datetime import timedelta

    d1, d2 = now + timedelta(days=3), now + timedelta(days=4)
    example = (f"Консультации по итогам четверти\nкаб. 214\n{d1:%d.%m} 15:00-17:00 по 15 мин\n"
               f"{d2:%d.%m} 16:00-18:00")
    await send(uid, ASK_WINDOWS.format(example=html.escape(example), default=meetings.DEFAULT_MINUTES),
               [[kb.cb("Отмена", "mt:x")]])


async def step(uid: int, step_name: str, data: dict, text: str) -> bool:
    if step_name != "text" or not text:
        return False
    tz = await _tz(uid)
    windows, rest, errors = meetings.parse_windows(text, tz)
    slots = meetings.build_slots(windows, tz)
    if not slots:
        why = ("; ".join(errors) + ".") if errors else "Не нашёл ни одного окна в будущем."
        await send(uid, f"Не получилось: {html.escape(why)}\n\nПример строки: <b>14.11 15:00-17:00 по 15 мин</b>. "
                        "Отменить — /cancel")
        return True
    data |= {"title": rest[0][:300] if rest else "Консультация с классным руководителем",
             "place": rest[1][:300] if len(rest) > 1 else "",
             "slots": [[at.isoformat(), mins] for at, mins in slots]}
    await set_state(uid, "meet", "confirm", data)
    chat = await chats.get_chat(data["chat_id"])
    fmt = meetings.FORMATS.get(data.get("format"), "очно")
    days: dict[str, list[str]] = {}
    for at, _ in slots:
        label = meetings.fmt_slot(at, tz)
        day, _, hm = label.rpartition(" ")
        days.setdefault(day, []).append(hm)
    lines = [f"🗓 <b>{html.escape(data['title'])}</b> · {html.escape(chat.title if chat else 'класс')}",
             f"Формат: {fmt}" + (f", {html.escape(data['place'])}" if data["place"] else ""),
             f"Слотов: <b>{len(slots)}</b>", ""]
    lines += [f"{d}: {', '.join(hms)}" for d, hms in days.items()]
    if errors:
        lines += ["", "<i>Пропустил: " + html.escape("; ".join(errors)) + "</i>"]
    lines += ["", "Разослать родителям класса? Каждый получит в личку кнопки со свободным временем."]
    await send(uid, "\n".join(lines), [[kb.cb("📤 Разослать", "mt:go"), kb.cb("Отмена", "mt:x")]])
    return True


async def confirm(uid: int) -> None:
    st = await get_state(uid)
    if not st or st["flow"] != "meet" or st["step"] != "confirm":
        await send(uid, "Черновик устарел — начните заново: /meet")
        return
    await clear_state(uid)
    d = st["data"]
    chat = await chats.get_chat(d["chat_id"])
    if not chat or chat.teacher_id != uid:
        await send(uid, "Этот класс больше не ваш — запись не создана.")
        return
    slots = [(datetime.fromisoformat(at), mins) for at, mins in d["slots"]]
    m = await meetings.create(uid, chat.chat_id, d["title"], d.get("format", "office"), d.get("place", ""), slots)
    rep = await meetings.dispatch(m.id)
    await send(uid, f"✅ Разослал: в личку дошло {rep['delivered']} из {rep['total']}. "
                    "Кто запишется — увидите в расписании, а в день встреч я напомню в утренней сводке. "
                    "Родителям напомню за 2 часа.",
               [[kb.cb("📋 Расписание", f"mt:s:{m.id}")]])


async def show_schedule(uid: int, mid: int) -> None:
    m = await meetings.get(mid)
    if not m or m.teacher_id != uid:
        await send(uid, "Запись не найдена.")
        return
    text = meetings.schedule_text(m, await _tz(uid), await meetings.chat_title(m.chat_id))
    rows = [[kb.cb("🔄 Обновить", f"mt:s:{m.id}")]]
    if not m.closed:
        rows[0].append(kb.cb("⛔ Закрыть запись", f"mt:close:{m.id}"))
    rows.append([kb.cb("📆 В календарь", "m:cal")])
    await send(uid, text, rows)


# ====================================================================== родитель
async def show_parent(uid: int) -> None:
    items = await meetings.for_parent(uid)
    if not items:
        await send(uid, "🗓 Сейчас учитель не приглашает на встречи. Когда пригласит — пришлю время, "
                        "и записаться можно будет одной кнопкой.")
        return
    for m in items[:3]:
        await show_meeting_to_parent(uid, m)


async def show_meeting_to_parent(uid: int, m, head: str = "") -> None:
    teacher = await chats.get_user(m.teacher_id)
    tz = teacher.tz if teacher else ctx().settings.default_tz
    mine = meetings.slot_of(m, uid)
    if mine:
        text = (head + f"🗓 Вы записаны: <b>{meetings.fmt_slot(mine.starts_at, tz)}</b>\n"
                f"«{html.escape(m.title)}», {html.escape(meetings.fmt_format(m))}.")
        await send(uid, text, meetings.parent_buttons(m))
        return
    text = head + meetings.parent_text(m, tz, await meetings.chat_title(m.chat_id),
                                       teacher.display_name if teacher else "")
    await send(uid, text, meetings.slot_buttons(m, tz))


async def book(cid: str, uid: int, user: dict, slot_id: int) -> None:
    status, slot, m = await meetings.book(slot_id, uid)
    if status in ("bad", "forbidden"):
        await ack(cid, "Эта запись не для вас" if status == "forbidden" else "Кнопка устарела")
        return
    if status == "closed":
        await ack(cid, "Учитель закрыл запись")
        return
    teacher = await chats.get_user(m.teacher_id)
    tz = teacher.tz if teacher else ctx().settings.default_tz
    if status in ("taken", "past"):
        await ack(cid, "Это время уже заняли" if status == "taken" else "Это время уже прошло")
        await show_meeting_to_parent(uid, m, head="Это время успели занять — выберите другое.\n\n")
        return
    await ack(cid, "Записал")
    when = meetings.fmt_slot(slot.starts_at, tz)
    head = {"ok": "✅ Записал вас", "moved": "🔁 Перенёс вашу запись", "same": "Вы уже записаны"}[status]
    remind = "" if slot.reminded else " Напомню за 2 часа."
    await send(uid, f"{head}: <b>{when}</b>\n«{html.escape(m.title)}», {html.escape(meetings.fmt_format(m))}.{remind}",
               meetings.parent_buttons(m))


async def on_cb(cid: str, uid: int, user: dict, tail: str) -> None:
    action, _, arg = tail.partition(":")
    if action == "b" and arg.isdigit():
        await book(cid, uid, user, int(arg))
        return
    if action == "c" and arg.isdigit():
        freed = await meetings.cancel(int(arg), uid)
        await ack(cid, "Запись отменена" if freed else "Записи уже нет")
        m = await meetings.get(int(arg))
        if freed and m:
            await send(uid, "Запись отменена — время освободилось для других. Передумаете — выберите снова:",
                       meetings.slot_buttons(m, await _tz(m.teacher_id)))
        return
    if action == "mv" and arg.isdigit():
        await ack(cid)
        m = await meetings.get(int(arg))
        if m and not m.closed:
            await send(uid, "Выберите новое время — прежнее освобожу сам:", meetings.slot_buttons(m, await _tz(m.teacher_id)))
        return
    # дальше — только учитель
    if action == "new":
        await ack(cid)
        await new(uid)
    elif action == "chat" and arg.lstrip("-").isdigit():
        await ack(cid)
        chat = await chats.get_chat(int(arg))
        if not chat or chat.teacher_id != uid:
            return
        await set_state(uid, "meet", "fmt", {"chat_id": chat.chat_id})
        await ask_format(uid)
    elif action == "fmt" and arg in meetings.FORMATS:
        await ack(cid)
        st = await get_state(uid)
        if not st or st["flow"] != "meet":
            await send(uid, "Черновик устарел — начните заново: /meet")
            return
        await set_state(uid, "meet", "text", st["data"] | {"format": arg})
        await ask_windows(uid)
    elif action == "go":
        await ack(cid, "Рассылаю")
        await confirm(uid)
    elif action == "x":
        await ack(cid, "Отменил")
        await clear_state(uid)
    elif action == "s" and arg.isdigit():
        await ack(cid)
        await show_schedule(uid, int(arg))
    elif action == "close" and arg.isdigit():
        ok = await meetings.close(int(arg), uid)
        await ack(cid, "Запись закрыта: новые родители записаться не смогут" if ok else "Запись не найдена")
    else:
        await ack(cid, "Кнопка устарела")

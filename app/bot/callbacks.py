"""Нажатия inline-кнопок (событие message_callback → ответ через POST /answers)."""
from __future__ import annotations

import html
import logging

from .. import keyboards as kb
from ..core import ctx
from ..db import session
from ..models import ClassChat, User
from ..services import chats, faq, polls, quiet, shelf, stats
from . import dialog
from .common import ack, clear_state, get_state, send, set_state

log = logging.getLogger(__name__)


async def on_callback(update: dict) -> None:
    cbk = update.get("callback") or {}
    user = cbk.get("user") or {}
    uid = user.get("user_id")
    payload = cbk.get("payload") or ""
    cid = cbk.get("callback_id") or ""
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    in_group = recipient.get("chat_type") == "chat"
    if not uid:
        return
    await chats.upsert_user(user, dialog_open=None if in_group else True)

    head, _, tail = payload.partition(":")
    try:
        if head == "pa":
            await on_poll_answer(cid, uid, user, tail, in_group, message)
            return
        if head == "m":
            await ack(cid)
            await on_menu(uid, tail)
            return
        if head == "f":
            await on_poll_flow(cid, uid, tail)
            return
        if head in ("ps", "pr", "pl", "pc", "st"):
            await on_poll_teacher(cid, uid, head, tail)
            return
        if head == "fq":
            await on_faq(cid, uid, tail)
            return
        if head == "fw":
            await ack(cid)
            if tail == "x":
                await ctx().kv.delete(f"q:{uid}")
                await send(uid, "Хорошо.")
            else:
                await dialog.forward_question(uid, None if tail == "ask" else int(tail))
            return
        if head == "fr":
            await ack(cid)
            if not await ctx().kv.get(f"qq:{tail}"):
                await send(uid, "Вопрос устарел — ответьте родителю напрямую.")
                return
            await set_state(uid, "faq_reply", "a", {"qid": tail})
            await send(uid, "✍️ Напишите ответ — отправлю родителю и сохраню в справочник.")
            return
        if head == "sh":
            await on_shelf(cid, uid, tail)
            return
        if head == "q":
            await on_quiet(cid, uid, tail)
            return
        if head == "c":
            await on_chat_admin(cid, uid, tail)
            return
        await ack(cid, "Кнопка устарела")
    except Exception:
        log.exception("Ошибка обработки кнопки %s", payload)
        await ack(cid, "Что-то пошло не так, попробуйте ещё раз")


# ---------------------------------------------------------------- ответы на сбор
async def on_poll_answer(cid: str, uid: int, user: dict, tail: str, in_group: bool, message: dict) -> None:
    try:
        pid_s, opt_s = tail.split(":")
        pid, opt = int(pid_s), int(opt_s)
    except ValueError:
        await ack(cid, "Кнопка устарела")
        return
    poll, status = await polls.record_answer(pid, user, opt)
    if status == "bad" or poll is None:
        await ack(cid, "Сбор не найден")
        return
    if status == "closed":
        await ack(cid, "Сбор уже закрыт — напишите учителю, если ответ ещё нужен")
        return
    if poll.teacher_id == uid and in_group:
        await ack(cid, "Это ваш сбор — статус в личке: /status")
        return
    option = poll.options[opt]
    note = f"Записал ваш ответ: «{option}»" if status != "changed" else f"Ответ изменён: «{option}»"
    if in_group:
        await ack(cid, note)
    else:
        teacher = await chats.get_user(poll.teacher_id)
        chat = await chats.get_chat(poll.chat_id)
        tz = teacher.tz if teacher else ctx().settings.default_tz
        body = {
            "text": polls.parent_text(poll, tz, chat.title if chat else "", teacher.display_name if teacher else "",
                                      selected=opt),
            "format": "html",
            "attachments": kb.kb_list(polls.answer_buttons(poll, selected=opt)),
        }
        await ack(cid, note, message=body)

    if poll.clarify_option is not None and opt == poll.clarify_option and status != "same":
        name = await chats.member_name(poll.chat_id, uid)
        text = (f"💬 <b>Нужно уточнить</b> по сбору «{html.escape(poll.title)}»\n"
                f"<a href=\"max://user/{uid}\">{html.escape(name)}</a> выбрал(а) «{html.escape(option)}» — "
                f"напишите ему(ей) напрямую.")
        await quiet.notify_teacher(poll.teacher_id, text, kind="clarify", chat_id=poll.chat_id,
                                   from_user_id=uid, from_name=name, raw_text=f"«{poll.title}»: {option}")

    answered, total, _ = polls.counts(poll)
    if total and answered == total and not poll.closed:
        text, buttons = await polls.status_card(poll)
        await quiet.notify_teacher(poll.teacher_id, "🎉 Ответили все!\n\n" + text, kind="report",
                                   chat_id=poll.chat_id, raw_text=f"«{poll.title}»: ответили все {total}",
                                   buttons=buttons)


# ---------------------------------------------------------------- меню
async def on_menu(uid: int, action: str) -> None:
    mapping = {
        "poll": dialog.start_poll, "faq": dialog.show_faq, "quiet": dialog.show_quiet,
        "stats": dialog.show_stats, "chats": dialog.show_chats, "my": dialog.show_my_polls,
        "help": dialog.show_start,
    }
    if action == "shelf":
        await dialog.ask_shelf(uid)
    elif action == "ask":
        await set_state(uid, "ask", "q", {})
        await send(uid, "❓ Напишите вопрос — отвечу тем, что сохранил учитель.")
    elif action in mapping:
        await mapping[action](uid)


# ---------------------------------------------------------------- черновик сбора
async def on_poll_flow(cid: str, uid: int, tail: str) -> None:
    action, _, arg = tail.partition(":")
    if action == "cancel":
        await clear_state(uid)
        await ack(cid, "Отменено")
        await send(uid, "Отменил.")
        return
    st = await get_state(uid)
    if action == "chat":
        await ack(cid)
        chat = await chats.get_chat(int(arg))
        if not chat or chat.teacher_id != uid:
            await send(uid, "Этот чат не привязан к вам.")
            return
        await set_state(uid, "poll", "tpl", {"chat_id": chat.chat_id})
        await dialog.ask_template(uid)
    elif action == "tpl":
        await ack(cid)
        if not st or st["flow"] != "poll" or arg not in polls.TEMPLATES:
            await send(uid, "Черновик устарел. Новый сбор — /poll")
            return
        await dialog.on_template(uid, arg)
    elif action == "dl":
        await ack(cid)
        if not st or st["flow"] != "poll":
            await send(uid, "Черновик устарел. Новый сбор — /poll")
            return
        await dialog.on_deadline_preset(uid, arg)
    elif action == "send":
        await ack(cid, "Рассылаю")
        await dialog.confirm_poll(uid)
    else:
        await ack(cid)


# ---------------------------------------------------------------- управление сбором
async def _own_poll(cid: str, uid: int, pid: int):
    poll = await polls.get_poll(pid)
    if poll is None or poll.teacher_id != uid:
        await ack(cid, "Сбор не найден")
        return None
    return poll


async def on_poll_teacher(cid: str, uid: int, head: str, tail: str) -> None:
    if head == "st":
        await ack(cid)
        await dialog.show_status_list(uid)
        return
    poll = await _own_poll(cid, uid, int(tail))
    if poll is None:
        return
    if head == "ps":
        await ack(cid)
        text, buttons = await polls.status_card(poll)
        await send(uid, text, buttons)
    elif head == "pl":
        await ack(cid)
        await send(uid, polls.full_list(poll))
    elif head == "pr":
        await ack(cid, "Напоминаю")
        res = await polls.remind(poll.id)
        msg = f"⏰ Напомнил в личку: {res['sent']}."
        if res["group"]:
            msg += f" Ещё {res['group']} упомянул в чате класса."
        await send(uid, msg)
    elif head == "pc":
        await ack(cid, "Закрываю")
        await polls.close_poll(poll.id)


# ---------------------------------------------------------------- справочник
async def on_faq(cid: str, uid: int, tail: str) -> None:
    action, _, arg = tail.partition(":")
    c = ctx()
    if action == "add":
        await ack(cid)
        await dialog.start_faq_add(uid)
    elif action == "del":
        e = await faq.get_entry(int(arg))
        if not e or e.teacher_id != uid:
            await ack(cid, "Карточка не найдена")
            return
        await ack(cid)
        await send(uid, f"Удалить карточку «{html.escape(e.question[:100])}»?",
                   [[kb.cb("🗑 Да, удалить", f"fq:delok:{e.id}"), kb.cb("Нет", "fq:noop")]])
    elif action == "delok":
        ok = await faq.delete_entry(int(arg), uid)
        await ack(cid, "Удалено" if ok else "Уже удалено")
    elif action == "show":
        e = await faq.get_entry(int(arg))
        await ack(cid)
        if e:
            await send(uid, await faq.render_answer(e), [[kb.cb("Это не то", f"fq:no:{e.id}")]])
            if e.teacher_id != uid:
                await faq.mark_hit(e.id)
                await stats.add(e.teacher_id, "faq_answer")
    elif action == "no":
        e = await faq.mark_miss(int(arg))
        await ack(cid, "Спасибо, учту")
        q = await c.kv.get(f"q:{uid}")
        if e:
            name = await chats.member_name(e.chat_id, uid)
            await quiet.notify_teacher(
                e.teacher_id,
                f"🙅 Ответ из справочника не подошёл\nКарточка: «{html.escape(e.question[:150])}»\n"
                f"Кто: <a href=\"max://user/{uid}\">{html.escape(name)}</a>"
                + (f"\nВопрос был: «{html.escape(q['text'][:200])}»" if q else ""),
                kind="not_that", from_user_id=uid, from_name=name, raw_text=f"Не подошло: «{e.question[:150]}»",
            )
        await send(uid, "Передать ваш вопрос учителю?", [[kb.cb("✉️ Передать", "fw:ask"), kb.cb("Не надо", "fw:x")]])
        if not q:
            await set_state(uid, "ask", "q", {})
    elif action == "save":
        draft = await c.kv.get(f"sv:{arg}")
        if not draft or draft["teacher_id"] != uid:
            await ack(cid, "Черновик устарел")
            return
        e = await faq.add_entry(uid, draft["q"], draft["a"], chat_id=None)
        await c.kv.delete(f"sv:{arg}")
        await ack(cid, "Сохранено в справочник")
        await send(uid, f"✅ В справочнике: «{html.escape(e.question[:100])}»\n<i>Ключевые слова: "
                        f"{html.escape(e.keywords or '—')}</i>. Поправить можно в приложении.")
    elif action == "nosave":
        await c.kv.delete(f"sv:{arg}")
        await ack(cid, "Хорошо, не сохраняю")
    else:
        await ack(cid)


# ---------------------------------------------------------------- полка
async def on_shelf(cid: str, uid: int, tail: str) -> None:
    parts = tail.split(":")
    action = parts[0]
    m = await shelf.get(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else None
    if m is None:
        await ack(cid, "Материал не найден")
        return
    if action == "open":
        await ack(cid)
        await dialog.show_material(uid, m.id)
    elif action == "get":
        await ack(cid, "Отправляю")
        if not await _can_see(uid, m):
            await send(uid, "Этот материал вам недоступен.")
            return
        if await shelf.send_material(m, user_id=uid):
            await stats.add(m.teacher_id, "material_found")
    elif action == "to":
        if m.teacher_id != uid or len(parts) < 3:
            await ack(cid, "Пересылать может только учитель")
            return
        chat_id = int(parts[2])
        chat = await chats.get_chat(chat_id)
        if not chat or chat.teacher_id != uid:
            await ack(cid, "Чат не найден")
            return
        ok = await shelf.send_material(m, chat_id=chat_id, caption=html.escape(m.title) if m.title else None)
        await ack(cid, f"Отправлено в {chat.title}" if ok else "Не получилось отправить")
        if ok:
            async with session() as s:
                row = await s.get(type(m), m.id)
                if row and not row.chat_id:
                    row.chat_id = chat_id
            await stats.add(uid, "material_found")
    elif action == "cap":
        if m.teacher_id != uid:
            await ack(cid)
            return
        await ack(cid)
        await set_state(uid, "shelf_caption", "t", {"mid": m.id})
        await send(uid, "Напишите подпись. #теги можно добавить прямо в текст: «Памятка по ПБ #безопасность».")
    elif action == "del":
        if m.teacher_id != uid:
            await ack(cid)
            return
        ok = await shelf.delete(m.id, uid)
        await ack(cid, "Убрал с полки" if ok else "Уже убрано")
    else:
        await ack(cid)


async def _can_see(uid: int, m) -> bool:
    if m.teacher_id == uid:
        return True
    chat_ids = {c.chat_id for c in await chats.member_chats(uid)}
    return bool(m.chat_id and m.chat_id in chat_ids)


# ---------------------------------------------------------------- тихий режим
async def on_quiet(cid: str, uid: int, tail: str) -> None:
    parts = tail.split(":")
    action = parts[0]
    if not await chats.is_teacher(uid):
        await ack(cid)
        return
    async with session() as s:
        u = await s.get(User, uid)
        if action == "h" and len(parts) == 3:
            u.work_start, u.work_end = int(parts[1]), int(parts[2])
        elif action == "we":
            u.weekends_off = not u.weekends_off
        elif action == "on":
            u.quiet_enabled = not u.quiet_enabled
        elif action == "sugg":
            u.suggest_save = not u.suggest_save
        elif action == "tz" and len(parts) == 2:
            idx = int(parts[1])
            if 0 <= idx < len(dialog.TIMEZONES):
                u.tz = dialog.TIMEZONES[idx][1]
        elif action == "auto" and len(parts) == 2:
            chat = await s.get(ClassChat, int(parts[1]))
            if chat and chat.teacher_id == uid:
                chat.autoanswer = not chat.autoanswer
    if action == "hours":
        await ack(cid)
        await set_state(uid, "hours", "t", {})
        await send(uid, "Напишите рабочее время, например <b>8:00-17:30</b>")
        return
    if action == "urgent":
        await ack(cid)
        await set_state(uid, "urgent", "t", {})
        await send(uid, "Напишите слова-маркеры через запятую, например: <b>срочно, важно</b>")
        return
    if action == "tzlist":
        await ack(cid)
        rows, row = [], []
        for i, (name, _) in enumerate(dialog.TIMEZONES):
            row.append(kb.cb(name, f"q:tz:{i}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await send(uid, "Выберите часовой пояс:", rows)
        return
    await ack(cid, "Сохранено")
    await dialog.show_quiet(uid)


# ---------------------------------------------------------------- классы
async def on_chat_admin(cid: str, uid: int, tail: str) -> None:
    action, _, arg = tail.partition(":")
    chat = await chats.get_chat(int(arg)) if arg.lstrip("-").isdigit() else None
    if not chat or chat.teacher_id != uid:
        await ack(cid, "Чат не найден")
        return
    if action == "sync":
        await ack(cid, "Обновляю")
        n = await chats.sync_members(chat.chat_id)
        await chats.refresh_admin_status(chat.chat_id)
        if n < 0:
            await send(uid, "⚠️ Не получилось получить список — назначьте меня администратором чата.")
        else:
            await send(uid, f"🔄 {html.escape(chat.title)}: в списке {n} участников.")
    elif action == "hist":
        await ack(cid, "Смотрю историю")
        n = await shelf.import_history(chat.chat_id, uid)
        await send(uid, f"📥 Из истории «{html.escape(chat.title)}» добавлено на полку файлов: {n}."
                        + ("" if n else "\n<i>Если вы отправляли файлы — проверьте, что я администратор чата.</i>"))
    else:
        await ack(cid)


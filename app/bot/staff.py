"""Сценарии для сотрудников школы: доска класса, приглашение предметников, школа и сводка завуча."""
from __future__ import annotations

import html
import logging
import re

from .. import keyboards as kb
from ..core import ctx
from ..models import ClassChat
from ..services import board, chats, school, websession
from ..timeutil import parse_deadline, to_local
from .common import ack, clear_state, get_state, send, set_state

log = logging.getLogger(__name__)

POPULAR_SUBJECTS = ["Алгебра", "Геометрия", "Русский язык", "Литература", "Английский язык", "История",
                    "Физика", "Биология"]


async def staff_chats(uid: int) -> list[tuple[ClassChat, str]]:
    return (await school.roles(uid)).board_chats()


# ====================================================================== «Доска класса»
async def start_board(uid: int, kind: str) -> None:
    targets = await staff_chats(uid)
    if not targets:
        await send(uid, "Публиковать на доску может классный руководитель или приглашённый им предметник.\n"
                        "Классный руководитель: /invite — ссылка-приглашение для предметника.")
        return
    data = {"kind": kind}
    if len(targets) == 1:
        data["chat_id"] = targets[0][0].chat_id
        data["subject"] = targets[0][1]
        await _after_chat(uid, data)
        return
    await set_state(uid, "board", "chat", data)
    rows = [[kb.cb(f"{c.title or c.chat_id}" + (f" · {subj}" if subj else ""), f"bd:chat:{c.chat_id}")]
            for c, subj in targets[:20]]
    rows.append([kb.cb("Отмена", "f:cancel")])
    what = "домашнее задание" if kind == "hw" else "объявление"
    await send(uid, f"📌 Куда публикуем {what}?", rows)


async def _after_chat(uid: int, data: dict) -> None:
    if data["kind"] == "hw" and not data.get("subject"):
        await set_state(uid, "board", "subject", data)
        rows = [[kb.cb(s, f"bd:subj:{i}") for i, s in enumerate(POPULAR_SUBJECTS[j:j + 2], start=j)]
                for j in range(0, len(POPULAR_SUBJECTS), 2)]
        await send(uid, "📚 Какой предмет? Выберите или напишите.", rows)
        return
    await set_state(uid, "board", "text", data)
    if data["kind"] == "hw":
        await send(uid, f"📚 <b>{html.escape(data['subject'])}</b>. Напишите задание одним сообщением.\n"
                        "<i>Например: №245, 247; выучить правило на с. 63.</i>", [[kb.cb("Отмена", "f:cancel")]])
    else:
        await send(uid, "📣 Напишите объявление одним сообщением.", [[kb.cb("Отмена", "f:cancel")]])


async def board_step(uid: int, step: str, data: dict, text: str) -> bool:
    if step == "subject":
        if not text:
            return False
        data["subject"] = board.canonical_subject(text)
        await _after_chat(uid, data)
        return True
    if step == "text":
        if len(text) < 2:
            await send(uid, "Напишите текст.")
            return True
        data["text"] = text[:3500]
        if data["kind"] == "hw":
            await set_state(uid, "board", "date", data)
            await send(uid, "К какому дню?", [
                [kb.cb("Завтра", "bd:fd:tomorrow"), kb.cb("Послезавтра", "bd:fd:after")],
                [kb.cb("К понедельнику", "bd:fd:monday"), kb.cb("Без даты", "bd:fd:none")],
            ])
        else:
            await _ask_publish(uid, data)
        return True
    if step == "date":
        user = await chats.get_user(uid)
        dt = parse_deadline(text, user.tz if user else None)
        if not dt:
            await send(uid, "Не понял дату. Напишите, например, <b>28.10</b> или выберите кнопкой.")
            return True
        data["for_date"] = to_local(dt, user.tz if user else None).date().isoformat()
        await _ask_publish(uid, data)
        return True
    await send(uid, "Выберите вариант кнопкой выше или напишите /cancel.")
    return True


async def _ask_publish(uid: int, data: dict) -> None:
    await set_state(uid, "board", "publish", data)
    user = await chats.get_user(uid)
    morning = to_local(board.next_morning(user), user.tz if user else None)
    quiet_now = board.in_quiet_hours(user)
    later = f"🌙 Утром, {morning:%d.%m} в {morning:%H:%M}" + (" — рекомендую" if quiet_now else "")
    now_btn, later_btn = [kb.cb("📤 Опубликовать сейчас", "bd:pub:now")], [kb.cb(later, "bd:pub:later")]
    rows = [later_btn, now_btn] if quiet_now else [now_btn, later_btn]
    rows += [[kb.cb("🤫 Не публиковать, только отвечать", "bd:pub:silent")], [kb.cb("Отмена", "f:cancel")]]
    note = ("\n\n<i>Сейчас нерабочее время: если опубликовать утром, родителям не придёт уведомление ночью.</i>"
            if quiet_now else "")
    await send(uid, "Когда опубликовать в чат класса? Бот в любом случае будет отвечать по этой записи "
                    "на «что задали?»." + note, rows)


async def on_board_cb(cid: str, uid: int, tail: str) -> None:
    action, _, arg = tail.partition(":")
    st = await get_state(uid)
    if action == "del":
        ok = await board.delete(int(arg), uid)
        await ack(cid, "Удалено" if ok else "Не получилось удалить")
        return
    if action == "kind":
        await ack(cid)
        await start_board(uid, arg)
        return
    if not st or st["flow"] != "board":
        await ack(cid, "Черновик устарел")
        return
    data = st["data"]
    await ack(cid)
    if action == "chat":
        target = next(((c, s) for c, s in await staff_chats(uid) if str(c.chat_id) == arg), None)
        if not target:
            await send(uid, "Нет доступа к этому классу.")
            return
        data["chat_id"], data["subject"] = target[0].chat_id, target[1]
        await _after_chat(uid, data)
    elif action == "subj":
        data["subject"] = POPULAR_SUBJECTS[int(arg)]
        await _after_chat(uid, data)
    elif (action in ("fd", "pub") and not data.get("text")) or (action == "pub" and st["step"] != "publish"):
        await send(uid, "Эта кнопка от старого черновика. Начните заново: /hw или /news")
    elif action == "fd":
        user = await chats.get_user(uid)
        data["for_date"] = board.parse_for_date(arg, user.tz if user else None)
        await _ask_publish(uid, data)
    elif action == "pub":
        await clear_state(uid)
        a = await board.create(data["chat_id"], uid, data["kind"], data["text"], data.get("subject", ""),
                               data.get("for_date"), publish_mode=arg)
        user = await chats.get_user(uid)
        if arg == "now":
            status = "✅ Опубликовано в чате класса." if a.message_id else \
                "⚠️ Не получилось опубликовать в чате — проверьте, что я в нём. Отвечать по записи буду."
        elif arg == "later":
            status = f"🌙 Опубликую {to_local(a.publish_at, user.tz if user else None):%d.%m в %H:%M}."
        else:
            status = "🤫 Записал без публикации — буду отвечать на вопросы."
        await send(uid, f"{status}\n\n{board.render(a)}", [[kb.cb("🗑 Удалить", f"bd:del:{a.id}"),
                                                           kb.cb("➕ Ещё", f"bd:kind:{a.kind}")]])


async def show_board(uid: int) -> None:
    targets = await staff_chats(uid)
    if not targets:
        await start_board(uid, "hw")
        return
    items = await board.list_for_chats([c.chat_id for c, _ in targets], days=14, limit=15)
    titles = {c.chat_id: c.title for c, _ in targets}
    user = await chats.get_user(uid)
    tz = user.tz if user else None
    lines = ["📌 <b>Доска класса</b> — последние записи", ""]
    for a in items:
        mark = "" if a.published else f" ⏳ выйдет {to_local(a.publish_at, tz):%d.%m %H:%M}" if a.publish_at else ""
        if a.publish_mode == "silent":
            mark = " 🤫"
        kind = f"📚 {html.escape(a.subject)}" if a.kind == "hw" else "📣"
        day = f" (на {board.fmt_day(a.for_date)})" if a.for_date else ""
        lines.append(f"{kind}{day} · {html.escape(titles.get(a.chat_id, ''))}{mark}\n{html.escape(a.text[:160])}")
    if not items:
        lines.append("Пусто. Запишите домашнее задание — и бот сам ответит на «что задали?».")
    rows = [[kb.cb("📚 Домашка", "bd:kind:hw"), kb.cb("📣 Объявление", "bd:kind:news")]]
    if ctx().bot_username:
        rows.append([kb.app("Открыть доску в приложении", ctx().bot_username, "board")])
    await send(uid, "\n\n".join(lines) if items else "\n".join(lines), rows)


async def answer_board_question(uid: int, text: str, chat_ids: list[int], tz: str | None) -> bool:
    """Ответ родителю/ученику по доске. True — ответ дан."""
    if board.is_homework_question(text):
        from ..services import stats

        items = await board.find_homework(text, chat_ids, tz)
        subj = board.subject_in(text)
        day = board.date_in(text, tz)
        prefix = ""
        if not items and day:
            # на запрошенный день ничего нет — покажем ближайшее, иначе родитель решит, что не задано вовсе
            items = await board.find_homework(f"задали {subj or ''}", chat_ids, tz)
            if items:
                prefix = (f"На {board.WEEKDAYS[day.weekday()]} {day:%d.%m} на доске ничего не записано. "
                          f"Ближайшее:\n\n")
                text = f"задали {subj or ''}"
        if items:
            await send(uid, prefix + await board.render_answer(items, text, tz))
            teacher_ids = {c.teacher_id for c in [await chats.get_chat(a.chat_id) for a in items] if c}
            for t in teacher_ids:
                await stats.add(t, "faq_answer")
            return True
        what = "📚 На доске класса пока нет задания" + (f" по предмету «{html.escape(subj)}»" if subj else "")
        if (await school.roles(uid)).is_staff:
            # учителю «спросить учителя» не нужно — подскажем, как записать
            await send(uid, what + ". Записать — /hw")
            return True
        await send(uid, what + ". Если задание давали устно — могу передать вопрос учителю.",
                   [[kb.cb("✉️ Спросить учителя", "fw:ask")]])
        await ctx().kv.set(f"q:{uid}", {"text": text, "chats": chat_ids}, 3600 * 6)
        return True
    if board.is_news_question(text):
        items = await board.recent_news(chat_ids)
        if items:
            await send(uid, await board.render_news(items, tz))
            return True
    return False


# ====================================================================== предметники
async def start_invite(uid: int) -> None:
    my = await chats.teacher_chats(uid)
    if not my:
        await send(uid, "Приглашать предметников может классный руководитель — тот, кто добавил меня в чат класса.")
        return
    if len(my) == 1:
        await _send_invite(uid, my[0])
        return
    await send(uid, "Для какого класса приглашение?",
               [[kb.cb(c.title or str(c.chat_id), f"iv:chat:{c.chat_id}")] for c in my[:20]])


async def _send_invite(uid: int, chat: ClassChat) -> None:
    code = await school.subject_invite_code(chat.chat_id)
    link = ctx().start_link(f"subj_{code}")
    teachers = await school.subject_teachers(chat.chat_id)
    lines = [
        f"🔗 <b>Приглашение предметника в {html.escape(chat.title)}</b>", "",
        "Перешлите учителю это сообщение. По ссылке он сможет публиковать домашние задания и объявления "
        "в ваш класс — без прав администратора в чате. Ссылка действует 7 дней.", "",
        link,
    ]
    rows = [[kb.link("Открыть приглашение", link)]]
    if teachers:
        lines += ["", "<b>Уже подключены:</b>"]
        for st, u in teachers:
            lines.append(f"• {html.escape(u.display_name if u else str(st.user_id))} — {html.escape(st.subject or 'предмет не указан')}")
            rows.append([kb.cb(f"✖ Отключить {(u.display_name if u else '')[:20]}", f"iv:rm:{chat.chat_id}:{st.user_id}")])
    await send(uid, "\n".join(lines), rows)


async def on_invite_cb(cid: str, uid: int, tail: str) -> None:
    parts = tail.split(":")
    if parts[0] == "subj":
        await ack(cid)
        st = await get_state(uid)
        if st and st["flow"] == "subj_name" and parts[1].isdigit():
            await subj_name_step(uid, st["data"], POPULAR_SUBJECTS[int(parts[1])])
        return
    chat = await chats.get_chat(int(parts[1])) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
    if not chat or chat.teacher_id != uid:
        await ack(cid, "Чат не найден")
        return
    if parts[0] == "chat":
        await ack(cid)
        await _send_invite(uid, chat)
    elif parts[0] == "rm" and len(parts) == 3:
        await school.remove_subject_teacher(chat.chat_id, int(parts[2]))
        await ack(cid, "Отключено")


async def handle_start_payload(uid: int, payload: str) -> bool:
    """Глубокие ссылки: subj_<код> — приглашение предметника, school_<код> — вступление в школу,
    login_<код> — вход на сайт."""
    if payload.startswith("login_"):
        await ask_login_confirm(uid, payload[6:])
        return True
    if payload.startswith("subj_"):
        code = payload[5:]
        data = await ctx().kv.get(f"subj:{code}")
        chat = await chats.get_chat(data["chat_id"]) if data else None
        if not chat:
            await send(uid, "Приглашение устарело — попросите классного руководителя прислать новое (/invite).")
            return True
        await set_state(uid, "subj_name", "s", {"code": code})
        rows = [[kb.cb(s, f"iv:subj:{i}") for i, s in enumerate(POPULAR_SUBJECTS[j:j + 2], start=j)]
                for j in range(0, len(POPULAR_SUBJECTS), 2)]
        await send(uid, f"👋 Вас пригласили в класс <b>{html.escape(chat.title)}</b> как предметника.\n\n"
                        "Какой предмет вы ведёте? Выберите или напишите.", rows)
        return True
    if payload.startswith("school_"):
        sch, role = await school.join_by_code(uid, payload[7:])
        if not sch:
            await send(uid, "Код школы не найден. Проверьте ссылку.")
            return True
        what = "администратор" if role == "admin" else "учитель"
        await send(uid, f"🏫 Вы в школе <b>{html.escape(sch.name)}</b> ({what}). Общая база ответов школы теперь "
                        "работает и в ваших классах. Сводка — /school", )
        return True
    return False


async def subj_name_step(uid: int, data: dict, subject: str) -> bool:
    await clear_state(uid)
    chat = await school.accept_subject_invite(uid, data["code"], board.canonical_subject(subject))
    if not chat:
        await send(uid, "Приглашение устарело — попросите новое у классного руководителя.")
        return True
    await send(uid, f"✅ Вы подключены к <b>{html.escape(chat.title)}</b> · {html.escape(board.canonical_subject(subject))}.\n\n"
                    "Домашнее задание — /hw, объявление — /news. Родители и ученики получат ответ на «что задали?» "
                    "от бота, даже ночью.",
               [[kb.cb("📚 Записать домашку", "bd:kind:hw")]])
    if chat.teacher_id:
        u = await chats.get_user(uid)
        await ctx().safe_send(user_id=chat.teacher_id,
                              text=f"👩‍🏫 {html.escape(u.display_name if u else '')} подключился к "
                                   f"{html.escape(chat.title)} как предметник ({html.escape(board.canonical_subject(subject))}).")
    return True


# ====================================================================== школа
async def show_school(uid: int) -> None:
    r = await school.roles(uid)
    if not r.school:
        await set_state(uid, "school_name", "n", {})
        await send(uid, "🏫 <b>Школа</b>\n\nОбъедините классы школы: общая база ответов (звонки, форма, справки) "
                        "сразу работает у каждого нового классного руководителя, а администрация видит сводку "
                        "и запускает сборы сразу на параллель.\n\n"
                        "Чтобы создать школу, напишите её название. Если школа уже есть — откройте ссылку-приглашение "
                        "от завуча или пришлите код.", [[kb.cb("Отмена", "f:cancel")]])
        return
    rows = []
    if r.is_admin:
        text = await school.render_dashboard_text(r.school)
        text += (f"\n\n🔑 Код для учителей: <code>{r.school.code}</code>\n"
                 f"Ссылка: {ctx().start_link('school_' + r.school.code)}")
        rows.append([kb.cb("🔑 Код для администрации", "sc:admin")])
    else:
        text = f"🏫 Вы в школе <b>{html.escape(r.school.name)}</b>. Общая база ответов школы работает в ваших классах."
    if ctx().bot_username:
        rows.insert(0, [kb.app("📊 Сводка и сборы на параллель" if r.is_admin else "Открыть школу", ctx().bot_username, "school")])
    await send(uid, text, rows)


async def school_name_step(uid: int, text: str) -> bool:
    await clear_state(uid)
    t = text.strip()
    if len(t) <= 10 and t.replace("-", "").isalnum() and t.upper() == t and any(ch.isdigit() for ch in t):
        sch, role = await school.join_by_code(uid, t)
        if sch:
            await send(uid, f"🏫 Вы в школе <b>{html.escape(sch.name)}</b> ({'администратор' if role == 'admin' else 'учитель'}).")
            return True
    if len(t) < 3:
        await send(uid, "Название слишком короткое. /school — попробовать ещё раз.")
        return True
    sch = await school.create_school(uid, t)
    await send(uid,
               f"✅ Школа <b>{html.escape(sch.name)}</b> создана, вы — администратор.\n\n"
               f"Пригласите классных руководителей — перешлите им ссылку:\n{ctx().start_link('school_' + sch.code)}\n\n"
               f"Код для учителей: <code>{sch.code}</code>",
               [[kb.cb("📊 Сводка", "sc:dash")]])
    return True


async def on_school_cb(cid: str, uid: int, tail: str) -> None:
    r = await school.roles(uid)
    if tail == "dash":
        await ack(cid)
        await show_school(uid)
    elif tail == "admin" and r.is_admin and r.school:
        await ack(cid)
        await send(uid, "🔑 Ссылка для другого администратора (завуча, директора). Не пересылайте её учителям:\n"
                        + ctx().start_link("school_" + r.school.admin_code))
    else:
        await ack(cid)



async def on_login_cb(cid: str, uid: int, tail: str) -> None:
    action, _, code = tail.partition(":")
    if action == "ok":
        ok = await websession.confirm_login(code, uid)
        await ack(cid, "Вход подтверждён" if ok else "Ссылка устарела")
        await send(uid, "✅ Готово — вернитесь на сайт, кабинет откроется сам." if ok
                   else "Ссылка для входа устарела. Обновите страницу на сайте.")
    else:
        await websession.reject_login(code)
        await ack(cid, "Вход отклонён")
        await send(uid, "Вход отклонён. Никто не получил доступ к вашему кабинету.")


async def ask_login_confirm(uid: int, code: str) -> None:
    if not await websession.get_login(code):
        await send(uid, "Код для входа устарел. Обновите страницу на сайте и попробуйте ещё раз.")
        return
    await send(uid, "🔐 <b>Вход на сайт «Перемены»</b>\n\nПодтвердите, только если вы сами сейчас входите "
                    "на сайт. Если код или ссылку прислал кто-то другой — нажмите «Это не я».",
               [[kb.cb("✅ Войти", f"lg:ok:{code}"), kb.cb("Это не я", f"lg:no:{code}")]])


PIN_RE = re.compile(r"^\s*(?:/?(?:login|вход)\s*)?(\d{3})[\s-]?(\d{3})\s*$", re.IGNORECASE)


async def try_login_pin(uid: int, text: str) -> bool:
    """6 цифр с сайта, отправленные боту, — вход без камеры и QR. True, если это был код входа."""
    m = PIN_RE.match(text or "")
    if not m:
        return False
    code = await websession.code_by_pin(m[1] + m[2])
    if not code:
        await send(uid, "Такого кода входа нет или он устарел. Обновите страницу входа на сайте — появится новый.")
        return True
    await ask_login_confirm(uid, code)
    return True

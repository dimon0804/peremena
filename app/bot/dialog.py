"""Личка с ботом: команды учителя, пошаговые сценарии и вопросы родителей."""
from __future__ import annotations

import html
import logging
import secrets

from .. import keyboards as kb
from ..core import ctx
from ..models import User
from ..services import chats, faq, polls, quiet, shelf, stats
from ..timeutil import fmt_dt, fmt_minutes, parse_deadline, parse_hours, preset_deadline
from .common import (
    ONBOARD_HELP, PARENT_HELP, TEACHER_HELP, clear_state, get_state, parent_menu, parse_command, send, set_state,
    teacher_menu,
)

log = logging.getLogger(__name__)

TIMEZONES = [
    ("Калининград", "Europe/Kaliningrad"), ("Москва", "Europe/Moscow"), ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"), ("Омск", "Asia/Omsk"), ("Новосибирск", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"), ("Иркутск", "Asia/Irkutsk"), ("Якутск", "Asia/Yakutsk"),
    ("Владивосток", "Asia/Vladivostok"), ("Магадан", "Asia/Magadan"), ("Камчатка", "Asia/Kamchatka"),
]


# ====================================================================== вход
async def on_message(update: dict) -> None:
    msg = update.get("message") or {}
    sender = msg.get("sender") or {}
    uid = sender.get("user_id")
    if not uid or sender.get("is_bot"):
        return
    await chats.upsert_user(sender, dialog_open=True)
    text = ((msg.get("body") or {}).get("text") or "").strip()

    cmd, rest = parse_command(text)
    if cmd:
        await clear_state(uid)
        await run_command(uid, cmd, rest)
        return

    state = await get_state(uid)
    if state and await handle_state(uid, state, text, msg):
        return

    media = shelf.extract_media(msg)
    is_teacher = await chats.is_teacher(uid)
    if media:
        if is_teacher:
            await save_to_shelf(uid, media, text)
        else:
            await send(uid, "Файлы на полку класса может класть только учитель. Если это справка — "
                            "передайте её учителю так, как он просил.")
        return
    if text:
        await answer_text(uid, text, is_teacher)


async def run_command(uid: int, cmd: str, rest: str) -> None:
    handlers = {
        "start": show_start, "help": show_start, "poll": start_poll, "status": show_status_list,
        "faq": show_faq, "add": start_faq_add, "quiet": show_quiet, "morning": send_morning,
        "stats": show_stats, "chats": show_chats, "app": show_app, "my": show_my_polls,
        "cancel": cancel,
    }
    if cmd == "shelf":
        await (shelf_search(uid, rest) if rest else ask_shelf(uid))
        return
    h = handlers.get(cmd)
    if h:
        await h(uid)
    else:
        await send(uid, "Не знаю такой команды. Вот что я умею:", await menu_for(uid))


async def menu_for(uid: int) -> list[list[dict]]:
    if await chats.is_teacher(uid):
        return teacher_menu()
    return parent_menu()


async def show_start(uid: int) -> None:
    if await chats.is_teacher(uid):
        await send(uid, TEACHER_HELP, teacher_menu())
    elif await chats.member_chats(uid):
        await send(uid, PARENT_HELP, parent_menu())
    else:
        await send(uid, ONBOARD_HELP, parent_menu())


async def cancel(uid: int) -> None:
    await send(uid, "Отменил. Чем помочь?", await menu_for(uid))


async def show_app(uid: int) -> None:
    c = ctx()
    if not c.bot_username:
        await send(uid, "Приложение ещё не подключено к боту.")
        return
    await send(uid, "📱 В приложении — все сборы, справочник, полка и настройки на одном экране.",
               [[kb.app("Открыть «Перемену»", c.bot_username, "home")]])


async def _teacher_or_explain(uid: int) -> bool:
    if await chats.is_teacher(uid):
        return True
    await send(uid, "Эта функция для классного руководителя. Добавьте меня в чат класса и назначьте "
                    "администратором — после этого всё заработает.\n\n" + ONBOARD_HELP)
    return False


# ====================================================================== состояние
async def handle_state(uid: int, state: dict, text: str, msg: dict) -> bool:
    flow, step, data = state["flow"], state["step"], state.get("data") or {}
    if flow == "poll":
        return await poll_step(uid, step, data, text)
    if flow == "faq_add":
        return await faq_add_step(uid, step, data, text)
    if flow == "faq_reply":
        return await faq_reply_step(uid, data, text)
    if flow == "shelf_search":
        await clear_state(uid)
        if text:
            await shelf_search(uid, text)
            return True
        return False
    if flow == "shelf_caption":
        await clear_state(uid)
        m = await shelf.update(data["mid"], uid, title=text) if text else None
        if m:
            await send(uid, "Подписал:\n\n" + await shelf.describe(m))
        return True
    if flow == "ask":
        await clear_state(uid)
        if text:
            await answer_text(uid, text, False)
            return True
        return False
    if flow == "hours":
        parsed = parse_hours(text)
        if not parsed:
            await send(uid, "Не понял. Напишите время так: <b>8:00-17:30</b>")
            return True
        await clear_state(uid)
        await update_user(uid, work_start=parsed[0], work_end=parsed[1])
        await show_quiet(uid)
        return True
    if flow == "urgent":
        await clear_state(uid)
        words = ", ".join(w.strip().lower() for w in text.split(",") if w.strip())[:300]
        if words:
            await update_user(uid, urgent_words=words)
        await show_quiet(uid)
        return True
    return False


async def update_user(uid: int, **fields) -> User | None:
    from ..db import session

    async with session() as s:
        u = await s.get(User, uid)
        if u:
            for k, v in fields.items():
                setattr(u, k, v)
        return u


# ====================================================================== «Перекличка»
async def start_poll(uid: int) -> None:
    if not await _teacher_or_explain(uid):
        return
    my_chats = await chats.teacher_chats(uid)
    if len(my_chats) == 1:
        await set_state(uid, "poll", "tpl", {"chat_id": my_chats[0].chat_id})
        await ask_template(uid)
        return
    await set_state(uid, "poll", "chat", {})
    rows = [[kb.cb(c.title or str(c.chat_id), f"f:chat:{c.chat_id}")] for c in my_chats[:20]]
    rows.append([kb.cb("Отмена", "f:cancel")])
    await send(uid, "📋 <b>Новый сбор.</b> В какой класс?", rows)


async def ask_template(uid: int) -> None:
    rows = [[kb.cb(t["label"], f"f:tpl:{key}")] for key, t in polls.TEMPLATES.items()]
    rows.append([kb.cb("Отмена", "f:cancel")])
    await send(uid, "Что собираем?", rows)


async def on_template(uid: int, key: str) -> None:
    st = await get_state(uid)
    if not st or st["flow"] != "poll":
        return
    data = st["data"]
    data["template"] = key
    await set_state(uid, "poll", "text", data)
    tpl = polls.TEMPLATES[key]
    opts = " / ".join(tpl["options"]) if tpl["options"] else "свои варианты"
    await send(uid, f"<b>{tpl['label']}</b> · кнопки: {html.escape(opts)}\n\n"
                    f"Напишите текст сбора одним сообщением. Первая строка станет заголовком.\n\n"
                    f"<i>{html.escape(tpl['hint'])}</i>", [[kb.cb("Отмена", "f:cancel")]])


async def ask_deadline(uid: int) -> None:
    rows = [
        [kb.cb("Сегодня 18:00", "f:dl:today"), kb.cb("Завтра 18:00", "f:dl:tomorrow")],
        [kb.cb("Пятница 18:00", "f:dl:friday"), kb.cb("Через неделю", "f:dl:week")],
        [kb.cb("Без срока", "f:dl:none"), kb.cb("Отмена", "f:cancel")],
    ]
    await send(uid, "⏱ До какого времени ждём ответы? Выберите или напишите: <b>28.09 18:00</b>, "
                    "<b>завтра 9:00</b>, <b>пятница</b>.", rows)


async def poll_step(uid: int, step: str, data: dict, text: str) -> bool:
    if step == "text":
        if len(text) < 3:
            await send(uid, "Слишком коротко — опишите, что собираем.")
            return True
        data["text"] = text[:3000]
        if data.get("template") == "choice":
            await set_state(uid, "poll", "options", data)
            await send(uid, "Пришлите варианты ответа — каждый с новой строки (от 2 до 10).\n"
                            "<i>Например:\n18 октября, 18:00\n19 октября, 18:30\nНужно уточнить</i>")
        else:
            await set_state(uid, "poll", "deadline", data)
            await ask_deadline(uid)
        return True
    if step == "options":
        opts = [o.strip(" -•\t") for o in text.splitlines() if o.strip(" -•\t")]
        if not 2 <= len(opts) <= 10:
            await send(uid, "Нужно от 2 до 10 вариантов, каждый с новой строки.")
            return True
        data["options"] = [o[:60] for o in opts]
        await set_state(uid, "poll", "deadline", data)
        await ask_deadline(uid)
        return True
    if step == "deadline":
        user = await chats.get_user(uid)
        dl = parse_deadline(text, user.tz if user else None)
        if not dl:
            await send(uid, "Не понял дату. Примеры: <b>28.09 18:00</b>, <b>завтра 9:00</b>, <b>пятница</b>.")
            return True
        data["deadline"] = dl.isoformat()
        await set_state(uid, "poll", "confirm", data)
        await show_poll_preview(uid, data)
        return True
    # на шагах выбора кнопкой текст не ждём
    await send(uid, "Выберите вариант кнопкой выше или напишите /cancel.")
    return True


async def on_deadline_preset(uid: int, key: str) -> None:
    st = await get_state(uid)
    if not st or st["flow"] != "poll":
        return
    data = st["data"]
    user = await chats.get_user(uid)
    data["deadline"] = None if key == "none" else preset_deadline(key, user.tz if user else None).isoformat()
    await set_state(uid, "poll", "confirm", data)
    await show_poll_preview(uid, data)


async def show_poll_preview(uid: int, data: dict) -> None:
    from datetime import datetime

    user = await chats.get_user(uid)
    chat = await chats.get_chat(data["chat_id"])
    count = await chats.sync_members(data["chat_id"])
    targets = await chats.class_list(data["chat_id"], exclude={uid})
    tpl = polls.TEMPLATES[data["template"]]
    opts = data.get("options") or tpl["options"]
    dl = datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None
    lines = [
        "👀 <b>Проверьте сбор</b>", "",
        f"📋 {html.escape(data['text'])}", "",
        "Кнопки: " + " · ".join(html.escape(o) for o in opts),
        f"Срок: <b>{fmt_dt(dl, user.tz if user else None)}</b>",
        f"Класс: {html.escape(chat.title if chat else '')}",
    ]
    if count < 0 or not targets:
        lines.append("\n⚠️ Не вижу список участников чата. Назначьте меня администратором — иначе сбор "
                     "уйдёт только сообщением в чат, без сверки «кто не ответил».")
    else:
        lines.append(f"Получат в личку: <b>{len(targets)}</b> человек + сообщение в чат класса.")
    await send(uid, "\n".join(lines), [[kb.cb("✅ Разослать", "f:send")], [kb.cb("Отмена", "f:cancel")]])


async def confirm_poll(uid: int) -> None:
    from datetime import datetime

    st = await get_state(uid)
    if not st or st["flow"] != "poll" or st["step"] != "confirm":
        await send(uid, "Этот черновик уже отправлен или устарел. Новый сбор — /poll")
        return
    await clear_state(uid)
    data = st["data"]
    dl = datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None
    poll = await polls.create_poll(uid, data["chat_id"], data["template"], data["text"], data.get("options") or [], dl)
    await send(uid, "⏳ Рассылаю…")
    report = await polls.send_poll(poll.id)
    lines = [f"✅ Сбор запущен: в личку доставлено <b>{report['delivered']}</b> из {report['total']}."]
    if report["failed"]:
        lines.append(f"{report['failed']} ещё не запускали бота — они ответят кнопкой в чате класса, "
                     f"ответ всё равно засчитается лично им.")
    if not report["group_posted"]:
        lines.append("⚠️ Не получилось написать в чат класса — проверьте, что я всё ещё в нём.")
    lines.append("Напомню молчащим за сутки и за 2 часа до срока. Статус — /status")
    await send(uid, "\n".join(lines))
    poll = await polls.get_poll(poll.id)
    text, buttons = await polls.status_card(poll)
    await send(uid, text, buttons)


async def show_status_list(uid: int) -> None:
    if not await _teacher_or_explain(uid):
        return
    items = await polls.list_polls(uid, include_closed=True, limit=10)
    if not items:
        await send(uid, "Сборов пока нет. Запустить первый — /poll", [[kb.cb("📋 Новый сбор", "m:poll")]])
        return
    open_ = [p for p in items if not p.closed]
    if len(open_) == 1:
        text, buttons = await polls.status_card(open_[0])
        await send(uid, text, buttons)
        return
    lines = ["📊 <b>Сборы</b>", ""]
    rows = []
    for p in items:
        answered, total, _ = polls.counts(p)
        mark = "✅" if p.closed else "⏳"
        lines.append(f"{mark} {html.escape(p.title)} — {answered}/{total}")
        rows.append([kb.cb(f"{mark} {p.title[:40]} · {answered}/{total}", f"ps:{p.id}")])
    await send(uid, "\n".join(lines), rows)


async def show_my_polls(uid: int) -> None:
    pending = await polls.pending_for_user(uid)
    if not pending:
        await send(uid, "✅ Неотвеченных сборов нет.", parent_menu())
        return
    for p in pending[:10]:
        chat = await chats.get_chat(p.chat_id)
        teacher = await chats.get_user(p.teacher_id)
        tz = teacher.tz if teacher else ctx().settings.default_tz
        await send(uid, polls.parent_text(p, tz, chat.title if chat else "", teacher.display_name if teacher else ""),
                   polls.answer_buttons(p))


# ====================================================================== «Справочник»
async def show_faq(uid: int) -> None:
    if not await _teacher_or_explain(uid):
        return
    text, entries = await faq.render_list(uid)
    rows = [[kb.cb("➕ Добавить", "fq:add")]]
    for e in entries[:10]:
        rows.append([kb.cb(f"🗑 {e.question[:50]}", f"fq:del:{e.id}")])
    c = ctx()
    if c.bot_username:
        rows.append([kb.app("Править в приложении", c.bot_username, "faq")])
    await send(uid, text, rows)


async def start_faq_add(uid: int) -> None:
    if not await _teacher_or_explain(uid):
        return
    await set_state(uid, "faq_add", "q", {})
    await send(uid, "📖 Какой вопрос вам задают? Напишите его так, как спрашивают родители.\n"
                    "<i>Например: «Во сколько родительское собрание?»</i>", [[kb.cb("Отмена", "f:cancel")]])


async def faq_add_step(uid: int, step: str, data: dict, text: str) -> bool:
    if step == "q":
        if len(text) < 3:
            await send(uid, "Напишите вопрос чуть подробнее.")
            return True
        data["q"] = text[:1000]
        await set_state(uid, "faq_add", "a", data)
        await send(uid, "Теперь ваш ответ — я буду отвечать им родителям от имени справочника "
                        "(с подписью, что это сохранил учитель).")
        return True
    if step == "a":
        await clear_state(uid)
        e = await faq.add_entry(uid, data["q"], text)
        await send(uid, f"✅ Сохранил.\n\n<b>{html.escape(e.question)}</b>\n→ {html.escape(e.answer)}\n\n"
                        f"<i>Ключевые слова: {html.escape(e.keywords or '—')}</i>",
                   [[kb.cb("➕ Ещё", "fq:add"), kb.cb("📖 Весь справочник", "m:faq")]])
        return True
    return False


async def faq_reply_step(uid: int, data: dict, text: str) -> bool:
    """Учитель отвечает на переданный вопрос: ответ уходит родителю и сохраняется в справочник."""
    await clear_state(uid)
    q = await ctx().kv.get(f"qq:{data['qid']}")
    if not q:
        await send(uid, "Вопрос устарел — ответьте родителю напрямую.")
        return True
    sent = await ctx().safe_send(
        user_id=q["from_user_id"],
        text=f"✉️ <b>Ответ учителя</b> на ваш вопрос «{html.escape(q['text'][:200])}»:\n\n{html.escape(text)}",
    )
    e = await faq.add_entry(uid, q["text"], text)
    await ctx().kv.delete(f"qq:{data['qid']}")
    await send(uid, ("✅ Ответ отправлен" if sent else "⚠️ Не удалось доставить ответ родителю") +
               " и сохранён в справочник — в следующий раз отвечу сам.",
               [[kb.cb("Отменить сохранение", f"fq:del:{e.id}")]])
    return True


# ====================================================================== вопросы
async def answer_text(uid: int, text: str, is_teacher: bool) -> None:
    low = text.lower()
    if is_teacher and low.startswith(("найди", "найти", "полка", "где ")):
        await shelf_search(uid, text.split(" ", 1)[1] if " " in text else "")
        return

    if is_teacher:
        entries = await faq.list_entries(uid)
        chat_ids = []
    else:
        chat_ids = [c.chat_id for c in await chats.member_chats(uid)]
        if not chat_ids:
            await send(uid, ONBOARD_HELP)
            return
        entries = await faq.candidates_for_chats(chat_ids)

    hits = faq.rank(text, entries)
    if hits and hits[0].score >= faq.ANSWER_SCORE:
        e = hits[0].item
        prefix = "👩‍🏫 <i>Так я отвечу родителям:</i>\n\n" if is_teacher else ""
        await send(uid, prefix + await faq.render_answer(e), [[kb.cb("Это не то", f"fq:no:{e.id}")]] if not is_teacher else None)
        if not is_teacher:
            await faq.mark_hit(e.id)
            await stats.add(e.teacher_id, "faq_answer")
        return

    # материалы на полке
    mats = await shelf.find(uid, text, teacher_view=is_teacher, limit=3)
    if hits:
        rows = [[kb.cb(h.item.question[:60], f"fq:show:{h.item.id}")] for h in hits]
        if not is_teacher:
            rows.append([kb.cb("Нет, спросить учителя", "fw:ask")])
            await ctx().kv.set(f"q:{uid}", {"text": text, "chats": chat_ids}, 3600 * 6)
        await send(uid, "🤔 Возможно, вы имели в виду:", rows)
        return
    if mats:
        await send_material_list(uid, mats, is_teacher, header="🗂 Нашёл на полке:")
        if not is_teacher:
            await ctx().kv.set(f"q:{uid}", {"text": text, "chats": chat_ids}, 3600 * 6)
            await send(uid, "Если это не то — могу передать вопрос учителю.", [[kb.cb("Спросить учителя", "fw:ask")]])
        return

    if is_teacher:
        await send(uid, "В справочнике и на полке такого нет. Добавить ответ — /add, найти файл — /shelf.",
                   teacher_menu())
        return
    await ctx().kv.set(f"q:{uid}", {"text": text, "chats": chat_ids}, 3600 * 6)
    await send(uid, "В справочнике класса пока нет готового ответа. Передать вопрос учителю?",
               [[kb.cb("✉️ Передать учителю", "fw:ask")], [kb.cb("Не надо", "fw:x")]])


async def forward_question(uid: int, chat_id: int | None = None) -> None:
    c = ctx()
    q = await c.kv.get(f"q:{uid}")
    if not q:
        await send(uid, "Напишите вопрос ещё раз — я передам его учителю.")
        return
    chat_ids = q.get("chats") or []
    if chat_id is None:
        teachers: dict[int, int] = {}
        for cid in chat_ids:
            ch = await chats.get_chat(cid)
            if ch and ch.teacher_id and ch.teacher_id not in teachers:
                teachers[ch.teacher_id] = cid
        if len(teachers) > 1:
            rows = []
            for tid, cid in teachers.items():
                ch = await chats.get_chat(cid)
                rows.append([kb.cb(ch.title or "Класс", f"fw:{cid}")])
            await send(uid, "Какому классному руководителю передать?", rows)
            return
        if not teachers:
            await send(uid, "Не нашёл учителя вашего класса. Напишите ему напрямую.")
            return
        chat_id = next(iter(teachers.values()))
    ch = await chats.get_chat(chat_id)
    if not ch or not ch.teacher_id:
        await send(uid, "Не нашёл учителя этого класса.")
        return
    await c.kv.delete(f"q:{uid}")
    name = await chats.member_name(chat_id, uid)
    qid = secrets.token_hex(5)
    await c.kv.set(f"qq:{qid}", {"from_user_id": uid, "text": q["text"], "chat_id": chat_id}, 3600 * 24 * 7)
    text = (f"❓ <b>Вопрос</b> от <a href=\"max://user/{uid}\">{html.escape(name)}</a> "
            f"({html.escape(ch.title or 'класс')}):\n\n{html.escape(q['text'])}\n\n"
            f"<i>Ответьте кнопкой — ответ уйдёт родителю и сохранится в справочник.</i>")
    now = await quiet.notify_teacher(
        ch.teacher_id, text, kind="question", chat_id=chat_id, from_user_id=uid, from_name=name,
        raw_text=q["text"], buttons=[[kb.cb("✍️ Ответить и сохранить", f"fr:{qid}")]],
    )
    if now:
        await send(uid, "✅ Передал учителю. Ответ придёт сюда или в чат класса.")
    else:
        teacher = await chats.get_user(ch.teacher_id)
        start = fmt_minutes(teacher.work_start) if teacher else "8:00"
        await send(uid, f"🌙 Учитель сейчас вне рабочего времени. Вопрос записан — он увидит его утром "
                        f"(с {start}). Если дело срочное, напишите вопрос со словом «срочно».")


# ====================================================================== «Полка»
async def save_to_shelf(uid: int, media: list[dict], caption: str) -> None:
    my_chats = await chats.teacher_chats(uid)
    for item in media:
        m, created = await shelf.save(uid, item, caption, source="manual")
        rows = [[kb.cb(f"➡️ В {c.title[:30] or 'класс'}", f"sh:to:{m.id}:{c.chat_id}")] for c in my_chats[:4]]
        rows.append([kb.cb("✏️ Подпись и теги", f"sh:cap:{m.id}"), kb.cb("🗑 Убрать", f"sh:del:{m.id}")])
        head = "🗂 Положил на полку:" if created else "🗂 Этот файл уже на полке:"
        tip = "" if m.title else "\n\n<i>Добавьте подпись — по ней потом легко найти. #теги тоже работают.</i>"
        await send(uid, f"{head}\n\n{await shelf.describe(m)}{tip}", rows)


async def ask_shelf(uid: int) -> None:
    await set_state(uid, "shelf_search", "q", {})
    await send(uid, "🗂 Что найти на полке? Напишите пару слов из названия, подписи или тег.\n"
                    "<i>Например: памятка пожарная безопасность</i>")


async def shelf_search(uid: int, query: str) -> None:
    is_teacher = await chats.is_teacher(uid)
    if not query.strip():
        await ask_shelf(uid)
        return
    hits = await shelf.find(uid, query, teacher_view=is_teacher, limit=6)
    if not hits:
        await send(uid, "Ничего не нашёл. Попробуйте другие слова или тег.")
        return
    await send_material_list(uid, hits, is_teacher, header=f"🗂 Нашёл по запросу «{html.escape(query)}»:")


async def send_material_list(uid: int, hits, is_teacher: bool, header: str) -> None:
    if len(hits) == 1:
        await show_material(uid, hits[0].item.id, is_teacher)
        return
    lines = [header, ""]
    rows = []
    for i, h in enumerate(hits, 1):
        m = h.item
        lines.append(f"{i}. {html.escape(m.filename or 'Файл')}" + (f" — {html.escape(m.title[:60])}" if m.title else ""))
        rows.append([kb.cb(f"{i}. {(m.title or m.filename)[:50]}", f"sh:open:{m.id}")])
    await send(uid, "\n".join(lines), rows)


async def show_material(uid: int, mid: int, is_teacher: bool | None = None) -> None:
    m = await shelf.get(mid)
    if not m:
        await send(uid, "Материал не найден — возможно, его убрали с полки.")
        return
    if is_teacher is None:
        is_teacher = m.teacher_id == uid
    rows = [[kb.cb("📥 Прислать мне", f"sh:get:{m.id}")]]
    if is_teacher and m.teacher_id == uid:
        for c in (await chats.teacher_chats(uid))[:4]:
            rows.append([kb.cb(f"➡️ Переслать в {c.title[:30] or 'класс'}", f"sh:to:{m.id}:{c.chat_id}")])
        rows.append([kb.cb("✏️ Подпись", f"sh:cap:{m.id}"), kb.cb("🗑 Убрать", f"sh:del:{m.id}")])
    await send(uid, await shelf.describe(m), rows)


# ====================================================================== настройки
async def show_quiet(uid: int) -> None:
    if not await _teacher_or_explain(uid):
        return
    u = await chats.get_user(uid)
    my_chats = await chats.teacher_chats(uid)
    tz_name = next((n for n, z in TIMEZONES if z == u.tz), u.tz)
    lines = [
        "🌙 <b>Тихий режим</b>", "",
        f"Тихий режим: <b>{'включён' if u.quiet_enabled else 'выключен'}</b>",
        f"Рабочее время: <b>{fmt_minutes(u.work_start)}–{fmt_minutes(u.work_end)}</b> ({html.escape(tz_name)})",
        f"Выходные: <b>{'не беспокоить' if u.weekends_off else 'рабочие'}</b>",
        f"Слово-маркер срочного: <b>{html.escape(u.urgent_words)}</b>",
        f"Предлагать сохранять ответы из чата: <b>{'да' if u.suggest_save else 'нет'}</b>",
        "",
        "Вне рабочего времени я продолжаю отвечать родителям тем, что знаю, а всё новое приношу утром одной карточкой.",
    ]
    if my_chats:
        lines.append("")
        lines.append("Автоответ прямо в чате класса (по точному совпадению со справочником):")
        for c in my_chats:
            lines.append(f"• {html.escape(c.title or str(c.chat_id))}: {'вкл' if c.autoanswer else 'выкл'}")
    rows = [
        [kb.cb("8–17", "q:h:480:1020"), kb.cb("8–19", "q:h:480:1140"), kb.cb("9–18", "q:h:540:1080")],
        [kb.cb("✏️ Своё время", "q:hours"), kb.cb("🌍 Часовой пояс", "q:tzlist")],
        [kb.cb(f"Выходные: {'тихо' if u.weekends_off else 'работаю'}", "q:we"),
         kb.cb(f"Тихий режим: {'вкл' if u.quiet_enabled else 'выкл'}", "q:on")],
        [kb.cb("Слово-маркер", "q:urgent"), kb.cb(f"Сохранять ответы: {'да' if u.suggest_save else 'нет'}", "q:sugg")],
    ]
    for c in my_chats[:5]:
        rows.append([kb.cb(f"Автоответ в {c.title[:25] or 'чате'}: {'вкл' if c.autoanswer else 'выкл'}",
                           f"q:auto:{c.chat_id}")])
    await send(uid, "\n".join(lines), rows)


async def send_morning(uid: int) -> None:
    if not await _teacher_or_explain(uid):
        return
    u = await chats.get_user(uid)
    await quiet.send_digest(u, include_empty=True)


async def show_stats(uid: int) -> None:
    if not await _teacher_or_explain(uid):
        return
    await send(uid, await stats.render(uid))


async def show_chats(uid: int) -> None:
    my_chats = await chats.teacher_chats(uid)
    if not my_chats:
        await send(uid, ONBOARD_HELP)
        return
    lines = ["🏫 <b>Мои классы</b>", ""]
    rows = []
    for c in my_chats:
        members = await chats.class_list(c.chat_id, exclude={uid})
        admin = "администратор ✅" if c.bot_is_admin else "⚠️ я не администратор"
        lines.append(f"<b>{html.escape(c.title or str(c.chat_id))}</b> — в списке {len(members)}, {admin}")
        rows.append([kb.cb(f"🔄 {c.title[:20]}: обновить список", f"c:sync:{c.chat_id}"),
                     kb.cb("📥 Файлы из истории", f"c:hist:{c.chat_id}")])
    lines.append("")
    lines.append("<i>Чтобы я видел вопросы и файлы в чате и сверял ответы со списком класса, "
                 "назначьте меня администратором с правом читать все сообщения.</i>")
    await send(uid, "\n".join(lines), rows)

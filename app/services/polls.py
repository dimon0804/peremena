"""«Перекличка» — сбор ответов, доведённый до конца без ручного подсчёта.

Учитель задаёт текст, варианты и срок. Бот берёт список участников чата,
рассылает каждому в личку кнопки, принимает ответы (ответ однозначно привязан к человеку),
сам напоминает молчащим за сутки и за два часа до срока и в любой момент показывает
«Ответили 25 из 27. Не ответили: …».
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy import update as sa_update

from .. import keyboards as kb
from ..core import ctx
from ..db import session
from ..max_api import MaxApiError
from ..models import ClassChat, Poll, PollTarget, User
from ..timeutil import fmt_dt, local_now, to_local, to_utc, utcnow
from . import chats, stats
from .quiet import notify_teacher

log = logging.getLogger(__name__)

TEMPLATES: dict[str, dict] = {
    "consent": {
        "label": "Согласие",
        "hint": "Например: «Экскурсия в музей в субботу, 300 ₽. Нужно согласие до пятницы.»",
        "options": ["Согласен", "Не согласен", "Нужно уточнить"],
        "clarify": 2,
    },
    "certificate": {
        "label": "Справка",
        "hint": "Например: «Принесите, пожалуйста, справку после болезни / медосмотра до пятницы.»",
        "options": ["Справку принесу", "Справка уже сдана", "Нужно уточнить"],
        "clarify": 2,
    },
    "ack": {
        "label": "Ознакомление",
        "hint": "Например: «Ознакомьтесь с правилами поведения на каникулах (файл в чате).»",
        "options": ["Ознакомлен(а)", "Есть вопрос"],
        "clarify": 1,
    },
    "signup": {
        "label": "Запись на мероприятие",
        "hint": "Например: «Субботник в школьном саду, суббота 10:00. Кто сможет прийти с ребёнком?»",
        "options": ["Записываюсь", "Не смогу", "Нужно уточнить"],
        "clarify": 2,
    },
    "money": {
        "label": "Сбор денег",
        "hint": "Например: «Экскурсия в музей в субботу, оплата до пятницы.» Сумму спрошу следующим шагом.",
        "options": ["Перевёл(а)", "Сдам наличными", "Не участвуем", "Нужно уточнить"],
        "clarify": 3,
    },
    "choice": {
        "label": "Выбор варианта",
        "hint": "Например: «Выбираем дату родительского собрания.» Варианты пришлёте следующим сообщением.",
        "options": [],
        "clarify": None,
    },
}

CLARIFY_WORDS = ("уточн", "вопрос")

# сбор денег: индексы вариантов ответа
PAY_TRANSFER, PAY_CASH, PAY_SKIP = 0, 1, 2


def _split_text(text: str) -> tuple[str, str]:
    lines = [l for l in (text or "").strip().splitlines()]
    title = (lines[0] if lines else "Сбор").strip()
    if len(title) > 120:
        title = title[:117] + "…"
    return title, text.strip()


def answer_buttons(poll: Poll, selected: int | None = None) -> list[list[dict]]:
    rows = []
    for i, opt in enumerate(poll.options):
        mark = "✓ " if selected == i else ""
        rows.append([kb.cb(f"{mark}{opt}", f"pa:{poll.id}:{i}")])
    return rows


async def _teacher(poll: Poll) -> User | None:
    async with session() as s:
        return await s.get(User, poll.teacher_id)


def parent_text(poll: Poll, tz: str, chat_title: str, teacher_name: str, selected: int | None = None,
                reminder: bool = False) -> str:
    head = "⏰ <b>Напоминание</b>\n\n" if reminder else ""
    parts = [head + f"📋 {html.escape(poll.text)}"]
    meta = []
    if poll.amount:
        meta.append(f"Сумма: <b>{rub(poll.amount)}</b>")
    if poll.deadline:
        meta.append(f"Срок ответа: <b>{fmt_dt(poll.deadline, tz)}</b>")
    if chat_title:
        meta.append(f"Класс: {html.escape(chat_title)}")
    if teacher_name:
        meta.append(f"Учитель: {html.escape(teacher_name)}")
    parts.append("\n".join(meta))
    if selected is not None and 0 <= selected < len(poll.options):
        parts.append(f"✅ Ваш ответ: <b>{html.escape(poll.options[selected])}</b>. Изменить можно до срока.")
    else:
        parts.append("Нажмите кнопку — ответ сразу попадёт учителю.")
    return "\n\n".join(p for p in parts if p)


def rub(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


async def create_poll(teacher_id: int, chat_id: int, template: str, text: str, options: list[str],
                      deadline, amount: int | None = None, send_at=None) -> Poll:
    tpl = TEMPLATES.get(template, TEMPLATES["consent"])
    opts = [o.strip()[:60] for o in (options or tpl["options"]) if o.strip()][:10]
    clarify = tpl["clarify"]
    if template == "choice" or clarify is None:
        clarify = next((i for i, o in enumerate(opts) if o.lower().startswith(CLARIFY_WORDS)), None)
    title, body = _split_text(text)
    async with session() as s:
        poll = Poll(
            teacher_id=teacher_id, chat_id=chat_id, template=template, title=title, text=body,
            options=opts, clarify_option=clarify, deadline=deadline,
            amount=amount if template == "money" and amount else None,
            dispatched=False, send_at=send_at,
        )
        s.add(poll)
        await s.flush()
        return poll


async def get_poll(poll_id: int) -> Poll | None:
    async with session() as s:
        return await s.get(Poll, poll_id)


# сборы, которые рассылаются в этом процессе прямо сейчас (возобновление их не трогает)
_dispatching: set[int] = set()
SEND_CONCURRENCY = 5  # одновременных отправок: общий лимит MAX держит ограничитель клиента


async def send_many(jobs: list[tuple[object, dict]]) -> list[tuple[object, dict | None]]:
    """Отправляет пачку сообщений с ограниченным параллелизмом. jobs — (ключ, kwargs для send_message).
    Возвращает (ключ, ответ MAX или None при ошибке) в том же порядке."""
    c = ctx()
    sem = asyncio.Semaphore(SEND_CONCURRENCY)

    async def one(key, kw):
        async with sem:
            try:
                return key, await c.api.send_message(**kw)
            except MaxApiError as e:
                log.info("Не доставлено %s: %s", kw.get("user_id") or kw.get("chat_id"), e)
                return key, None

    return list(await asyncio.gather(*(one(k, kw) for k, kw in jobs)))


async def send_poll(poll_id: int) -> dict:
    """Рассылает сбор: каждому в личку + одно сообщение в чат класса (как запасной путь).
    Можно вызывать повторно: уже доставленным второй раз не пишем — так рассылка, прерванная
    перезапуском, продолжается с места остановки."""
    if poll_id in _dispatching:
        return {"total": 0, "delivered": 0, "failed": 0, "synced": 0, "group_posted": False, "busy": True}
    _dispatching.add(poll_id)
    try:
        return await _send_poll(poll_id)
    finally:
        _dispatching.discard(poll_id)


async def _send_poll(poll_id: int) -> dict:
    c = ctx()
    poll = await get_poll(poll_id)
    assert poll is not None
    chat = await chats.get_chat(poll.chat_id)
    teacher = await _teacher(poll)
    tz = teacher.tz if teacher else c.settings.default_tz
    teacher_name = teacher.display_name if teacher else ""
    chat_title = chat.title if chat else ""

    synced = await chats.sync_members(poll.chat_id)
    members = await chats.class_list(poll.chat_id, exclude={poll.teacher_id})

    async with session() as s:
        known = {t.user_id: t for t in (await s.scalars(select(PollTarget).where(PollTarget.poll_id == poll.id))).all()}
        for m in members:
            if m.user_id not in known:
                t = PollTarget(poll_id=poll.id, user_id=m.user_id, name=m.name)
                s.add(t)
                known[m.user_id] = t
        await s.flush()
        todo = [(t.user_id, t.id) for t in known.values() if not t.delivered and t.answer is None]
        already = sum(1 for t in known.values() if t.delivered)

    text = parent_text(poll, tz, chat_title, teacher_name)
    att = kb.kb_list(answer_buttons(poll))
    results = await send_many([((uid, tid), {"user_id": uid, "text": text, "attachments": att}) for uid, tid in todo])
    delivered = already
    failed = 0
    async with session() as s:
        for (uid, tid), msg in results:
            if msg:
                delivered += 1
                row = await s.get(PollTarget, tid)
                if row:
                    row.delivered = True
                    row.message_id = (msg.get("body") or {}).get("mid")
            else:
                failed += 1

    # Сообщение в общий чат: кто не получил личку (не запускал бота), ответит здесь — ответ всё равно личный.
    gmsg = None
    if not poll.group_message_id:
        group_text = (
            f"📋 <b>Сбор от учителя</b>\n\n{html.escape(poll.text)}\n\n"
            + (f"Сумма: <b>{rub(poll.amount)}</b>\n" if poll.amount else "")
            + (f"Срок: <b>{fmt_dt(poll.deadline, tz)}</b>\n" if poll.deadline else "")
            + "Ответьте кнопкой ниже — ответ засчитается лично вам, другим участникам он не виден."
        )
        gmsg = await c.safe_send(chat_id=poll.chat_id, text=group_text, attachments=att)
    async with session() as s:
        p = await s.get(Poll, poll.id)
        if gmsg:
            p.group_message_id = (gmsg.get("body") or {}).get("mid")
        p.dispatched = True
        p.send_at = None

    if not poll.dispatched:  # первая рассылка — сообщаем внешним системам (вебхук poll.created)
        await _webhook("on_poll_created", await get_poll(poll.id))
    total = len(members) if members else delivered + failed
    return {"total": total, "delivered": delivered, "failed": failed, "synced": synced,
            "group_posted": bool(gmsg or poll.group_message_id)}


async def resume_dispatch() -> int:
    """Сборы, рассылка которых не доведена до конца (перезапуск посреди рассылки) или отложена
    до утра и время пришло, — досылаем. Возвращает, сколько сборов обработано."""
    now = utcnow()
    async with session() as s:
        rows = (await s.scalars(select(Poll).where(Poll.dispatched.is_(False), Poll.closed.is_(False)))).all()
    done = 0
    for p in rows:
        if p.id in _dispatching:
            continue
        if p.send_at is not None:
            if p.send_at > now:
                continue
        elif now - p.created_at < timedelta(minutes=2):
            continue  # только что созданный сбор рассылает тот, кто его создал
        try:
            rep = await send_poll(p.id)
            done += 1
            if p.send_at is not None and not rep.get("busy"):
                await notify_teacher(
                    p.teacher_id, f"📤 Сбор «{html.escape(p.title)}» разослан: в личку доставлено "
                                  f"{rep['delivered']} из {rep['total']}.",
                    kind="report", chat_id=p.chat_id, raw_text=f"«{p.title}»: разослан утром")
        except Exception:
            log.exception("Досылка сбора %s", p.id)
    return done


async def record_answer(poll_id: int, user: dict, option: int, chat_id_hint: int | None = None) -> tuple[Poll | None, str]:
    """Записывает ответ. Возвращает (poll, статус): ok | changed | same | closed | bad | forbidden.
    chat_id_hint — чат, где нажата кнопка: нажатие под сообщением в чате класса доказывает, что человек в нём."""
    uid = user.get("user_id")
    async with session() as s:
        poll = await s.get(Poll, poll_id)
        if poll is None or not (0 <= option < len(poll.options)):
            return poll, "bad"
        if poll.closed:
            return poll, "closed"
        t = await s.scalar(select(PollTarget).where(PollTarget.poll_id == poll_id, PollTarget.user_id == uid))
        if t is None and uid == poll.teacher_id:
            return poll, "forbidden"
        if t is None and chat_id_hint != poll.chat_id and not await chats.is_present(poll.chat_id, uid):
            # чужой сбор (ссылка ?start=poll_N переслана постороннему): ответ не принимаем,
            # иначе итог «1/4» вместо «1/3» и в списке класса чужой человек
            return poll, "forbidden"
        status = "ok"
        if t is None:
            # человек пришёл в чат позже рассылки — добавляем его в список
            name = await chats.member_name(poll.chat_id, uid, "")
            if not name or name == str(uid):
                from ..models import display_name

                name = display_name(user.get("first_name") or user.get("name"), user.get("last_name"))
            t = PollTarget(poll_id=poll_id, user_id=uid, name=name)
            s.add(t)
        elif t.answer is not None:
            status = "changed" if t.answer != option else "same"
        t.answer = option
        t.answered_at = utcnow()
        await s.flush()
        await s.refresh(poll)
    return poll, status


async def _webhook(name: str, *args) -> None:
    """Событие для внешних систем (app/integrations/webhooks.py); ошибка интеграции сбор не ломает."""
    from ..integrations import webhooks

    await webhooks.safe(getattr(webhooks, name)(*args))


async def after_answer(poll: Poll, uid: int, opt: int, status: str) -> None:
    """«Нужно уточнить» — сразу учителю (с учётом тихого режима); все ответили — сообщаем итог."""
    if status in ("ok", "changed"):
        await _webhook("on_poll_answered", poll, uid, opt, status)
    if poll.clarify_option is not None and opt == poll.clarify_option and status != "same":
        name = await chats.member_name(poll.chat_id, uid)
        option = poll.options[opt]
        text = (f"💬 <b>Нужно уточнить</b> по сбору «{html.escape(poll.title)}»\n"
                f"<a href=\"max://user/{uid}\">{html.escape(name)}</a> выбрал(а) «{html.escape(option)}» — "
                f"напишите ему(ей) напрямую.")
        await notify_teacher(poll.teacher_id, text, kind="clarify", chat_id=poll.chat_id,
                             from_user_id=uid, from_name=name, raw_text=f"«{poll.title}»: {option}")
    if status != "ok" or poll.closed:
        return
    chat = await chats.get_chat(poll.chat_id)
    if not chat or not chat.bot_is_admin:
        return  # без списка класса «все» — это только те, кто ответил; такое уведомление обманывает
    await backfill_targets(poll.chat_id)
    poll = await get_poll(poll.id)
    answered, total, _ = counts(poll)
    # два последних ответа почти одновременно — оба видят «все ответили»; сообщаем учителю один раз
    if total and answered == total and await ctx().kv.add_once(f"allans:{poll.id}:{total}", ttl=30 * 24 * 3600):
        text, buttons = await status_card(poll)
        await notify_teacher(poll.teacher_id, "🎉 Ответили все!\n\n" + text, kind="report",
                             chat_id=poll.chat_id, raw_text=f"«{poll.title}»: ответили все {total}",
                             buttons=buttons)


async def backfill_targets(chat_id: int) -> int:
    """Добавляет в открытые сборы чата тех, кто есть в списке класса, но не попал в рассылку
    (пришёл в чат позже или список был недоступен, пока бот не стал администратором)."""
    async with session() as s:
        open_polls = (await s.scalars(select(Poll).where(Poll.chat_id == chat_id, Poll.closed.is_(False)))).all()
    if not open_polls:
        return 0
    added = 0
    for p in open_polls:
        members = await chats.class_list(chat_id, exclude={p.teacher_id})
        known = {t.user_id for t in p.targets}
        async with session() as s:
            for m in members:
                if m.user_id not in known:
                    s.add(PollTarget(poll_id=p.id, user_id=m.user_id, name=m.name))
                    added += 1
    return added


async def drop_member(chat_id: int, user_id: int) -> int:
    """Человек ушёл из чата класса: убираем его из открытых сборов, где он ещё не ответил, —
    иначе ему продолжают приходить напоминания, а «25/27» никогда не станет «все ответили».
    Уже данные ответы остаются: они были, учителю их видеть нужно."""
    async with session() as s:
        rows = (await s.scalars(
            select(PollTarget).join(Poll, Poll.id == PollTarget.poll_id).where(
                Poll.chat_id == chat_id, Poll.closed.is_(False), PollTarget.user_id == user_id,
                PollTarget.answer.is_(None), PollTarget.paid.is_(False))
        )).all()
        for t in rows:
            await s.delete(t)
    return len(rows)


def counts(poll: Poll) -> tuple[int, int, list[int]]:
    per = [0] * len(poll.options)
    answered = 0
    for t in poll.targets:
        if t.answer is not None and 0 <= t.answer < len(per):
            per[t.answer] += 1
            answered += 1
    return answered, len(poll.targets), per


def money(poll: Poll) -> dict | None:
    """Сбор денег: сколько ждём, сколько учитель подтвердил, кто отметил «перевёл», но ещё не подтверждён."""
    if not poll.amount:
        return None
    # сдал деньги — участник, даже если потом нажал «Не участвуем»: деньги у учителя, их надо учесть
    participants = [t for t in poll.targets if t.answer != PAY_SKIP or t.paid]
    paid = [t for t in poll.targets if t.paid]
    waiting = [t for t in participants if not t.paid and t.answer in (PAY_TRANSFER, PAY_CASH)]
    return {
        "amount": poll.amount,
        "expected": poll.amount * len(participants), "collected": poll.amount * len(paid),
        "participants": len(participants), "paid": len(paid),
        "waiting": [t.name + (" (перевёл)" if t.answer == PAY_TRANSFER else " (наличные)") for t in waiting],
        "not_paid": [t.name for t in participants if not t.paid],
    }


PAY_PAGE = 25  # MAX принимает не больше 30 рядов кнопок — оставляем место под листание и «Готово»


def pay_buttons(poll: Poll, page: int = 0) -> list[list[dict]]:
    """Кнопки «отметить сдавших»: ✅/⬜ у каждого участника (кроме «не участвуем», если он не сдавал).
    В кнопке — значение, которое она поставит (а не «переключить»): двойное нажатие не снимет отметку."""
    people = [t for t in poll.targets if t.answer != PAY_SKIP or t.paid]
    pages = max(1, -(-len(people) // PAY_PAGE))
    page = min(max(page, 0), pages - 1)
    rows = []
    for t in people[page * PAY_PAGE:(page + 1) * PAY_PAGE]:
        hint = " · перевёл" if t.answer == PAY_TRANSFER else " · наличные" if t.answer == PAY_CASH else \
            " · не участвует" if t.answer == PAY_SKIP else ""
        rows.append([kb.cb(f"{'✅' if t.paid else '⬜'} {t.name}{hint}"[:64],
                           f"pp:{poll.id}:{t.id}:{0 if t.paid else 1}:{page}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(kb.cb("◀️ Назад", f"pm:{poll.id}:{page - 1}"))
        nav.append(kb.cb(f"{page + 1}/{pages}", f"pm:{poll.id}:{page}"))
        if page < pages - 1:
            nav.append(kb.cb("Дальше ▶️", f"pm:{poll.id}:{page + 1}"))
        rows.append(nav)
    rows.append([kb.cb("Готово", f"ps:{poll.id}")])
    return rows


async def set_paid(poll_id: int, target_id: int, paid: bool | None = None) -> PollTarget | None:
    """Учитель подтверждает (или снимает) оплату. paid=None — переключить. Родителю — уведомление."""
    async with session() as s:
        t = await s.get(PollTarget, target_id)
        if t is None or t.poll_id != poll_id:
            return None
        new = (not t.paid) if paid is None else paid
        changed = new != t.paid
        t.paid = new
        t.paid_at = utcnow() if new else None
    poll = await get_poll(poll_id)
    if changed and new and poll and poll.amount:
        await ctx().safe_send(user_id=t.user_id,
                              text=f"✅ Учитель подтвердил получение {rub(poll.amount)} — «{html.escape(poll.title)}». "
                                   "Спасибо!")
    return t


def progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * done / total)
    return "▓" * filled + "░" * (width - filled)


async def status_card(poll: Poll) -> tuple[str, list[list[dict]]]:
    c = ctx()
    teacher = await _teacher(poll)
    tz = teacher.tz if teacher else c.settings.default_tz
    chat = await chats.get_chat(poll.chat_id)
    if chat and chat.bot_is_admin and not poll.closed and await backfill_targets(poll.chat_id):
        poll = await get_poll(poll.id)
    answered, total, per = counts(poll)
    missing = [t.name for t in poll.targets if t.answer is None]
    state = "закрыт" if poll.closed else f"срок: {fmt_dt(poll.deadline, tz)}"
    lines = [
        f"📋 <b>{html.escape(poll.title)}</b>",
        f"{html.escape(chat.title if chat else '')} · {state}",
        "",
        f"Ответили <b>{answered} из {total}</b>  {progress_bar(answered, total)}",
        " · ".join(f"{html.escape(o)} — {n}" for o, n in zip(poll.options, per)),
    ]
    m = money(poll)
    if m:
        lines += ["", f"💰 <b>Собрано {rub(m['collected'])} из {rub(m['expected'])}</b> "
                      f"(подтверждено {m['paid']} из {m['participants']})"]
        if m["waiting"]:
            lines.append("Ждут подтверждения: " + html.escape(", ".join(m["waiting"][:15]))
                         + (" …" if len(m["waiting"]) > 15 else ""))
    if missing:
        shown = ", ".join(missing[:25])
        lines += ["", f"<b>Не ответили ({len(missing)}):</b> {html.escape(shown)}" + (" …" if len(missing) > 25 else "")]
    elif total:
        lines += ["", "✅ Ответили все!"]
    else:
        lines += ["", "Список класса пуст: сделайте бота администратором чата, чтобы сверять ответы со списком."]
    buttons: list[list[dict]] = []
    if not poll.closed:
        row = []
        if missing:
            row.append(kb.cb("Напомнить", f"pr:{poll.id}"))
        row.append(kb.cb("Список", f"pl:{poll.id}"))
        buttons.append(row)
        buttons.append([kb.cb("🔄 Обновить", f"ps:{poll.id}"), kb.cb("Закрыть сбор", f"pc:{poll.id}")])
        if poll.amount:
            buttons.insert(0, [kb.cb("💰 Отметить сдавших", f"pm:{poll.id}")])
    else:
        buttons.append([kb.cb("Список", f"pl:{poll.id}"), kb.cb("🔁 Повторить", f"prp:{poll.id}")])
        buttons.append([kb.cb("🗑 Удалить сбор", f"pd:{poll.id}")])
    from ..integrations import printing, tokens

    base = tokens.public_base()
    if base.startswith("https://"):  # ведомость для подписи и бланки с QR (ссылки на сутки)
        buttons.append([kb.link("🖨 Ведомость", base + printing.url(poll, "sheet")),
                        kb.link("📝 Бланки с QR", base + printing.url(poll, "blanks"))])
    if c.bot_username:
        buttons.append([kb.app("Открыть в приложении", c.bot_username, f"poll_{poll.id}")])
    return "\n".join(lines), buttons


def full_list(poll: Poll) -> str:
    lines = [f"📄 <b>{html.escape(poll.title)}</b> — список", ""]
    m = money(poll)
    if m:
        lines += [f"💰 Собрано {rub(m['collected'])} из {rub(m['expected'])}. Не сдали: "
                  + (html.escape(", ".join(m["not_paid"])) if m["not_paid"] else "—"), ""]
    for i, opt in enumerate(poll.options):
        names = [t.name + (" ✅" if m and t.paid else "") for t in poll.targets if t.answer == i]
        lines.append(f"<b>{html.escape(opt)} ({len(names)}):</b>")
        lines.append(html.escape(", ".join(names)) if names else "—")
        lines.append("")
    missing = [t.name for t in poll.targets if t.answer is None]
    lines.append(f"<b>Не ответили ({len(missing)}):</b>")
    lines.append(html.escape(", ".join(missing)) if missing else "—")
    return "\n".join(lines)


async def remind(poll_id: int, auto: bool = False) -> dict:
    """Напоминание молчащим: в личку, а кому личка не дошла — одним сообщением в чат класса.
    Возвращает {"sent", "group", "pending"}; "dup" — такое же ручное напоминание только что уже ушло."""
    c = ctx()
    poll = await get_poll(poll_id)
    if poll is None or poll.closed:
        return {"sent": 0, "group": 0, "pending": 0}
    if not auto and not await c.kv.add_once(f"rmd:{poll_id}", ttl=60):
        # двойное нажатие «Напомнить» (или кнопка + приложение) — второй раз родителям не пишем
        return {"sent": 0, "group": 0, "pending": 0, "dup": True}
    teacher = await _teacher(poll)
    tz = teacher.tz if teacher else c.settings.default_tz
    chat = await chats.get_chat(poll.chat_id)
    pending = [t for t in poll.targets if t.answer is None]
    text = parent_text(poll, tz, chat.title if chat else "", teacher.display_name if teacher else "", reminder=True)
    att = kb.kb_list(answer_buttons(poll))
    results = await send_many([(t, {"user_id": t.user_id, "text": text, "attachments": att}) for t in pending])
    sent = 0
    undelivered: list[PollTarget] = []
    async with session() as s:
        for t, msg in results:
            if msg:
                sent += 1
                row = await s.get(PollTarget, t.id)
                if row:
                    row.delivered = True
                    row.reminders += 1
            else:
                undelivered.append(t)  # недоставленных упомянем в общем чате одним сообщением
    group = 0
    if undelivered:
        mentions = ", ".join(f'<a href="max://user/{t.user_id}">{html.escape(t.name)}</a>' for t in undelivered[:40])
        text = (f"⏰ <b>{html.escape(poll.title)}</b>\nЖдём ответ от: {mentions}\n"
                + (f"Срок: {fmt_dt(poll.deadline, tz)}. " if poll.deadline else "")
                + "Ответьте кнопкой ниже.")
        if await c.safe_send(chat_id=poll.chat_id, text=text, attachments=att):
            group = len(undelivered)
    await stats.add(poll.teacher_id, "reminder", sent + group)
    return {"sent": sent, "group": group, "pending": len(pending)}


DEFERRED_KEY = "rma"  # сборы, где учитель ночью попросил «напомнить утром»


async def defer_remind(poll_id: int) -> None:
    ids = await ctx().kv.get(DEFERRED_KEY) or []
    if poll_id not in ids:
        await ctx().kv.set(DEFERRED_KEY, [*ids, poll_id], ttl=3 * 24 * 3600)


async def run_deferred_reminders() -> int:
    """Утренние напоминания, отложенные учителем ночью: уходят, как только у родителей наступает день."""
    ids = await ctx().kv.get(DEFERRED_KEY) or []
    if not ids:
        return 0
    left, sent = [], 0
    for pid in ids:
        poll = await get_poll(pid)
        if poll is None or poll.closed:
            continue
        teacher = await _teacher(poll)
        tz = teacher.tz if teacher else ctx().settings.default_tz
        if not _is_daytime(tz):
            left.append(pid)
            continue
        res = await remind(pid, auto=True)
        sent += 1
        await notify_teacher(poll.teacher_id, f"⏰ Напомнил молчащим по сбору «{html.escape(poll.title)}»: "
                                              f"в личку {res['sent']}" + (f", в чате {res['group']}" if res["group"] else "") + ".",
                             kind="report", chat_id=poll.chat_id, raw_text=f"«{poll.title}»: напоминание утром")
    await ctx().kv.set(DEFERRED_KEY, left, ttl=3 * 24 * 3600)
    return sent


async def close_poll(poll_id: int, by_deadline: bool = False) -> Poll | None:
    c = ctx()
    # атомарно: кнопка «Закрыть» и закрытие по сроку одновременно — итог уходит один раз
    async with session() as s:
        res = await s.execute(sa_update(Poll).where(Poll.id == poll_id, Poll.closed.is_(False))
                              .values(closed=True, closed_at=utcnow()))
        won = res.rowcount == 1
    poll = await get_poll(poll_id)
    if not won or poll is None:
        return poll
    await stats.add(poll.teacher_id, "poll_done")
    await _webhook("on_poll_closed", poll)
    if poll.group_message_id:
        try:
            answered, total, _ = counts(poll)
            await c.api.edit_message(
                poll.group_message_id,
                text=f"📋 <b>Сбор закрыт</b>\n\n{html.escape(poll.text)}\n\nОтветили {answered} из {total}. Спасибо!",
                attachments=[],
            )
        except MaxApiError as e:
            log.info("Не удалось обновить сообщение сбора в чате: %s", e)
    text, buttons = await status_card(poll)
    prefix = "⏱ Срок сбора истёк — итог:\n\n" if by_deadline else "Сбор закрыт — итог:\n\n"
    await notify_teacher(poll.teacher_id, prefix + text, kind="report", chat_id=poll.chat_id,
                         raw_text=f"«{poll.title}»: ответили {counts(poll)[0]} из {len(poll.targets)}",
                         buttons=buttons)
    return poll


async def delete_poll(poll_id: int, teacher_id: int) -> str:
    """Учитель удаляет закрытый сбор вместе с ответами: «учитель видит и удаляет всё, что знает бот».
    Открытый сбор сначала нужно закрыть — у родителей под ним ещё живые кнопки.
    Возвращает: deleted | open | missing."""
    async with session() as s:
        poll = await s.get(Poll, poll_id)
        if poll is None or poll.teacher_id != teacher_id:
            return "missing"
        if not poll.closed:
            return "open"
        await s.delete(poll)  # ответы (poll_targets) удаляются каскадом
    return "deleted"


async def list_polls(teacher_id: int, include_closed: bool = False, limit: int = 30) -> list[Poll]:
    q = select(Poll).where(Poll.teacher_id == teacher_id)
    if not include_closed:
        q = q.where(Poll.closed.is_(False))
    q = q.order_by(Poll.closed, Poll.created_at.desc()).limit(limit)
    async with session() as s:
        return list((await s.scalars(q)).all())


async def pending_for_user(user_id: int) -> list[Poll]:
    """Открытые сборы, где пользователь ещё не ответил (для родителя)."""
    async with session() as s:
        rows = (await s.scalars(
            select(Poll).join(PollTarget, PollTarget.poll_id == Poll.id)
            .where(PollTarget.user_id == user_id, PollTarget.answer.is_(None), Poll.closed.is_(False))
            .order_by(Poll.deadline)
        )).all()
        open_chats = [c.chat_id for c in await chats.member_chats(user_id)]
        if open_chats:
            # сборы в чатах, куда человек пришёл после рассылки
            extra = (await s.scalars(
                select(Poll).where(Poll.chat_id.in_(open_chats), Poll.closed.is_(False))
            )).all()
            known = {p.id for p in rows}
            for p in extra:
                if p.id not in known and not any(t.user_id == user_id for t in p.targets) and p.teacher_id != user_id:
                    rows = list(rows) + [p]
    return list(rows)


async def is_addressee(poll: Poll, user_id: int) -> bool:
    """Сбор адресован человеку: он в списке рассылки или сейчас участник чата класса (не учитель)."""
    if user_id == poll.teacher_id:
        return False
    return any(t.user_id == user_id for t in poll.targets) or await chats.is_present(poll.chat_id, user_id)


async def user_answer(poll: Poll, user_id: int) -> int | None:
    for t in poll.targets:
        if t.user_id == user_id:
            return t.answer
    return None


DAY_START, DAY_END = 8, 21  # родителям пишем только днём: граница рабочего дня работает в обе стороны
TICK_BUDGET = 50  # секунд на один проход tick (запускается раз в минуту)


def _is_daytime(tz: str) -> bool:
    h = local_now(tz).hour
    return DAY_START <= h < DAY_END


def next_daytime(tz: str | None, at=None):
    """Ближайший момент (UTC), когда родителям уже можно писать: сам at, если он днём, иначе 8:00."""
    loc = to_local(at or utcnow(), tz)
    if loc.hour < DAY_START:
        loc = loc.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
    elif loc.hour >= DAY_END:
        loc = (loc + timedelta(days=1)).replace(hour=DAY_START, minute=0, second=0, microsecond=0)
    return to_utc(loc)


def reminder_plan(deadline, created_at, tz: str | None) -> list:
    """Когда tick() напомнит молчащим (UTC). Повторяет его правила: за сутки (если сбор дольше 26 ч)
    и за 2 часа (если дольше 4 ч); ночные напоминания переносятся на 8:00, если срок ещё не прошёл;
    два напоминания ближе 6 часов друг к другу не шлём."""
    if not deadline:
        return []
    lifetime = deadline - created_at
    t24 = t2 = None
    if lifetime > timedelta(hours=26):
        t24 = next_daytime(tz, deadline - timedelta(hours=24))
        if t24 >= deadline:
            t24 = None
    if lifetime > timedelta(hours=4):
        t2 = next_daytime(tz, deadline - timedelta(hours=2))
        if t2 >= deadline:
            t2 = None
    if t24 and deadline - t24 <= timedelta(hours=6):
        t2 = None  # позднее «суточное» напоминание заменяет «двухчасовое»
    return [t for t in (t24, t2) if t]


def plan_text(poll: Poll, tz: str | None) -> str:
    """Что пообещать учителю про напоминания — из реального плана, а не «за сутки и за 2 часа» всегда."""
    if not poll.deadline:
        return "Срока нет — напомнить молчащим можно кнопкой «Напомнить» в статусе."
    plan = reminder_plan(poll.deadline, poll.created_at, tz)
    if not plan:
        return "До срока мало времени — сам напоминать не буду, напомнить можно кнопкой «Напомнить»."
    when = " и ".join(fmt_dt(t, tz) for t in plan)
    return f"Напомню молчащим: {when} (ночью родителям не пишу)."


def _due_reminder(p: Poll, now) -> str | None:
    left = p.deadline - now
    lifetime = p.deadline - p.created_at
    if not p.reminded_2h and left <= timedelta(hours=2) and lifetime > timedelta(hours=4):
        return "2h"
    if not p.reminded_24h and left <= timedelta(hours=24) and lifetime > timedelta(hours=26):
        return "24h"
    return None


async def tick() -> None:
    """Раз в минуту: досылка прерванных и отложенных рассылок, напоминания за сутки и за 2 часа,
    закрытие по сроку. Проход ограничен по времени — остальное доделает следующий."""
    started = time.monotonic()
    try:
        await resume_dispatch()
    except Exception:
        log.exception("Досылка сборов")
    try:
        await run_deferred_reminders()
    except Exception:
        log.exception("Отложенные напоминания")
    now = utcnow()
    async with session() as s:
        polls = (await s.scalars(select(Poll).where(Poll.closed.is_(False), Poll.deadline.is_not(None))
                                 .order_by(Poll.deadline))).all()
    for p in polls:
        if time.monotonic() - started > TICK_BUDGET:
            log.warning("tick сборов не уложился в %s с — продолжим через минуту", TICK_BUDGET)
            break
        try:
            if p.deadline <= now:
                await close_poll(p.id, by_deadline=True)
                continue
            if not p.dispatched:
                continue  # рассылка ещё не прошла — напоминать рано
            kind = _due_reminder(p, now)
            if kind is None:
                continue
            teacher = await _teacher(p)
            tz = teacher.tz if teacher else ctx().settings.default_tz
            if not _is_daytime(tz):
                continue  # ночью не пишем: напоминание уйдёт утром, если срок ещё не наступит
            # одна попытка в 15 минут: при сбое MAX не долбим родителей и API каждую минуту
            if not await ctx().kv.add_once(f"rmt:{p.id}:{kind}", ttl=15 * 60):
                continue
            res = await remind(p.id, auto=True)
            if res["sent"] + res["group"] == 0 and res["pending"]:
                continue  # ничего не ушло — не отмечаем, попробуем позже
            left = p.deadline - now
            await _mark(p.id, "reminded_24h")
            if kind == "2h" or left <= timedelta(hours=6):
                await _mark(p.id, "reminded_2h")
        except Exception:
            log.exception("Обработка сбора %s", p.id)


async def _mark(poll_id: int, field: str) -> None:
    async with session() as s:
        p = await s.get(Poll, poll_id)
        if p:
            setattr(p, field, True)


async def chat_of(poll: Poll) -> ClassChat | None:
    return await chats.get_chat(poll.chat_id)

"""«Перекличка» — сбор ответов, доведённый до конца без ручного подсчёта.

Учитель задаёт текст, варианты и срок. Бот берёт список участников чата,
рассылает каждому в личку кнопки, принимает ответы (ответ однозначно привязан к человеку),
сам напоминает молчащим за сутки и за два часа до срока и в любой момент показывает
«Ответили 25 из 27. Не ответили: …».
"""
from __future__ import annotations

import html
import logging
from datetime import timedelta

from sqlalchemy import select

from .. import keyboards as kb
from ..core import ctx
from ..db import session
from ..max_api import MaxApiError
from ..models import ClassChat, Poll, PollTarget, User
from ..timeutil import fmt_dt, local_now, utcnow
from . import chats, stats
from .quiet import notify_teacher

log = logging.getLogger(__name__)

TEMPLATES: dict[str, dict] = {
    "consent": {
        "label": "Согласие",
        "hint": "Например: «Экскурсия в музей, 28 сентября, 300 ₽. Нужно согласие до пятницы.»",
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
    "choice": {
        "label": "Выбор варианта",
        "hint": "Например: «Выбираем дату родительского собрания.» Варианты пришлёте следующим сообщением.",
        "options": [],
        "clarify": None,
    },
}

CLARIFY_WORDS = ("уточн", "вопрос")


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


async def create_poll(teacher_id: int, chat_id: int, template: str, text: str, options: list[str],
                      deadline) -> Poll:
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
        )
        s.add(poll)
        await s.flush()
        return poll


async def get_poll(poll_id: int) -> Poll | None:
    async with session() as s:
        return await s.get(Poll, poll_id)


async def send_poll(poll_id: int) -> dict:
    """Рассылает сбор: каждому в личку + одно сообщение в чат класса (как запасной путь)."""
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
        for m in members:
            s.add(PollTarget(poll_id=poll.id, user_id=m.user_id, name=m.name))

    delivered = failed = 0
    text = parent_text(poll, tz, chat_title, teacher_name)
    for m in members:
        msg = None
        try:
            msg = await c.api.send_message(user_id=m.user_id, text=text, attachments=kb.kb_list(answer_buttons(poll)))
        except MaxApiError as e:
            log.info("Сбор %s: не доставлено %s: %s", poll.id, m.user_id, e)
        async with session() as s:
            t = await s.scalar(select(PollTarget).where(PollTarget.poll_id == poll.id, PollTarget.user_id == m.user_id))
            if t and msg:
                t.delivered = True
                t.message_id = (msg.get("body") or {}).get("mid")
        if msg:
            delivered += 1
        else:
            failed += 1

    # Сообщение в общий чат: кто не получил личку (не запускал бота), ответит здесь — ответ всё равно личный.
    group_text = (
        f"📋 <b>Сбор от учителя</b>\n\n{html.escape(poll.text)}\n\n"
        + (f"Срок: <b>{fmt_dt(poll.deadline, tz)}</b>\n" if poll.deadline else "")
        + "Ответьте кнопкой ниже — ответ засчитается лично вам, другим участникам он не виден."
    )
    gmsg = await c.safe_send(chat_id=poll.chat_id, text=group_text, attachments=kb.kb_list(answer_buttons(poll)))
    if gmsg:
        async with session() as s:
            p = await s.get(Poll, poll.id)
            p.group_message_id = (gmsg.get("body") or {}).get("mid")

    return {"total": len(members), "delivered": delivered, "failed": failed, "synced": synced,
            "group_posted": bool(gmsg)}


async def record_answer(poll_id: int, user: dict, option: int, chat_id_hint: int | None = None) -> tuple[Poll | None, str]:
    """Записывает ответ. Возвращает (poll, статус): ok | changed | closed | bad."""
    uid = user.get("user_id")
    async with session() as s:
        poll = await s.get(Poll, poll_id)
        if poll is None or not (0 <= option < len(poll.options)):
            return poll, "bad"
        if poll.closed:
            return poll, "closed"
        t = await s.scalar(select(PollTarget).where(PollTarget.poll_id == poll_id, PollTarget.user_id == uid))
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


async def after_answer(poll: Poll, uid: int, opt: int, status: str) -> None:
    """«Нужно уточнить» — сразу учителю (с учётом тихого режима); все ответили — сообщаем итог."""
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
    if total and answered == total:
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


def counts(poll: Poll) -> tuple[int, int, list[int]]:
    per = [0] * len(poll.options)
    answered = 0
    for t in poll.targets:
        if t.answer is not None and 0 <= t.answer < len(per):
            per[t.answer] += 1
            answered += 1
    return answered, len(poll.targets), per


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
        f"<b>{answered}/{total}</b> ответили  {progress_bar(answered, total)}",
        " · ".join(f"{html.escape(o)} — {n}" for o, n in zip(poll.options, per)),
    ]
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
    else:
        buttons.append([kb.cb("Список", f"pl:{poll.id}"), kb.cb("🔁 Повторить", f"prp:{poll.id}")])
    if c.bot_username:
        buttons.append([kb.app("Открыть в приложении", c.bot_username, f"poll_{poll.id}")])
    return "\n".join(lines), buttons


def full_list(poll: Poll) -> str:
    lines = [f"📄 <b>{html.escape(poll.title)}</b> — список", ""]
    for i, opt in enumerate(poll.options):
        names = [t.name for t in poll.targets if t.answer == i]
        lines.append(f"<b>{html.escape(opt)} ({len(names)}):</b>")
        lines.append(html.escape(", ".join(names)) if names else "—")
        lines.append("")
    missing = [t.name for t in poll.targets if t.answer is None]
    lines.append(f"<b>Не ответили ({len(missing)}):</b>")
    lines.append(html.escape(", ".join(missing)) if missing else "—")
    return "\n".join(lines)


async def remind(poll_id: int, auto: bool = False) -> dict:
    c = ctx()
    poll = await get_poll(poll_id)
    if poll is None or poll.closed:
        return {"sent": 0, "group": 0}
    teacher = await _teacher(poll)
    tz = teacher.tz if teacher else c.settings.default_tz
    chat = await chats.get_chat(poll.chat_id)
    pending = [t for t in poll.targets if t.answer is None]
    sent = 0
    undelivered: list[PollTarget] = []
    for t in pending:
        text = parent_text(poll, tz, chat.title if chat else "", teacher.display_name if teacher else "", reminder=True)
        msg = await c.safe_send(user_id=t.user_id, text=text, attachments=kb.kb_list(answer_buttons(poll)))
        async with session() as s:
            row = await s.get(PollTarget, t.id)
            if msg:
                row.delivered = True
                row.reminders += 1
            # недоставленных упомянем в общем чате одним сообщением
        if msg:
            sent += 1
        else:
            undelivered.append(t)
    group = 0
    if undelivered:
        mentions = ", ".join(f'<a href="max://user/{t.user_id}">{html.escape(t.name)}</a>' for t in undelivered[:40])
        text = (f"⏰ <b>{html.escape(poll.title)}</b>\nЖдём ответ от: {mentions}\n"
                + (f"Срок: {fmt_dt(poll.deadline, tz)}. " if poll.deadline else "")
                + "Ответьте кнопкой ниже.")
        if await c.safe_send(chat_id=poll.chat_id, text=text, attachments=kb.kb_list(answer_buttons(poll))):
            group = len(undelivered)
    await stats.add(poll.teacher_id, "reminder", sent + group)
    return {"sent": sent, "group": group}


async def close_poll(poll_id: int, by_deadline: bool = False) -> Poll | None:
    c = ctx()
    async with session() as s:
        poll = await s.get(Poll, poll_id)
        if poll is None or poll.closed:
            return poll
        poll.closed = True
        poll.closed_at = utcnow()
    poll = await get_poll(poll_id)
    await stats.add(poll.teacher_id, "poll_done")
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


async def user_answer(poll: Poll, user_id: int) -> int | None:
    for t in poll.targets:
        if t.user_id == user_id:
            return t.answer
    return None


def _is_daytime(tz: str) -> bool:
    h = local_now(tz).hour
    return 8 <= h < 21


async def tick() -> None:
    """Раз в минуту: напоминания за сутки и за 2 часа, закрытие по сроку."""
    now = utcnow()
    async with session() as s:
        polls = (await s.scalars(select(Poll).where(Poll.closed.is_(False), Poll.deadline.is_not(None)))).all()
    for p in polls:
        try:
            if p.deadline <= now:
                await close_poll(p.id, by_deadline=True)
                continue
            teacher = await _teacher(p)
            tz = teacher.tz if teacher else ctx().settings.default_tz
            left = p.deadline - now
            lifetime = p.deadline - p.created_at
            if not p.reminded_24h and left <= timedelta(hours=24) and lifetime > timedelta(hours=26):
                if _is_daytime(tz) or left <= timedelta(hours=10):
                    await remind(p.id, auto=True)
                    await _mark(p.id, "reminded_24h")
            elif not p.reminded_2h and left <= timedelta(hours=2) and lifetime > timedelta(hours=4):
                if _is_daytime(tz):
                    await remind(p.id, auto=True)
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

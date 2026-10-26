"""События в учебном чате: подключение бота, вопросы, файлы учителя, «Сохранить этот ответ»."""
from __future__ import annotations

import html
import logging
import secrets

from .. import keyboards as kb
from ..core import ctx
from ..services import attendance, board, chats, faq, faq_learning, polls, quiet, search, shelf, stats

log = logging.getLogger(__name__)

GREETING = (
    "👋 Здравствуйте! Я <b>«Перемена»</b> — помощник классного руководителя.\n\n"
    "• собираю согласия и справки — каждому в личку, с кнопками;\n"
    "• отвечаю на частые вопросы тем, что сохранил учитель;\n"
    "• храню материалы класса.\n\n"
    "Родители, нажмите «Открыть бота» и запустите его — тогда сборы будут приходить вам лично. "
    "Я бот и никогда не выдаю себя за учителя."
)


async def on_bot_added(update: dict) -> None:
    c = ctx()
    chat_id = update.get("chat_id")
    user = update.get("user") or {}
    if not chat_id or update.get("is_channel"):
        return
    await chats.upsert_user(user)
    chat = await chats.register_chat(chat_id, user)
    n = await chats.sync_members(chat_id)
    chat = await chats.get_chat(chat_id)

    buttons = [[kb.link("Открыть бота", c.start_link("hi"))]] if c.bot_username else []
    await c.safe_send(chat_id=chat_id, text=GREETING, attachments=kb.kb_list(buttons))

    teacher_id = chat.teacher_id if chat else None
    if teacher_id:
        tip = ("" if chat.bot_is_admin else
               "\n\n⚠️ Назначьте меня <b>администратором</b> чата с правом читать сообщения — "
               "без этого я не вижу список класса и вопросы.")
        sent = await c.safe_send(
            user_id=teacher_id,
            text=(f"✅ Я подключён к чату <b>{html.escape(chat.title or 'класса')}</b>"
                  + (f", в списке {n} участников." if n >= 0 else ".") + tip
                  + "\n\nЗапустите первый сбор — /poll, добавьте частые вопросы — /add."),
            attachments=kb.kb_list([[kb.cb("📋 Новый сбор", "m:poll"), kb.cb("📖 Справочник", "m:faq")]]),
        )
        if not sent:
            log.info("Учитель %s ещё не открывал диалог с ботом", teacher_id)
        if chat.bot_is_admin:
            await shelf.import_history(chat_id, teacher_id, pages=3)


async def on_bot_removed(update: dict) -> None:
    if update.get("chat_id"):
        await chats.deactivate_chat(update["chat_id"])


async def on_admin_changed(update: dict) -> None:
    chat_id = update.get("chat_id")
    if not chat_id:
        return
    chat = await chats.get_chat(chat_id)
    if chat is None:
        await chats.register_chat(chat_id, None)
    is_admin = await chats.refresh_admin_status(chat_id)
    if is_admin:
        await chats.sync_members(chat_id)
        await polls.backfill_targets(chat_id)
        chat = await chats.get_chat(chat_id)
        if chat and chat.teacher_id:
            await ctx().safe_send(
                user_id=chat.teacher_id,
                text=f"✅ Теперь я администратор в «{html.escape(chat.title)}»: вижу список класса и вопросы в чате.",
            )


async def on_user_added(update: dict) -> None:
    if update.get("chat_id") and update.get("user"):
        await chats.touch_member(update["chat_id"], update["user"], present=True)
        # новичок попадает в открытые сборы — иначе «25/27» не видит его, а «Ответили все» срабатывает раньше времени
        await polls.backfill_targets(update["chat_id"])


async def on_user_removed(update: dict) -> None:
    if update.get("chat_id") and update.get("user"):
        await chats.touch_member(update["chat_id"], update["user"], present=False)


async def on_title_changed(update: dict) -> None:
    if update.get("chat_id") and update.get("title"):
        await chats.set_title(update["chat_id"], update["title"])


async def on_message(update: dict) -> None:
    c = ctx()
    msg = update.get("message") or {}
    sender = msg.get("sender") or {}
    chat_id = (msg.get("recipient") or {}).get("chat_id")
    uid = sender.get("user_id")
    if not chat_id or not uid or sender.get("is_bot"):
        return
    chat = await chats.get_chat(chat_id)
    if chat is None:
        chat = await chats.register_chat(chat_id, None)
        await chats.sync_members(chat_id)
        chat = await chats.get_chat(chat_id)
    if not chat or not chat.active:
        return
    await chats.touch_member(chat_id, sender)
    body = msg.get("body") or {}
    text = (body.get("text") or "").strip()

    if text.startswith("/"):
        if c.bot_username:
            await c.safe_send(chat_id=chat_id, text="Команды работают в личке со мной 👇",
                              attachments=kb.kb_list([[kb.link("Открыть бота", c.start_link())]]),
                              reply_to=body.get("mid"))
        return

    if uid == chat.teacher_id:
        await on_teacher_message(chat_id, uid, msg, text)
        return

    # «мы сегодня не придём, заболел» — предлагаем отметить отсутствие кнопкой (не чаще раза в день на человека)
    if attendance.looks_like_absence(text) and await c.kv.add_once(f"abq:{chat_id}:{uid}", ttl=12 * 3600):
        await c.safe_send(
            chat_id=chat_id, reply_to=body.get("mid"),
            text="🤒 Отметить, что ребёнок не придёт? Учитель увидит это в утреннем списке — отдельно писать не нужно.",
            attachments=kb.kb_list([[kb.cb("Сегодня", f"ab:g:{chat_id}:{uid}:today"),
                                     kb.cb("Завтра", f"ab:g:{chat_id}:{uid}:tomorrow")]]),
        )
        return

    if not search.looks_like_question(text):
        return

    # 1) автоответ — только если учитель его включил: сначала доска («что задали?»), потом справочник
    if chat.autoanswer and board.is_homework_question(text):
        teacher = await chats.get_user(chat.teacher_id) if chat.teacher_id else None
        tz = teacher.tz if teacher else c.settings.default_tz
        items = await board.find_homework(text, [chat_id], tz)
        if items:
            sent = await c.safe_send(chat_id=chat_id, text=await board.render_answer(items, text, tz),
                                     reply_to=body.get("mid"))
            if sent:
                await stats.add(chat.teacher_id, "faq_answer")
                return
    if chat.autoanswer:
        hits = faq.rank(text, await faq.candidates_for_chats([chat_id]), limit=1)
        if hits and hits[0].score >= faq.GROUP_SCORE:
            e = hits[0].item
            sent = await c.safe_send(
                chat_id=chat_id, text=await faq.render_answer(e), reply_to=body.get("mid"),
                attachments=kb.kb_list([[kb.cb("Это не то", f"fq:no:{e.id}")]]),
            )
            if sent:
                await faq.mark_hit(e.id)
                await stats.add(e.teacher_id, "faq_answer")
                return

    # 2) вопрос пришёл вне рабочего времени — положим во «входящие» к утру
    if chat.teacher_id:
        teacher = await chats.get_user(chat.teacher_id)
        if teacher and not quiet.in_work_window(teacher):
            if await c.kv.add_once(f"cq:{chat_id}:{uid}", ttl=30 * 60):
                name = await chats.member_name(chat_id, uid)
                await quiet.notify_teacher(
                    chat.teacher_id, f"❓ В чате «{html.escape(chat.title)}» спрашивает {html.escape(name)}: "
                                     f"{html.escape(text[:300])}",
                    kind="chat_question", chat_id=chat_id, from_user_id=uid, from_name=name, raw_text=text[:500],
                )
    # запоминаем вопрос: если учитель ответит на него реплаем, предложим сохранить ответ
    await c.kv.set(f"lq:{body.get('mid')}", {"text": text[:1000], "user_id": uid}, 3 * 24 * 3600)
    # и копим вопросы без ответа в справочнике — частые бот предложит учителю сохранить
    hits = faq.rank(text, await faq.candidates_for_chats([chat_id]), limit=1)
    if not hits or hits[0].score < faq.ANSWER_SCORE:
        await faq_learning.record([chat_id], uid, text)


async def on_teacher_message(chat_id: int, uid: int, msg: dict, text: str) -> None:
    c = ctx()
    # файлы учителя — на полку
    media = shelf.extract_media(msg)
    for item in media:
        await shelf.save(uid, item, text, chat_id=chat_id, source="chat")

    # учитель ответил реплаем на вопрос — предложим сохранить ответ в справочник
    link = msg.get("link") or {}
    if link.get("type") != "reply" or not text or len(text) < 2:
        return
    teacher = await chats.get_user(uid)
    if not teacher or not teacher.suggest_save:
        return
    orig = link.get("message") or {}
    q_text = (orig.get("text") or "").strip()
    orig_sender = (link.get("sender") or {}).get("user_id")
    if not q_text:
        cached = await c.kv.get(f"lq:{orig.get('mid')}")
        if cached:
            q_text, orig_sender = cached["text"], cached["user_id"]
    if not q_text or orig_sender == uid or not search.looks_like_question(q_text):
        return
    # уже есть такая карточка — не спрашиваем
    hits = faq.rank(q_text, await faq.list_entries(uid), limit=1)
    if hits and hits[0].score >= faq.ANSWER_SCORE:
        return
    key = secrets.token_hex(5)
    await c.kv.set(f"sv:{key}", {"teacher_id": uid, "q": q_text[:1000], "a": text[:3500]}, 3 * 24 * 3600)
    await c.safe_send(
        user_id=uid,
        text=("💾 <b>Сохранить этот ответ в справочник?</b>\n\n"
              f"<b>Вопрос:</b> {html.escape(q_text[:300])}\n<b>Ваш ответ:</b> {html.escape(text[:500])}\n\n"
              "<i>В следующий раз я отвечу на такой вопрос сам.</i>"),
        attachments=kb.kb_list([[kb.cb("💾 Сохранить", f"fq:save:{key}"), kb.cb("Не надо", f"fq:nosave:{key}")]]),
    )

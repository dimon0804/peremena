"""Регион (сбор сверху, сводка для методиста) и открытая библиотека шаблонов — в чате с ботом."""
from __future__ import annotations

import html
import logging

from .. import keyboards as kb
from ..core import ctx
from ..services import chats, faq, library, polls, region, school
from ..timeutil import fmt_dt, preset_deadline
from . import dialog
from .common import ack, clear_state, get_state, send, set_state

log = logging.getLogger(__name__)

GRADE_SETS = {"all": [], "1-4": [1, 2, 3, 4], "5-9": [5, 6, 7, 8, 9], "10-11": [10, 11]}
GRADE_LABELS = {"all": "Все классы", "1-4": "1–4 классы", "5-9": "5–9 классы", "10-11": "10–11 классы"}


async def on_state(uid: int, flow: str, step: str, data: dict, text: str) -> bool:
    if not text:
        return False
    if flow == "rg_note":
        await clear_state(uid)
        p, status = await region.set_note(int(data["pid"]), uid, text)
        if status == "missing":
            await send(uid, "Сбор не найден.")
        elif status == "closed":
            await send(uid, "Сбор уже закрыт — приписку добавить нельзя.")
        else:
            tail = ("Сбор уже разослан: приписку родители увидят в напоминании и в карточке сбора."
                    if status == "sent" else "Родители увидят её вместе со сбором.")
            await send(uid, f"✍️ Приписка сохранена. {tail}", [[kb.cb("📤 Разослать сейчас", f"rg:now:{p.id}")]]
                       if status == "ok" else None)
        return True
    if flow == "rg_code":
        await clear_state(uid)
        await _join(uid, text)
        return True
    if flow == "rg_create":
        await clear_state(uid)
        name = " ".join(text.split())
        if len(name) < 3:
            await send(uid, "Название слишком короткое. /region — попробовать ещё раз.")
            return True
        if step == "district":
            reg = await region.create_district(uid, name)
            if reg is None:
                await send(uid, "Округ может завести методист региона.")
                return True
            await send(uid, f"✅ Округ <b>{html.escape(reg.name)}</b> создан.\n\nКод для школ округа: "
                            f"<code>{reg.code}</code>\nКод для методистов округа: <code>{reg.staff_code}</code>")
            return True
        if not region.can_create_region(uid):
            await send(uid, "Регион заводит администратор платформы.")
            return True
        reg = await region.create_region(uid, name)
        await send(uid, f"✅ <b>{html.escape(reg.name)}</b> создан, вы — методист.\n\n"
                        f"Код для школ (его вводит завуч): <code>{reg.code}</code>\n"
                        f"Код для коллег из управления: <code>{reg.staff_code}</code>", [[kb.cb("🏛 Сводка", "rg:home")]])
        return True
    if flow == "rg_camp" and step == "text":
        if len(text) < 3:
            await send(uid, "Слишком коротко — опишите, что собираем.")
            return True
        data["text"] = text[:3000]
        await set_state(uid, "rg_camp", "grades", data)
        await send(uid, "Каким классам?", [[kb.cb(GRADE_LABELS[k], f"rg:gr:{k}")] for k in GRADE_SETS])
        return True
    if flow == "lb_search":
        await clear_state(uid)
        await show_library(uid, text)
        return True
    await send(uid, "Выберите вариант кнопкой выше или напишите /cancel.")
    return True


# ====================================================================== регион
async def show_region(uid: int) -> None:
    reg, _ = await region.membership(uid)
    rows: list[list[dict]] = []
    if reg:
        text = await region.render_dashboard_text(reg)
        rows = [[kb.cb("📣 Сбор на регион", "rg:new"), kb.cb("📊 Сборы", "rg:list")],
                [kb.cb("🔑 Коды приглашения", "rg:codes")]]
        if reg.kind == "region":
            rows[-1].append(kb.cb("🏘 Новый округ", "rg:district"))
        if ctx().bot_username:
            rows.append([kb.app("Открыть сводку в приложении", ctx().bot_username, "region")])
        await send(uid, text, rows)
        return
    r = await school.roles(uid)
    lines = ["🏛 <b>Управление образования</b>", ""]
    if r.is_admin and r.school:
        current = await region.get_region(r.school.region_id) if r.school.region_id else None
        if current:
            lines.append(f"Ваша школа подключена к «{html.escape(current.name)}». Сборы оттуда придут классным "
                         "руководителям, а вы увидите их в сводке школы.")
        else:
            lines.append("Школа пока не подключена к управлению. Пришлите код, который дал методист, — и сборы "
                         "от управления будут приходить классным руководителям сами, а методист увидит только "
                         "цифры по классам, без имён родителей.")
    else:
        lines.append("Если вы методист, пришлите код для сотрудников управления. Школу к управлению подключает "
                     "завуч в /school.")
    await set_state(uid, "rg_code", "c", {})
    if region.can_create_region(uid):
        rows.append([kb.cb("➕ Создать регион", "rg:create")])
    rows.append([kb.cb("Отмена", "f:cancel")])
    await send(uid, "\n".join(lines), rows)


async def _join(uid: int, code: str) -> None:
    reg, res = await region.join_by_code(uid, code)
    if res == "limit":
        await send(uid, "Слишком много попыток. Попробуйте через час.")
    elif res == "methodist":
        await send(uid, f"🏛 Вы методист «{html.escape(reg.name)}». Сводка и сборы — /region")
    elif res == "school":
        await send(uid, f"✅ Школа подключена к «{html.escape(reg.name)}». Методист будет видеть только цифры: "
                        "сколько ответили по классам, без имён и ответов родителей.")
    elif res == "not_admin":
        await send(uid, "Это код для школ — его вводит администратор школы (завуч). Если вы завуч, сначала "
                        "вступите в школу: /school")
    else:
        await send(uid, "Код не найден — проверьте его у методиста.")


async def on_rg_cb(cid: str, uid: int, tail: str) -> None:
    action, _, arg = tail.partition(":")
    if action == "note":
        p = await region.own_origin_poll(int(arg), uid) if arg.isdigit() else None
        await ack(cid)
        if p is None:
            await send(uid, "Сбор не найден.")
            return
        await set_state(uid, "rg_note", "t", {"pid": p.id})
        await send(uid, f"✍️ Напишите приписку к сбору «{html.escape(p.title)}» — например, где сдать бумагу "
                        "или когда вы на связи. Текст сбора и варианты ответа останутся как есть.",
                   [[kb.cb("Отмена", "f:cancel")]])
        return
    if action == "now":
        rep = await region.send_now(int(arg), uid) if arg.isdigit() else None
        await ack(cid, "Рассылаю" if rep and not rep.get("already") else None)
        if rep is None:
            await send(uid, "Сбор не найден или уже закрыт.")
        elif rep.get("already") or rep.get("busy"):
            await send(uid, "Сбор уже разослан (или рассылается прямо сейчас). Статус — /status")
        else:
            await send(uid, f"📤 Разослал: в личку доставлено {rep['delivered']} из {rep['total']}. Статус — /status")
        return
    if action == "create":
        await ack(cid)
        if not region.can_create_region(uid):
            await send(uid, "Регион заводит администратор платформы.")
            return
        await set_state(uid, "rg_create", "region", {})
        await send(uid, "Напишите название — так его увидят учителя и родители. Например: "
                        "«Управление образования Октябрьского района».", [[kb.cb("Отмена", "f:cancel")]])
        return
    reg, _ = await region.membership(uid)
    if reg is None:
        await ack(cid, "Только для методиста")
        return
    await ack(cid)
    if action == "home":
        await show_region(uid)
    elif action == "codes":
        await send(uid, f"🔑 <b>{html.escape(reg.name)}</b>\n\nКод для школ — его вводит завуч в /region: "
                        f"<code>{reg.code}</code>\nКод для коллег-методистов: <code>{reg.staff_code}</code>\n\n"
                        "Коллегам из школ код для методистов не пересылайте.")
    elif action == "district":
        await set_state(uid, "rg_create", "district", {})
        await send(uid, "Как называется округ (муниципалитет)?", [[kb.cb("Отмена", "f:cancel")]])
    elif action == "new":
        await set_state(uid, "rg_camp", "tpl", {"region_id": reg.id})
        rows = [[kb.cb(polls.TEMPLATES[k]["label"], f"rg:tpl:{k}")] for k in region.REGION_TEMPLATES if k != "choice"]
        rows.append([kb.cb("Отмена", "f:cancel")])
        await send(uid, "📣 <b>Сбор на регион.</b> Что собираем? Объявление — это «Ознакомление»: родители "
                        "отметят, что прочитали.", rows)
    elif action in ("tpl", "gr", "dl", "go"):
        await _camp_step(uid, reg, action, arg)
    elif action == "list":
        items = await region.list_campaigns(reg.id, 10)
        if not items:
            await send(uid, "Сборов от управления пока не было.", [[kb.cb("📣 Сбор на регион", "rg:new")]])
            return
        await send(uid, "📊 <b>Сборы управления</b>", [[kb.cb(rc.title[:60], f"rg:sum:{rc.id}")] for rc in items])
    elif action == "sum" and arg.isdigit():
        rc = await region.get_campaign(int(arg))
        if rc is None or not await region.can_see(uid, rc):
            await send(uid, "Сбор не найден.")
            return
        d = await region.summary(rc)
        rows = []
        from ..integrations import tokens
        from ..webapp import platform_api

        base = tokens.public_base()
        if base.startswith("https://"):
            rows.append([kb.link("⬇️ Таблица CSV", base + platform_api.region_export_url(rc))])
        await send(uid, region.render_summary_text(d), rows)


async def _camp_step(uid: int, reg, action: str, arg: str) -> None:
    st = await get_state(uid)
    if not st or st["flow"] != "rg_camp":
        await send(uid, "Черновик устарел. /region — начать заново.")
        return
    data = st["data"]
    if action == "tpl" and arg in region.REGION_TEMPLATES:
        data["template"] = arg
        await set_state(uid, "rg_camp", "text", data)
        await send(uid, "Напишите текст одним сообщением — первая строка станет заголовком. Его увидят все "
                        "родители выбранных классов, а учитель сможет лишь добавить свою приписку.",
                   [[kb.cb("Отмена", "f:cancel")]])
    elif action == "gr" and arg in GRADE_SETS and data.get("text"):
        data["grades"] = arg
        await set_state(uid, "rg_camp", "deadline", data)
        await send(uid, "До какого срока ждём ответы?", [
            [kb.cb("Пятница 18:00", "rg:dl:friday"), kb.cb("Через неделю", "rg:dl:week")],
            [kb.cb("Без срока", "rg:dl:none")]])
    elif action == "dl" and data.get("grades"):
        user = await chats.get_user(uid)
        data["deadline"] = None if arg == "none" else preset_deadline(arg, user.tz if user else None).isoformat()
        await set_state(uid, "rg_camp", "confirm", data)
        pairs = await region.classes(reg.id, None, GRADE_SETS[data["grades"]])
        n_sch = len({s_.id for s_, _ in pairs})
        tpl = polls.TEMPLATES[data["template"]]
        await send(uid, f"👀 <b>Проверьте</b>\n\n📋 {html.escape(data['text'])}\n\nКнопки: "
                        + " · ".join(html.escape(o) for o in tpl["options"])
                        + f"\nКому: {GRADE_LABELS[data['grades']]} — {len(pairs)} класс(ов) в {n_sch} школ(ах)\n"
                        f"Срок: {fmt_dt(polls_deadline(data), user.tz if user else None)}\n\n"
                        f"Классные руководители получат сбор и {region.HOLD_MINUTES} минут на приписку, потом бот "
                        "разошлёт родителям сам (ночью — утром).",
                   [[kb.cb("✅ Запустить", "rg:go")], [kb.cb("Отмена", "f:cancel")]] if pairs
                   else [[kb.cb("Отмена", "f:cancel")]])
    elif action == "go" and st["step"] == "confirm":
        await clear_state(uid)
        rc, info = await region.launch(uid, reg.id, data["template"], data["text"], [], polls_deadline(data),
                                       grades=GRADE_SETS[data["grades"]])
        await send(uid, f"✅ Сбор запущен: {info['classes']} класс(ов) в {info['schools']} школ(ах). Сводка — "
                        "/region → «Сборы».", [[kb.cb("📊 Сводка", f"rg:sum:{rc.id}")]] if rc else None)
    else:
        await send(uid, "Эта кнопка от старого черновика. /region — начать заново.")


def polls_deadline(data: dict):
    from datetime import datetime

    return datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None


# ====================================================================== библиотека
async def show_library(uid: int, query: str = "", topic: str | None = None) -> None:
    items = await library.search_items(uid, q=query, topic=topic, limit=10)
    head = f"📚 <b>Библиотека шаблонов</b>" + (f" · «{html.escape(query)}»" if query else "")
    lines = [head, "", "Шаблоны сборов и ответы для справочника от коллег и нашей команды. "
                       "Берите к себе в один тап."]
    if not items:
        lines += ["", "Ничего не нашлось. Попробуйте другое слово."]
    rows = [[kb.cb(("📋 " if it.kind == "poll" else "📖 ") + it.title[:44] + (f" · {it.uses}" if it.uses else ""),
                   f"lb:show:{it.id}")] for it in items]
    rows.append([kb.cb("🔎 Найти", "lb:search"), kb.cb("📤 Поделиться своим", "lb:share")])
    if ctx().bot_username:
        rows.append([kb.app("Открыть библиотеку в приложении", ctx().bot_username, "library")])
    await send(uid, "\n".join(lines), rows)


def _card(it) -> str:
    d = library.out(it)
    lines = [f"{'📋 Сбор' if it.kind == 'poll' else '📖 Ответ для справочника'} · {html.escape(d['topic_label'])}"
             + (" · от команды «Перемены»" if it.official else ""), "",
             f"<b>{html.escape(it.title)}</b>"]
    if it.kind == "poll":
        body = it.text.split("\n", 1)[1].strip() if "\n" in it.text else ""
        if body:
            lines.append(html.escape(body))
        lines += ["", "Кнопки: " + " · ".join(html.escape(o) for o in d["options"])]
    else:
        lines.append(html.escape(it.text))
    lines += ["", f"<i>{d['grades_label']} · взяли {it.uses} раз</i>"]
    return "\n".join(lines)


async def on_lb_cb(cid: str, uid: int, tail: str) -> None:
    parts = tail.split(":")
    action = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    if action == "mod" and len(parts) == 3 and parts[2].isdigit():
        it = await library.moderate(uid, int(parts[2]), parts[1])
        await ack(cid, ("Скрыт" if parts[1] == "hide" else "Оставлен") if it else "Нет прав")
        return
    await ack(cid)
    if action == "list":
        await show_library(uid)
    elif action == "search":
        await set_state(uid, "lb_search", "q", {})
        await send(uid, "🔎 Что ищем? Например: экскурсия, справка, подарок, собрание.")
    elif action == "show" and arg.isdigit():
        it = await library.get(int(arg))
        if not library.visible_to(it, uid):
            await send(uid, "Шаблон больше недоступен.")
            return
        rows = [[kb.cb("📥 Взять к себе", f"lb:take:{it.id}")],
                [kb.cb("⚠️ Пожаловаться", f"lb:rep:{it.id}"), kb.cb("◀️ К списку", "lb:list")]]
        await send(uid, _card(it), rows)
    elif action == "take" and arg.isdigit():
        await _take(uid, int(arg))
    elif action == "keep":
        st = await get_state(uid)
        if not st or st["flow"] != "poll" or not st["data"].get("lib_text"):
            await send(uid, "Черновик устарел. /library — выбрать шаблон снова.")
            return
        data = st["data"]
        data["text"] = data.pop("lib_text")
        await _continue_draft(uid, data)
    elif action == "rep" and arg.isdigit():
        res = await library.report(uid, int(arg))
        await send(uid, {"ok": "Спасибо, передали модератору.", "dup": "Вы уже жаловались на этот шаблон.",
                         "hidden": "Спасибо. Шаблон скрыт, пока его не проверит модератор."}.get(res, "Шаблон не найден."))
    elif action == "share":
        my_polls = [p for p in await polls.list_polls(uid, include_closed=True, limit=10) if not p.origin]
        my_faq = (await faq.list_entries(uid))[:8]
        if not my_polls and not my_faq:
            await send(uid, "Пока нечем делиться: создайте сбор (/poll) или карточку справочника (/add).")
            return
        rows = [[kb.cb("📋 " + p.title[:50], f"lb:pp:{p.id}")] for p in my_polls[:8]]
        rows += [[kb.cb("📖 " + e.question[:50], f"lb:pf:{e.id}")] for e in my_faq]
        await send(uid, "📤 Что опубликовать в библиотеке? Автора не покажем, имена и телефоны проверим "
                        "перед публикацией.", rows)
    elif action in ("pp", "pf") and arg.isdigit():
        confirm = len(parts) > 2 and parts[2] == "y"
        fn = library.publish_poll if action == "pp" else library.publish_faq
        it, blocks, warns = await fn(uid, int(arg), confirm=confirm)
        if it:
            await send(uid, f"✅ Опубликовано в библиотеке: «{html.escape(it.title)}». Спасибо — коллегам пригодится.")
        elif blocks:
            await send(uid, "⛔ " + "\n".join(html.escape(b) for b in blocks)
                       + "\n\nПоправьте текст и попробуйте снова.")
        else:
            await send(uid, "⚠️ " + "\n".join(html.escape(w) for w in warns),
                       [[kb.cb("Всё равно опубликовать", f"lb:{action}:{arg}:y")], [kb.cb("Отмена", "f:cancel")]])


async def _take(uid: int, item_id: int) -> None:
    it, res = await library.take(uid, item_id)
    if it is None:
        await send(uid, "Шаблон больше недоступен.")
        return
    if it.kind == "faq":
        await send(uid, f"📖 Добавил в ваш справочник: «{html.escape(it.title)}». Поправить ответ под свою школу "
                        "можно в /faq или в приложении.")
        return
    my = await chats.teacher_chats(uid)
    if not my:
        await send(uid, "Сбор можно запустить, когда бот подключён к чату вашего класса. Пока сохраните шаблон "
                        "на будущее — он останется в библиотеке.")
        return
    data = {"template": res["template"], "lib_text": res["text"], "options": res["options"], "from_lib": it.id}
    if len(my) == 1:
        data["chat_id"] = my[0].chat_id
        await offer_lib_text(uid, data)
        return
    await set_state(uid, "poll", "chat", data)
    rows = [[kb.cb(c.title or str(c.chat_id), f"f:chat:{c.chat_id}")] for c in my[:20]]
    rows.append([kb.cb("Отмена", "f:cancel")])
    await send(uid, "📋 В какой класс?", rows)


async def offer_lib_text(uid: int, data: dict) -> None:
    """Текст из библиотеки: оставить как есть или прислать свой вариант (дата, сумма, место)."""
    await set_state(uid, "poll", "text", data)
    await send(uid, "Вот текст из библиотеки. Если нужно поправить дату или место — пришлите исправленный "
                    f"текст сообщением.\n\n<code>{html.escape(data['lib_text'])}</code>",
               [[kb.cb("✅ Текст подходит", "lb:keep")], [kb.cb("Отмена", "f:cancel")]])


async def _continue_draft(uid: int, data: dict) -> None:
    if data.get("template") == "choice" and len(data.get("options") or []) < 2:
        await set_state(uid, "poll", "options", data)
        await send(uid, "Пришлите варианты ответа — каждый с новой строки (от 2 до 10).")
        return
    await set_state(uid, "poll", "deadline", data)
    await dialog.ask_deadline(uid)


REGION_HELP = (
    "Я <b>«Перемена»</b>. Вы — методист управления образования.\n\n"
    "🏛 /region — сводка: сколько школ и классов подключено, как отвечают на сборы.\n"
    "📣 Сбор на регион или выбранные параллели: каждый класс получит его от своего классного руководителя, "
    "а вы увидите только цифры — «ответили 82 %, школа № 5 — 40 %», без имён родителей.\n"
    "📚 /library — библиотека шаблонов."
)


def region_menu() -> list[list[dict]]:
    rows = [[kb.cb("🏛 Сводка по региону", "rg:home"), kb.cb("📣 Сбор на регион", "rg:new")],
            [kb.cb("📚 Библиотека шаблонов", "lb:list")]]
    if ctx().bot_username:
        rows.append([kb.app("📊 Открыть в приложении", ctx().bot_username, "region")])
    return rows

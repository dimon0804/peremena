"""Бот: электронный журнал (/journal).

Учитель присылает выгрузку из журнала (CSV или XLSX) с подписью «журнал» или после кнопки «Загрузить» —
бот показывает, что нашёл («27 учеников, 34 задания, расписание на 5 дней»), и по кнопке кладёт
домашку на доску, расписание — в ответы бота, список класса — в сверку с чатом.
Обратно — ссылки на CSV с пропусками и итогами сборов для переноса в журнал.
"""
from __future__ import annotations

import html
import logging
import secrets
from datetime import timedelta

from .. import keyboards as kb
from ..core import ctx
from ..integrations import journals, tokens
from ..integrations.journals import dnevnik, routes as jroutes, service
from ..services import chats, search
from ..timeutil import local_now
from .common import clear_state, send, set_state

log = logging.getLogger(__name__)

CAPTION_WORDS = ("журнал", "выгрузк", "импорт", "расписан", "список класса", "домашк", "дз", "/journal")

HELP = (
    "📒 <b>Электронный журнал</b>\n\n"
    "<b>Из журнала в «Перемену».</b> Скачайте из журнала таблицу (Excel или CSV) и пришлите мне — "
    "с подписью «журнал» или после кнопки «Загрузить выгрузку». Я разберу:\n"
    "• <b>домашние задания</b> — положу на доску класса без сообщения в чат и буду отвечать на «что задали?»;\n"
    "• <b>расписание</b> — буду отвечать на «какие уроки завтра?» и «что взять в среду?»;\n"
    "• <b>список класса</b> — покажу, кого из учеников ещё нет в чате.\n"
    "Повторная загрузка ничего не дублирует. Столбцы узнаю по названиям; если не узнал — возьмите шаблон.\n\n"
    "<b>Из «Перемены» в журнал.</b> Пропуски и итоги сборов — в CSV, который открывается в Excel "
    "и переносится в журнал."
)


def _base() -> str:
    return tokens.public_base()


def is_journal_caption(text: str) -> bool:
    t = search.normalize(text)
    return any(w in t for w in CAPTION_WORDS)


async def show(uid: int) -> None:
    my = await chats.teacher_chats(uid)
    if not my:
        await send(uid, "Журнал — для классного руководителя: сначала добавьте меня в чат класса.")
        return
    rows = [[kb.cb("📥 Загрузить выгрузку", "jr:up")],
            [kb.cb("👥 Кого нет в чате", "jr:roster")],
            [kb.cb("📤 Пропуски в CSV", "jr:exp:absences"), kb.cb("📤 Итоги сборов", "jr:exp:polls")]]
    base = _base()
    if base.startswith("https://"):
        rows.append([kb.link("📄 Шаблон таблицы", f"{base}/api/journals/template.xlsx")])
    if dnevnik.enabled():
        for c in my[:4]:
            if await dnevnik.get_link(c.chat_id):
                rows.append([kb.cb(f"🔄 «Дневник.ру» → {c.title[:24] or 'класс'}", f"jr:dn:{c.chat_id}")])
    if ctx().bot_username:
        rows.append([kb.app("📱 Журнал в приложении", ctx().bot_username, "journal")])
    await send(uid, HELP, rows)


async def ask_file(uid: int) -> None:
    await set_state(uid, "journal", "file", {})
    await send(uid, "📥 Пришлите файл выгрузки из журнала — CSV или XLSX, до "
                    f"{journals.parse.MAX_BYTES // 1024 // 1024} МБ. Старый .xls откройте в Excel и сохраните как .xlsx.",
               [[kb.cb("Отмена", "jr:cancel")]])


# ------------------------------------------------------------------ файл → предпросмотр
async def handle_file(uid: int, item: dict) -> None:
    my = await chats.teacher_chats(uid)
    if not my:
        await send(uid, "Загружать выгрузку журнала может классный руководитель.")
        return
    url = item.get("url")
    name = item.get("filename") or "файл"
    if not url:
        await send(uid, "MAX не дал мне ссылку на этот файл. Загрузите его в приложении: «Ещё → Интеграции → Журнал».")
        return
    try:
        data = await journals.download(url)
        parsed = journals.parse_bytes(data, name, local_now((await chats.get_user(uid)).tz).date())
    except journals.BadFile as e:
        await send(uid, f"😕 {html.escape(str(e))}")
        return
    except Exception:
        log.exception("Разбор выгрузки журнала")
        await send(uid, "😕 Не получилось разобрать файл. Попробуйте сохранить его как CSV или возьмите шаблон — /journal")
        return
    ref = await journals.save_draft(uid, parsed, name)
    lines = [f"📥 <b>{html.escape(name)}</b>", f"Нашёл: {journals.describe(parsed)}."]
    if parsed.tables:
        lines.append("\n" + "\n".join(f"• {html.escape(t)}" for t in parsed.tables[:6]))
    if parsed.warnings:
        lines.append("\n⚠️ " + "\n⚠️ ".join(html.escape(w) for w in parsed.warnings[:3]))
    if parsed.homework:
        lines.append("\n<i>Задания лягут на доску без сообщения в чат — я буду отвечать по ним на «что задали?».</i>")
    if len(my) == 1:
        lines.append(f"\nЗагрузить в «{html.escape(my[0].title or 'класс')}»?")
        rows = [[kb.cb("✅ Загрузить", f"jr:ok:{ref}:{my[0].chat_id}"), kb.cb("Отмена", f"jr:no:{ref}")]]
    else:
        lines.append("\nВ какой класс загрузить?")
        rows = [[kb.cb(f"✅ {c.title[:40] or c.chat_id}", f"jr:ok:{ref}:{c.chat_id}")] for c in my[:10]]
        rows.append([kb.cb("Отмена", f"jr:no:{ref}")])
    await send(uid, "\n".join(lines), rows)


async def on_state(uid: int, msg: dict) -> bool:
    """Шаг «пришлите файл»: файл — разбираем; что-то другое — выходим из шага."""
    from ..services import shelf

    media = [m for m in shelf.extract_media(msg) if m["type"] == "file"]
    await clear_state(uid)
    if not media:
        return False
    await handle_file(uid, media[0])
    return True


async def offer(uid: int, media: list[dict]) -> None:
    """Таблицу положили на полку без подписи «журнал» — предлагаем разобрать её как выгрузку."""
    tables = [m for m in media if m["type"] == "file" and journals.looks_like_table(m.get("filename") or "")
              and m.get("url")]
    if not tables or not await chats.teacher_chats(uid):
        return
    ref = secrets.token_urlsafe(8)
    await ctx().kv.set(f"jrf:{ref}", {"owner": uid, "url": tables[0]["url"], "filename": tables[0]["filename"]},
                       journals.DRAFT_TTL)
    await send(uid, "Похоже на таблицу. Если это выгрузка из электронного журнала — могу разобрать: домашку на доску, "
                    "расписание, список класса.", [[kb.cb("📥 Разобрать как журнал", f"jr:file:{ref}")]])


# ------------------------------------------------------------------ кнопки
async def on_cb(uid: int, tail: str) -> None:
    action, _, arg = tail.partition(":")
    if action == "up":
        await ask_file(uid)
    elif action == "cancel":
        await clear_state(uid)
        await send(uid, "Отменил.")
    elif action == "file":
        d = await ctx().kv.get(f"jrf:{arg}")
        if not d or d.get("owner") != uid:
            await send(uid, "Кнопка устарела — пришлите файл ещё раз с подписью «журнал».")
            return
        await handle_file(uid, {"url": d["url"], "filename": d.get("filename") or "файл"})
    elif action == "no":
        await journals.drop_draft(arg)
        await send(uid, "Хорошо, ничего не загружал.")
    elif action == "ok":
        ref, _, chat = arg.partition(":")
        await confirm(uid, ref, int(chat) if chat.lstrip("-").isdigit() else 0)
    elif action == "roster":
        await show_roster(uid, int(arg) if arg.lstrip("-").isdigit() else None)
    elif action == "exp":
        await send_export(uid, arg)
    elif action == "dn":
        await sync_dnevnik(uid, int(arg) if arg.lstrip("-").isdigit() else 0)


async def confirm(uid: int, ref: str, chat_id: int) -> None:
    c = await chats.get_chat(chat_id)
    if c is None or c.teacher_id != uid:
        await send(uid, "Этот класс не привязан к вам.")
        return
    d = await journals.load_draft(uid, ref)
    if d is None:
        await send(uid, "Предпросмотр устарел или уже загружен. Пришлите файл ещё раз.")
        return
    await journals.drop_draft(ref)  # второе нажатие «Загрузить» не повторит импорт
    res = await service.apply(chat_id, uid, d["parsed"], source="journal")
    parts = []
    if res.hw_added or res.hw_updated or res.hw_same:
        parts.append(f"заданий на доске: новых {res.hw_added}, обновлено {res.hw_updated}, без изменений {res.hw_same}")
    if res.lessons:
        parts.append(f"уроков в расписании: {res.lessons}")
    if res.students:
        parts.append(f"учеников в списке: {res.students}")
    text = f"✅ Загрузил в «{html.escape(c.title or 'класс')}»: " + "; ".join(parts) + "."
    if res.students:
        text += "\n\n" + service.render_check(await service.roster_check(chat_id))
    if res.lessons:
        text += "\n\nТеперь спросите меня: «какие уроки завтра?»"
    await send(uid, text)


async def show_roster(uid: int, chat_id: int | None = None) -> None:
    my = await chats.teacher_chats(uid)
    if chat_id is None and len(my) > 1:
        await send(uid, "Какой класс сверить?", [[kb.cb(c.title[:40] or str(c.chat_id), f"jr:roster:{c.chat_id}")]
                                                 for c in my[:10]])
        return
    c = next((x for x in my if x.chat_id == chat_id), my[0] if my and chat_id is None else None)
    if c is None:
        await send(uid, "Класс не найден.")
        return
    await send(uid, service.render_check(await service.roster_check(c.chat_id), c.title))


async def send_export(uid: int, kind: str) -> None:
    if kind not in ("absences", "polls") or not await chats.teacher_chats(uid):
        return
    base = _base()
    if not base.startswith("https://"):
        await send(uid, "Ссылки на выгрузку заработают, когда сервис будет опубликован по HTTPS (PUBLIC_URL).")
        return
    user = await chats.get_user(uid)
    end = local_now(user.tz if user else None).date()
    # отметки «не придёт» хранятся 14 дней (services/retention.py) — дальше выгружать нечего
    start = end - timedelta(days=13 if kind == "absences" else 30)
    url = base + jroutes.export_url(kind, uid, 0, start, end)
    what = "Пропуски" if kind == "absences" else "Итоги сборов"
    keep = " Отметки об отсутствии я храню две недели — выгружайте их раз в неделю." if kind == "absences" else ""
    await send(uid, f"📤 <b>{what}</b> за {start:%d.%m}–{end:%d.%m} по всем вашим классам — CSV для Excel и журнала "
                    f"(UTF-8, разделитель «;»). Ссылка действует сутки.{keep}",
               [[kb.link("Скачать CSV", url)]])


async def sync_dnevnik(uid: int, chat_id: int) -> None:
    c = await chats.get_chat(chat_id)
    if c is None or c.teacher_id != uid:
        await send(uid, "Этот класс не привязан к вам.")
        return
    user = await chats.get_user(uid)
    start = local_now(user.tz if user else None).date()
    try:
        parsed, res = await dnevnik.sync(chat_id, uid, start, start + timedelta(days=7))
    except dnevnik.DnevnikError as e:
        await send(uid, f"😕 {html.escape(str(e))}")
        return
    await send(uid, f"🔄 «Дневник.ру» → «{html.escape(c.title)}»: {journals.describe(parsed) or 'ничего нового'}. "
                    f"Новых заданий на доске: {res['hw_added']}, обновлено: {res['hw_updated']}.")

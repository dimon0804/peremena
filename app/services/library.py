"""Открытая библиотека шаблонов: сборы и карточки справочника, которыми учителя делятся друг с другом.

* Публикация обезличена: автора не показываем, а текст перед публикацией проверяем — телефоны и почту
  не пропускаем, имена детей и родителей из списков своих классов и похожее на ФИО — предупреждаем.
* Взять шаблон к себе — один тап: сбор открывается черновиком (класс и срок выбирает учитель),
  карточка сразу попадает в его справочник. Счётчик «взяли N раз» считает каждого учителя один раз.
* Жалоба — одна от человека; после трёх жалоб шаблон скрывается до решения модератора
  (администратора платформы, ADMIN_MAX_IDS), модератору приходит уведомление.
* Стартовый набор от команды — STARTER, появляется в пустой библиотеке сам.
* Демо отдельно: то, что опубликовано из песочницы, видно только в демо; взятия и жалобы
  из демо не влияют на счётчики настоящей библиотеки.
"""
from __future__ import annotations

import html
import logging
import re

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from .. import keyboards as kb
from ..core import ctx
from ..db import session
from ..demo_ids import is_demo
from ..models import ClassChat, FaqEntry, LibraryItem, LibraryReport, Member
from . import polls, search

log = logging.getLogger(__name__)

AUTO_HIDE_REPORTS = 3
TAKE_TTL = 400 * 24 * 3600

TOPICS = {
    "trips": "Экскурсии и поездки",
    "health": "Здоровье и справки",
    "money": "Деньги и подарки",
    "meetings": "Собрания и встречи",
    "food": "Питание",
    "clubs": "Кружки и секции",
    "school": "Учёба и школьная жизнь",
    "safety": "Безопасность",
}
GRADES = {"1-4": "1–4 классы", "5-9": "5–9 классы", "10-11": "10–11 классы"}

# ---------------------------------------------------------------- стартовый набор от команды
STARTER: list[dict] = [
    {"kind": "poll", "template": "consent", "topic": "trips", "text":
        "Экскурсия в краеведческий музей\n"
        "В субботу едем классом в музей. Сбор у школы в 9:30, вернёмся к 14:00. Билет и автобус — 450 ₽. "
        "Ребёнку с собой: перекус, воду и удобную обувь. Отметьте, пожалуйста, отпускаете ли."},
    {"kind": "poll", "template": "consent", "topic": "trips", "grades": "5-9", "text":
        "Поездка на выходные в другой город\n"
        "Планируем поездку с ночёвкой: выезд в пятницу после уроков, возвращение в воскресенье вечером. "
        "Едут двое сопровождающих из школы. Нужно ваше согласие — без него взять ребёнка не сможем."},
    {"kind": "poll", "template": "certificate", "topic": "health", "text":
        "Справка после болезни\n"
        "Если ребёнок болел, в первый день после выхода нужна справка от педиатра. Без неё медсестра "
        "не допустит до уроков. Отметьте, когда принесёте."},
    {"kind": "poll", "template": "certificate", "topic": "health", "text":
        "Медосмотр\n"
        "На следующей неделе в школе плановый медосмотр. Нужно подписать согласие на осмотр "
        "и принести карту прививок, если её нет в медкабинете."},
    {"kind": "poll", "template": "certificate", "topic": "health", "text":
        "Справка о группе здоровья для физкультуры\n"
        "Чтобы правильно распределить нагрузку на физкультуре, нужна справка о группе здоровья. "
        "Принесите, пожалуйста, до конца месяца."},
    {"kind": "poll", "template": "signup", "topic": "money", "text":
        "Подарок учителю\n"
        "Скоро день рождения у нашего учителя. Предлагаю скинуться на общий подарок от класса — "
        "по желанию, сумму обсудим в чате. Кто участвует?"},
    {"kind": "poll", "template": "consent", "topic": "money", "text":
        "Новогодний подарок детям\n"
        "Собираем сладкие подарки к Новому году — каждому одинаковый. Отметьте, участвуете ли, "
        "чтобы я знала, на сколько детей заказывать."},
    {"kind": "poll", "template": "choice", "topic": "meetings",
     "options": ["Вторник, 18:00", "Четверг, 18:30", "Суббота, 11:00", "Не смогу ни в один день"], "text":
        "Родительское собрание\n"
        "Выбираем удобное время для собрания: итоги четверти, поездки, питание. Минут на 40. "
        "Отметьте, когда вам удобнее."},
    {"kind": "poll", "template": "ack", "topic": "meetings", "text":
        "Итоги родительского собрания\n"
        "Кто не смог прийти — коротко главное: расписание на каникулы, график дежурств и правила "
        "телефонов на уроках. Протокол лежит в чате. Отметьте, что прочитали."},
    {"kind": "poll", "template": "choice", "topic": "food",
     "options": ["Завтрак", "Обед", "Завтрак и обед", "Не будет питаться в школе"], "text":
        "Питание в столовой\n"
        "Уточняю питание на следующий месяц, чтобы столовая заказала продукты. "
        "Что будет брать ваш ребёнок?"},
    {"kind": "poll", "template": "ack", "topic": "food", "text":
        "Меню и аллергии\n"
        "Если у ребёнка аллергия или особая диета, напишите мне лично и принесите справку — "
        "передам в столовую. Отметьте, что прочитали."},
    {"kind": "poll", "template": "ack", "topic": "school", "text":
        "Форма на физкультуру\n"
        "На физкультуре нужна спортивная форма: футболка, шорты или спортивные штаны, кроссовки "
        "со светлой подошвой. Без формы ребёнок будет сидеть на скамейке."},
    {"kind": "poll", "template": "signup", "topic": "clubs", "text":
        "Запись в кружки\n"
        "В школе открылись бесплатные кружки: робототехника, шахматы, хор и волейбол. "
        "Расписание в чате. Запишется ли ваш ребёнок хотя бы в один?"},
    {"kind": "poll", "template": "signup", "topic": "school", "text":
        "Субботник\n"
        "В субботу в 10:00 убираем школьный двор. Перчатки и мешки дадим, нужна удобная одежда. "
        "Придёте вместе с ребёнком?"},
    {"kind": "poll", "template": "ack", "topic": "safety", "text":
        "Правила безопасности на каникулах\n"
        "Перед каникулами напоминаю правила: дорога, водоёмы, пиротехника, незнакомые люди. "
        "Памятка в чате. Поговорите с ребёнком и отметьте, что ознакомились."},
    {"kind": "poll", "template": "consent", "topic": "school", "grades": "5-9", "text":
        "Согласие на анкетирование\n"
        "Школьный психолог проводит анонимную анкету о самочувствии в классе. Ответы не подписываются. "
        "Для участия нужно ваше согласие."},
    {"kind": "faq", "topic": "health", "title": "Где взять справку после болезни?", "keywords": "справка, болел, педиатр",
     "text": "Справку выдаёт участковый педиатр в поликлинике по месту жительства. Принести её нужно в первый "
             "день после болезни, иначе медсестра не допустит ребёнка до уроков."},
    {"kind": "faq", "topic": "food", "title": "Как оплатить питание?", "keywords": "питание, оплата, столовая",
     "text": "Питание оплачивается через личный кабинет на сайте школы или в приложении банка по лицевому счёту "
             "ребёнка. Лицевой счёт можно узнать у классного руководителя."},
    {"kind": "faq", "topic": "school", "title": "Что делать, если ребёнок заболел?", "keywords": "заболел, не придёт",
     "text": "Отметьте в боте «Ребёнок не придёт» — классный руководитель увидит это утром. "
             "Когда ребёнок выздоровеет, принесите справку от врача."},
    {"kind": "faq", "topic": "school", "title": "Какая форма нужна на физкультуру?", "keywords": "физкультура, форма",
     "text": "Футболка, шорты или спортивные штаны и кроссовки со светлой подошвой. Зимой — тёплый спортивный "
             "костюм для занятий на улице."},
]


# ---------------------------------------------------------------- проверка на персональные данные
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
FIO_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ](?:\.\s*[А-ЯЁ]\.|[а-яё]{2,})")
# слова с большой буквы, которые встречаются парой и не похожи на имя
_NOT_NAMES = {"Новый", "Новым", "Новому", "Новогодний", "Красная", "Красной", "Российской", "Российская", "Федерации",
              "День", "Дня", "Отечества", "Победы", "Пример", "Например", "Уважаемые", "Дорогие", "Добрый"}


def _names_of_classes(names: list[str]) -> list[str]:
    out = []
    for n in names:
        for part in re.split(r"[\s,]+", n or ""):
            part = part.strip(".")
            if len(part) >= 3 and part[:1].isupper():
                out.append(part)
    return out


async def pii_check(user_id: int, text: str) -> tuple[list[str], list[str]]:
    """(блокирующие, предупреждения). Телефон и почту не публикуем никогда; имена из списков своих классов
    и похожее на ФИО — предупреждаем: учитель может убрать или подтвердить, что это не человек из класса."""
    blocks, warns = [], []
    if PHONE_RE.search(text or ""):
        blocks.append("В тексте есть номер телефона — уберите его: шаблон увидят учителя других школ.")
    if EMAIL_RE.search(text or ""):
        blocks.append("В тексте есть адрес почты — уберите его перед публикацией.")
    async with session() as s:
        chat_ids = list((await s.scalars(select(ClassChat.chat_id).where(ClassChat.teacher_id == user_id))).all())
        rows = (await s.execute(select(Member.name, Member.child_name).where(Member.chat_id.in_(chat_ids)))).all() \
            if chat_ids else []
    known = set(_names_of_classes([r[0] for r in rows] + [r[1] for r in rows if r[1]]))
    words = set(re.findall(r"[А-ЯЁ][а-яё]{2,}", text or ""))
    hit = sorted(words & known)
    if hit:
        warns.append("Похоже, в тексте имена из вашего класса: " + ", ".join(hit[:5])
                     + ". Уберите их — в библиотеке шаблон должен быть без имён.")
    else:
        fio = [m.group(0) for m in FIO_RE.finditer(text or "") if m.group(0).split()[0] not in _NOT_NAMES]
        if fio:
            warns.append("Похоже на имя и фамилию: «" + fio[0] + "». Если это человек — уберите, если нет — "
                         "публикуйте как есть.")
    return blocks, warns


# ---------------------------------------------------------------- стартовый набор
def guess_topic(text: str) -> str:
    t = search.normalize(text)
    rules = (("trips", ("экскурс", "поездк", "музе", "театр", "выезд")),
             ("health", ("справк", "медосмотр", "прививк", "болезн", "врач")),
             ("money", ("подар", "сдаём", "сдаем", "оплат", "₽", "руб")),
             ("meetings", ("собрани", "встреч")), ("food", ("питани", "столов", "меню")),
             ("clubs", ("кружк", "секци")), ("safety", ("безопасн", "каникул", "правил")))
    for key, stems in rules:
        if any(st in t for st in stems):
            return key
    return "school"


def grades_of_title(title: str | None) -> str:
    from .region import grade_of

    g = grade_of(title)
    if not g:
        return ""
    return "1-4" if g <= 4 else "5-9" if g <= 9 else "10-11"


async def ensure_seed() -> int:
    """Пустая библиотека получает стартовый набор от команды. Один раз — даже если два процесса стартуют вместе."""
    async with session() as s:
        if await s.scalar(select(exists().where(LibraryItem.official.is_(True)))):
            return 0
    async with ctx().kv.lock("lib:seed", ttl=60, wait=30):
        async with session() as s:
            if await s.scalar(select(exists().where(LibraryItem.official.is_(True)))):
                return 0
            for it in STARTER:
                title, body = (it["text"].split("\n", 1) + [""])[:2] if it["kind"] == "poll" else (it["title"], it["text"])
                s.add(LibraryItem(
                    kind=it["kind"], author_id=None, title=title.strip(), text=it["text"] if it["kind"] == "poll" else body,
                    template=it.get("template", ""), options=it.get("options") or [], keywords=it.get("keywords", ""),
                    topic=it["topic"], grades=it.get("grades", ""), official=True))
        return len(STARTER)


# ---------------------------------------------------------------- поиск
def out(item: LibraryItem, me: int | None = None) -> dict:
    tpl = polls.TEMPLATES.get(item.template) if item.kind == "poll" else None
    return {
        "id": item.id, "kind": item.kind, "title": item.title, "text": item.text, "template": item.template,
        "template_label": tpl["label"] if tpl else "", "options": item.options or (tpl["options"] if tpl else []),
        "keywords": item.keywords, "topic": item.topic, "topic_label": TOPICS.get(item.topic, ""),
        "grades": item.grades, "grades_label": GRADES.get(item.grades, "любой класс"),
        "official": item.official, "uses": item.uses, "hidden": item.hidden, "reports": item.reports,
        "mine": me is not None and item.author_id == me,
        "created_at": item.created_at.isoformat() + "Z",
    }


async def search_items(user_id: int | None, q: str = "", kind: str | None = None, topic: str | None = None,
                       grades: str | None = None, sort: str = "popular", limit: int = 30,
                       include_hidden: bool = False) -> list[LibraryItem]:
    await ensure_seed()
    demo = is_demo(user_id) if user_id is not None else False
    cond = [] if include_hidden else [LibraryItem.hidden.is_(False)]
    # из демо видно и настоящую библиотеку, а опубликованное в демо — только в демо
    cond.append(LibraryItem.demo.is_(False) if not demo else or_(LibraryItem.demo.is_(False),
                                                                    LibraryItem.author_id == user_id))
    if kind in ("poll", "faq"):
        cond.append(LibraryItem.kind == kind)
    if topic in TOPICS:
        cond.append(LibraryItem.topic == topic)
    if grades in GRADES:
        cond.append(or_(LibraryItem.grades == grades, LibraryItem.grades == ""))
    order = (LibraryItem.created_at.desc(),) if sort == "new" else (LibraryItem.uses.desc(), LibraryItem.official.desc(),
                                                                   LibraryItem.id)
    async with session() as s:
        rows = list((await s.scalars(select(LibraryItem).where(*cond).order_by(*order).limit(500))).all())
    words = [search.normalize(w)[:5] for w in (q or "").split() if len(w) >= 3]
    if words:
        def hay(it: LibraryItem) -> str:
            return search.normalize(f"{it.title} {it.text} {it.keywords} {TOPICS.get(it.topic, '')}")
        rows = [it for it in rows if all(w in hay(it) for w in words)]
    return rows[:limit]


async def get(item_id: int) -> LibraryItem | None:
    async with session() as s:
        return await s.get(LibraryItem, item_id)


def visible_to(item: LibraryItem | None, user_id: int | None) -> bool:
    if item is None or item.hidden:
        return False
    return not item.demo or (user_id is not None and is_demo(user_id) and item.author_id == user_id)


# ---------------------------------------------------------------- публикация
async def publish(user_id: int, kind: str, title: str, text: str, template: str = "", options: list[str] | None = None,
                  topic: str = "", grades: str = "", keywords: str = "",
                  confirm: bool = False) -> tuple[LibraryItem | None, list[str], list[str]]:
    """(шаблон, блокирующие, предупреждения). С предупреждениями публикуется только при confirm=True,
    с блокирующими — никогда."""
    kind = "faq" if kind == "faq" else "poll"
    title = " ".join((title or "").split())[:300]
    text = (text or "").strip()[:3500]
    if kind == "poll":
        template = template if template in polls.TEMPLATES and template != "money" else "consent"
        if not title:
            title = (text.splitlines() or [""])[0][:300]
    options = [o.strip()[:60] for o in (options or []) if o and o.strip()][:10]
    if len(title) < 3 or len(text) < 3:
        return None, ["Слишком коротко — шаблон должен быть понятен без вас."], []
    blocks, warns = await pii_check(user_id, f"{title}\n{text}\n" + "\n".join(options))
    if blocks or (warns and not confirm):
        return None, blocks, warns
    async with session() as s:
        it = LibraryItem(kind=kind, author_id=user_id, title=title, text=text, template=template if kind == "poll" else "",
                         options=options if kind == "poll" and template == "choice" else [],
                         keywords=(keywords or (search.auto_keywords(title) if kind == "faq" else ""))[:500],
                         topic=topic if topic in TOPICS else guess_topic(f"{title} {text}"),
                         grades=grades if grades in GRADES else "", demo=is_demo(user_id))
        s.add(it)
        await s.flush()
        return it, [], warns


async def publish_poll(user_id: int, poll_id: int, confirm: bool = False):
    p = await polls.get_poll(poll_id)
    if p is None or p.teacher_id != user_id or p.origin:  # сбор сверху — не авторский, им не делятся от себя
        return None, ["Сбор не найден."], []
    return await publish(user_id, "poll", p.title, p.text, p.template, p.options if p.template == "choice" else [],
                         grades=grades_of_title(await _chat_title(p.chat_id)), confirm=confirm)


async def publish_faq(user_id: int, faq_id: int, confirm: bool = False):
    async with session() as s:
        e = await s.get(FaqEntry, faq_id)
    if e is None or e.teacher_id != user_id:
        return None, ["Карточка не найдена."], []
    return await publish(user_id, "faq", e.question, e.answer, keywords=e.keywords, confirm=confirm)


async def _chat_title(chat_id: int) -> str:
    async with session() as s:
        c = await s.get(ClassChat, chat_id)
    return c.title if c else ""


# ---------------------------------------------------------------- взять к себе
async def take(user_id: int, item_id: int) -> tuple[LibraryItem | None, dict | None]:
    """Сбор → черновик {template, text, options}; карточка → сразу в справочник учителя ({"faq_id"})."""
    it = await get(item_id)
    if not visible_to(it, user_id):
        return None, None
    if it.kind == "faq":
        from . import faq

        e = await faq.add_entry(user_id, it.title, it.text, keywords=it.keywords or None)
        result = {"faq_id": e.id}
    else:
        result = {"template": it.template or "consent", "text": it.text,
                  "options": it.options if it.template == "choice" else []}
    # «взяли N раз» — каждый учитель считается один раз; демо счётчик настоящей библиотеки не накручивает
    if (not is_demo(user_id) or it.demo) and await ctx().kv.add_once(f"libtake:{item_id}:{user_id}", ttl=TAKE_TTL):
        async with session() as s:
            row = await s.get(LibraryItem, item_id)
            row.uses = (row.uses or 0) + 1
    return await get(item_id), result


# ---------------------------------------------------------------- жалобы и модерация
async def report(user_id: int, item_id: int, reason: str = "") -> str:
    """ok | dup | missing | hidden (скрыт после этой жалобы)."""
    it = await get(item_id)
    if not visible_to(it, user_id):
        return "missing"
    if is_demo(user_id) and not it.demo:
        return "ok"  # жалоба из демо настоящую библиотеку не трогает
    try:
        async with session() as s:
            s.add(LibraryReport(item_id=item_id, user_id=user_id, reason=(reason or "")[:300]))
            await s.flush()
            row = await s.get(LibraryItem, item_id)
            row.reports = (row.reports or 0) + 1
            hide = row.reports >= AUTO_HIDE_REPORTS and not row.hidden
            if hide:
                row.hidden = True
            count = row.reports
    except IntegrityError:
        return "dup"
    await _tell_moderators(it, count, hide, reason)
    return "hidden" if hide else "ok"


async def _tell_moderators(it: LibraryItem, count: int, hidden: bool, reason: str) -> None:
    c = ctx()
    if it.demo:
        return
    state = "Шаблон скрыт до вашего решения." if hidden else "Пока виден всем."
    text = (f"⚠️ Жалоба на шаблон в библиотеке ({count}).\n\n<b>{html.escape(it.title)}</b>\n"
            f"{html.escape(it.text[:500])}\n\n" + (f"Причина: {html.escape(reason)}\n" if reason else "") + state)
    rows = [[kb.cb("🙈 Скрыть", f"lb:mod:hide:{it.id}"), kb.cb("✅ Оставить", f"lb:mod:show:{it.id}")]]
    for admin in c.settings.platform_admins:
        await c.safe_send(user_id=admin, text=text, attachments=kb.kb_list(rows))


async def moderate(admin_id: int, item_id: int, action: str) -> LibraryItem | None:
    """Только администратор платформы: hide | show (снимает жалобы) | delete."""
    from .region import is_platform_admin

    if not is_platform_admin(admin_id):
        return None
    async with session() as s:
        it = await s.get(LibraryItem, item_id)
        if it is None:
            return None
        if action == "delete":
            await s.delete(it)
            for r in (await s.scalars(select(LibraryReport).where(LibraryReport.item_id == item_id))).all():
                await s.delete(r)
            return it
        it.hidden = action == "hide"
        if action == "show":
            it.reports = 0
            for r in (await s.scalars(select(LibraryReport).where(LibraryReport.item_id == item_id))).all():
                await s.delete(r)
        return it


async def remove_own(user_id: int, item_id: int) -> bool:
    async with session() as s:
        it = await s.get(LibraryItem, item_id)
        if it is None or it.author_id != user_id:
            return False
        await s.delete(it)
    return True


async def pending_moderation(limit: int = 30) -> list[LibraryItem]:
    async with session() as s:
        return list((await s.scalars(select(LibraryItem).where(LibraryItem.reports > 0, LibraryItem.demo.is_(False))
                                     .order_by(LibraryItem.hidden.desc(), LibraryItem.reports.desc())
                                     .limit(limit))).all())


async def counts() -> dict:
    async with session() as s:
        n = await s.scalar(select(func.count(LibraryItem.id)).where(LibraryItem.hidden.is_(False),
                                                                    LibraryItem.demo.is_(False)))
    return {"items": int(n or 0)}


__all__ = ["search_items", "publish", "take", "report", "moderate", "pii_check", "STARTER", "TOPICS", "GRADES"]

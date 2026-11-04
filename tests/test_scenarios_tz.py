"""Сквозные сценарии из ТЗ, анкеты и презентации — вживую на поддельном MAX API.

Каждый тест — один сценарий или обещание; в докстроке указано, откуда оно взято
(ТЗ = «01_ИДЕЯ_ПРОЕКТА», §6 — сценарии А–Г; слайды — презентация). Таблица соответствия — docs/СООТВЕТСТВИЕ.md.
"""
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.bot.router import handle_update
from app.db import session
from app.models import FaqEntry, InboxItem, Material, Metric, OpenQuestion, Poll, PollTarget, User
from app.services import faq, polls, quiet, school, stats
from app.timeutil import utcnow
from app.webapp import api as webapi

from .conftest import BOT, CHAT_ID, PARENTS, SUBJ, TEACHER, cb_update, msg_update

T = TEACHER["user_id"]
P1, P2, P3 = (p["user_id"] for p in PARENTS)


def run(loop, coro):
    return loop.run_until_complete(coro)


def connect(loop, working_hours: bool = True):
    """Учитель добавляет бота в чат класса (бот — администратор, видит список)."""
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))
    set_teacher(loop, quiet_enabled=not working_hours)


def set_teacher(loop, **fields):
    async def go():
        async with session() as s:
            u = await s.get(User, T)
            for k, v in fields.items():
                setattr(u, k, v)
    run(loop, go())


def buttons_of(sent: dict) -> list[dict]:
    return [b for att in sent.get("attachments") or [] for row in att.get("payload", {}).get("buttons", []) for b in row]


def last_payload(api, prefix: str) -> str:
    for s in reversed(api.sent):
        for b in buttons_of(s):
            if (b.get("payload") or "").startswith(prefix):
                return b["payload"]
    raise AssertionError(f"нет кнопки {prefix}")


def first_poll(loop) -> Poll:
    async def go():
        async with session() as s:
            return (await s.scalars(select(Poll).order_by(Poll.id))).first()
    return run(loop, go())


def count(loop, model, *where) -> int:
    async def go():
        async with session() as s:
            return await s.scalar(select(func.count()).select_from(model).where(*where)) or 0
    return run(loop, go())


def shift_poll(loop, pid: int, **fields):
    async def go():
        async with session() as s:
            p = await s.get(Poll, pid)
            for k, v in fields.items():
                setattr(p, k, v)
    run(loop, go())


def add_card(loop, q: str, a: str):
    run(loop, handle_update(msg_update(TEACHER, "/add", mid=f"add-{q}")))
    run(loop, handle_update(msg_update(TEACHER, q, mid=f"q-{q}")))
    run(loop, handle_update(msg_update(TEACHER, a, mid=f"a-{q}")))


# ============================================================== Перекличка
def test_a_rollcall_consent_trip_to_the_end(env, monkeypatch):
    """ТЗ §6А «Сбор согласий на экскурсию», слайд 6 «Перекличка», бриф: мокапы родителя и учителя.
    /сбор → шаблон → текст → срок; рассылка каждому в личку с кнопками; не подключившийся отвечает в чате;
    напоминание только молчащим; по сроку учителю итог «Ответили N из M. Не ответили: …». Учитель не считал."""
    loop, api, c = env
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: True)
    connect(loop)
    # приглашение в чате при подключении — с кнопкой, чтобы родители открыли бота
    greet = next(s for s in api.sent if s.get("chat_id") == CHAT_ID)
    assert "Перемена" in greet["text"] and any(b.get("type") == "link" for b in buttons_of(greet))
    api.blocked.add(P2)  # Сидоров не запускал бота — личка не доставится

    run(loop, handle_update(msg_update(TEACHER, "/сбор", mid="a1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:consent", "a2")))
    run(loop, handle_update(msg_update(TEACHER, "Экскурсия в музей, 28 сентября, 300 ₽.\nНужно согласие до пятницы.",
                                       mid="a3")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:friday", "a4")))
    assert "Получат в личку: <b>3</b>" in api.texts_to(user_id=T)[-1]
    run(loop, handle_update(cb_update(TEACHER, "f:send", "a5")))

    poll = first_poll(loop)
    dm = next(s for s in api.sent if s.get("user_id") == P1 and "Экскурсия" in (s.get("text") or ""))
    assert [b["text"] for b in buttons_of(dm)] == ["Согласен", "Не согласен", "Нужно уточнить"]
    assert "Срок ответа" in dm["text"] and "Петрова М." in dm["text"]  # видно, от какого учителя
    assert any("Ответьте кнопкой" in t for t in api.texts_to(chat_id=CHAT_ID))  # запасной путь в чате
    assert "доставлено <b>2</b> из 3" in "\n".join(api.texts_to(user_id=T))

    # Иванова — кнопкой в личке; ответ сразу закреплён за ней
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{poll.id}:0", "a6")))
    assert "Ваш ответ: <b>Согласен</b>" in api.answers[-1]["message"]["text"]

    # за сутки до срока — напоминание только молчащим: Козловой в личку, Сидорова — упоминанием в чате
    before_p1 = len(api.texts_to(user_id=P1))
    shift_poll(loop, poll.id, created_at=utcnow() - timedelta(days=3), deadline=utcnow() + timedelta(hours=20))
    run(loop, polls.tick())
    assert any("Напоминание" in t for t in api.texts_to(user_id=P3))
    assert len(api.texts_to(user_id=P1)) == before_p1
    assert any("Ждём ответ от" in t and "Сидоров О." in t for t in api.texts_to(chat_id=CHAT_ID))

    # срок истёк — сбор закрывается сам, учителю итог готовым списком
    shift_poll(loop, poll.id, deadline=utcnow() - timedelta(minutes=1))
    run(loop, polls.tick())
    final = api.texts_to(user_id=T)[-1]
    assert "Срок сбора истёк" in final and "Ответили <b>1 из 3</b>" in final
    assert "Не ответили (2):</b> Козлова И., Сидоров О." in final
    assert run(loop, polls.get_poll(poll.id)).closed
    assert count(loop, Metric, Metric.teacher_id == T, Metric.kind == "poll_done") == 1

    # выгрузка списка (CSV) — по подписанной ссылке
    out = run(loop, webapi.get_poll(poll.id, run(loop, webapi.chats.get_user(T))))
    q = parse_qs(urlparse(out["export_url"]).query)
    resp = run(loop, webapi.export_csv(poll.id, sig=q["sig"][0], exp=int(q["exp"][0])))

    async def body():
        return b"".join([chunk async for chunk in resp.body_iterator]).decode("utf-8-sig")
    csv_text = run(loop, body())
    assert "Иванова А." in csv_text and "Согласен" in csv_text


def test_a_clarify_goes_to_teacher_and_all_answered(env):
    """ТЗ §5①: «Нужно уточнить» сразу учителю; «Ответили все» — без ручного подсчёта."""
    loop, api, c = env
    connect(loop)
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="b1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:certificate", "b2")))
    run(loop, handle_update(msg_update(TEACHER, "Справка после болезни до пятницы", mid="b3")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:week", "b4")))
    run(loop, handle_update(cb_update(TEACHER, "f:send", "b5")))
    poll = first_poll(loop)
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{poll.id}:2", "b6")))
    assert any("Нужно уточнить" in t and "Иванова А." in t for t in api.texts_to(user_id=T))
    run(loop, handle_update(cb_update(PARENTS[1], f"pa:{poll.id}:0", "b7")))
    run(loop, handle_update(cb_update(PARENTS[2], f"pa:{poll.id}:1", "b8")))
    assert any("Ответили все" in t for t in api.texts_to(user_id=T))


def test_templates_from_tz_are_all_there(env):
    """ТЗ §5①: шаблоны «из коробки» — согласие, справка, ознакомление, запись на мероприятие, выбор варианта."""
    loop, api, c = env
    connect(loop)
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="t1")))
    labels = [b["text"] for b in buttons_of(api.sent[-1])]
    for need in ("Согласие", "Справка", "Ознакомление", "Запись на мероприятие", "Выбор варианта"):
        assert need in labels
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:signup", "t2")))
    assert "Записываюсь" in api.texts_to(user_id=T)[-1]


# ============================================================== Справочник
def test_b_twentieth_question_answered_without_teacher(env, monkeypatch):
    """ТЗ §6Б «Вопрос, который задают в двадцатый раз», слайд 6: 19:42, мама спрашивает — бот отвечает сам
    с подписью «сохранено учителем <дата>» и кнопкой «Это не то»; учитель уведомления не получает."""
    loop, api, c = env
    connect(loop)
    add_card(loop, "Во сколько собрание?", "24 сентября, 18:30, кабинет 204")
    monkeypatch.setattr(quiet, "in_work_window", lambda u: False)  # 19:42 — уже нерабочее время
    to_teacher = len(api.texts_to(user_id=T))

    run(loop, handle_update(msg_update(PARENTS[0], "Во сколько собрание?", mid="b1")))
    reply = api.sent[-1]
    assert reply["user_id"] == P1 and "18:30, кабинет 204" in reply["text"]
    assert "сохранено учителем" in reply["text"] and datetime.now().strftime("%Y") in reply["text"]
    assert [b["text"] for b in buttons_of(reply)] == ["Это не то"]
    assert len(api.texts_to(user_id=T)) == to_teacher and count(loop, InboxItem) == 0
    assert count(loop, Metric, Metric.kind == "faq_answer") == 1

    # «Это не то» — учитель узнает (утром), родителю предлагают передать вопрос
    e_id = int(buttons_of(reply)[0]["payload"].split(":")[2])
    run(loop, handle_update(cb_update(PARENTS[0], f"fq:no:{e_id}", "b2")))
    assert "Передать ваш вопрос учителю?" in api.texts_to(user_id=P1)[-1]
    assert count(loop, InboxItem, InboxItem.kind == "not_that") == 1


def test_save_this_answer_from_chat_reply(env):
    """ТЗ §5②, слайд 6: учитель ответил в чате на вопрос — бот предлагает «Сохранить этот ответ», база растёт сама."""
    loop, api, c = env
    connect(loop)
    run(loop, handle_update(msg_update(PARENTS[1], "А какая форма завтра?", chat_id=CHAT_ID, mid="s1")))
    link = {"type": "reply", "sender": PARENTS[1], "message": {"mid": "s1", "text": "А какая форма завтра?"}}
    run(loop, handle_update(msg_update(TEACHER, "Парадная: белый верх, тёмный низ.", chat_id=CHAT_ID, mid="s2",
                                       link=link)))
    assert "Сохранить этот ответ" in api.texts_to(user_id=T)[-1]
    run(loop, handle_update(cb_update(TEACHER, last_payload(api, "fq:save:"), "s3")))
    assert count(loop, FaqEntry) == 1
    run(loop, handle_update(msg_update(PARENTS[2], "Какая форма завтра?", mid="s4")))
    assert "белый верх" in api.texts_to(user_id=P3)[-1]


def test_mention_in_chat_and_optional_autoanswer(env):
    """ТЗ §5②: спрашивают бота «в личке или упоминанием в чате»; автоответ в чате без упоминания —
    опция, которую включает сам учитель, по строгому порогу, с подписью и «Это не то»."""
    loop, api, c = env
    connect(loop)
    add_card(loop, "Во сколько собрание?", "В четверг в 18:30, кабинет 214.")
    in_chat = len(api.texts_to(chat_id=CHAT_ID))
    # без упоминания и без автоответа бот в чате молчит — не мешает живому разговору
    run(loop, handle_update(msg_update(PARENTS[0], "Во сколько собрание?", chat_id=CHAT_ID, mid="m1")))
    assert len(api.texts_to(chat_id=CHAT_ID)) == in_chat
    # обратились к боту по имени — отвечает в чате ответом на сообщение
    run(loop, handle_update(msg_update(PARENTS[0], "@peremena_test_bot во сколько собрание?", chat_id=CHAT_ID,
                                       mid="m2")))
    reply = api.sent[-1]
    assert reply.get("chat_id") == CHAT_ID and "18:30" in reply["text"] and "сохранено учителем" in reply["text"]
    assert any(b["text"] == "Это не то" for b in buttons_of(reply))
    # упоминание через разметку MAX и вопрос, которого нет в справочнике, — честно говорит и зовёт в личку
    body_markup = msg_update(PARENTS[1], "Перемена, когда каникулы?", chat_id=CHAT_ID, mid="m3")
    body_markup["message"]["body"]["markup"] = [{"type": "user_mention", "from": 0, "length": 8,
                                                 "user_id": BOT["user_id"]}]
    run(loop, handle_update(body_markup))
    assert "пока нет готового ответа" in api.texts_to(chat_id=CHAT_ID)[-1]
    # учитель включил автоответ — бот отвечает в чате и без упоминания, но только на почти дословное совпадение
    run(loop, handle_update(cb_update(TEACHER, f"q:auto:{CHAT_ID}", "m4")))
    run(loop, handle_update(msg_update(PARENTS[2], "Во сколько собрание?", chat_id=CHAT_ID, mid="m5")))
    assert "18:30" in api.texts_to(chat_id=CHAT_ID)[-1]


def test_faq_suggests_itself_when_three_ask(env):
    """README/сайт «Справочник подсказывает сам»: один вопрос от трёх родителей — учителю «ответить один раз»."""
    loop, api, c = env
    connect(loop)
    for i, p in enumerate(PARENTS):
        run(loop, handle_update(msg_update(p, "Нужна ли сменная обувь на экскурсию?", mid=f"o{i}")))
    run(loop, handle_update(msg_update(TEACHER, "/faq", mid="o9")))
    assert any("Спрашивают, а ответа в справочнике нет" in t and "— 3" in t for t in api.texts_to(user_id=T))


# ============================================================== Полка
def test_c_shelf_find_last_year_material_and_forward(env):
    """ТЗ §6В «Поиск прошлогоднего материала», слайд 6 «Полка»: файл учителя в чате отмечен сам;
    поиск «памятка по пожарной безопасности» → «Памятка_ПБ.pdf, отправляли в … <дата>» → «Переслать» → готово."""
    loop, api, c = env
    connect(loop)
    att = [{"type": "file", "filename": "Памятка_ПБ.pdf", "size": 2048,
            "payload": {"token": "tok-pb", "fileId": 77, "url": "https://example.invalid/pb.pdf"}}]
    run(loop, handle_update(msg_update(TEACHER, "Памятка по пожарной безопасности #безопасность", chat_id=CHAT_ID,
                                       mid="c1", attachments=att)))
    assert count(loop, Material) == 1

    run(loop, handle_update(msg_update(TEACHER, "памятка по пожарной безопасности", mid="c2")))
    found = api.sent[-1]
    assert "Памятка_ПБ.pdf" in found["text"] and "отправляли в 9Б Родители" in found["text"]
    to = last_payload(api, "sh:to:")
    run(loop, handle_update(cb_update(TEACHER, to, "c3")))
    sent_file = api.sent[-1]
    assert sent_file.get("chat_id") == CHAT_ID and sent_file["attachments"][0]["payload"]["token"] == "tok-pb"
    assert count(loop, Material) == 1  # переслали, а не задвоили полку


def test_shelf_file_sent_to_bot_with_caption_and_tags(env):
    """ТЗ §5③: учитель пересылает боту файл с подписью — бот кладёт его на полку с тегом."""
    loop, api, c = env
    connect(loop)
    att = [{"type": "file", "filename": "Документ (3).docx", "payload": {"token": "tok-3", "fileId": 3}}]
    run(loop, handle_update(msg_update(TEACHER, "Бланк заявления на питание #питание", mid="d1", attachments=att)))
    assert "Положил на полку" in api.texts_to(user_id=T)[-1]
    run(loop, handle_update(msg_update(TEACHER, "/shelf питание", mid="d2")))
    assert "Документ (3).docx" in api.texts_to(user_id=T)[-1]  # находится по тегу, а не по имени файла


# ============================================================== Тихий режим и утро
def test_d_friday_evening_and_monday_card(env, monkeypatch):
    """ТЗ §6Г «Вечер пятницы»: 21:15 бот отвечает сам; 21:40 вопроса нет — «передам учителю в понедельник»;
    понедельник 8:00 — одна карточка: новый вопрос, сколько закрыто автоматически, закрытый сбор «N из M»."""
    loop, api, c = env
    connect(loop, working_hours=False)
    add_card(loop, "Во сколько собрание?", "В четверг в 18:30.")
    teacher = run(loop, webapi.chats.get_user(T))
    friday = datetime(2026, 11, 6, 21, 40, tzinfo=ZoneInfo("Europe/Moscow"))
    assert quiet.when_back_text(teacher, friday) == "в понедельник с 08:00"
    assert quiet.when_back_text(teacher, friday.replace(hour=6, minute=0)) == "сегодня с 08:00"
    assert quiet.when_back_text(teacher, friday - timedelta(days=1)) == "завтра с 08:00"

    monkeypatch.setattr(quiet, "in_work_window", lambda u: False)
    to_teacher = len(api.texts_to(user_id=T))
    run(loop, handle_update(msg_update(PARENTS[0], "Во сколько собрание?", mid="g1")))       # 21:15
    assert "18:30" in api.texts_to(user_id=P1)[-1]
    run(loop, handle_update(msg_update(PARENTS[1], "Когда сдавать деньги на подарки?", mid="g2")))  # 21:40
    run(loop, handle_update(cb_update(PARENTS[1], "fw:ask", "g3")))
    assert "передам его учителю" in api.texts_to(user_id=P2)[-1] and "с 08:00" in api.texts_to(user_id=P2)[-1]
    assert len(api.texts_to(user_id=T)) == to_teacher  # вечером учителя не дёргаем

    # сбор справок закрылся в выходные, прошлая сводка была в пятницу
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="g4")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:certificate", "g5")))
    run(loop, handle_update(msg_update(TEACHER, "Справка после каникул", mid="g6")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:week", "g7")))
    run(loop, handle_update(cb_update(TEACHER, "f:send:am" if "f:send:am" in str(api.sent[-1]) else "f:send", "g8")))
    poll = first_poll(loop)
    for i, p in enumerate(PARENTS[:2]):
        run(loop, handle_update(cb_update(p, f"pa:{poll.id}:0", f"g9{i}")))
    run(loop, polls.close_poll(poll.id))
    # «за выходные»: в понедельник в 8:00 карточка охватывает всё с вечера пятницы (19:00 МСК = 16:00 UTC)
    monday = datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert quiet.digest_since(teacher, monday) == datetime(2026, 11, 6, 16, 0)

    teacher = run(loop, webapi.chats.get_user(T))
    text, _, _ = run(loop, quiet.build_digest(teacher))
    assert "Пока вы отдыхали" in text and "деньги на подарки" in text
    assert "бот сам ответил на вопросы: <b>1</b>" in text
    assert "Справка после каникул — ответили 2/3" in text


def test_morning_card_arrives_by_itself_and_urgent_passes(env):
    """ТЗ §5④, слайд 6: новое копится и приходит учителю утром одной карточкой; срочное — сразу."""
    loop, api, c = env
    connect(loop, working_hours=False)
    set_teacher(loop, work_end=0, weekends_off=False)  # окно «пустое» — сейчас точно нерабочее время
    run(loop, handle_update(msg_update(PARENTS[0], "А экскурсия точно в субботу?", mid="u1")))
    run(loop, handle_update(cb_update(PARENTS[0], "fw:ask", "u2")))
    assert count(loop, InboxItem, InboxItem.delivered.is_(False)) == 1
    run(loop, handle_update(msg_update(PARENTS[1], "Срочно: ребёнку плохо, заберу его", chat_id=CHAT_ID, mid="u3")))
    assert any("Срочно" in t and "ребёнку плохо" in t for t in api.texts_to(user_id=T))

    # «утро»: рабочее окно учителя начинается сейчас — планировщик сам присылает карточку
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    set_teacher(loop, work_start=now.hour * 60 + now.minute, work_end=24 * 60 - 1)
    run(loop, quiet.morning_tick())
    card = api.texts_to(user_id=T)[-1]
    assert "Пока вы отдыхали" in card and "экскурсия точно в субботу" in card
    assert count(loop, InboxItem, InboxItem.delivered.is_(False)) == 0


def test_night_poll_waits_for_morning(env, monkeypatch):
    """Граница дня в обе стороны (README «Тихий режим»): ночью сбор не будит родителей — бот предлагает утро."""
    loop, api, c = env
    monkeypatch.setattr(polls, "_is_daytime", lambda tz: False)
    connect(loop)
    run(loop, handle_update(msg_update(TEACHER, "/poll", mid="n1")))
    run(loop, handle_update(cb_update(TEACHER, "f:tpl:ack", "n2")))
    run(loop, handle_update(msg_update(TEACHER, "Ознакомьтесь с правилами поведения на каникулах", mid="n3")))
    run(loop, handle_update(cb_update(TEACHER, "f:dl:week", "n4")))
    assert "Разослать утром" in str(api.sent[-1])
    run(loop, handle_update(cb_update(TEACHER, "f:send:am", "n5")))
    assert not any("правилами поведения" in t for t in api.texts_to(user_id=P1))
    assert "уйдёт родителям" in api.texts_to(user_id=T)[-1]


# ============================================================== Доска, роли, школа
def test_board_homework_for_parents_and_students(env):
    """README «Доска класса», ТЗ §4 «Ученик — что задали без необходимости писать учителю»."""
    loop, api, c = env
    connect(loop)
    run(loop, handle_update(msg_update(TEACHER, "/дз", mid="h1")))
    run(loop, handle_update(msg_update(TEACHER, "алгебра", mid="h2")))
    run(loop, handle_update(msg_update(TEACHER, "№245, 247", mid="h3")))
    run(loop, handle_update(cb_update(TEACHER, "bd:fd:tomorrow", "h4")))
    run(loop, handle_update(cb_update(TEACHER, "bd:pub:now", "h5")))
    run(loop, handle_update(msg_update(PARENTS[2], "что задали по алгебре?", mid="h6")))
    assert "№245" in api.texts_to(user_id=P3)[-1] and "записано учителем" in api.texts_to(user_id=P3)[-1]


def test_roles_menus(env):
    """README «Роли»: классный руководитель, предметник по ссылке, администратор школы, родитель —
    меню собирается из ролей."""
    loop, api, c = env
    connect(loop)

    def menu(user) -> list[str]:
        run(loop, handle_update(msg_update(user, "/start", mid=f"st{user['user_id']}{len(api.sent)}")))
        return [b["text"] for b in buttons_of(api.sent[-1])]

    assert "📋 Новый сбор" in menu(TEACHER)
    assert "📋 Мои сборы" in menu(PARENTS[0]) and "📋 Новый сбор" not in menu(PARENTS[0])
    run(loop, handle_update(msg_update(TEACHER, "/invite", mid="r1")))
    code = api.texts_to(user_id=T)[-1].split("start=subj_")[1].split()[0]
    run(loop, handle_update({"update_type": "bot_started", "timestamp": 2, "chat_id": 1, "user": SUBJ,
                             "payload": f"subj_{code}"}))
    run(loop, handle_update(msg_update(SUBJ, "физика", mid="r2")))
    subj = menu(SUBJ)
    assert "📚 Домашка" in subj and "📋 Новый сбор" not in subj
    # предметник не может запустить сбор — это функция классного руководителя
    run(loop, handle_update(msg_update(SUBJ, "/poll", mid="r3")))
    assert "для классного руководителя" in api.texts_to(user_id=SUBJ["user_id"])[-1]


def test_school_admin_sees_parallel(env):
    """ТЗ §12 этапы «Школа» и «Управление»: общая база ответов и сводка для завуча без обзвона."""
    loop, api, c = env
    connect(loop)
    run(loop, handle_update(msg_update(TEACHER, "/school", mid="w1")))
    run(loop, handle_update(msg_update(TEACHER, "Школа № 7", mid="w2")))
    run(loop, handle_update(cb_update(TEACHER, last_payload(api, "sc:new:"), "w3")))
    roles = run(loop, school.roles(T))
    assert roles.is_admin
    run(loop, faq.add_entry(T, "Расписание звонков", "1 урок 8:30–9:15", school_id=roles.school.id))
    cmp, reports = run(loop, school.launch_campaign(T, roles.school.id, [CHAT_ID], "consent", "Анкета по питанию",
                                                    [], None))
    dash = run(loop, school.dashboard(roles.school.id))
    assert dash["classes"] and dash["campaigns"][0]["total"] == 3


# ============================================================== Отсутствия, деньги
def test_absence_one_line_in_morning_card(env):
    """README «Кто сегодня не придёт»: родитель отмечает кнопками — учитель видит одну строку утром."""
    loop, api, c = env
    connect(loop)
    run(loop, handle_update(msg_update(PARENTS[0], "Ваня заболел, сегодня не придём", mid="ab1")))
    run(loop, handle_update(cb_update(PARENTS[0], "ab:start", "ab2")))
    run(loop, handle_update(msg_update(PARENTS[0], "Иванов Ваня", mid="ab3")))
    run(loop, handle_update(cb_update(PARENTS[0], "ab:d:today", "ab4")))
    run(loop, handle_update(cb_update(PARENTS[0], "ab:r:sick", "ab5")))
    teacher = run(loop, webapi.chats.get_user(T))
    text, _, _ = run(loop, quiet.build_digest(teacher))
    assert "Сегодня не придут (1)" in text and "Иванов Ваня" in text


def test_money_collection_with_confirmation(env):
    """README «Сбор денег с учётом»: сумма с каждого, учитель подтверждает, «Собрано X из Y»."""
    loop, api, c = env
    connect(loop)
    res = run(loop, webapi.create_poll(webapi.PollIn(chat_id=CHAT_ID, template="money", text="Подарок к 8 марта",
                                                     amount=200), run(loop, webapi.chats.get_user(T))))
    pid = res["poll"]["id"]
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{pid}:{polls.PAY_TRANSFER}", "mo1")))
    tid = next(t["id"] for t in res["poll"]["targets"] if t["user_id"] == P1)
    run(loop, handle_update(cb_update(TEACHER, f"pp:{pid}:{tid}:1:0", "mo2")))
    assert "подтвердил получение 200 ₽" in api.texts_to(user_id=P1)[-1]
    text, _ = run(loop, polls.status_card(run(loop, polls.get_poll(pid))))
    assert "Собрано 200 ₽ из 600 ₽" in text


# ============================================================== Мини-приложение (веб-интерфейс)
def test_miniapp_api_full_cycle(env):
    """П. 10.4.1 «отображает веб-интерфейс»: то же, что в боте, через API мини-приложения —
    шаблоны, сбор, напоминание, закрытие, удаление, счётчик пользы с метриками пилота, вопрос родителя."""
    loop, api, c = env
    connect(loop)
    teacher = run(loop, webapi.chats.get_user(T))
    me = run(loop, webapi.me(teacher))
    assert me["board_chats"][0]["chat_id"] == CHAT_ID
    assert "signup" in {t["key"] for t in run(loop, webapi.templates(teacher))}
    res = run(loop, webapi.create_poll(webapi.PollIn(chat_id=CHAT_ID, text="Согласие на экскурсию",
                                                     deadline="week"), teacher))
    pid = res["poll"]["id"]
    assert res["poll"]["total"] == 3
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{pid}:0", "x1")))
    assert run(loop, webapi.remind(pid, teacher))["sent"] == 2
    out = run(loop, webapi.close(pid, teacher))
    assert out["closed"] and out["answered"] == 1

    st = run(loop, webapi.get_stats(teacher))
    assert st["pilot"]["polls_closed"] == 1 and st["pilot"]["on_time_pct"] == 33

    parent = run(loop, webapi.chats.upsert_user(PARENTS[1]))
    run(loop, faq.add_entry(T, "Во сколько собрание?", "В четверг в 18:30"))
    ans = run(loop, webapi.ask(q="во сколько собрание", user=parent))
    assert ans["kind"] == "faq" and "18:30" in ans["text_plain"]

    assert run(loop, webapi.delete_poll(pid, teacher)) == {"ok": True}
    assert count(loop, Poll) == 0 and count(loop, PollTarget) == 0


# ============================================================== Польза и метрики пилота
def test_benefit_counter_and_pilot_metrics(env, monkeypatch):
    """Слайды 7 и 10, анкета «метрики пилота»: бот сам считает минуты и показывает учителю;
    доля ответивших до срока и доля вопросов, закрытых без учителя."""
    loop, api, c = env
    connect(loop)
    add_card(loop, "Во сколько собрание?", "В четверг в 18:30.")
    run(loop, handle_update(msg_update(PARENTS[0], "Во сколько собрание?", mid="k1")))
    run(loop, handle_update(msg_update(PARENTS[1], "Во сколько собрание?", mid="k2")))
    run(loop, handle_update(msg_update(PARENTS[2], "Когда каникулы?", mid="k3")))
    run(loop, handle_update(cb_update(PARENTS[2], "fw:ask", "k4")))
    res = run(loop, webapi.create_poll(webapi.PollIn(chat_id=CHAT_ID, text="Ознакомление с расписанием",
                                                     template="ack", deadline="week"),
                                       run(loop, webapi.chats.get_user(T))))
    for i, p in enumerate(PARENTS):
        run(loop, handle_update(cb_update(p, f"pa:{res['poll']['id']}:0", f"k5{i}")))
    run(loop, polls.close_poll(res["poll"]["id"]))

    run(loop, handle_update(msg_update(TEACHER, "/польза", mid="k6")))
    text = api.texts_to(user_id=T)[-1]
    assert "Счётчик пользы" in text and "сэкономил" in text
    assert "Ответили до срока: 100 %" in text
    assert "Вопросов бот закрыл сам, без вас: 67 % (2 из 3" in text
    p = run(loop, stats.pilot(T))
    assert p["self_pct"] == 67 and p["on_time_pct"] == 100


# ============================================================== Данные и 152-ФЗ, что бот НЕ делает
def test_bot_does_not_archive_chat_and_teacher_deletes_everything(env):
    """ТЗ §9 «Данные и безопасность»: переписку не сохраняем; учитель видит и удаляет всё, что знает бот
    (/что_я_отвечаю, /удалить, удаление сбора); родитель удаляет свои данные (/forget)."""
    loop, api, c = env
    connect(loop)
    in_chat = len(api.texts_to(chat_id=CHAT_ID))
    run(loop, handle_update(msg_update(PARENTS[0], "Спасибо всем, до завтра!", chat_id=CHAT_ID, mid="z1")))
    assert len(api.texts_to(chat_id=CHAT_ID)) == in_chat  # бот не встревает в болтовню
    assert count(loop, InboxItem) == 0 and count(loop, OpenQuestion) == 0

    add_card(loop, "Какая форма?", "Парадная.")
    run(loop, handle_update(msg_update(TEACHER, "/что_я_отвечаю", mid="z2")))
    assert "Что я отвечаю от вашего имени" in api.texts_to(user_id=T)[-1]
    run(loop, handle_update(msg_update(TEACHER, "/удалить", mid="z3")))
    run(loop, handle_update(cb_update(TEACHER, last_payload(api, "fq:del:"), "z4")))
    run(loop, handle_update(cb_update(TEACHER, last_payload(api, "fq:delok:"), "z5")))
    assert count(loop, FaqEntry) == 0

    res = run(loop, webapi.create_poll(webapi.PollIn(chat_id=CHAT_ID, text="Согласие", deadline="week"),
                                       run(loop, webapi.chats.get_user(T))))
    pid = res["poll"]["id"]
    run(loop, handle_update(cb_update(PARENTS[0], f"pa:{pid}:0", "z6")))
    run(loop, handle_update(cb_update(TEACHER, f"pd:{pid}", "z7")))  # открытый: сначала закрыть
    run(loop, handle_update(cb_update(TEACHER, f"pd:{pid}:ok", "z8")))
    assert count(loop, Poll) == 1 and "Сначала закройте" in api.answers[-1]["notification"]
    run(loop, polls.close_poll(pid))
    run(loop, handle_update(cb_update(TEACHER, last_payload(api, f"pd:{pid}"), "z9")))
    run(loop, handle_update(cb_update(TEACHER, f"pd:{pid}:ok", "z10")))
    assert count(loop, Poll) == 0 and count(loop, PollTarget) == 0

    run(loop, handle_update(msg_update(PARENTS[1], "/forget", mid="z11")))
    run(loop, handle_update(cb_update(PARENTS[1], "fg:yes", "z12")))
    assert "Готово" in api.texts_to(user_id=P2)[-1]


def test_bot_never_poses_as_teacher(env):
    """ТЗ §9: «Все автоответы подписаны… Бот никогда не выдаёт себя за учителя»."""
    loop, api, c = env
    connect(loop)
    assert any("никогда не выдаю себя за учителя" in t for t in api.texts_to(chat_id=CHAT_ID))
    add_card(loop, "Где взять справку?", "В медкабинете.")
    run(loop, handle_update(msg_update(PARENTS[0], "где взять справку", mid="v1")))
    assert "Из справочника класса" in api.texts_to(user_id=P1)[-1]
    run(loop, handle_update(msg_update(PARENTS[0], "/start", mid="v2")))
    assert "Я бот" in api.texts_to(user_id=P1)[-1]

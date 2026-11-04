"""Электронный журнал: разбор выгрузок (CSV, XLSX, сетка расписания), импорт без дублей, сверка списка класса,
экспорт в CSV, коннектор «Дневник.ру» на поддельном API, права и сценарий в боте.

Файлы-фикстуры маленькие и собираются прямо в тестах.
"""
import io
import json
import time
import zipfile
from datetime import date, timedelta

import httpx
import openpyxl
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import func, select

from app.bot.router import handle_update
from app.db import session
from app.integrations import api_v1, journals, tokens
from app.integrations.journals import dnevnik, export, parse, routes, service
from app.models import Announcement, JournalLink, Lesson, RosterEntry, User
from app.services import attendance, chats
from app.timeutil import local_now

from .conftest import CHAT2_ID, CHAT_ID, PARENTS, TEACHER, TEACHER2, cb_update, msg_update

TODAY = date(2026, 11, 4)  # среда


def run(loop, coro):
    return loop.run_until_complete(coro)


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def setup_classes(loop):
    for chat_id, t in ((CHAT_ID, TEACHER), (CHAT2_ID, TEACHER2)):
        run(loop, handle_update({"update_type": "bot_added", "timestamp": chat_id, "chat_id": chat_id, "user": t,
                                 "is_channel": False}))

    async def no_quiet():
        async with session() as s:
            (await s.get(User, TEACHER["user_id"])).quiet_enabled = False
    run(loop, no_quiet())


def buttons(msg) -> list[dict]:
    return [b for a in msg.get("attachments") or [] for row in a["payload"]["buttons"] for b in row]


# ====================================================================== разбор форматов
def test_csv_roster_cp1251_with_title_rows_and_contacts():
    csv_text = ("Список обучающихся 9Б класса;;\n"
                "Учебный год 2026/2027;;\n"
                "№;ФИО обучающегося;Родители (законные представители);Дата рождения\n"
                "1;ИВАНОВ Иван Сергеевич;Иванова Анна Петровна, +7 (900) 123-45-67, anna@mail.ru;01.02.2011\n"
                "2;Сидорова Мария Олеговна;\"Сидоров Олег (папа); Сидорова Ольга\";05.06.2011\n"
                "3;Петров Лев;;\n"
                "4;Петров Лев;;\n"
                ";Итого: 3;;\n")
    p = parse.parse_file(csv_text.encode("cp1251"), "spisok.csv", TODAY)
    assert [s["name"] for s in p.students] == ["Иванов Иван", "Сидорова Мария", "Петров Лев"]
    assert p.students[0]["parents"] == ["Иванова А."]           # без телефона, почты и отчества
    assert p.students[1]["parents"] == ["Сидоров О.", "Сидорова О."]
    flat = json.dumps(p.to_dict(), ensure_ascii=False)
    assert "900" not in flat and "@" not in flat and "Сергеевич" not in flat and "2011" not in flat
    assert parse.describe(p) == "3 ученика"
    assert not p.lessons and not p.homework


def test_xlsx_three_sheets_and_template_roundtrip():
    # наш шаблон разбирается сам собой
    p = parse.parse_file(export.template_xlsx(), "shablon.xlsx", TODAY)
    assert len(p.students) == 2 and len(p.lessons) == 2 and len(p.homework) == 2
    assert p.lessons[0] == {"day": "2026-11-10", "weekday": 1, "number": 1, "subject": "Алгебра",
                            "time": "08:30–09:15", "room": "21"}
    assert {"Лист «Ученики»: список класса", "Лист «Расписание»: расписание",
            "Лист «Домашние задания»: домашние задания"} <= set(p.tables)

    # отдельные столбцы Фамилия/Имя/Отчество, даты как настоящие даты Excel, формула не вычисляется
    data = xlsx({
        "Класс": [["Фамилия", "Имя", "Отчество", "Мать", "Отец"],
                  ["Орлова", "Кира", "Игоревна", "Орлова Нина Ивановна", "Орлов Игорь"],
                  ["Белов", "Артём", "", "", ""]],
        "ДЗ": [["Дата урока", "Урок", "Предмет", "Тема", "Домашнее задание"],
               [date(2026, 11, 5), 1, "Иностранный язык (английский)", "Present Simple", "Упр. 5 стр. 20"],
               [date(2026, 11, 5), 2, "Алгебра", "Квадратные уравнения", "=HYPERLINK(\"http://x\")"],
               [date(2026, 11, 5), 3, "Алгебра", "Квадратные уравнения", "№ 245"],
               [date(2026, 11, 6), 1, "Физическая культура", "", "-"]],
    })
    p = parse.parse_file(data, "zhurnal.xlsx", TODAY)
    assert p.students == [{"name": "Орлова Кира", "parents": ["Орлова Н.", "Орлов И."]},
                          {"name": "Белов Артём", "parents": []}]
    by_subj = {h["subject"]: h for h in p.homework}
    assert by_subj["Английский язык"] == {"day": "2026-11-05", "subject": "Английский язык", "text": "Упр. 5 стр. 20"}
    # два урока алгебры в один день — одна запись; формула не вычисляется и не попадает на доску
    # (openpyxl в режиме data_only отдаёт только сохранённое значение, а у формулы его нет)
    assert by_subj["Алгебра"]["text"] == "№ 245"
    assert "Физическая культура" not in by_subj  # «-» — не задание
    assert len(p.lessons) == 4 and p.lessons[3]["subject"] == "Физическая культура"


def test_wide_weekly_schedule_and_dates_without_year():
    csv_text = ("Расписание 9Б\n"
                "№,Время,Понедельник,Вторник,Среда,Четверг,Пятница\n"
                "1,08:30-09:15,Алгебра каб. 21,Русский,Физика,История,Химия\n"
                "2,09:25-10:10,Литература,Англ,,Геометрия,Биология\n")
    p = parse.parse_file(csv_text.encode(), "rasp.csv", TODAY)
    assert len(p.lessons) == 9 and p.summary()["days"] == 5
    mon = [x for x in p.lessons if x["weekday"] == 0]
    assert mon[0] == {"day": None, "weekday": 0, "number": 1, "subject": "Алгебра", "time": "08:30–09:15",
                      "room": "21"}
    assert {x["subject"] for x in p.lessons if x["weekday"] == 1} == {"Русский язык", "Английский язык"}

    # даты без года — ближайший год; «9 ноября»; день недели в столбце «День»
    assert parse.parse_when("09.11", TODAY) == (date(2026, 11, 9), None)
    assert parse.parse_when("15.01", TODAY) == (date(2027, 1, 15), None)
    assert parse.parse_when("9 ноября", TODAY) == (date(2026, 11, 9), None)
    assert parse.parse_when("Пн", TODAY) == (None, 0)
    assert parse.parse_when("пятница", TODAY) == (None, 4)
    tsv = "День\tПредмет\tДЗ\nПятница\tХимия\tПараграф 12\n"
    p = parse.parse_file(tsv.encode(), "dz.txt", TODAY)
    assert p.homework == [{"day": "2026-11-06", "subject": "Химия", "text": "Параграф 12"}]


def test_bad_files_are_refused_safely(monkeypatch):
    with pytest.raises(parse.BadFile, match="xls"):
        parse.read_tables(parse.OLE_MAGIC + b"\x00" * 100)
    with pytest.raises(parse.BadFile, match="Пустой"):
        parse.read_tables(b"")
    with pytest.raises(parse.BadFile, match="больше"):
        parse.read_tables(b"a;b\n" * (parse.MAX_BYTES // 4 + 10))
    with pytest.raises(parse.BadFile, match="не таблица"):
        parse.read_tables(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    with pytest.raises(parse.BadFile, match="повреждён"):
        parse.read_tables(parse.XLSX_MAGIC + b"garbage")
    # «zip-бомба»: распакованный размер считается до openpyxl
    monkeypatch.setattr(parse, "MAX_UNZIPPED", 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", "0" * 5000)
    with pytest.raises(parse.BadFile, match="слишком большая"):
        parse.read_tables(buf.getvalue())
    # ограничение числа строк
    monkeypatch.setattr(parse, "MAX_ROWS", 5)
    rows = parse.read_tables(("ФИО\n" + "Иванов Иван\n" * 50).encode())[0][1]
    assert len(rows) == 5
    # ничего не узнали — понятная ошибка с подсказкой про шаблон
    with pytest.raises(journals.BadFile, match="шаблон"):
        journals.parse_bytes("Колонка1;Колонка2\n1;2\n".encode())


# ====================================================================== импорт: доска, расписание, без дублей
HW_CSV = ("Дата;№ урока;Предмет;Домашнее задание\n"
          "{d1};1;Алгебра;№ 245, 247\n"
          "{d1};2;Русский язык;Упр. 112\n"
          "{d2};1;Физика;§ 14, вопросы\n")


def test_import_is_idempotent_and_keeps_manual_records(env):
    loop, api, c = env
    setup_classes(loop)
    d1, d2 = "2026-11-05", "2026-11-06"
    p = parse.parse_file(HW_CSV.format(d1="05.11.2026", d2="06.11.2026").encode(), "dz.csv", TODAY)
    assert len(p.homework) == 3 and len(p.lessons) == 3

    # учитель сам записал алгебру на тот же день другим текстом — это его запись, её не трогаем
    async def manual():
        from app.services import board
        await board.create(CHAT_ID, TEACHER["user_id"], "hw", "Повторить формулы", "Алгебра", d1, "silent")
    run(loop, manual())

    r1 = run(loop, service.apply(CHAT_ID, TEACHER["user_id"], p))
    assert (r1.hw_added, r1.hw_updated, r1.hw_same, r1.lessons) == (3, 0, 0, 3)
    r2 = run(loop, service.apply(CHAT_ID, TEACHER["user_id"], p))
    assert (r2.hw_added, r2.hw_updated, r2.hw_same, r2.lessons) == (0, 0, 3, 3)

    async def counts():
        async with session() as s:
            hw = (await s.scalars(select(Announcement).where(Announcement.chat_id == CHAT_ID))).all()
            ls = await s.scalar(select(func.count()).select_from(Lesson).where(Lesson.chat_id == CHAT_ID))
            return hw, ls
    hw, ls = run(loop, counts())
    assert len(hw) == 4 and ls == 3  # 3 из файла + 1 ручная, уроки не задвоились
    assert all(a.publish_mode == "silent" and a.published for a in hw)  # в чат ничего не ушло
    assert not api.texts_to(chat_id=CHAT_ID) or "Упр. 112" not in " ".join(api.texts_to(chat_id=CHAT_ID))

    # учитель исправил задание в журнале и выгрузил снова — запись обновилась, ручная осталась
    p2 = parse.parse_file(HW_CSV.format(d1="05.11.2026", d2="06.11.2026").replace("Упр. 112", "Упр. 113").encode(),
                          "dz.csv", TODAY)
    r3 = run(loop, service.apply(CHAT_ID, TEACHER["user_id"], p2))
    assert (r3.hw_added, r3.hw_updated, r3.hw_same) == (0, 1, 2)
    hw, _ = run(loop, counts())
    texts = sorted(a.text for a in hw)
    assert texts == sorted(["Повторить формулы", "Упр. 113", "§ 14, вопросы", "№ 245, 247"])
    assert {a.source for a in hw} == {"manual", "journal"}
    assert {(a.subject, a.for_date) for a in hw if a.source == "journal"} == {
        ("Алгебра", d1), ("Русский язык", d1), ("Физика", d2)}


def test_roster_check_against_chat(env):
    loop, api, c = env
    setup_classes(loop)
    roster = ("ФИО;Родители\n"
              "Иванов Иван Петрович;Иванова Анна\n"     # родитель в чате — «Иванова А.»
              "Сидоров Илья;\n"                         # родитель сам назвал ребёнка
              "Козлов Пётр;Козлов Николай\n"            # такого родителя в чате нет
              "Морозова Ева;\n")
    p = parse.parse_file(roster.encode(), "spisok.csv", TODAY)
    run(loop, attendance.set_child_name(CHAT_ID, PARENTS[1]["user_id"], "Илья Сидоров"))
    r = run(loop, service.apply(CHAT_ID, TEACHER["user_id"], p))
    assert r.students == 4
    check = run(loop, service.roster_check(CHAT_ID))
    assert check["total"] == 4 and check["in_chat"] == 2
    assert check["missing"] == ["Козлов Пётр", "Морозова Ева"]
    assert {m["name"]: m["via"] for m in check["matched"]} == {"Иванов Иван": "parent", "Сидоров Илья": "child"}
    text = service.render_check(check, "9Б")
    assert "в чате 2 из 4" in text and "Козлов Пётр" in text

    # храним только «Фамилия Имя» и «Фамилия И.» родителей
    async def stored():
        async with session() as s:
            return [(x.name, x.parents) for x in (await s.scalars(select(RosterEntry).order_by(RosterEntry.name))).all()]
    assert ("Иванов Иван", "Иванова А.") in run(loop, stored())
    assert all("Петрович" not in n for n, _ in run(loop, stored()))

    # новый список заменяет старый, а не добавляется к нему
    run(loop, service.apply(CHAT_ID, TEACHER["user_id"], parse.parse_file("ФИО\nМорозова Ева\n".encode())))
    assert run(loop, service.roster_check(CHAT_ID))["total"] == 1


# ====================================================================== бот: файл → предпросмотр → загрузка → ответы
def test_bot_file_preview_import_and_schedule_answers(env, monkeypatch):
    loop, api, c = env
    setup_classes(loop)
    tomorrow = local_now(None).date() + timedelta(days=1)
    d = f"{tomorrow:%d.%m.%Y}"
    data = xlsx({
        "Ученики": [["ФИО ученика", "Родители"], ["Иванов Иван", "Иванова Анна"], ["Петров Лев", ""],
                    ["Орлова Кира", ""]],
        "Журнал": [["Дата", "№", "Время", "Предмет", "Домашнее задание"],
                   [d, 1, "08:30", "Алгебра", "№ 245"], [d, 2, "09:25", "Химия", ""],
                   [d, 3, "10:20", "История", "Параграф 7"]],
    })
    fetched = []

    async def fake_download(url):
        fetched.append(url)
        return data
    monkeypatch.setattr(journals, "download", fake_download)
    att = [{"type": "file", "filename": "vygruzka.xlsx", "payload": {"token": "tok1", "fileId": 77,
                                                                     "url": "https://files.example.ru/f/77"}}]

    # родитель прислал файл с подписью «журнал» — это не импорт
    run(loop, handle_update(msg_update(PARENTS[0], "журнал", mid="p0", attachments=att)))
    assert not fetched and "только учитель" in api.sent[-1]["text"]

    run(loop, handle_update(msg_update(TEACHER, "выгрузка из журнала", mid="t1", attachments=att)))
    assert fetched == ["https://files.example.ru/f/77"]
    msg = api.sent[-1]
    assert "Нашёл: 3 ученика, 2 задания, расписание на 1 день (3 урока)" in msg["text"]
    ok = next(b["payload"] for b in buttons(msg) if b["payload"].startswith("jr:ok:"))
    assert ok.endswith(f":{CHAT_ID}")

    # чужой учитель не может нажать «Загрузить» за этого (черновик привязан к владельцу)
    run(loop, handle_update(cb_update(TEACHER2, ok.replace(str(CHAT_ID), str(CHAT2_ID)), "x1")))
    assert "устарел" in api.sent[-1]["text"]
    run(loop, handle_update(cb_update(TEACHER, ok, "c1")))
    done = api.sent[-1]["text"]
    assert "Загрузил" in done and "новых 2" in done and "уроков в расписании: 3" in done
    assert "в чате 1 из 3" in done and "Петров Лев" in done
    run(loop, handle_update(cb_update(TEACHER, ok, "c2")))  # повторное нажатие не импортирует второй раз
    assert "уже загружен" in api.sent[-1]["text"]

    # родитель спрашивает расписание и что взять
    run(loop, handle_update(msg_update(PARENTS[0], "Какие уроки завтра?", mid="p1")))
    t = api.sent[-1]["text"]
    assert "Уроки на" in t and "1. 08:30 Алгебра" in t and "3. 10:20 История" in t
    run(loop, handle_update(msg_update(PARENTS[0], "что взять завтра", mid="p2")))
    t = api.sent[-1]["text"]
    assert "Что взять" in t and "Алгебра — д/з: № 245" in t and "История — д/з: Параграф 7" in t
    run(loop, handle_update(msg_update(PARENTS[0], "что задали по истории на завтра", mid="p3")))
    assert "Параграф 7" in api.sent[-1]["text"]

    # /journal — меню; таблица без подписи ложится на полку, но бот предлагает разобрать её как журнал
    run(loop, handle_update(msg_update(TEACHER, "/журнал", mid="t2")))
    assert "Электронный журнал" in api.sent[-1]["text"]
    assert {"jr:up", "jr:roster", "jr:exp:absences"} <= {b.get("payload") for b in buttons(api.sent[-1])}
    att2 = [dict(att[0], payload={"token": "tok2", "fileId": 78, "url": "https://files.example.ru/f/78"})]
    run(loop, handle_update(msg_update(TEACHER, "", mid="t3", attachments=att2)))
    offer = api.sent[-1]
    assert "выгрузка из электронного журнала" in offer["text"]
    run(loop, handle_update(cb_update(TEACHER, buttons(offer)[0]["payload"], "c3")))
    assert "Нашёл:" in api.sent[-1]["text"]

    # «Загрузить выгрузку» → следующий файл разбирается без подписи
    run(loop, handle_update(cb_update(TEACHER, "jr:up", "c4")))
    run(loop, handle_update(msg_update(TEACHER, "", mid="t4", attachments=att)))
    assert "Нашёл:" in api.sent[-1]["text"]

    # учителю без расписания у класса — подсказка, где его взять
    run(loop, handle_update(msg_update(TEACHER2, "какие уроки завтра", mid="u1")))
    assert "/journal" in api.sent[-1]["text"]


# ====================================================================== HTTP: права, предпросмотр, экспорт
def upload_request(data: bytes, filename: str, fields: dict | None = None) -> Request:
    req = httpx.Request("POST", "http://testserver/api/journals/preview", files={"file": (filename, data)},
                        data=fields or {})
    body = req.read()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    headers = [(k.lower().encode(), v.encode()) for k, v in req.headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/api/journals/preview", "headers": headers,
                    "query_string": b""}, receive)


def test_http_permissions_preview_import_and_signed_export(env):
    loop, api, c = env
    setup_classes(loop)
    teacher = run(loop, chats.get_user(TEACHER["user_id"]))
    parent = run(loop, chats.upsert_user(PARENTS[0]))

    # родитель не может ни загружать, ни выгружать
    with pytest.raises(HTTPException) as e:
        run(loop, routes._teacher(parent))
    assert e.value.status_code == 403
    assert run(loop, routes._teacher(teacher)) is teacher

    data = HW_CSV.format(d1="05.11.2026", d2="06.11.2026").encode("utf-8-sig")
    prev = run(loop, routes.preview(upload_request(data, "dz.csv", {"chat_id": str(CHAT_ID)}), teacher))
    assert prev["summary"]["homework"] == 3 and prev["summary"]["days"] == 2
    assert prev["text"] == "3 задания, расписание на 2 дня (3 урока)" and prev["sample"]["homework"][0]["subject"]
    # в чужой класс — нельзя; чужой черновик — не найти
    with pytest.raises(HTTPException) as e:
        run(loop, routes.do_import(routes.ImportIn(ref=prev["ref"], chat_id=CHAT2_ID), teacher))
    assert e.value.status_code == 404
    teacher2 = run(loop, chats.get_user(TEACHER2["user_id"]))
    with pytest.raises(HTTPException) as e:
        run(loop, routes.do_import(routes.ImportIn(ref=prev["ref"], chat_id=CHAT2_ID), teacher2))
    assert e.value.status_code == 404
    res = run(loop, routes.do_import(routes.ImportIn(ref=prev["ref"], chat_id=CHAT_ID), teacher))
    assert res["result"]["hw_added"] == 3
    with pytest.raises(HTTPException):  # черновик одноразовый
        run(loop, routes.do_import(routes.ImportIn(ref=prev["ref"], chat_id=CHAT_ID), teacher))
    # плохой файл — 422 с понятным текстом, слишком большой — 413
    with pytest.raises(HTTPException) as e:
        run(loop, routes.preview(upload_request(parse.OLE_MAGIC + b"\x00" * 64, "old.xls"), teacher))
    assert e.value.status_code == 422 and "xlsx" in e.value.detail
    with pytest.raises(HTTPException) as e:
        run(loop, routes.preview(upload_request(b"x" * (parse.MAX_BYTES + 100), "big.csv"), teacher))
    assert e.value.status_code == 413

    ov = run(loop, routes.overview(teacher))
    assert ov["classes"][0]["has_schedule"] and not ov["dnevnik_enabled"]

    # экспорт: подписанная ссылка, CSV с BOM и «;», формулы обезврежены
    day = local_now(None).date()
    run(loop, attendance.mark(CHAT_ID, PARENTS[0]["user_id"], day.isoformat(), "sick", "=HYPERLINK(\"http://evil\")",
                              "+79001234567"))
    run(loop, attendance.mark(CHAT_ID, PARENTS[1]["user_id"], day.isoformat(), "family", "Сидоров Илья"))
    links = run(loop, routes.export_links(0, None, None, teacher))
    q = dict(x.split("=", 1) for x in links["absences"].split("?", 1)[1].split("&"))
    resp = run(loop, routes.export_csv("absences", int(q["owner"]), int(q["chat"]), date.fromisoformat(q["start"]),
                                       date.fromisoformat(q["end"]), int(q["exp"]), q["sig"]))

    async def body(r):
        return b"".join([chunk async for chunk in r.body_iterator])
    raw = run(loop, body(resp))
    assert raw.startswith("﻿".encode())
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0] == "Дата;Класс;Ученик;Причина;Комментарий;Сообщил"
    assert any(f"{day:%d.%m.%Y};" in x and "Сидоров Илья;семейные обстоятельства" in x for x in lines)
    assert any("'=HYPERLINK" in x and ";'+79001234567;" in x for x in lines)
    with pytest.raises(HTTPException) as e:  # подделанная подпись
        run(loop, routes.export_csv("absences", int(q["owner"]), int(q["chat"]), date.fromisoformat(q["start"]),
                                    date.fromisoformat(q["end"]), int(q["exp"]), "0" * 32))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException):  # просроченная ссылка
        run(loop, routes.export_csv("absences", int(q["owner"]), int(q["chat"]), date.fromisoformat(q["start"]),
                                    date.fromisoformat(q["end"]), int(time.time()) - 1, q["sig"]))


def test_export_polls_csv_and_api_v1(env):
    loop, api, c = env
    setup_classes(loop)
    from app.services import polls

    p = run(loop, polls.create_poll(TEACHER["user_id"], CHAT_ID, "consent", "@cmd Экскурсия\nВ пятницу", [], None))
    run(loop, polls.send_poll(p.id))
    p = run(loop, polls.get_poll(p.id))
    day = local_now(None).date()
    data = run(loop, export.polls_csv([CHAT_ID], {CHAT_ID: "9Б"}, day, day, None)).decode("utf-8-sig")
    lines = data.splitlines()
    assert lines[0].startswith("Создан;Класс;Сбор;Срок;Статус;Участник")
    assert len(lines) == 1 + len(p.targets) and all(";'@cmd Экскурсия;" in x for x in lines[1:])
    assert all(";не ответил;" in x for x in lines[1:])

    # открытый API: расписание и выгрузки тем же ключом, что и остальное
    run(loop, service.apply(CHAT_ID, TEACHER["user_id"], parse.parse_file(
        f"Дата;№;Предмет\n{day:%d.%m.%Y};1;Алгебра\n{day:%d.%m.%Y};2;Химия\n".encode())))
    _, raw = run(loop, tokens.create_api_key(TEACHER["user_id"], "Журнал"))

    async def call(fn, *a, **kw):
        k = await api_v1.api_key(authorization=f"Bearer {raw}", x_api_key=None)
        return await fn(*a, key=k, **kw)
    sched = run(loop, call(api_v1.get_schedule, day, 1, None))
    assert [(x["number"], x["subject"]) for x in sched] == [(1, "Алгебра"), (2, "Химия")]
    assert sched[0]["class"]["id"] == CHAT_ID
    resp = run(loop, call(api_v1.export_csv, "polls", day, day, None))
    assert resp.media_type.startswith("text/csv") and resp.body.decode("utf-8-sig").startswith("Создан;")
    with pytest.raises(HTTPException):
        run(loop, call(api_v1.export_csv, "secrets", day, day, None))


# ====================================================================== «Дневник.ру» на поддельном API
class FakeDnevnik:
    def __init__(self, status=200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "authorizationFailed"})
        path = request.url.path
        if "/edu-groups/555/lessons/" in path:
            return httpx.Response(200, json=[
                {"id": 1001, "date": "2026-11-05T00:00:00", "number": 1, "subject": {"id": 7, "name": "Алгебра"}},
                {"id": 1002, "date": "2026-11-05T00:00:00", "number": 2, "subject": {"id": 8, "name": "История"}},
            ])
        if path.endswith("/users/me/school/42/homeworks"):
            return httpx.Response(200, json={
                "works": [{"id": 1, "text": "№ 300", "subjectId": 7, "targetDate": "2026-11-05T00:00:00",
                           "lesson": 1001},
                          {"id": 2, "text": "Чужой класс", "subjectId": 7, "targetDate": "2026-11-05T00:00:00",
                           "lesson": 9999}],
                "subjects": [{"id": 7, "name": "Алгебра"}, {"id": 8, "name": "История"}]})
        return httpx.Response(404)

    def install(self, monkeypatch):
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(dnevnik, "client", lambda: httpx.AsyncClient(
            base_url="https://api.dnevnik.test/v2/", transport=transport))


def test_dnevnik_connector_with_mock(env, monkeypatch):
    loop, api, c = env
    setup_classes(loop)
    teacher = run(loop, chats.get_user(TEACHER["user_id"]))
    fake = FakeDnevnik()
    fake.install(monkeypatch)

    # по умолчанию выключен
    assert not dnevnik.enabled()
    with pytest.raises(HTTPException) as e:
        run(loop, routes.dnevnik_link(CHAT_ID, routes.DnevnikIn(group_ref="555"), teacher))
    assert e.value.status_code == 404

    c.settings.dnevnik_enabled = True
    out = run(loop, routes.dnevnik_link(CHAT_ID, routes.DnevnikIn(school_ref="42", group_ref="555",
                                                                  token="secret-oauth-token"), teacher))
    assert out == {"school_ref": "42", "group_ref": "555", "own_token": True}

    async def stored():
        async with session() as s:
            return (await s.scalar(select(JournalLink))).token_enc
    enc = run(loop, stored())
    assert "secret-oauth-token" not in enc and dnevnik.unseal(enc) == "secret-oauth-token"
    assert dnevnik.unseal(enc[:-4] + "AAAA") is None  # испорченная запись не расшифровывается

    res = run(loop, routes.dnevnik_sync(CHAT_ID, routes.SyncIn(**{"from": date(2026, 11, 2), "to": date(2026, 11, 8)}),
                                        teacher))
    assert res["result"]["lessons"] == 2 and res["result"]["hw_added"] == 1
    assert all(r.headers["Access-Token"] == "secret-oauth-token" for r in fake.requests)
    assert fake.requests[0].url.path == "/v2/edu-groups/555/lessons/2026-11-02/2026-11-08"
    assert fake.requests[1].url.params["startDate"] == "2026-11-02T00:00:00"

    async def hw():
        async with session() as s:
            return [(a.subject, a.text, a.for_date, a.source) for a in (await s.scalars(select(Announcement))).all()]
    assert run(loop, hw()) == [("Алгебра", "№ 300", "2026-11-05", "dnevnik")]
    # повтор — без дублей
    res = run(loop, routes.dnevnik_sync(CHAT_ID, routes.SyncIn(**{"from": date(2026, 11, 2), "to": date(2026, 11, 8)}),
                                        teacher))
    assert res["result"]["hw_added"] == 0 and len(run(loop, hw())) == 1

    # токен не принят — понятная ошибка и отметка в статусе
    fake.status = 401
    with pytest.raises(HTTPException) as e:
        run(loop, routes.dnevnik_sync(CHAT_ID, routes.SyncIn(), teacher))
    assert e.value.status_code == 502 and "токен" in e.value.detail
    link = run(loop, dnevnik.get_link(CHAT_ID))
    assert link.last_status.startswith("ошибка")
    # чужой класс подключить нельзя
    with pytest.raises(HTTPException):
        run(loop, routes.dnevnik_link(CHAT2_ID, routes.DnevnikIn(group_ref="1"), teacher))


def test_upload_size_limit_is_checked_by_handler(env):
    """Общий лимит тела запроса (1 МБ) не режет выгрузку журнала до 3 МБ — её размер проверяет сам обработчик."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)  # без lifespan: до базы запрос не доходит — вход не пройден
    big = b"x" * (2 * 1024 * 1024)
    r = client.post("/api/journals/preview", files={"file": ("big.csv", big)}, headers={"X-Peremena": "1"})
    assert r.status_code == 401
    r = client.post("/api/faq", content=big, headers={"X-Peremena": "1", "Content-Type": "application/json"})
    assert r.status_code == 413

"""Разбор выгрузок электронного журнала: CSV и XLSX.

Файл читается как данные и только как данные: никакого eval, формулы не вычисляются (openpyxl
в режиме read_only и data_only — берётся последнее сохранённое значение ячейки), размер файла,
распакованного архива XLSX, число листов, строк, столбцов и длина ячейки ограничены.

Формат определяется по заголовкам столбцов, а не по названию журнала: у разных журналов и регионов
одна и та же колонка называется по-разному («ФИО», «Обучающийся», «Ученик»), над шапкой бывают
строки с названием класса и периода. Варианты названий — в ``FIELDS``.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

from ...services import board

MAX_BYTES = 3 * 1024 * 1024          # выгрузка класса за четверть — десятки килобайт
MAX_UNZIPPED = 40 * 1024 * 1024      # XLSX — это zip: защита от «zip-бомбы»
MAX_ZIP_ENTRIES = 300
MAX_SHEETS = 10
MAX_ROWS = 5000                      # строк во всех листах вместе
MAX_COLS = 60
MAX_CELL = 1000
HEADER_SCAN = 20                     # в скольких первых строках искать шапку

XLSX_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # старый .xls


class BadFile(ValueError):
    """Файл не прочитать: понятное учителю объяснение — в тексте ошибки."""


# ------------------------------------------------------------------ варианты названий столбцов
# Порядок важен: «ФИО родителя» должно попасть в parents раньше, чем «ФИО» — в student.
FIELDS: list[tuple[str, tuple[str, ...]]] = [
    ("parents", ("родители", "родитель", "фио родителя", "фио родителей", "родители законные представители",
                 "законный представитель", "законные представители", "представитель", "представители",
                 "фио представителя", "мать", "отец", "мама", "папа", "контакты родителей")),
    ("student", ("фио ученика", "фио учащегося", "фио обучающегося", "фио ребенка", "ученик", "учащийся",
                 "обучающийся", "ученица", "ребенок", "фио", "ф и о", "фамилия имя", "фамилия имя отчество",
                 "фамилия и имя", "фамилия имя ученика", "ученики", "список класса")),
    ("last", ("фамилия",)),
    ("first", ("имя",)),
    ("middle", ("отчество",)),
    ("homework", ("домашнее задание", "дз", "д/з", "д з", "задание", "задания", "домашняя работа", "задано",
                  "домашка", "что задано", "задание на дом")),
    ("subject", ("предмет", "учебный предмет", "дисциплина", "название предмета", "предметы", "урок предмет")),
    ("when", ("дата", "дата урока", "на дату", "задано на", "к дате", "срок", "срок сдачи", "выполнить к",
              "сдать до", "день недели", "день", "число", "дата проведения")),
    ("number", ("№", "№ урока", "номер урока", "номер", "урок", "n", "no", "пара", "№ п/п урока")),
    ("time", ("время", "время урока", "начало", "звонок", "звонки", "время проведения")),
    ("room", ("кабинет", "каб", "ауд", "аудитория", "кабинет аудитория")),
    ("topic", ("тема", "тема урока", "тема занятия")),
]
MULTI = {"parents"}  # «Мать» и «Отец» — два столбца, оба в родителей

WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
WEEKDAY_WORDS = {
    "понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "среда": 2, "среду": 2, "ср": 2, "четверг": 3, "чт": 3,
    "пятница": 4, "пятницу": 4, "пт": 4, "суббота": 5, "субботу": 5, "сб": 5, "воскресенье": 6, "вс": 6,
}
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
SKIP_NAMES = ("итого", "всего", "средн", "классный руководитель", "кл. руководитель", "учитель")


# ------------------------------------------------------------------ результат разбора
@dataclass
class Parsed:
    students: list[dict] = field(default_factory=list)   # {"name": "Иванов Иван", "parents": ["Иванова А."]}
    lessons: list[dict] = field(default_factory=list)    # {"day", "weekday", "number", "subject", "time", "room"}
    homework: list[dict] = field(default_factory=list)   # {"day", "subject", "text"}
    tables: list[str] = field(default_factory=list)      # что распознано: «Лист «Ученики»: список класса»
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.students or self.lessons or self.homework)

    def summary(self) -> dict:
        days = {x["day"] or f"w{x['weekday']}" for x in self.lessons}
        return {"students": len(self.students), "lessons": len(self.lessons), "days": len(days),
                "homework": len(self.homework), "tables": self.tables, "warnings": self.warnings[:10]}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Parsed":
        return cls(**{k: d.get(k) or [] for k in ("students", "lessons", "homework", "tables", "warnings")})

    def merge(self, other: "Parsed") -> None:
        self.students += other.students
        self.lessons += other.lessons
        self.homework += other.homework
        self.tables += other.tables
        self.warnings += other.warnings


def describe(p: Parsed) -> str:
    """«27 учеников, 34 задания, расписание на 5 дней» — для предпросмотра в боте и кабинете."""
    s = p.summary()
    parts = []
    if s["students"]:
        parts.append(f"{s['students']} {plural(s['students'], 'ученик', 'ученика', 'учеников')}")
    if s["homework"]:
        parts.append(f"{s['homework']} {plural(s['homework'], 'задание', 'задания', 'заданий')}")
    if s["lessons"]:
        parts.append(f"расписание на {s['days']} {plural(s['days'], 'день', 'дня', 'дней')} "
                     f"({s['lessons']} {plural(s['lessons'], 'урок', 'урока', 'уроков')})")
    return ", ".join(parts)


def plural(n: int, one: str, few: str, many: str) -> str:
    a, b = abs(n) % 100, abs(n) % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    return one if b == 1 else many


# ------------------------------------------------------------------ чтение файла в таблицы
def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).replace("\x00", "").strip()
    return s[:MAX_CELL]


def _read_csv(data: bytes) -> list[tuple[str, list[list[str]]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")  # Excel под Windows сохраняет CSV в cp1251
    text = text.replace("\x00", "")
    head = "\n".join(text.splitlines()[:HEADER_SCAN])
    delim = max((";", "\t", ","), key=head.count)
    rows: list[list[str]] = []
    try:
        for row in csv.reader(io.StringIO(text), delimiter=delim):
            if len(rows) >= MAX_ROWS:
                break
            rows.append([_cell(c) for c in row[:MAX_COLS]])
    except csv.Error as e:
        raise BadFile(f"Не получилось разобрать CSV: {e}")
    return [("", rows)]


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        raise BadFile("Файл повреждён: это не XLSX")
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise BadFile("Таблица слишком большая — выгрузите один класс за неделю или четверть")


def _read_xlsx(data: bytes) -> list[tuple[str, list[list[str]]]]:
    _check_zip(data)
    try:
        import openpyxl
    except ImportError:  # pragma: no cover — зависимость в requirements.txt
        raise BadFile("Чтение XLSX не установлено на сервере — сохраните таблицу как CSV")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise BadFile("Не получилось открыть XLSX — сохраните таблицу заново или как CSV")
    out, total = [], 0
    try:
        for ws in wb.worksheets[:MAX_SHEETS]:
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True, max_col=MAX_COLS):
                if total >= MAX_ROWS:
                    break
                cells = [_cell(c) for c in row]
                total += 1
                rows.append(cells)
            out.append((ws.title, rows))
    except Exception:
        raise BadFile("Не получилось прочитать XLSX — сохраните таблицу заново или как CSV")
    finally:
        wb.close()
    return out


def read_tables(data: bytes, filename: str = "") -> list[tuple[str, list[list[str]]]]:
    """[(название листа, строки)]. Тип файла — по содержимому, а не по расширению."""
    if not data:
        raise BadFile("Пустой файл")
    if len(data) > MAX_BYTES:
        raise BadFile(f"Файл больше {MAX_BYTES // 1024 // 1024} МБ — выгрузите один класс")
    if data.startswith(OLE_MAGIC):
        raise BadFile("Это старый формат .xls — откройте файл и сохраните как .xlsx или .csv")
    if data.startswith(XLSX_MAGIC):
        return _read_xlsx(data)
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise BadFile("Это не таблица: пришлите выгрузку в формате CSV или XLSX")
    return _read_csv(data)


# ------------------------------------------------------------------ шапка
def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w№/ ]+", " ", s)
    return " ".join(s.split())


def _field_of(header: str, taken: set[str]) -> str | None:
    h = norm(header)
    if not h:
        return None
    for name, aliases in FIELDS:
        if name in taken and name not in MULTI:
            continue
        for a in aliases:
            # «Домашнее задание (на дату)» → домашнее задание; «Дата урока» не должна стать «датой» раньше времени —
            # поэтому сравнение по целым словам с начала
            if h == a or h.startswith(a + " "):
                return name
    return None


def _map_header(row: list[str]) -> dict[str, list[int]]:
    cols: dict[str, list[int]] = {}
    for i, h in enumerate(row):
        f = _field_of(h, set(cols))
        if f:
            cols.setdefault(f, []).append(i)
    return cols


def _weekday_cols(row: list[str]) -> dict[int, int]:
    out = {}
    for i, h in enumerate(row):
        w = norm(h).split(" ")[0] if norm(h) else ""
        if w in WEEKDAYS_FULL:
            out[i] = WEEKDAYS_FULL.index(w)
    return out


def _score(cols: dict[str, list[int]]) -> int:
    useful = {"student", "last", "parents", "homework", "subject", "when", "number", "time", "topic"}
    return len(useful & set(cols))


# ------------------------------------------------------------------ значения
DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b")
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
TEXT_DATE_RE = re.compile(r"\b(\d{1,2})\s+([а-яё]+)", re.IGNORECASE)
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


def _nearest_year(day: int, month: int, today: date) -> date | None:
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def parse_when(value: str, today: date) -> tuple[date | None, int | None]:
    """(дата, день недели). «09.11.2026», «2026-11-09», «9 ноября», «09.11» (год — ближайший), «Пн», «понедельник»."""
    v = (value or "").strip().lower().replace("ё", "е")
    if not v:
        return None, None
    m = ISO_RE.search(v)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), None
        except ValueError:
            return None, None
    m = DATE_RE.search(v)
    if m:
        d, mo, y = int(m[1]), int(m[2]), m[3]
        try:
            if y:
                yy = int(y) + (2000 if len(y) == 2 else 0)
                return date(yy, mo, d), None
            return _nearest_year(d, mo, today), None
        except ValueError:
            return None, None
    m = TEXT_DATE_RE.search(v)
    if m:
        mon = next((n for stem, n in MONTHS.items() if m[2].startswith(stem) and
                    (stem != "ма" or m[2] in ("мая", "май"))), None)
        if mon:
            return _nearest_year(int(m[1]), mon, today), None
    first = re.split(r"[\s,.]+", v)[0]
    if first in WEEKDAY_WORDS:
        return None, WEEKDAY_WORDS[first]
    return None, None


def parse_number(value: str) -> int | None:
    m = re.match(r"\s*(\d{1,2})\b", value or "")
    return int(m[1]) if m and 0 < int(m[1]) <= 12 else None


def parse_time(value: str) -> str:
    t = TIME_RE.findall(value or "")
    if not t:
        return ""
    fmt = [f"{int(h):02d}:{mm}" for h, mm in t[:2]]
    return "–".join(fmt)[:20]


PHONE_RE = re.compile(r"(\+?\d[\d\s()\-]{5,}\d)")
EMAIL_RE = re.compile(r"\S+@\S+")


def _words(s: str) -> list[str]:
    s = EMAIL_RE.sub(" ", s or "")
    s = PHONE_RE.sub(" ", s)
    s = re.sub(r"\([^)]*\)", " ", s)  # «(мама)», «(тел.)»
    return [w for w in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'\-]*", s)]


def _cap(w: str) -> str:
    return "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))


def student_name(full: str = "", last: str = "", first: str = "") -> str:
    """«ИВАНОВ Иван Иванович» → «Иванов Иван». Отчество не храним."""
    ws = _words(f"{last} {first}") if (last or first) else _words(full)
    ws = [w for w in ws if w.lower() not in ("мама", "папа", "мать", "отец")]
    if not ws:
        return ""
    return " ".join(_cap(w) for w in ws[:2])[:200]


def parent_names(cell: str) -> list[str]:
    """«Иванова Анна Петровна, +7 900 …; Иванов Пётр» → ["Иванова А.", "Иванов П."]. Телефоны и почта отбрасываются."""
    out = []
    for part in re.split(r"[,;\n/]+", cell or ""):
        ws = [w for w in _words(part) if w.lower() not in ("мама", "папа", "мать", "отец", "тел", "телефон")]
        if not ws:
            continue
        if len(ws) == 1:
            out.append(_cap(ws[0]))
        else:
            out.append(f"{_cap(ws[0])} {ws[1][0].upper()}.")
    return list(dict.fromkeys(out))[:4]


def _next_date(weekday: int, today: date) -> date:
    return today + timedelta(days=(weekday - today.weekday()) % 7)


# ------------------------------------------------------------------ таблицы
def _get(row: list[str], cols: dict[str, list[int]], name: str) -> str:
    idx = cols.get(name)
    if not idx:
        return ""
    return row[idx[0]].strip() if idx[0] < len(row) else ""


def _parse_wide(title: str, rows: list[list[str]], hi: int, wd: dict[int, int], today: date) -> Parsed:
    """Расписание «сеткой»: столбцы — дни недели, строки — уроки."""
    p = Parsed()
    head = rows[hi]
    for r_i, row in enumerate(rows[hi + 1:], 1):
        lead = row[0] if row else ""
        number = parse_number(lead) or r_i
        tm = parse_time(" ".join(row[:2]))
        for c, weekday in wd.items():
            if c >= len(row) or not row[c].strip():
                continue
            cell = row[c].strip()
            room = ""
            m = re.search(r"(?:каб\.?|кабинет|ауд\.?)\s*([\w-]+)", cell, re.IGNORECASE)
            if m:
                room = m[1]
                cell = cell[:m.start()].strip(" ,;()")
            p.lessons.append({"day": None, "weekday": weekday, "number": number,
                              "subject": board.canonical_subject(cell), "time": tm, "room": room[:30]})
    if p.lessons:
        p.tables.append(_where(title) + "расписание по дням недели")
    else:
        p.warnings.append(_where(title) + f"шапка с днями недели есть ({', '.join(head[c] for c in wd)}), но уроков нет")
    return p


def _where(title: str) -> str:
    return f"Лист «{title}»: " if title else ""


def parse_table(title: str, rows: list[list[str]], today: date) -> Parsed:
    p = Parsed()
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return p
    best, best_i, best_cols = 0, -1, {}
    for i, row in enumerate(rows[:HEADER_SCAN]):
        wd = _weekday_cols(row)
        if len(wd) >= 3:
            return _parse_wide(title, rows, i, wd, today)
        cols = _map_header(row)
        sc = _score(cols)
        if sc > best:
            best, best_i, best_cols = sc, i, cols
    cols = best_cols
    has_name = "student" in cols or "last" in cols
    if best == 0 or not (has_name or "subject" in cols):
        p.warnings.append(_where(title) + "не нашёл знакомых столбцов (ФИО, Предмет, Дата, Домашнее задание)")
        return p
    body = rows[best_i + 1:]

    if has_name and "subject" not in cols:
        seen = set()
        skipped = 0
        for row in body:
            raw = " ".join(_get(row, cols, k) for k in ("student", "last", "first")).strip()
            name = student_name(_get(row, cols, "student"), _get(row, cols, "last"), _get(row, cols, "first"))
            if not name or len(name) < 2 or any(name.lower().startswith(s) for s in SKIP_NAMES):
                if raw and not any(raw.lower().startswith(s) for s in SKIP_NAMES):
                    skipped += 1  # в ячейке что-то есть, но на имя не похоже — скажем учителю, а не промолчим
                continue
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            parents: list[str] = []
            for i in cols.get("parents", []):
                if i < len(row):
                    parents += parent_names(row[i])
            p.students.append({"name": name, "parents": list(dict.fromkeys(parents))[:4]})
        if p.students:
            p.tables.append(_where(title) + "список класса")
        if skipped:
            p.warnings.append(_where(title) + f"{skipped} строк(и) со столбцом ФИО не похожи на имя и пропущены — проверьте файл")
        return p

    # уроки и/или домашние задания
    hw_by_key: dict[tuple, list[str]] = {}
    lessons_seen = set()
    no_date = 0
    for row in body:
        subject_raw = _get(row, cols, "subject")
        num_raw = _get(row, cols, "number")
        number = parse_number(num_raw)
        if not subject_raw and num_raw and number is None:
            subject_raw = num_raw  # «Урок: Алгебра» — в столбце «Урок» написан предмет
        if not subject_raw:
            continue
        subject = board.canonical_subject(subject_raw)
        d, wd = parse_when(_get(row, cols, "when"), today)
        if d is None and wd is None:
            no_date += 1
        if "when" in cols and (d or wd is not None):
            key = (d.isoformat() if d else None, d.weekday() if d else wd, number or 0, subject)
            if key not in lessons_seen and ("number" in cols or "time" in cols or "homework" not in cols):
                lessons_seen.add(key)
                p.lessons.append({"day": key[0], "weekday": key[1], "number": number or 0, "subject": subject,
                                  "time": parse_time(_get(row, cols, "time")), "room": _get(row, cols, "room")[:30]})
        text = " ".join(_get(row, cols, "homework").split())
        if "homework" in cols and text and text not in ("-", "—", "нет", "не задано"):
            day = d or (_next_date(wd, today) if wd is not None else None)
            hw_by_key.setdefault((day.isoformat() if day else None, subject), [])
            if text not in hw_by_key[(day.isoformat() if day else None, subject)]:
                hw_by_key[(day.isoformat() if day else None, subject)].append(text)
    for (day, subject), texts in hw_by_key.items():
        p.homework.append({"day": day, "subject": subject, "text": "; ".join(texts)[:3500]})
    what = []
    if p.lessons:
        what.append("расписание")
    if p.homework:
        what.append("домашние задания")
    if what:
        p.tables.append(_where(title) + " и ".join(what))
    if no_date and p.homework and "when" in cols:
        p.warnings.append(_where(title) + f"в {no_date} строк(ах) нет даты — такие задания лягут на доску без дня")
    if not what:
        p.warnings.append(_where(title) + "столбцы нашлись, но строк с данными нет")
    return p


def parse_file(data: bytes, filename: str = "", today: date | None = None) -> Parsed:
    today = today or date.today()
    out = Parsed()
    for title, rows in read_tables(data, filename):
        out.merge(parse_table(title, rows, today))
    # один ученик на двух листах — один раз
    seen, uniq = set(), []
    for s in out.students:
        if s["name"].lower() not in seen:
            seen.add(s["name"].lower())
            uniq.append(s)
    out.students = uniq
    return out

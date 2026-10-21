"""Время: всё хранится в UTC, показывается и вводится в часовом поясе учителя."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "Europe/Moscow")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc: datetime, tz: str | None) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(zone(tz))


def local_now(tz: str | None) -> datetime:
    return datetime.now(zone(tz))


def to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_dt(dt_utc: datetime | None, tz: str | None) -> str:
    if not dt_utc:
        return "без срока"
    d = to_local(dt_utc, tz)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}, {d:%H:%M}"


def fmt_date(dt_utc: datetime | None, tz: str | None) -> str:
    if not dt_utc:
        return ""
    return f"{to_local(dt_utc, tz):%d.%m.%Y}"


def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def preset_deadline(key: str, tz: str | None) -> datetime:
    """Готовые варианты срока: today | tomorrow | friday | week. Возвращает UTC."""
    now = local_now(tz)
    at18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if key == "today":
        d = at18 if now < at18 else now + timedelta(hours=3)
    elif key == "tomorrow":
        d = at18 + timedelta(days=1)
    elif key == "friday":
        days = (4 - now.weekday()) % 7
        d = at18 + timedelta(days=days)
        if d <= now:
            d += timedelta(days=7)
    else:  # week
        d = at18 + timedelta(days=7)
    return to_utc(d)


_dt_re = re.compile(
    r"(?P<d>\d{1,2})[./](?P<m>\d{1,2})(?:[./](?P<y>\d{2,4}))?(?:\s+(?:в\s+)?(?P<h>\d{1,2})[:.](?P<mi>\d{2}))?"
)
_time_only_re = re.compile(r"^(?:сегодня\s+)?(?:в\s+)?(?P<h>\d{1,2}):(?P<mi>\d{2})$")
_tomorrow_re = re.compile(r"^завтра(?:\s+(?:в\s+)?(?P<h>\d{1,2})[:.](?P<mi>\d{2}))?$")


def parse_deadline(text: str, tz: str | None) -> datetime | None:
    """Разбирает «28.09», «28.09 18:00», «завтра 9:00», «18:00». Возвращает UTC или None."""
    t = (text or "").strip().lower()
    now = local_now(tz)
    m = _tomorrow_re.match(t)
    if m:
        h, mi = int(m["h"] or 18), int(m["mi"] or 0)
        if h > 23 or mi > 59:
            return None
        d = (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return to_utc(d)
    m = _time_only_re.match(t)
    if m:
        if int(m["h"]) > 23 or int(m["mi"]) > 59:
            return None
        d = now.replace(hour=int(m["h"]), minute=int(m["mi"]), second=0, microsecond=0)
        if d <= now:
            d += timedelta(days=1)
        return to_utc(d)
    m = _dt_re.search(t)
    if m:
        year = now.year
        if m["y"]:
            year = int(m["y"]) + (2000 if len(m["y"]) == 2 else 0)
        h, mi = int(m["h"] or 18), int(m["mi"] or 0)
        try:
            d = now.replace(year=year, month=int(m["m"]), day=int(m["d"]), hour=h, minute=mi, second=0, microsecond=0)
        except ValueError:
            return None
        if d <= now and not m["y"]:
            d = d.replace(year=year + 1)
        if d <= now:
            return None
        return to_utc(d)
    for i, name in enumerate(WEEKDAYS_FULL):
        if t.startswith(name[:4]):
            days = (i - now.weekday()) % 7 or 7
            d = (now + timedelta(days=days)).replace(hour=18, minute=0, second=0, microsecond=0)
            return to_utc(d)
    return None


def parse_hours(text: str) -> tuple[int, int] | None:
    """«8-19», «8:30–18:00» → (480, 1140)."""
    m = re.match(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?\s*$", text or "")
    if not m:
        return None
    a = int(m[1]) * 60 + int(m[2] or 0)
    b = int(m[3]) * 60 + int(m[4] or 0)
    if not (0 <= a < b <= 24 * 60):
        return None
    return a, b

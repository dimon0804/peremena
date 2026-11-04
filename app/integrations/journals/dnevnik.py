"""Коннектор «Дневник.ру» по REST API (api.dnevnik.ru, версия v2) — за флагом DNEVNIK_ENABLED.

Что взято из открытых источников (не проверено на живом сервисе — у команды нет доступа партнёра):
* адрес https://api.dnevnik.ru/v2/, токен OAuth передаётся заголовком ``Access-Token``;
  токен выдаёт login.dnevnik.ru/oauth2 с правами CommonInfo, EducationalInfo;
* уроки класса за период: ``GET edu-groups/{группа}/lessons/{с}/{по}``;
* домашние задания школы за период: ``GET users/me/school/{школа}/homeworks?startDate=…&endDate=…``.

Источник — открытая обёртка pydnevnikruapi (github.com/kesha1225/DnevnikRuAPI) и описание методов
на api.dnevnik.ru/partners/swagger. Названия полей в ответах разбираются терпимо (несколько вариантов),
потому что сверить их с живыми ответами не было возможности. Всё, что пришло, проходит тот же путь,
что и импорт файла: ``service.apply`` с source="dnevnik".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import date, datetime

import httpx
from sqlalchemy import select

from ...core import ctx
from ...db import session, utcnow
from ...models import JournalLink
from ...services import board
from .parse import Parsed

PROVIDER = "dnevnik"
TIMEOUT = 15.0
MAX_RESPONSE = 5 * 1024 * 1024
MAX_DAYS = 31


class DnevnikError(Exception):
    pass


def enabled() -> bool:
    return bool(ctx().settings.dnevnik_enabled)


# ------------------------------------------------------------------ хранение токена
# Токен учителя нужен сервису в открытом виде (его надо отправить в API), поэтому хранится он
# зашифрованным: поток HMAC-SHA256 (режим счётчика) + отдельный HMAC-тег, ключи выводятся из ключа
# подписей сервиса (SECRET_KEY). Копия базы без SECRET_KEY токена не раскрывает.
def _keys() -> tuple[bytes, bytes]:
    k = ctx().settings.signing_key
    return (hmac.new(k, b"journal-token:enc", hashlib.sha256).digest(),
            hmac.new(k, b"journal-token:mac", hashlib.sha256).digest())


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(token: str) -> str:
    enc, mac = _keys()
    nonce = secrets.token_bytes(16)
    raw = token.encode()
    ct = bytes(a ^ b for a, b in zip(raw, _stream(enc, nonce, len(raw))))
    tag = hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:16]
    return "v1." + base64.urlsafe_b64encode(nonce + ct + tag).decode()


def unseal(value: str | None) -> str | None:
    if not value or not value.startswith("v1."):
        return None
    try:
        blob = base64.urlsafe_b64decode(value[3:].encode())
    except (ValueError, TypeError):
        return None
    if len(blob) < 33:
        return None
    nonce, ct, tag = blob[:16], blob[16:-16], blob[-16:]
    enc, mac = _keys()
    if not hmac.compare_digest(tag, hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:16]):
        return None  # сменили SECRET_KEY или запись испорчена
    return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct)))).decode(errors="replace")


# ------------------------------------------------------------------ подключение класса
async def get_link(chat_id: int) -> JournalLink | None:
    async with session() as s:
        return await s.scalar(select(JournalLink).where(JournalLink.chat_id == chat_id,
                                                        JournalLink.provider == PROVIDER))


async def save_link(chat_id: int, owner_id: int, school_ref: str, group_ref: str,
                    token: str | None = None) -> JournalLink:
    """token: None — не менять сохранённый; "" — убрать свой и пользоваться токеном школы."""
    async with session() as s:
        link = await s.scalar(select(JournalLink).where(JournalLink.chat_id == chat_id,
                                                        JournalLink.provider == PROVIDER))
        if link is None:
            link = JournalLink(chat_id=chat_id, owner_id=owner_id, provider=PROVIDER)
            s.add(link)
        link.owner_id = owner_id
        link.school_ref, link.group_ref = school_ref.strip()[:40], group_ref.strip()[:40]
        if token is not None:
            link.token_enc = seal(token.strip()) if token.strip() else None
        await s.flush()
        return link


async def remove_link(chat_id: int) -> bool:
    async with session() as s:
        link = await s.scalar(select(JournalLink).where(JournalLink.chat_id == chat_id,
                                                        JournalLink.provider == PROVIDER))
        if link is None:
            return False
        await s.delete(link)
        return True


def token_for(link: JournalLink) -> str:
    return unseal(link.token_enc) or ctx().settings.dnevnik_token


# ------------------------------------------------------------------ HTTP
def client() -> httpx.AsyncClient:
    """Отдельная функция — в тестах её подменяют на клиента с поддельным транспортом."""
    return httpx.AsyncClient(base_url=ctx().settings.dnevnik_api_url.rstrip("/") + "/", timeout=TIMEOUT,
                             follow_redirects=False)


async def _get(http: httpx.AsyncClient, path: str, token: str, params: dict | None = None):
    try:
        resp = await http.get(path, params=params, headers={"Access-Token": token, "Accept": "application/json"})
    except httpx.HTTPError as e:
        raise DnevnikError(f"«Дневник.ру» не ответил: {type(e).__name__}")
    if resp.status_code in (401, 403):
        raise DnevnikError("«Дневник.ру» не принял токен — получите новый и сохраните его в настройках")
    if resp.status_code >= 400:
        raise DnevnikError(f"«Дневник.ру» ответил ошибкой HTTP {resp.status_code}")
    if len(resp.content) > MAX_RESPONSE:
        raise DnevnikError("Слишком большой ответ — возьмите период покороче")
    try:
        return resp.json()
    except ValueError:
        raise DnevnikError("«Дневник.ру» вернул не JSON")


def _day(v) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")[:19]).date()
    except ValueError:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None


def _subject_name(obj: dict, subjects: dict) -> str:
    subj = obj.get("subject")
    if isinstance(subj, dict) and subj.get("name"):
        return str(subj["name"])
    for k in ("subjectName", "subject_name"):
        if obj.get(k):
            return str(obj[k])
    sid = obj.get("subjectId") or obj.get("subject_id") or (subj if isinstance(subj, (int, str)) else None)
    return subjects.get(str(sid), "") if sid is not None else ""


def _items(data, *keys) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return []


async def fetch(link: JournalLink, start: date, end: date) -> Parsed:
    """Уроки и домашние задания класса за период (не длиннее месяца) в виде, как после разбора файла."""
    token = token_for(link)
    if not token:
        raise DnevnikError("Нет токена: укажите свой или попросите администратора задать DNEVNIK_TOKEN")
    if not link.group_ref:
        raise DnevnikError("Не указан идентификатор класса (группы) в «Дневник.ру»")
    if (end - start).days > MAX_DAYS or end < start:
        raise DnevnikError("Период — не длиннее месяца")
    p = Parsed()
    async with client() as http:
        lessons_raw = await _get(http, f"edu-groups/{link.group_ref}/lessons/{start.isoformat()}/{end.isoformat()}",
                                 token)
        homework_raw = None
        if link.school_ref:
            homework_raw = await _get(http, f"users/me/school/{link.school_ref}/homeworks", token,
                                      {"startDate": f"{start.isoformat()}T00:00:00",
                                       "endDate": f"{end.isoformat()}T23:59:59"})
    lesson_ids = set()
    for x in _items(lessons_raw, "lessons", "items"):
        d = _day(x.get("date"))
        subj = _subject_name(x, {}) or str(x.get("title") or "")
        if d is None or not subj:
            continue
        if x.get("id") is not None:
            lesson_ids.add(str(x["id"]))
        number = x.get("number")
        p.lessons.append({"day": d.isoformat(), "weekday": d.weekday(),
                          "number": number if isinstance(number, int) and 0 < number <= 12 else 0,
                          "subject": board.canonical_subject(subj), "time": "", "room": ""})
    skipped = 0
    if homework_raw is not None:
        subjects = {str(s.get("id")): str(s.get("name") or "") for s in _items(homework_raw, "subjects")}
        lessons_in_hw = {str(x.get("id")): x for x in _items(homework_raw, "lessons")}
        for w in _items(homework_raw, "works", "homeworks", "items"):
            lid = w.get("lesson") if not isinstance(w.get("lesson"), dict) else w["lesson"].get("id")
            lid = str(lid if lid is not None else w.get("lessonId", ""))
            group = str(w.get("eduGroup") or w.get("eduGroupId") or "")
            if not ((lid and lid in lesson_ids) or (group and group == str(link.group_ref))):
                skipped += 1
                continue
            text = " ".join(str(w.get("text") or "").split())
            if not text:
                continue
            subj = _subject_name(w, subjects) or _subject_name(lessons_in_hw.get(lid, {}), subjects)
            d = _day(w.get("targetDate") or w.get("date"))
            p.homework.append({"day": d.isoformat() if d else None, "subject": board.canonical_subject(subj),
                               "text": text[:3500]})
    if p.lessons:
        p.tables.append("«Дневник.ру»: расписание")
    if p.homework:
        p.tables.append("«Дневник.ру»: домашние задания")
    if skipped:
        p.warnings.append(f"Пропущено заданий других классов: {skipped}")
    if homework_raw is None:
        p.warnings.append("Не указан идентификатор школы — домашние задания не запрашивались")
    return p


async def mark(link_id: int, status: str) -> None:
    async with session() as s:
        link = await s.get(JournalLink, link_id)
        if link:
            link.last_sync_at = utcnow()
            link.last_status = status[:300]


async def sync(chat_id: int, author_id: int, start: date, end: date) -> tuple[Parsed, dict]:
    """Забрать из «Дневник.ру» и положить в «Перемену». Возвращает (что пришло, итог импорта)."""
    from . import service

    if not enabled():
        raise DnevnikError("Коннектор «Дневник.ру» выключен (DNEVNIK_ENABLED)")
    link = await get_link(chat_id)
    if link is None:
        raise DnevnikError("Класс не подключён к «Дневник.ру»")
    try:
        parsed = await fetch(link, start, end)
    except DnevnikError as e:
        await mark(link.id, f"ошибка: {e}")
        raise
    res = await service.apply(chat_id, author_id, parsed, source="dnevnik")
    await mark(link.id, f"ок: уроков {res.lessons}, заданий новых {res.hw_added}, обновлено {res.hw_updated}")
    return parsed, res.as_dict()

"""Электронные журналы: импорт выгрузок, экспорт обратно, коннектор «Дневник.ру».

У «Сетевого города», ЭлЖура, ЭЖД/МЭШ, региональных «Электронных школ» и ФГИС «Моя школа» нет открытого
API для сторонних ботов, а выгрузка в Excel/CSV есть почти везде. Поэтому основной путь — файл:

* ``parse``    — разбор CSV/XLSX: список класса, расписание, домашние задания (формат — по заголовкам);
* ``service``  — запись в «Перемену»: доска без дублей, расписание, сверка списка класса с чатом;
* ``schedule`` — ответы бота «какие уроки завтра» и «что взять завтра»;
* ``export``   — обратно в журнал: отсутствия и итоги сборов в CSV (UTF-8 с BOM, «;»), шаблон для импорта;
* ``dnevnik``  — «Дневник.ру» по OAuth-токену, за флагом DNEVNIK_ENABLED (на живом API не проверен);
* ``routes``   — HTTP: предпросмотр и загрузка файла, выгрузки, шаблон, подключение «Дневник.ру».

Здесь — общий для бота и кабинета шаг «разобрал файл → показал, что нашёл → по кнопке загрузил».
"""
from __future__ import annotations

import secrets
from datetime import date

import httpx

from ...core import ctx
from .parse import MAX_BYTES, BadFile, Parsed, describe, parse_file

DRAFT_TTL = 30 * 60
EXTENSIONS = (".csv", ".xlsx", ".txt", ".tsv")


async def save_draft(owner_id: int, parsed: Parsed, filename: str = "", chat_id: int | None = None) -> str:
    """Разобранный файл ждёт подтверждения полчаса; ключ — случайный, привязан к владельцу."""
    ref = secrets.token_urlsafe(9)
    await ctx().kv.set(f"jr:{ref}", {"owner": owner_id, "parsed": parsed.to_dict(), "filename": filename[:200],
                                     "chat_id": chat_id}, DRAFT_TTL)
    return ref


async def load_draft(owner_id: int, ref: str) -> dict | None:
    if not ref or len(ref) > 40:
        return None
    d = await ctx().kv.get(f"jr:{ref}")
    if not d or d.get("owner") != owner_id:
        return None
    d["parsed"] = Parsed.from_dict(d.get("parsed") or {})
    return d


async def drop_draft(ref: str) -> None:
    await ctx().kv.delete(f"jr:{ref}")


def looks_like_table(filename: str) -> bool:
    return (filename or "").lower().endswith(EXTENSIONS)


async def download(url: str) -> bytes:
    """Скачивает файл, присланный боту (адрес из вложения MAX), не больше MAX_BYTES.
    Адрес проверяется так же, как адреса вебхуков: только https и не во внутреннюю сеть."""
    from ..webhooks import BadUrl, check_url

    try:
        url = await check_url(url)
    except BadUrl as e:
        raise BadFile(f"Не удалось скачать файл: {e}")
    buf = bytearray()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False) as http:
            async with http.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise BadFile(f"Не удалось скачать файл: HTTP {resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise BadFile(f"Файл больше {MAX_BYTES // 1024 // 1024} МБ — выгрузите один класс")
    except httpx.HTTPError:
        raise BadFile("Не удалось скачать файл — пришлите его ещё раз")
    return bytes(buf)


def parse_bytes(data: bytes, filename: str = "", today: date | None = None) -> Parsed:
    p = parse_file(data, filename, today)
    if p.empty:
        why = "; ".join(p.warnings[:3])
        raise BadFile("Не нашёл в файле ни учеников, ни расписания, ни заданий. "
                      + (why + ". " if why else "")
                      + "Проверьте, что в первой строке таблицы — названия столбцов, или возьмите наш шаблон.")
    return p


__all__ = ["BadFile", "Parsed", "describe", "parse_bytes", "save_draft", "load_draft", "drop_draft", "download",
           "looks_like_table", "DRAFT_TTL"]

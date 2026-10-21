"""«Полка» — материалы класса под рукой.

Учитель пересылает боту файл с подписью — бот кладёт его на полку с тегами (#тег в подписи).
Вложения, которые учитель отправляет в учебный чат, бот отмечает сам. Поиск — по названию
файла, подписи, тегам, классу; найденное можно переслать в любой чат одной кнопкой.
"""
from __future__ import annotations

import html
import logging
import re

from sqlalchemy import select

from ..core import ctx
from ..db import session
from ..max_api import MaxApiError
from ..models import ClassChat, Material
from ..timeutil import fmt_date
from . import search

log = logging.getLogger(__name__)

MEDIA_TYPES = ("file", "image", "video", "audio")
TAG_RE = re.compile(r"#([\wа-яё-]+)", re.IGNORECASE)
TYPE_ICON = {"file": "📄", "image": "🖼", "video": "🎬", "audio": "🎧"}


def extract_media(message: dict) -> list[dict]:
    """Медиавложения сообщения в виде {type, payload, filename, url, size, key}."""
    out = []
    for a in ((message.get("body") or {}).get("attachments") or []):
        t = a.get("type")
        if t not in MEDIA_TYPES:
            continue
        p = a.get("payload") or {}
        token = p.get("token")
        if not token:
            continue
        key = str(p.get("photo_id") or p.get("fileId") or p.get("id") or token)[:200]
        out.append({
            "type": t,
            "payload": {"token": token},
            "filename": a.get("filename") or {"image": "Фото", "video": "Видео", "audio": "Аудио"}.get(t, "Файл"),
            "url": p.get("url"),
            "size": a.get("size"),
            "key": key,
        })
    return out


def split_caption(caption: str) -> tuple[str, str]:
    tags = [t.lower() for t in TAG_RE.findall(caption or "")]
    title = TAG_RE.sub("", caption or "").strip()
    return title, ", ".join(dict.fromkeys(tags))


async def save(teacher_id: int, media: dict, caption: str = "", chat_id: int | None = None,
               source: str = "manual", sent_at=None) -> tuple[Material, bool]:
    """Сохраняет материал. Возвращает (материал, создан_ли_новый)."""
    title, tags = split_caption(caption)
    async with session() as s:
        existing = await s.scalar(select(Material).where(
            Material.teacher_id == teacher_id, Material.file_key == media["key"]
        ))
        if existing:
            if title and not existing.title:
                existing.title = title
            if tags:
                existing.tags = ", ".join(dict.fromkeys([*search.split_keywords(existing.tags), *tags.split(", ")]))
            if chat_id and not existing.chat_id:
                existing.chat_id = chat_id
            return existing, False
        m = Material(
            teacher_id=teacher_id, chat_id=chat_id, title=title[:1000], filename=media.get("filename") or "",
            tags=tags, attach_type=media["type"], attach_payload=media["payload"], file_key=media["key"],
            url=media.get("url"), size=media.get("size"), source=source,
        )
        if sent_at is not None:
            m.sent_at = sent_at
        s.add(m)
        await s.flush()
        return m, True


async def visible_materials(user_id: int, teacher_view: bool) -> list[Material]:
    """Учитель видит всю свою полку; родитель — материалы, отправленные в его чаты."""
    from . import chats

    async with session() as s:
        if teacher_view:
            return list((await s.scalars(select(Material).where(Material.teacher_id == user_id))).all())
        chat_ids = [c.chat_id for c in await chats.member_chats(user_id)]
        if not chat_ids:
            return []
        return list((await s.scalars(select(Material).where(Material.chat_id.in_(chat_ids)))).all())


def _doc_text(m: Material) -> str:
    return f"{m.title} {m.filename.rsplit('.', 1)[0].replace('_', ' ')}"


async def find(user_id: int, query: str, teacher_view: bool, limit: int = 5) -> list[search.Hit[Material]]:
    items = await visible_materials(user_id, teacher_view)
    q = query.strip().lstrip("#")
    hits = search.rank(q, items, lambda m: m.tags, _doc_text, limit=limit, min_score=0.25)
    if not hits:
        # подстрока в имени файла — на случай «Памятка_ПБ.pdf»
        ql = search.normalize(q)
        hits = [search.Hit(m, 0.3) for m in items if ql and ql in search.normalize(m.filename + " " + m.title)][:limit]
    return hits


async def chat_titles() -> dict[int, str]:
    async with session() as s:
        return {c.chat_id: c.title for c in (await s.scalars(select(ClassChat))).all()}


async def describe(m: Material, tz: str | None = None) -> str:
    titles = await chat_titles()
    icon = TYPE_ICON.get(m.attach_type, "📄")
    parts = [f"{icon} <b>{html.escape(m.filename or 'Файл')}</b>"]
    if m.title:
        parts.append(html.escape(m.title[:200]))
    meta = []
    if m.chat_id and titles.get(m.chat_id):
        meta.append(f"отправляли в {html.escape(titles[m.chat_id])} {fmt_date(m.sent_at, tz)}")
    else:
        meta.append(f"на полке с {fmt_date(m.created_at, tz)}")
    if m.tags:
        meta.append(" ".join("#" + t.strip() for t in m.tags.split(",") if t.strip()))
    parts.append("<i>" + " · ".join(meta) + "</i>")
    return "\n".join(parts)


async def send_material(m: Material, *, user_id: int | None = None, chat_id: int | None = None,
                        caption: str | None = None) -> bool:
    """Пересылает материал по сохранённому токену (файл должен быть единственным вложением)."""
    att = [{"type": m.attach_type, "payload": m.attach_payload}]
    try:
        await ctx().api.send_message(user_id=user_id, chat_id=chat_id, text=caption, attachments=att)
        return True
    except MaxApiError as e:
        log.warning("Не удалось переслать материал %s: %s", m.id, e)
        return False


async def get(material_id: int) -> Material | None:
    async with session() as s:
        return await s.get(Material, material_id)


async def delete(material_id: int, teacher_id: int) -> bool:
    async with session() as s:
        m = await s.get(Material, material_id)
        if m is None or m.teacher_id != teacher_id:
            return False
        await s.delete(m)
        return True


async def update(material_id: int, teacher_id: int, title: str | None = None, tags: str | None = None) -> Material | None:
    async with session() as s:
        m = await s.get(Material, material_id)
        if m is None or m.teacher_id != teacher_id:
            return None
        if title is not None:
            m.title = title.strip()[:1000]
        if tags is not None:
            m.tags = ", ".join(t.strip().lstrip("#").lower() for t in search.split_keywords(tags))
        return m


async def list_for_teacher(teacher_id: int, query: str = "", limit: int = 100) -> list[Material]:
    if query.strip():
        return [h.item for h in await find(teacher_id, query, teacher_view=True, limit=limit)]
    async with session() as s:
        return list((await s.scalars(
            select(Material).where(Material.teacher_id == teacher_id).order_by(Material.sent_at.desc()).limit(limit)
        )).all())


async def import_history(chat_id: int, teacher_id: int, pages: int = 5) -> int:
    """MAX отдаёт историю сообщений через API — подтягиваем файлы учителя, отправленные раньше."""
    from datetime import datetime, timezone

    added = 0
    before = None
    for _ in range(pages):
        try:
            data = await ctx().api.get_messages(chat_id, count=100, before=before)
        except MaxApiError as e:
            log.info("История чата %s недоступна: %s", chat_id, e)
            break
        msgs = data.get("messages") or []
        if not msgs:
            break
        for msg in msgs:
            sender = (msg.get("sender") or {}).get("user_id")
            if sender != teacher_id:
                continue
            ts = msg.get("timestamp")
            sent_at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).replace(tzinfo=None) if ts else None
            for media in extract_media(msg):
                _, created = await save(teacher_id, media, (msg.get("body") or {}).get("text") or "", chat_id,
                                        source="history", sent_at=sent_at)
                added += int(created)
        oldest = min((m.get("timestamp") or 0) for m in msgs)
        if not oldest or len(msgs) < 100:
            break
        before = oldest - 1
    return added


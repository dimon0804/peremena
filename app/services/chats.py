"""Пользователи, учебные чаты и списки участников («список класса»)."""
from __future__ import annotations

import logging

from sqlalchemy import select, update

from ..core import ctx
from ..db import session, utcnow
from ..max_api import MaxApiError
from ..models import ClassChat, Member, User, display_name

log = logging.getLogger(__name__)


async def upsert_user(u: dict | None, dialog_open: bool | None = None) -> User | None:
    if not u or not u.get("user_id"):
        return None
    async with session() as s:
        user = await s.get(User, u["user_id"])
        if user is None:
            user = User(user_id=u["user_id"], tz=ctx().settings.default_tz)
            s.add(user)
        user.first_name = u.get("first_name") or u.get("name") or user.first_name or ""
        if "last_name" in u:
            user.last_name = u.get("last_name")
        if u.get("username"):
            user.username = u["username"]
        if dialog_open is not None and dialog_open:
            user.dialog_open = True
        await s.flush()
        return user


async def get_user(user_id: int) -> User | None:
    async with session() as s:
        return await s.get(User, user_id)


async def register_chat(chat_id: int, teacher: dict | None) -> ClassChat:
    title = ""
    try:
        info = await ctx().api.get_chat(chat_id)
        title = info.get("title") or ""
    except MaxApiError as e:
        log.info("GET /chats/%s: %s", chat_id, e)
    async with session() as s:
        chat = await s.get(ClassChat, chat_id)
        if chat is None:
            chat = ClassChat(chat_id=chat_id)
            s.add(chat)
        chat.active = True
        if title:
            chat.title = title
        if teacher and teacher.get("user_id") and not chat.teacher_id:
            chat.teacher_id = teacher["user_id"]
        await s.flush()
    await refresh_admin_status(chat_id)
    return chat


async def refresh_admin_status(chat_id: int) -> bool:
    is_admin = False
    try:
        me = await ctx().api.get_my_membership(chat_id)
        is_admin = bool(me.get("is_admin") or me.get("is_owner"))
    except MaxApiError as e:
        log.info("members/me %s: %s", chat_id, e)
    async with session() as s:
        await s.execute(update(ClassChat).where(ClassChat.chat_id == chat_id).values(bot_is_admin=is_admin))
    return is_admin


async def deactivate_chat(chat_id: int) -> None:
    async with session() as s:
        await s.execute(update(ClassChat).where(ClassChat.chat_id == chat_id).values(active=False))


async def set_title(chat_id: int, title: str) -> None:
    async with session() as s:
        await s.execute(update(ClassChat).where(ClassChat.chat_id == chat_id).values(title=title))


async def sync_members(chat_id: int) -> int:
    """Тянет GET /chats/{chatId}/members и обновляет локальный список класса."""
    try:
        raw = await ctx().api.get_members(chat_id)
    except MaxApiError as e:
        log.warning("Не удалось получить участников %s: %s", chat_id, e)
        return -1
    async with session() as s:
        existing = {m.user_id: m for m in (await s.scalars(select(Member).where(Member.chat_id == chat_id))).all()}
        seen = set()
        for r in raw:
            uid = r.get("user_id")
            if not uid:
                continue
            seen.add(uid)
            m = existing.get(uid)
            if m is None:
                m = Member(chat_id=chat_id, user_id=uid)
                s.add(m)
            m.name = display_name(r.get("first_name") or r.get("name"), r.get("last_name"))
            m.is_bot = bool(r.get("is_bot"))
            m.is_admin = bool(r.get("is_admin"))
            m.is_owner = bool(r.get("is_owner"))
            m.present = True
        for uid, m in existing.items():
            if uid not in seen:
                m.present = False
        chat = await s.get(ClassChat, chat_id)
        if chat:
            chat.members_synced_at = utcnow()
    return len(seen)


async def touch_member(chat_id: int, user: dict, present: bool = True) -> None:
    uid = user.get("user_id")
    if not uid:
        return
    async with session() as s:
        m = await s.scalar(select(Member).where(Member.chat_id == chat_id, Member.user_id == uid))
        if m is None:
            m = Member(chat_id=chat_id, user_id=uid)
            s.add(m)
        m.name = display_name(user.get("first_name") or user.get("name"), user.get("last_name"))
        m.is_bot = bool(user.get("is_bot"))
        m.present = present


async def class_list(chat_id: int, exclude: set[int] | None = None) -> list[Member]:
    """Те, от кого ждём ответа: присутствующие люди, кроме учителя и владельца чата."""
    exclude = exclude or set()
    async with session() as s:
        rows = (await s.scalars(
            select(Member).where(Member.chat_id == chat_id, Member.present.is_(True), Member.is_bot.is_(False))
        )).all()
    me = ctx().bot_user_id
    return [m for m in rows if m.user_id not in exclude and m.user_id != me and not m.is_owner]


async def teacher_chats(teacher_id: int) -> list[ClassChat]:
    async with session() as s:
        return list((await s.scalars(
            select(ClassChat).where(ClassChat.teacher_id == teacher_id, ClassChat.active.is_(True))
            .order_by(ClassChat.title)
        )).all())


async def is_teacher(user_id: int) -> bool:
    return bool(await teacher_chats(user_id))


async def member_chats(user_id: int) -> list[ClassChat]:
    """Учебные чаты, где пользователь состоит (для родителя/ученика)."""
    async with session() as s:
        ids = (await s.scalars(
            select(Member.chat_id).where(Member.user_id == user_id, Member.present.is_(True))
        )).all()
        if not ids:
            return []
        return list((await s.scalars(
            select(ClassChat).where(ClassChat.chat_id.in_(ids), ClassChat.active.is_(True))
        )).all())


async def get_chat(chat_id: int) -> ClassChat | None:
    async with session() as s:
        return await s.get(ClassChat, chat_id)


async def member_name(chat_id: int | None, user_id: int, fallback: str = "") -> str:
    async with session() as s:
        if chat_id:
            m = await s.scalar(select(Member).where(Member.chat_id == chat_id, Member.user_id == user_id))
            if m and m.name:
                return m.name
        u = await s.get(User, user_id)
        if u:
            return u.display_name
    return fallback or str(user_id)

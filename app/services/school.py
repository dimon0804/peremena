"""Школа, роли и сборы на параллель.

Роли (один человек может совмещать несколько):
* классный руководитель — добавил бота в чат класса (или владелец чата);
* предметник — приглашён классным руководителем по ссылке, публикует домашку и объявления в класс;
* администратор (завуч) — видит сводку по всем классам школы и запускает сборы сразу в нескольких классах;
* родитель / ученик — участник учебного чата.
"""
from __future__ import annotations

import html
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select

from ..core import ctx
from ..db import session
from ..models import Campaign, ClassChat, Poll, School, SchoolMember, SubjectTeacher, User
from ..timeutil import fmt_dt
from . import chats


def _code(n: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


@dataclass
class Roles:
    class_chats: list[ClassChat] = field(default_factory=list)      # где я классный руководитель
    subject_chats: list[tuple[ClassChat, str]] = field(default_factory=list)  # где я предметник
    school: School | None = None
    school_role: str | None = None                                  # teacher | admin

    @property
    def is_class_teacher(self) -> bool:
        return bool(self.class_chats)

    @property
    def is_subject_teacher(self) -> bool:
        return bool(self.subject_chats)

    @property
    def is_admin(self) -> bool:
        return self.school_role == "admin"

    @property
    def is_staff(self) -> bool:
        return self.is_class_teacher or self.is_subject_teacher or self.is_admin

    def board_chats(self) -> list[tuple[ClassChat, str]]:
        """Чаты, куда пользователь может публиковать на доску: (чат, предмет по умолчанию)."""
        out = [(c, "") for c in self.class_chats]
        seen = {c.chat_id for c in self.class_chats}
        for c, subj in self.subject_chats:
            if c.chat_id not in seen:
                out.append((c, subj))
                seen.add(c.chat_id)
        return out


async def roles(user_id: int) -> Roles:
    r = Roles(class_chats=await chats.teacher_chats(user_id))
    async with session() as s:
        rows = (await s.execute(
            select(ClassChat, SubjectTeacher.subject).join(SubjectTeacher, SubjectTeacher.chat_id == ClassChat.chat_id)
            .where(SubjectTeacher.user_id == user_id, ClassChat.active.is_(True))
        )).all()
        r.subject_chats = [(c, subj) for c, subj in rows]
        m = await s.scalar(select(SchoolMember).where(SchoolMember.user_id == user_id)
                           .order_by(SchoolMember.role))  # admin < teacher по алфавиту — админство важнее
        if m:
            r.school = await s.get(School, m.school_id)
            r.school_role = m.role
    return r


# ---------------------------------------------------------------- школа
async def create_school(user_id: int, name: str) -> School:
    async with session() as s:
        sch = School(name=name.strip()[:300] or "Школа", code=_code(), admin_code=_code(8), created_by=user_id)
        s.add(sch)
        await s.flush()
        await _set_member(s, sch.id, user_id, "admin")
        return sch


async def _set_member(s, school_id: int, user_id: int, role: str) -> None:
    # человек состоит в одной школе: при вступлении в другую старое членство заменяется
    for old in (await s.scalars(select(SchoolMember).where(SchoolMember.user_id == user_id))).all():
        if old.school_id != school_id:
            await s.delete(old)
    m = await s.scalar(select(SchoolMember).where(SchoolMember.school_id == school_id, SchoolMember.user_id == user_id))
    if m is None:
        s.add(SchoolMember(school_id=school_id, user_id=user_id, role=role))
    elif role == "admin":
        m.role = "admin"


async def join_by_code(user_id: int, code: str) -> tuple[School | None, str]:
    code = code.strip().upper()
    async with session() as s:
        sch = await s.scalar(select(School).where(School.code == code))
        role = "teacher"
        if sch is None:
            sch = await s.scalar(select(School).where(School.admin_code == code))
            role = "admin"
        if sch is None:
            return None, ""
        await _set_member(s, sch.id, user_id, role)
        return sch, role


async def members(school_id: int) -> list[SchoolMember]:
    async with session() as s:
        return list((await s.scalars(select(SchoolMember).where(SchoolMember.school_id == school_id))).all())


async def school_chats(school_id: int) -> list[ClassChat]:
    """Все учебные чаты классных руководителей школы."""
    ids = [m.user_id for m in await members(school_id)]
    if not ids:
        return []
    async with session() as s:
        return list((await s.scalars(
            select(ClassChat).where(ClassChat.teacher_id.in_(ids), ClassChat.active.is_(True)).order_by(ClassChat.title)
        )).all())


async def school_of_teacher(user_id: int) -> int | None:
    async with session() as s:
        return await s.scalar(select(SchoolMember.school_id).where(SchoolMember.user_id == user_id))


async def dashboard(school_id: int) -> dict:
    """Сводка для администрации: классы, открытые сборы, где затык."""
    class_chats = await school_chats(school_id)
    chat_ids = [c.chat_id for c in class_chats]
    async with session() as s:
        open_polls = (await s.scalars(
            select(Poll).where(Poll.chat_id.in_(chat_ids), Poll.closed.is_(False)).order_by(Poll.deadline)
        )).all() if chat_ids else []
        teachers = {u.user_id: u for u in (await s.scalars(
            select(User).where(User.user_id.in_([c.teacher_id for c in class_chats if c.teacher_id]))
        )).all()} if class_chats else {}
        campaigns = (await s.scalars(
            select(Campaign).where(Campaign.school_id == school_id).order_by(Campaign.created_at.desc()).limit(10)
        )).all()
    from .polls import counts

    by_chat: dict[int, list] = {}
    for p in open_polls:
        by_chat.setdefault(p.chat_id, []).append(p)
    classes = []
    for c in class_chats:
        ps = by_chat.get(c.chat_id, [])
        answered = sum(counts(p)[0] for p in ps)
        total = sum(counts(p)[1] for p in ps)
        t = teachers.get(c.teacher_id)
        classes.append({
            "chat_id": c.chat_id, "title": c.title, "teacher": t.display_name if t else "",
            "open_polls": len(ps), "answered": answered, "total": total, "bot_is_admin": c.bot_is_admin,
        })
    camp_out = []
    for cmp in campaigns:
        camp_out.append(await campaign_summary(cmp))
    return {"classes": classes, "campaigns": camp_out}


# ---------------------------------------------------------------- предметники
async def subject_invite_code(chat_id: int) -> str:
    """Код приглашения предметника в класс (хранится в KV неделю)."""
    code = _code(8)
    await ctx().kv.set(f"subj:{code}", {"chat_id": chat_id}, 7 * 24 * 3600)
    return code


async def accept_subject_invite(user_id: int, code: str, subject: str = "") -> ClassChat | None:
    data = await ctx().kv.get(f"subj:{code}")
    if not data:
        return None
    chat = await chats.get_chat(data["chat_id"])
    if not chat:
        return None
    async with session() as s:
        exists = await s.scalar(select(SubjectTeacher).where(
            SubjectTeacher.chat_id == chat.chat_id, SubjectTeacher.user_id == user_id))
        if exists is None:
            s.add(SubjectTeacher(chat_id=chat.chat_id, user_id=user_id, subject=subject[:100]))
        elif subject:
            exists.subject = subject[:100]
    return chat


async def set_subject(chat_id: int, user_id: int, subject: str) -> None:
    async with session() as s:
        row = await s.scalar(select(SubjectTeacher).where(
            SubjectTeacher.chat_id == chat_id, SubjectTeacher.user_id == user_id))
        if row:
            row.subject = subject[:100]


async def subject_teachers(chat_id: int) -> list[tuple[SubjectTeacher, User | None]]:
    async with session() as s:
        rows = (await s.scalars(select(SubjectTeacher).where(SubjectTeacher.chat_id == chat_id))).all()
        return [(r, await s.get(User, r.user_id)) for r in rows]


async def remove_subject_teacher(chat_id: int, user_id: int) -> None:
    async with session() as s:
        for r in (await s.scalars(select(SubjectTeacher).where(
                SubjectTeacher.chat_id == chat_id, SubjectTeacher.user_id == user_id))).all():
            await s.delete(r)


# ---------------------------------------------------------------- сборы на параллель
async def launch_campaign(author_id: int, school_id: int, chat_ids: list[int], template: str, text: str,
                          options: list[str], deadline) -> tuple[Campaign, list[dict]]:
    """Один сбор — сразу в нескольких классах. Каждый сбор ведёт свой классный руководитель."""
    from . import polls
    from .quiet import notify_teacher

    allowed = {c.chat_id: c for c in await school_chats(school_id)}
    title = (text.strip().splitlines() or ["Сбор"])[0][:300]
    async with session() as s:
        cmp = Campaign(school_id=school_id, author_id=author_id, title=title)
        s.add(cmp)
        await s.flush()
    author = await chats.get_user(author_id)
    reports = []
    for cid in chat_ids:
        chat = allowed.get(cid)
        if not chat or not chat.teacher_id:
            continue
        p = await polls.create_poll(chat.teacher_id, cid, template, text, options, deadline)
        async with session() as s:
            row = await s.get(Poll, p.id)
            row.campaign_id = cmp.id
        rep = await polls.send_poll(p.id)
        reports.append({"chat_id": cid, "title": chat.title, **rep})
        if chat.teacher_id != author_id:
            await notify_teacher(
                chat.teacher_id,
                f"🏫 Администрация ({html.escape(author.display_name if author else '')}) запустила сбор "
                f"«{html.escape(title)}» в вашем классе {html.escape(chat.title)}. Я веду его сам: "
                f"напомню молчащим и пришлю итог. Статус — /status",
                kind="report", chat_id=cid, raw_text=f"Сбор администрации: «{title}»",
            )
    return cmp, reports


async def campaign_summary(cmp: Campaign) -> dict:
    from .polls import counts

    async with session() as s:
        ps = (await s.scalars(select(Poll).where(Poll.campaign_id == cmp.id))).all()
        titles = {c.chat_id: c.title for c in (await s.scalars(
            select(ClassChat).where(ClassChat.chat_id.in_([p.chat_id for p in ps]))
        )).all()} if ps else {}
    rows = []
    for p in ps:
        a, t, per = counts(p)
        rows.append({"poll_id": p.id, "chat_id": p.chat_id, "title": titles.get(p.chat_id, ""), "answered": a,
                     "total": t, "per_option": per, "closed": p.closed,
                     "missing": [x.name for x in p.targets if x.answer is None]})
    answered = sum(r["answered"] for r in rows)
    total = sum(r["total"] for r in rows)
    options = ps[0].options if ps else []
    per_total = [sum(r["per_option"][i] for r in rows if i < len(r["per_option"])) for i in range(len(options))]
    return {"id": cmp.id, "title": cmp.title, "answered": answered, "total": total, "options": options,
            "per_option": per_total, "classes": rows,
            "deadline_text": fmt_dt(ps[0].deadline, None) if ps else ""}


async def render_dashboard_text(school: School) -> str:
    d = await dashboard(school.id)
    lines = [f"🏫 <b>{html.escape(school.name)}</b> — сводка", ""]
    if not d["classes"]:
        lines.append("Пока ни один классный руководитель не подключил бота. Отправьте учителям код школы.")
    for c in d["classes"]:
        status = f"{c['answered']}/{c['total']} по {c['open_polls']} сбор." if c["open_polls"] else "открытых сборов нет"
        warn = "" if c["bot_is_admin"] else " ⚠️"
        lines.append(f"• <b>{html.escape(c['title'] or str(c['chat_id']))}</b>{warn} ({html.escape(c['teacher'])}) — {status}")
    if d["campaigns"]:
        lines += ["", "<b>Сборы на параллель</b>"]
        for cmp in d["campaigns"][:5]:
            lines.append(f"• {html.escape(cmp['title'])} — {cmp['answered']}/{cmp['total']}")
            stuck = [r for r in cmp["classes"] if r["total"] and r["answered"] < r["total"]]
            for r in sorted(stuck, key=lambda r: r["answered"] / r["total"])[:3]:
                lines.append(f"   ↳ {html.escape(r['title'])}: {r['answered']}/{r['total']}")
    return "\n".join(lines)

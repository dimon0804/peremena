"""Засев базы для нагрузки: школы, классы, учителя, родители, (по желанию) открытые сборы.

    python loadtest/seed.py --db postgresql://pm:pm@127.0.0.1:55433/pm --classes 1700 --polls --reset

Схема строится миграциями (как при старте бота), данные — пакетными INSERT. Состав — loadtest/roster.py.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import roster  # noqa: E402


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--project", default=str(HERE.parent), help="корень проекта (для замера старой версии)")
    p.add_argument("--classes", type=int, default=10)
    p.add_argument("--polls", action="store_true", help="открытый сбор в каждом классе (для нажатий кнопок)")
    p.add_argument("--reset", action="store_true", help="DROP SCHEMA public CASCADE перед засевом")
    a = p.parse_args()
    sys.path.insert(0, a.project)

    from sqlalchemy import insert, text

    from app import db
    from app.models import ClassChat, Member, Poll, PollTarget, School, SchoolMember, User

    url = db._normalize_url(a.db)
    if a.reset:
        from sqlalchemy.ext.asyncio import create_async_engine

        e = create_async_engine(url)
        async with e.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await e.dispose()
    await db.init_db(url)
    t0 = time.time()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = a.classes
    schools = (n + roster.CLASSES_PER_SCHOOL - 1) // roster.CLASSES_PER_SCHOOL

    def chunks(rows, size=5000):
        for i in range(0, len(rows), size):
            yield rows[i:i + size]

    users = []
    for i in range(n):
        users.append({"user_id": roster.teacher_id(i), "first_name": "Учитель", "last_name": f"№{i}",
                      "dialog_open": True, "created_at": now, "tz": "Europe/Moscow", "work_start": 0,
                      "work_end": 24 * 60, "weekends_off": False, "quiet_enabled": False,
                      "urgent_words": "срочно", "suggest_save": False})
        for j in range(roster.PARENTS):
            users.append({"user_id": roster.parent_id(i, j), "first_name": "Родитель", "last_name": f"{i}-{j}",
                          "dialog_open": True, "created_at": now, "tz": "Europe/Moscow", "work_start": 480,
                          "work_end": 1140, "weekends_off": True, "quiet_enabled": True,
                          "urgent_words": "срочно", "suggest_save": True})
    for s in range(schools):
        users.append({"user_id": roster.admin_id(s), "first_name": "Завуч", "last_name": f"№{s}", "dialog_open": True,
                      "created_at": now, "tz": "Europe/Moscow", "work_start": 480, "work_end": 1140,
                      "weekends_off": True, "quiet_enabled": True, "urgent_words": "срочно", "suggest_save": True})
    chats = [{"chat_id": roster.chat_id(i), "title": roster.chat_title(roster.chat_id(i)),
              "teacher_id": roster.teacher_id(i), "active": True, "bot_is_admin": True, "autoanswer": False,
              "added_at": now, "members_synced_at": now} for i in range(n)]
    members = []
    for i in range(n):
        cid = roster.chat_id(i)
        for m in roster.members(cid):
            members.append({"chat_id": cid, "user_id": m["user_id"],
                            "name": f"{m.get('last_name', '')} {m['first_name'][:1]}.".strip(),
                            "is_bot": m["is_bot"], "is_admin": m["is_admin"], "is_owner": m["is_owner"],
                            "present": True})
    school_rows = [{"id": s + 1, "name": f"Школа №{s + 1}", "code": f"S{s:06d}", "admin_code": f"A{s:07d}",
                    "created_by": roster.admin_id(s), "created_at": now} for s in range(schools)]
    sm = [{"school_id": s + 1, "user_id": roster.admin_id(s), "role": "admin", "joined_at": now} for s in range(schools)]
    sm += [{"school_id": i // roster.CLASSES_PER_SCHOOL + 1, "user_id": roster.teacher_id(i), "role": "teacher",
            "joined_at": now} for i in range(n)]

    async with db.session() as s:
        for rows, model in ((users, User), (chats, ClassChat), (members, Member), (school_rows, School),
                            (sm, SchoolMember)):
            for part in chunks(rows):
                await s.execute(insert(model), part)
        if db.dialect() == "postgresql":
            await s.execute(text("SELECT setval(pg_get_serial_sequence('schools', 'id'), :v)"), {"v": schools + 1})
    polls = 0
    if a.polls:
        async with db.session() as s:
            prow = [{"teacher_id": roster.teacher_id(i), "chat_id": roster.chat_id(i), "template": "consent",
                     "title": "Экскурсия", "text": "Экскурсия в музей в субботу", "options": ["Да", "Нет", "Уточнить"],
                     "clarify_option": 2, "deadline": now + timedelta(days=3), "created_at": now, "closed": False,
                     "reminded_24h": False, "reminded_2h": False, "dispatched": True} for i in range(n)]
            for part in chunks(prow, 2000):
                await s.execute(insert(Poll), part)
        async with db.session() as s:
            from sqlalchemy import select

            ids = {cid: pid for pid, cid in (await s.execute(select(Poll.id, Poll.chat_id))).all()}
            targets = [{"poll_id": ids[roster.chat_id(i)], "user_id": roster.parent_id(i, j), "name": f"{i}-{j}",
                        "delivered": True, "reminders": 0, "paid": False}
                       for i in range(n) for j in range(roster.PARENTS)]
            for part in chunks(targets):
                await s.execute(insert(PollTarget), part)
        polls = n
    if db.dialect() == "postgresql":
        async with db.session() as s:
            await s.execute(text("ANALYZE"))
    await db.close_db()
    print(f"засеяно: классов {n}, школ {schools}, пользователей {len(users)}, участников {len(members)}, "
          f"сборов {polls} — {time.time() - t0:.1f} с")


if __name__ == "__main__":
    asyncio.run(main())

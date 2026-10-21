"""Демо-данные для показа мини-приложения без MAX: python scripts/seed_demo.py

Создаёт школу, два класса, сборы, справочник, доску и полку. Затем запустите сервис с DEV_AUTH=true
и откройте http://localhost:8080/app/?dev_user=100 (классный руководитель и завуч),
?dev_user=120 (предметник) или ?dev_user=201 (родитель).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    Announcement, ClassChat, FaqEntry, Material, Member, Metric, Poll, PollTarget, School, SchoolMember,
    SubjectTeacher, User,
)
from app.timeutil import utcnow  # noqa: E402

PARENTS_9B = ["Иванова А.", "Петрова М.", "Сидоров О.", "Козлова И.", "Морозов Д.", "Волкова Е.", "Соколов П.",
              "Лебедева Н.", "Новиков С.", "Фёдорова Т.", "Михайлов К.", "Егорова В."]
PARENTS_9A = ["Орлов Г.", "Зайцева Л.", "Павлов Р.", "Семёнова Ю.", "Голубев А.", "Виноградова Д."]


async def main() -> None:
    s = get_settings()
    await db.init_db(s.database_url)
    now = utcnow()
    async with db.session() as ss:
        if await ss.get(User, 100):
            print("Демо-данные уже есть")
            return
        ss.add_all([
            User(user_id=100, first_name="Мария", last_name="Петрова", quiet_enabled=True),
            User(user_id=110, first_name="Игорь", last_name="Смирнов"),
            User(user_id=120, first_name="Елена", last_name="Орлова"),
            User(user_id=201, first_name="Анна", last_name="Иванова"),
        ])
        ss.add_all([
            ClassChat(chat_id=-5000, title="9Б · Родители", teacher_id=100, bot_is_admin=True, autoanswer=True),
            ClassChat(chat_id=-6000, title="9А · Родители", teacher_id=110, bot_is_admin=True),
        ])
        for i, name in enumerate(PARENTS_9B):
            ss.add(Member(chat_id=-5000, user_id=201 + i, name=name))
        for i, name in enumerate(PARENTS_9A):
            ss.add(Member(chat_id=-6000, user_id=301 + i, name=name))
        sch = School(name="МБОУ СОШ № 7", code="K7QX2M", admin_code="ADM7Z9QW", created_by=100)
        ss.add(sch)
        await ss.flush()
        ss.add_all([SchoolMember(school_id=sch.id, user_id=100, role="admin"),
                    SchoolMember(school_id=sch.id, user_id=110, role="teacher"),
                    SubjectTeacher(chat_id=-5000, user_id=120, subject="Английский язык")])

        p1 = Poll(teacher_id=100, chat_id=-5000, template="consent", title="Экскурсия в музей, 28 октября, 300 ₽.",
                  text="Экскурсия в музей, 28 октября, 300 ₽.\nНужно согласие до пятницы.",
                  options=["Согласен", "Не согласен", "Нужно уточнить"], clarify_option=2,
                  deadline=now + timedelta(days=2), created_at=now - timedelta(days=1))
        p2 = Poll(teacher_id=100, chat_id=-5000, template="ack", title="Правила поведения на каникулах",
                  text="Правила поведения на каникулах", options=["Ознакомлен(а)", "Есть вопрос"], clarify_option=1,
                  closed=True, closed_at=now - timedelta(days=3), created_at=now - timedelta(days=6))
        ss.add_all([p1, p2])
        await ss.flush()
        for i, name in enumerate(PARENTS_9B):
            ans = [0, 0, 0, 1, 0, 2, 0, 0, None, 0, None, 0][i]
            ss.add(PollTarget(poll_id=p1.id, user_id=201 + i, name=name, delivered=True, answer=ans,
                              answered_at=now if ans is not None else None))
            ss.add(PollTarget(poll_id=p2.id, user_id=201 + i, name=name, delivered=True, answer=0, answered_at=now))

        ss.add_all([
            FaqEntry(teacher_id=100, question="Во сколько родительское собрание?", keywords="собрание, родительское",
                     answer="В четверг в 18:30, кабинет 214.", hits=14),
            FaqEntry(teacher_id=100, question="Какая форма завтра?", keywords="форма, одежда",
                     answer="Парадная: белый верх, тёмный низ.", hits=9, misses=1),
            FaqEntry(teacher_id=100, school_id=sch.id, question="Расписание звонков", keywords="звонки, расписание",
                     answer="1 урок 8:30–9:15, 2 урок 9:25–10:10, 3 урок 10:30–11:15.", hits=22),
        ])
        tomorrow = (now + timedelta(days=1)).date().isoformat()
        ss.add_all([
            Announcement(chat_id=-5000, author_id=100, kind="hw", subject="Алгебра", text="№245, 247; правило на с. 63",
                         for_date=tomorrow, published=True),
            Announcement(chat_id=-5000, author_id=120, kind="hw", subject="Английский язык", text="Unit 5, ex. 3, 4",
                         for_date=tomorrow, publish_mode="silent", published=True),
            Announcement(chat_id=-5000, author_id=100, kind="news", text="В субботу субботник, форма свободная.",
                         publish_mode="later", publish_at=now + timedelta(hours=10)),
        ])
        ss.add(Material(teacher_id=100, chat_id=-5000, title="Памятка по пожарной безопасности",
                        filename="Памятка_ПБ.pdf", tags="безопасность", attach_type="file",
                        attach_payload={"token": "demo"}, file_key="demo1", source="chat"))
        for kind, n in (("faq_answer", 31), ("poll_done", 2), ("reminder", 14), ("material_found", 5), ("quiet_held", 9)):
            from app.services.stats import MINUTES

            ss.add_all(Metric(teacher_id=100, kind=kind, minutes=MINUTES[kind], created_at=now - timedelta(days=1))
                       for _ in range(n))
    await db.close_db()
    print("Демо-данные созданы")


if __name__ == "__main__":
    asyncio.run(main())

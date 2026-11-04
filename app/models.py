"""«Память класса» — всё, что бот хранит.

Хранится только то, что учитель явно сохранил, и служебные отметки о том, кто ответил на сбор.
Переписка родителей не сохраняется (кроме вопросов, пришедших вне рабочего окна, —
они лежат во «входящих» до утренней сводки и помечаются доставленными).
Сроки хранения и «удалить мои данные» (/forget) — app/services/retention.py.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false, true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_name: Mapped[str] = mapped_column(String(200), default="")
    last_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Отвечал ли пользователь боту в личке (значит, бот точно может ему писать)
    dialog_open: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Настройки учителя («Тихий режим»)
    tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    work_start: Mapped[int] = mapped_column(Integer, default=8 * 60)   # минуты от полуночи
    work_end: Mapped[int] = mapped_column(Integer, default=19 * 60)
    weekends_off: Mapped[bool] = mapped_column(Boolean, default=True)
    quiet_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    urgent_words: Mapped[str] = mapped_column(String(300), default="срочно")
    suggest_save: Mapped[bool] = mapped_column(Boolean, default=True)
    last_digest_on: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD

    @property
    def display_name(self) -> str:
        return display_name(self.first_name, self.last_name)


def display_name(first: str | None, last: str | None) -> str:
    first = (first or "").strip()
    last = (last or "").strip()
    if last and first:
        return f"{last} {first[0]}."
    return last or first or "Без имени"


class ClassChat(Base):
    """Учебный чат, куда добавлен бот (чат с родителями / учениками)."""
    __tablename__ = "chats"
    # «классы учителя» и «кто классный руководитель» — на каждом событии и в утреннем отборе
    __table_args__ = (Index("ix_chats_teacher_active", "teacher_id", "active"),)

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(300), default="")
    teacher_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    bot_is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    autoanswer: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    members_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id"),
        Index("ix_members_user_present", "user_id", "present"),  # классы родителя: «Мои сборы», вопросы
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    # имя ребёнка, за которого отвечает этот родитель (спрашиваем один раз — для списка «кто не придёт»)
    child_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


# ---------------------------------------------------------------- «Перекличка»
class Poll(Base):
    __tablename__ = "polls"
    __table_args__ = (
        Index("ix_polls_open_deadline", "closed", "deadline"),        # tick: срок и напоминания
        Index("ix_polls_chat_open", "chat_id", "closed"),              # открытые сборы класса
        Index("ix_polls_teacher_open", "teacher_id", "closed", "created_at"),  # список сборов учителя
        Index("ix_polls_dispatch", "dispatched", "closed"),            # досылка прерванных рассылок
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    template: Mapped[str] = mapped_column(String(32), default="consent")
    title: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON)            # ["Согласен", "Не согласен", ...]
    clarify_option: Mapped[int | None] = mapped_column(Integer, nullable=True)  # индекс «Нужно уточнить»
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminded_24h: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_2h: Mapped[bool] = mapped_column(Boolean, default=False)
    group_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # сбор на параллель
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)  # сбор денег: рублей с каждого
    # рассылка доведена до конца; False — ещё не начата (отложена до утра) или прервана перезапуском
    dispatched: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC: отложенная рассылка

    targets: Mapped[list["PollTarget"]] = relationship(
        back_populates="poll", cascade="all, delete-orphan", lazy="selectin", order_by="PollTarget.name"
    )


class PollTarget(Base):
    __tablename__ = "poll_targets"
    __table_args__ = (
        UniqueConstraint("poll_id", "user_id"),
        Index("ix_poll_targets_user_answer", "user_id", "answer"),  # сборы, ждущие ответа родителя
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    answer: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminders: Mapped[int] = mapped_column(Integer, default=0)
    # сбор денег: учитель подтвердил, что деньги получены (родитель лишь отмечает «перевёл»)
    paid: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    poll: Mapped[Poll] = relationship(back_populates="targets")


# ---------------------------------------------------------------- «Справочник»
class FaqEntry(Base):
    __tablename__ = "faq"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # None — для всех чатов
    school_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # общая база школы
    question: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    misses: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- «Полка»
class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (Index("ix_materials_teacher_token", "teacher_id", "file_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, default="")          # подпись учителя
    filename: Mapped[str] = mapped_column(String(500), default="")
    tags: Mapped[str] = mapped_column(String(500), default="")
    attach_type: Mapped[str] = mapped_column(String(20), default="file")  # file | image | video | audio
    attach_payload: Mapped[dict] = mapped_column(JSON)             # payload для повторной отправки
    file_key: Mapped[str] = mapped_column(String(200), default="")  # для дедупликации
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | chat | history | upload
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # когда файл появился в чате
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- «Тихий режим»
class InboxItem(Base):
    """То, что пришло учителю вне рабочего окна и ждёт утренней сводки."""
    __tablename__ = "inbox"
    __table_args__ = (Index("ix_inbox_teacher_pending", "teacher_id", "delivered", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    from_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    from_name: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(30), default="message")  # question | clarify | not_that | chat_question | report
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # ключ переданного вопроса (qq:<ref> в KV) — чтобы ответить родителю прямо из утренней сводки
    ref: Mapped[str | None] = mapped_column(String(32), nullable=True)


# ---------------------------------------------------------------- «Счётчик пользы»
class Metric(Base):
    __tablename__ = "metrics"
    __table_args__ = (Index("ix_metrics_teacher_created", "teacher_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    minutes: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- Школа и роли
class School(Base):
    """Образовательная организация: общая база ответов, сводка и сборы на параллель для завуча."""
    __tablename__ = "schools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # код приглашения учителей
    admin_code: Mapped[str] = mapped_column(String(32), unique=True)       # код приглашения администрации
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SchoolMember(Base):
    __tablename__ = "school_members"
    __table_args__ = (UniqueConstraint("school_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    school_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(20), default="teacher")  # teacher | admin
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SubjectTeacher(Base):
    """Учитель-предметник, приглашённый классным руководителем в класс (без прав администратора чата)."""
    __tablename__ = "subject_teachers"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", "subject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    subject: Mapped[str] = mapped_column(String(100), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Campaign(Base):
    """Сбор, запущенный администрацией сразу в нескольких классах."""
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    school_id: Mapped[int] = mapped_column(Integer, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- «Доска класса»
class Announcement(Base):
    """Домашнее задание или объявление. Бот отвечает по нему на «что задали?» и публикует в чат по расписанию."""
    __tablename__ = "announcements"
    __table_args__ = (
        Index("ix_announcements_due", "published", "publish_at"),       # отложенные публикации (tick)
        Index("ix_announcements_chat_created", "chat_id", "created_at"),  # «что задали?» по классу
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(10), default="hw")  # hw | news
    subject: Mapped[str] = mapped_column(String(100), default="")
    text: Mapped[str] = mapped_column(Text)
    for_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD — к какому дню
    publish_mode: Mapped[str] = mapped_column(String(10), default="now")    # now | later | silent
    publish_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- Сайт
class WebSession(Base):
    """Сессия веб-кабинета. Вход подтверждается в боте — паролей нет.
    В базе хранится только хеш токена: утечка таблицы не даёт доступа к кабинетам."""
    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    user_agent: Mapped[str] = mapped_column(String(300), default="")


# ---------------------------------------------------------------- Служебное
class KVEntry(Base):
    """Короткоживущее состояние (шаги диалогов, приглашения, коды входа), если Redis не подключён.
    В памяти процесса оно терялось при каждом перезапуске — вместе с «ссылкой на 7 дней»."""
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# ---------------------------------------------------------------- «Кто сегодня не придёт»
class Absence(Base):
    """Родитель отметил, что ребёнок не придёт. Учитель получает список утром, без сообщений в общем чате."""
    __tablename__ = "absences"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", "day", "child_name"),
        Index("ix_absences_chat_day", "chat_id", "day"),  # «кто сегодня не придёт» по классу
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)      # кто отметил (родитель)
    child_name: Mapped[str] = mapped_column(String(200), default="")
    day: Mapped[str] = mapped_column(String(10), index=True)          # YYYY-MM-DD
    reason: Mapped[str] = mapped_column(String(20), default="sick")   # sick | family | other
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- Справочник, который подсказывает сам
class OpenQuestion(Base):
    """Вопрос, на который в справочнике не нашлось ответа. Похожие вопросы группируются,
    и бот предлагает учителю сохранить ответ на самый частый."""
    __tablename__ = "open_questions"
    __table_args__ = (Index("ix_open_questions_teacher_open", "teacher_id", "resolved", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    stems: Mapped[str] = mapped_column(Text, default="")  # основы слов через пробел — для группировки
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- Демо-режим для жюри
class DemoSandbox(Base):
    """Песочница демо-режима: вымышленные класс, родители и данные; сообщения никуда не уходят."""
    __tablename__ = "demo_sandboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    parent_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- Интеграции с внешними системами
class IntegrationToken(Base):
    """Секрет для внешней системы: лента календаря (ical) или ключ открытого API (api).
    В базе — только SHA-256 токена. Ссылку на календарь можно показать снова: токен выводится из salt
    и ключа подписей сервиса (без ключа утечка таблицы ничего не даёт). Ключ API показывается один раз."""
    __tablename__ = "integration_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(10), default="api")        # ical | api
    name: Mapped[str] = mapped_column(String(100), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16), default="")         # начало ключа — узнать его в списке
    salt: Mapped[str | None] = mapped_column(String(64), nullable=True)  # ical: из него выводится ссылка
    can_write: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # api: создавать сборы
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Webhook(Base):
    """Исходящий вебхук: «Перемена» сама сообщает внешней системе о событиях (ответ на сбор, отсутствие…).
    Тело подписывается HMAC-SHA256; секрет выводится из salt и ключа подписей сервиса."""
    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    url: Mapped[str] = mapped_column(Text)
    events: Mapped[str] = mapped_column(String(300), default="")  # через запятую
    salt: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # неудач подряд
    last_status: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookDelivery(Base):
    """Журнал доставок (последние 30 на вебхук, не старше 14 дней) — видно, что и когда ушло."""
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    webhook_id: Mapped[int] = mapped_column(Integer, index=True)
    event: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | error | demo
    code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(String(300), default="")
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

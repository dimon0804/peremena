"""«Память класса» — всё, что бот хранит.

Хранится только то, что учитель явно сохранил, и служебные отметки о том, кто ответил на сбор.
Переписка родителей не сохраняется (кроме вопросов, пришедших вне рабочего окна, —
они лежат во «входящих» до утренней сводки и помечаются доставленными).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index,
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
    __table_args__ = (UniqueConstraint("chat_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    present: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------- «Перекличка»
class Poll(Base):
    __tablename__ = "polls"

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

    targets: Mapped[list["PollTarget"]] = relationship(
        back_populates="poll", cascade="all, delete-orphan", lazy="selectin", order_by="PollTarget.name"
    )


class PollTarget(Base):
    __tablename__ = "poll_targets"
    __table_args__ = (UniqueConstraint("poll_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    answer: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminders: Mapped[int] = mapped_column(Integer, default=0)

    poll: Mapped[Poll] = relationship(back_populates="targets")


# ---------------------------------------------------------------- «Справочник»
class FaqEntry(Base):
    __tablename__ = "faq"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # None — для всех чатов
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    from_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    from_name: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(30), default="message")  # question | clarify | not_that | chat_question | report
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ---------------------------------------------------------------- «Счётчик пользы»
class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    minutes: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

"""индексы под горячие запросы (масштаб «все школы региона»)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04 18:00:00

Составные индексы под запросы, которые идут на каждом событии или каждую минуту:
tick сборов и досылка, ответы на сбор, «Мои сборы» родителя, утренняя сводка, входящие,
отложенные публикации доски, «кто сегодня не придёт», статистика.
"""
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_chats_teacher_active", "chats", ["teacher_id", "active"]),
    ("ix_members_user_present", "members", ["user_id", "present"]),
    ("ix_polls_open_deadline", "polls", ["closed", "deadline"]),
    ("ix_polls_chat_open", "polls", ["chat_id", "closed"]),
    ("ix_polls_teacher_open", "polls", ["teacher_id", "closed", "created_at"]),
    ("ix_polls_dispatch", "polls", ["dispatched", "closed"]),
    ("ix_poll_targets_user_answer", "poll_targets", ["user_id", "answer"]),
    ("ix_inbox_teacher_pending", "inbox", ["teacher_id", "delivered", "created_at"]),
    ("ix_metrics_teacher_created", "metrics", ["teacher_id", "created_at"]),
    ("ix_announcements_due", "announcements", ["published", "publish_at"]),
    ("ix_announcements_chat_created", "announcements", ["chat_id", "created_at"]),
    ("ix_absences_chat_day", "absences", ["chat_id", "day"]),
    ("ix_open_questions_teacher_open", "open_questions", ["teacher_id", "resolved", "created_at"]),
]


def upgrade() -> None:
    for name, table, cols in INDEXES:
        op.create_index(name, table, cols, unique=False)


def downgrade() -> None:
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)

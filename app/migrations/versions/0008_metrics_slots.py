"""метрики пилота (время ответа, активные дни учителя) и запись на встречу по слотам

Revision ID: 0008
Revises: 0006
Create Date: 2026-11-04 22:00:00

* question_timings — когда родитель спросил и когда пришёл первый ответ (без текста переписки);
* teacher_activity — одна строка на учителя в день, когда он что-то делал (для удержания D7/D30);
* meetings, meeting_slots — консультации по слотам; занятость слота защищена уникальностью в базе.
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'question_timings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('teacher_id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=True),
        sa.Column('channel', sa.String(length=10), nullable=False),
        sa.Column('asked_at', sa.DateTime(), nullable=False),
        sa.Column('answered_at', sa.DateTime(), nullable=True),
        sa.Column('answered_by', sa.String(length=10), nullable=True),
        sa.Column('forwarded', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('demo', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_question_timings_teacher_id', 'question_timings', ['teacher_id'])
    op.create_index('ix_question_timings_asked_at', 'question_timings', ['asked_at'])
    op.create_index('ix_question_timings_teacher_asked', 'question_timings', ['teacher_id', 'asked_at'])

    op.create_table(
        'teacher_activity',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('demo', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'day'),
    )
    op.create_index('ix_teacher_activity_user_id', 'teacher_activity', ['user_id'])
    op.create_index('ix_teacher_activity_day', 'teacher_activity', ['day'])

    op.create_table(
        'meetings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('teacher_id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('format', sa.String(length=10), nullable=False),
        sa.Column('place', sa.String(length=300), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('closed', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meetings_teacher_id', 'meetings', ['teacher_id'])
    op.create_index('ix_meetings_chat_id', 'meetings', ['chat_id'])
    op.create_index('ix_meetings_chat_closed', 'meetings', ['chat_id', 'closed'])

    op.create_table(
        'meeting_slots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('starts_at', sa.DateTime(), nullable=False),
        sa.Column('minutes', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('name', sa.String(length=300), nullable=False),
        sa.Column('booked_at', sa.DateTime(), nullable=True),
        sa.Column('reminded', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meeting_id', 'user_id'),
    )
    op.create_index('ix_meeting_slots_meeting_id', 'meeting_slots', ['meeting_id'])
    op.create_index('ix_meeting_slots_user_id', 'meeting_slots', ['user_id'])
    op.create_index('ix_meeting_slots_remind', 'meeting_slots', ['reminded', 'starts_at'])


def downgrade() -> None:
    op.drop_table('meeting_slots')
    op.drop_table('meetings')
    op.drop_table('teacher_activity')
    op.drop_table('question_timings')

"""journals: расписание, список класса, подключение к «Дневник.ру», источник записи доски

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04 21:00:00

Импорт выгрузок электронного журнала: уроки (lessons), список класса для сверки с чатом (roster),
подключение класса к журналу по API (journal_links). У записей доски появляется source — чтобы
повторный импорт обновлял свои записи и не плодил дубли.
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('announcements', sa.Column('source', sa.String(length=20), server_default='manual',
                                             nullable=False))
    op.create_table(
        'lessons',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=True),
        sa.Column('weekday', sa.Integer(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('subject', sa.String(length=100), nullable=False),
        sa.Column('time', sa.String(length=20), nullable=False),
        sa.Column('room', sa.String(length=30), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lessons_chat_day', 'lessons', ['chat_id', 'day'])
    op.create_index('ix_lessons_chat_weekday', 'lessons', ['chat_id', 'weekday'])
    op.create_table(
        'roster',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('parents', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', 'name'),
    )
    op.create_index('ix_roster_chat_id', 'roster', ['chat_id'])
    op.create_table(
        'journal_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('owner_id', sa.BigInteger(), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('school_ref', sa.String(length=40), nullable=False),
        sa.Column('group_ref', sa.String(length=40), nullable=False),
        sa.Column('token_enc', sa.Text(), nullable=True),
        sa.Column('last_sync_at', sa.DateTime(), nullable=True),
        sa.Column('last_status', sa.String(length=300), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', 'provider'),
    )
    op.create_index('ix_journal_links_chat_id', 'journal_links', ['chat_id'])
    op.create_index('ix_journal_links_owner_id', 'journal_links', ['owner_id'])


def downgrade() -> None:
    op.drop_index('ix_journal_links_owner_id', table_name='journal_links')
    op.drop_index('ix_journal_links_chat_id', table_name='journal_links')
    op.drop_table('journal_links')
    op.drop_index('ix_roster_chat_id', table_name='roster')
    op.drop_table('roster')
    op.drop_index('ix_lessons_chat_weekday', table_name='lessons')
    op.drop_index('ix_lessons_chat_day', table_name='lessons')
    op.drop_table('lessons')
    with op.batch_alter_table('announcements') as b:
        b.drop_column('source')

"""регион и округ, сборы сверху, открытая библиотека шаблонов

Revision ID: 0009
Revises: 0007
Create Date: 2026-11-04 23:00:00

Уровень выше школы: регион (и округ внутри него), методисты, сбор от управления, который
раскладывается в школьные и классные сборы. Приписка классного руководителя к сбору сверху.
Общая библиотека шаблонов сборов и карточек справочника: счётчик «взяли», жалобы, скрытие.
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'regions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=300), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('staff_code', sa.String(length=32), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('staff_code'),
    )
    op.create_index('ix_regions_code', 'regions', ['code'], unique=True)
    op.create_index('ix_regions_parent_id', 'regions', ['parent_id'])

    op.create_table(
        'region_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('region_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('joined_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('region_id', 'user_id'),
    )
    op.create_index('ix_region_members_region_id', 'region_members', ['region_id'])
    op.create_index('ix_region_members_user_id', 'region_members', ['user_id'])

    op.create_table(
        'region_campaigns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('region_id', sa.Integer(), nullable=False),
        sa.Column('author_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('template', sa.String(length=32), nullable=False),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('deadline', sa.DateTime(), nullable=True),
        sa.Column('school_ids', sa.JSON(), nullable=False),
        sa.Column('grades', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_region_campaigns_region_id', 'region_campaigns', ['region_id'])

    op.create_table(
        'library_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('author_id', sa.BigInteger(), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('template', sa.String(length=32), nullable=False),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('keywords', sa.String(length=500), nullable=False),
        sa.Column('topic', sa.String(length=30), nullable=False),
        sa.Column('grades', sa.String(length=20), nullable=False),
        sa.Column('official', sa.Boolean(), nullable=False),
        sa.Column('demo', sa.Boolean(), nullable=False),
        sa.Column('uses', sa.Integer(), nullable=False),
        sa.Column('reports', sa.Integer(), nullable=False),
        sa.Column('hidden', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_library_items_author_id', 'library_items', ['author_id'])
    op.create_index('ix_library_items_visible', 'library_items', ['hidden', 'kind', 'uses'])

    op.create_table(
        'library_reports',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.String(length=300), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('item_id', 'user_id'),
    )
    op.create_index('ix_library_reports_item_id', 'library_reports', ['item_id'])

    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.add_column(sa.Column('region_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_schools_region_id', ['region_id'])
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('region_campaign_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_campaigns_region_campaign_id', ['region_campaign_id'])
    with op.batch_alter_table('polls', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('note', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('polls', schema=None) as batch_op:
        batch_op.drop_column('note')
        batch_op.drop_column('origin')
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.drop_index('ix_campaigns_region_campaign_id')
        batch_op.drop_column('region_campaign_id')
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.drop_index('ix_schools_region_id')
        batch_op.drop_column('region_id')
    op.drop_index('ix_library_reports_item_id', table_name='library_reports')
    op.drop_table('library_reports')
    op.drop_index('ix_library_items_visible', table_name='library_items')
    op.drop_index('ix_library_items_author_id', table_name='library_items')
    op.drop_table('library_items')
    op.drop_index('ix_region_campaigns_region_id', table_name='region_campaigns')
    op.drop_table('region_campaigns')
    op.drop_index('ix_region_members_user_id', table_name='region_members')
    op.drop_index('ix_region_members_region_id', table_name='region_members')
    op.drop_table('region_members')
    op.drop_index('ix_regions_parent_id', table_name='regions')
    op.drop_index('ix_regions_code', table_name='regions')
    op.drop_table('regions')

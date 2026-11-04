"""inbox question ref

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04 13:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('inbox', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ref', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('inbox', schema=None) as batch_op:
        batch_op.drop_column('ref')

"""poll dispatch state

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # уже существующие сборы считаем разосланными: возобновлять их рассылку после обновления не нужно
    with op.batch_alter_table('polls', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dispatched', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(sa.Column('send_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('polls', schema=None) as batch_op:
        batch_op.drop_column('send_at')
        batch_op.drop_column('dispatched')

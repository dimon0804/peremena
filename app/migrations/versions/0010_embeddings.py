"""векторы карточек справочника для семантического поиска (опционально)

Revision ID: 0010
Revises: 0006
Create Date: 2026-11-04 21:00:00

Таблица faq_embeddings: вектор карточки байтами (float32) — работает везде, включая SQLite.
На PostgreSQL с доступным расширением pgvector — ещё столбец embedding vector(312) и индекс HNSW
(vector_cosine_ops). Без расширения (обычный образ postgres:16) и на SQLite этот шаг пропускается,
ничего не ломая: поиск тогда считает косинус в процессе. Если базу позже перевели на образ с pgvector,
столбец и индекс добавятся при старте с EMBEDDINGS=on (app/services/semantic.py: ensure_pgvector).
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'faq_embeddings',
        sa.Column('faq_id', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('text_hash', sa.String(length=40), nullable=False),
        sa.Column('dim', sa.Integer(), nullable=False),
        sa.Column('vec', sa.LargeBinary(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['faq_id'], ['faq.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('faq_id'),
    )
    from app.services.semantic import ensure_pgvector

    ensure_pgvector(op.get_bind())


def downgrade() -> None:
    op.drop_table('faq_embeddings')  # столбец vector и индекс HNSW уходят вместе с таблицей

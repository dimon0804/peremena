"""integrations: calendar feeds, API keys, outgoing webhooks

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-04 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'integration_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('salt', sa.String(length=64), nullable=True),
        sa.Column('can_write', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('uses', sa.Integer(), server_default='0', nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_integration_tokens_owner_id', 'integration_tokens', ['owner_id'])
    op.create_index('ix_integration_tokens_token_hash', 'integration_tokens', ['token_hash'], unique=True)
    op.create_table(
        'webhooks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.BigInteger(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('events', sa.String(length=300), nullable=False),
        sa.Column('salt', sa.String(length=64), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('failures', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_status', sa.String(length=200), nullable=True),
        sa.Column('last_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_webhooks_owner_id', 'webhooks', ['owner_id'])
    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('webhook_id', sa.Integer(), nullable=False),
        sa.Column('event', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('code', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_webhook_deliveries_webhook_id', 'webhook_deliveries', ['webhook_id'])
    op.create_index('ix_webhook_deliveries_created_at', 'webhook_deliveries', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_webhook_deliveries_created_at', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_webhook_id', table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
    op.drop_index('ix_webhooks_owner_id', table_name='webhooks')
    op.drop_table('webhooks')
    op.drop_index('ix_integration_tokens_token_hash', table_name='integration_tokens')
    op.drop_index('ix_integration_tokens_owner_id', table_name='integration_tokens')
    op.drop_table('integration_tokens')

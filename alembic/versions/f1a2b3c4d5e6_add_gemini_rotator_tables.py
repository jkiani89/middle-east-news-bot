"""add gemini rotator tables

Revision ID: f1a2b3c4d5e6
Revises: e24bf6e4d3eb
Create Date: 2026-11-11 10:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e24bf6e4d3eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'gemini_suspended_keys',
        sa.Column('key', sa.String(length=200), nullable=False),
        sa.Column('suspended_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('key', name='pk_gemini_suspended_keys'),
    )

    op.create_table(
        'gemini_key_stats',
        sa.Column('key', sa.String(length=200), nullable=False),
        sa.Column('total_requests', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_success', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_rate_limit', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_suspended', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_transient', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_error', sa.Integer(), server_default='0', nullable=False),
        sa.Column('latency_sum', sa.Float(), server_default='0.0', nullable=False),
        sa.Column('latency_min', sa.Float(), nullable=True),
        sa.Column('latency_max', sa.Float(), server_default='0.0', nullable=False),
        sa.Column('latency_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_used_at', sa.Float(), server_default='0.0', nullable=False),
        sa.PrimaryKeyConstraint('key', name='pk_gemini_key_stats'),
    )

    op.create_table(
        'gemini_request_log',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('key', sa.String(length=200), nullable=False),
        sa.Column('event', sa.String(length=50), nullable=False),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('ts', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_gemini_request_log'),
    )
    op.create_index('ix_gemini_request_log_key', 'gemini_request_log', ['key'], unique=False)
    op.create_index('ix_gemini_request_log_ts', 'gemini_request_log', ['ts'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_gemini_request_log_ts', table_name='gemini_request_log')
    op.drop_index('ix_gemini_request_log_key', table_name='gemini_request_log')
    op.drop_table('gemini_request_log')
    op.drop_table('gemini_key_stats')
    op.drop_table('gemini_suspended_keys')

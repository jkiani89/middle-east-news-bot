"""add telegraph_reports with a unique (report_date, slot_hour)

Revision ID: c3d4e5f6a7b8
Revises: a7c1d2e3f4b5
Create Date: 2026-11-17 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'a7c1d2e3f4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the per-slot Telegraph/Telegram publication state table."""
    op.create_table(
        'telegraph_reports',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('slot_hour', sa.Integer(), nullable=False),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('window_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=30), server_default='pending', nullable=False),
        sa.Column('telegram_status', sa.String(length=30), server_default='pending', nullable=False),
        sa.Column('index_path', sa.Text(), nullable=True),
        sa.Column('index_url', sa.Text(), nullable=True),
        sa.Column(
            'topic_pages',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='{}',
            nullable=True,
        ),
        sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_telegraph_reports'),
        sa.UniqueConstraint('report_date', 'slot_hour', name='uq_telegraph_report_date_hour'),
    )

    # Every slot already delivered by Telegram keeps its delivery record: mark the
    # notification as sent so the first run after this migration never re-sends history.
    op.execute(
        """
        INSERT INTO telegraph_reports (report_date, slot_hour, status, telegram_status)
        SELECT report_date, slot_hour, 'pending', 'sent'
          FROM bulletin_slots
         WHERE status = 'published'
        ON CONFLICT ON CONSTRAINT uq_telegraph_report_date_hour DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table('telegraph_reports')

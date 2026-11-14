"""add bulletin_slots with a unique (report_date, slot_hour)

Revision ID: a7c1d2e3f4b5
Revises: a7d3c1e9f2b4
Create Date: 2026-11-14 23:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a7c1d2e3f4b5'
down_revision: Union[str, Sequence[str], None] = 'a7d3c1e9f2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the slot table and copy the delivery history out of the JSON blob."""
    op.create_table(
        'bulletin_slots',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('slot_hour', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=30), server_default='pending', nullable=False),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'message_ids',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='[]',
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id', name='pk_bulletin_slots'),
        sa.UniqueConstraint('report_date', 'slot_hour', name='uq_bulletin_slot_date_hour'),
    )

    # Preserve history: every slot already delivered is stored as
    # daily_reports.content->'bulletins'->'HH:MM' = {"status": "published", ...}.
    op.execute(
        """
        INSERT INTO bulletin_slots (report_date, slot_hour, status, message_ids)
        SELECT dr.report_date,
               split_part(b.key, ':', 1)::int,
               'published',
               COALESCE(b.value->'message_ids', '[]'::jsonb)
          FROM daily_reports AS dr
          CROSS JOIN LATERAL jsonb_each(
                 COALESCE(dr.content->'bulletins', '{}'::jsonb)
               ) AS b(key, value)
         WHERE b.value->>'status' = 'published'
           AND split_part(b.key, ':', 1) ~ '^[0-9]{1,2}$'
        ON CONFLICT ON CONSTRAINT uq_bulletin_slot_date_hour DO NOTHING
        """
    )


def downgrade() -> None:
    """Drop the slot table (the JSON history in daily_reports is untouched)."""
    op.drop_table('bulletin_slots')

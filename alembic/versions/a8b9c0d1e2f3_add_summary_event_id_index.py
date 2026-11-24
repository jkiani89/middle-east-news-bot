"""add ix_summaries_event_id index on summaries(event_id)

Revision ID: a8b9c0d1e2f3
Revises: 8b7c6d5e4f3a
Create Date: 2026-11-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "8b7c6d5e4f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index("ix_summaries_event_id", "summaries", ["event_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_summaries_event_id", table_name="summaries")

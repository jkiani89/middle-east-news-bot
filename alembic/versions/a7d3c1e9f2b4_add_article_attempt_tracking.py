"""add_article_attempt_tracking

Revision ID: a7d3c1e9f2b4
Revises: f1a2b3c4d5e6
Create Date: 2026-11-12 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3c1e9f2b4'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'articles',
        sa.Column('attempt_count', sa.SmallInteger(), server_default='0', nullable=False),
    )
    op.add_column(
        'articles',
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('articles', 'last_attempt_at')
    op.drop_column('articles', 'attempt_count')

"""add parent_event_id to events

Revision ID: e24bf6e4d3eb
Revises: ec4cd113bf5a
Create Date: 2026-11-10 16:19:00.923885

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e24bf6e4d3eb'
down_revision: Union[str, Sequence[str], None] = 'ec4cd113bf5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('events', sa.Column('parent_event_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_events_parent_event_id'), 'events', ['parent_event_id'], unique=False)
    op.create_foreign_key(
        'fk_events_parent_event_id_events',
        'events',
        'events',
        ['parent_event_id'],
        ['id'],
        ondelete='SET NULL'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_events_parent_event_id_events', 'events', type_='foreignkey')
    op.drop_index(op.f('ix_events_parent_event_id'), table_name='events')
    op.drop_column('events', 'parent_event_id')

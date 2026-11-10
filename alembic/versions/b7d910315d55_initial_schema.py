"""initial_schema

Revision ID: b7d910315d55
Revises: 
Create Date: 2026-11-10 09:50:57.034221

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b7d910315d55'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. sources table
    op.create_table(
        'sources',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('base_url', sa.Text(), nullable=True),
        sa.Column('feed_url', sa.Text(), nullable=True),
        sa.Column('source_type', sa.String(length=40), nullable=False, server_default='regional'),
        sa.Column('country', sa.String(length=100), nullable=True),
        sa.Column('language', sa.String(length=10), nullable=False, server_default='en'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('reliability_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # 2. articles table
    op.create_table(
        'articles',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('source_id', sa.BigInteger(), sa.ForeignKey('sources.id'), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('original_url', sa.Text(), nullable=False),
        sa.Column('canonical_url', sa.Text(), nullable=False),
        sa.Column('original_language', sa.String(length=10), nullable=True),
        sa.Column('original_text', sa.Text(), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='new'),
        sa.Column('ai_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.UniqueConstraint('canonical_url', name='uq_articles_canonical_url'),
    )
    op.create_index('ix_articles_published_at', 'articles', ['published_at'], unique=False)
    op.create_index('ix_articles_content_hash', 'articles', ['content_hash'], unique=False)
    op.create_index('ix_articles_collected_at', 'articles', ['collected_at'], unique=False)

    # 3. events table
    op.create_table(
        'events',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=True),
        sa.Column('countries', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('actors', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('location', sa.Text(), nullable=True),
        sa.Column('event_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('last_updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('importance', sa.SmallInteger(), nullable=False, server_default='3'),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='active'),
    )

    # 4. article_events table
    op.create_table(
        'article_events',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('article_id', sa.BigInteger(), sa.ForeignKey('articles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event_id', sa.BigInteger(), sa.ForeignKey('events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('match_type', sa.String(length=30), nullable=False, server_default='initial'),
        sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('article_id', 'event_id', name='uq_article_event'),
    )

    # 5. summaries table
    op.create_table(
        'summaries',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('event_id', sa.BigInteger(), sa.ForeignKey('events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('headline_fa', sa.Text(), nullable=False),
        sa.Column('summary_fa', sa.Text(), nullable=False),
        sa.Column('details_fa', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('verification_status', sa.String(length=30), nullable=False, server_default='unconfirmed'),
        sa.Column('model_name', sa.String(length=100), nullable=False),
        sa.Column('prompt_version', sa.String(length=30), nullable=False, server_default='v1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # 6. daily_reports table
    op.create_table(
        'daily_reports',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('telegram_message_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='generated'),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('report_date', name='uq_daily_report_date'),
    )

    # 7. job_logs table
    op.create_table(
        'job_logs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('job_name', sa.String(length=100), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='running'),
        sa.Column('processed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_message', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('job_logs')
    op.drop_table('daily_reports')
    op.drop_table('summaries')
    op.drop_table('article_events')
    op.drop_table('events')
    op.drop_table('articles')
    op.drop_table('sources')

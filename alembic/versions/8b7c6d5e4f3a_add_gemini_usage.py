"""add gemini_usage table

Revision ID: 8b7c6d5e4f3a
Revises: 9a8b7c6d5e4f
Create Date: 2026-11-22 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b7c6d5e4f3a"
down_revision: Union[str, Sequence[str], None] = "9a8b7c6d5e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gemini_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("prompt_token_count", sa.Integer(), nullable=True),
        sa.Column("candidates_token_count", sa.Integer(), nullable=True),
        sa.Column("total_token_count", sa.Integer(), nullable=True),
        sa.Column("thoughts_token_count", sa.Integer(), nullable=True),
        sa.Column("cached_content_token_count", sa.Integer(), nullable=True),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="success"),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("request_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name="fk_gemini_usage_article_id_articles",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_gemini_usage_source_id_sources",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_gemini_usage"),
    )
    op.create_index("ix_gemini_usage_article_id", "gemini_usage", ["article_id"], unique=False)
    op.create_index("ix_gemini_usage_source_id", "gemini_usage", ["source_id"], unique=False)
    op.create_index("ix_gemini_usage_request_timestamp", "gemini_usage", ["request_timestamp"], unique=False)
    op.create_index("ix_gemini_usage_model_name", "gemini_usage", ["model_name"], unique=False)
    op.create_index("ix_gemini_usage_operation", "gemini_usage", ["operation"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_gemini_usage_operation", table_name="gemini_usage")
    op.drop_index("ix_gemini_usage_model_name", table_name="gemini_usage")
    op.drop_index("ix_gemini_usage_request_timestamp", table_name="gemini_usage")
    op.drop_index("ix_gemini_usage_source_id", table_name="gemini_usage")
    op.drop_index("ix_gemini_usage_article_id", table_name="gemini_usage")
    op.drop_table("gemini_usage")

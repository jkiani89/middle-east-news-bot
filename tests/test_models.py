"""Unit tests for database models and schema definitions."""

from app.database.models import Base


def test_all_seven_tables_registered():
    table_names = set(Base.metadata.tables.keys())
    expected_tables = {
        "sources", "articles", "events", "article_events",
        "summaries", "daily_reports", "job_logs"
    }
    assert expected_tables.issubset(table_names)


def test_article_table_constraints_and_indexes():
    articles_table = Base.metadata.tables["articles"]
    index_names = {idx.name for idx in articles_table.indexes}
    assert "ix_articles_published_at" in index_names
    assert "ix_articles_content_hash" in index_names
    assert "ix_articles_collected_at" in index_names
    # Verify text_source column exists on articles table
    assert "text_source" in articles_table.columns
    assert str(articles_table.columns["text_source"].server_default.arg) == "rss_summary"


def test_article_event_unique_constraint():
    ae_table = Base.metadata.tables["article_events"]
    constraint_names = {c.name for c in ae_table.constraints}
    assert "uq_article_event" in constraint_names


def test_daily_report_unique_date_constraint():
    dr_table = Base.metadata.tables["daily_reports"]
    constraint_names = {c.name for c in dr_table.constraints}
    assert "uq_daily_report_date" in constraint_names

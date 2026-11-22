from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)

class Base(DeclarativeBase):
    pass

class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    base_url: Mapped[str | None] = mapped_column(Text)
    feed_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40), default="regional")
    country: Mapped[str | None] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(10), default="en")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    reliability_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    articles: Mapped[list[Article]] = relationship(back_populates="source")

class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_articles_canonical_url"),
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_content_hash", "content_hash"),
        Index("ix_articles_collected_at", "collected_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_language: Mapped[str | None] = mapped_column(String(10))
    original_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="new")
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0", nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text_source: Mapped[str] = mapped_column(String(30), default="rss_summary", server_default="rss_summary", nullable=False)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    ai_payload: Mapped[dict | None] = mapped_column(JSONB)
    source: Mapped[Source] = relationship(back_populates="articles")
    event_links: Mapped[list[ArticleEvent]] = relationship(back_populates="article")

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(50))
    countries: Mapped[list] = mapped_column(JSONB, default=list)
    actors: Mapped[list] = mapped_column(JSONB, default=list)
    location: Mapped[str | None] = mapped_column(Text)
    event_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    importance: Mapped[int] = mapped_column(SmallInteger, default=3)
    status: Mapped[str] = mapped_column(String(30), default="active")
    parent_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    article_links: Mapped[list[ArticleEvent]] = relationship(back_populates="event")
    summaries: Mapped[list[Summary]] = relationship(back_populates="event")

class ArticleEvent(Base):
    __tablename__ = "article_events"
    __table_args__ = (UniqueConstraint("article_id", "event_id", name="uq_article_event"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    match_type: Mapped[str] = mapped_column(String(30), default="initial")
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    article: Mapped[Article] = relationship(back_populates="event_links")
    event: Mapped[Event] = relationship(back_populates="article_links")

class Summary(Base):
    __tablename__ = "summaries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    headline_fa: Mapped[str] = mapped_column(Text, nullable=False)
    summary_fa: Mapped[str] = mapped_column(Text, nullable=False)
    details_fa: Mapped[dict] = mapped_column(JSONB, default=dict)
    verification_status: Mapped[str] = mapped_column(String(30), default="unconfirmed")
    model_name: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(30), default="v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event: Mapped[Event] = relationship(back_populates="summaries")

class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("report_date", name="uq_daily_report_date"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    telegram_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="generated")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class BulletinSlot(Base):
    """One row per scheduled briefing slot.

    The claim and its release are single SQL statements, so two runs cannot both decide a
    slot is free; the JSON copy inside ``daily_reports.content`` is kept for the reader
    payload (content quality, Telegraph link) but is no longer the lock.
    """

    __tablename__ = "bulletin_slots"
    __table_args__ = (
        UniqueConstraint("report_date", "slot_hour", name="uq_bulletin_slot_date_hour"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    slot_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_ids: Mapped[list] = mapped_column(JSONB, default=list)


class TelegraphReport(Base):
    """Publication state of one 12-hour report on Telegraph and Telegram.

    The two delivery channels are tracked independently: ``status`` describes the
    Telegraph pages (pending / in_progress / telegraph_completed / telegraph_partial /
    failed) and ``telegram_status`` describes the notification (pending / sent / failed),
    so a failed notification never causes the pages to be rebuilt.
    """

    __tablename__ = "telegraph_reports"
    __table_args__ = (
        UniqueConstraint("report_date", "slot_hour", name="uq_telegraph_report_date_hour"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    slot_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    telegram_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    index_path: Mapped[str | None] = mapped_column(Text)
    index_url: Mapped[str | None] = mapped_column(Text)
    # topic_key -> one record per published topic, holding every part of it:
    #   {name_fa, path, url, item_count, layout_version, layout_hash, part_count,
    #    planned_item_count, published_item_count, links_complete,
    #    parts: [{part, path, url, item_count, bytes, retired?, pending_create?}]}
    # ``path``/``url`` mirror part 1, so readers written before pagination keep working.
    # A flat record {path, url, name_fa, item_count} is read as the single part 1.
    topic_pages: Mapped[dict] = mapped_column(JSONB, default=dict)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="running")
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)


class GeminiUsage(Base):
    __tablename__ = "gemini_usage"
    __table_args__ = (
        Index("ix_gemini_usage_article_id", "article_id"),
        Index("ix_gemini_usage_source_id", "source_id"),
        Index("ix_gemini_usage_request_timestamp", "request_timestamp"),
        Index("ix_gemini_usage_model_name", "model_name"),
        Index("ix_gemini_usage_operation", "operation"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_token_count: Mapped[int | None] = mapped_column(Integer)
    candidates_token_count: Mapped[int | None] = mapped_column(Integer)
    total_token_count: Mapped[int | None] = mapped_column(Integer)
    thoughts_token_count: Mapped[int | None] = mapped_column(Integer)
    cached_content_token_count: Mapped[int | None] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="success", nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    request_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

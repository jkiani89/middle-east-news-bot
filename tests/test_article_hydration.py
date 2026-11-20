"""Tests for lazy full-body extraction (Phase O: ingestion is metadata-only)."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Article
from app.processors.pipeline import _hydrate_article_body


def _article(text: str = "RSS summary text", text_source: str = "rss_summary") -> Article:
    return Article(
        id=1,
        source_id=1,
        title="Region article",
        original_url="https://example.com/a",
        canonical_url="https://example.com/a",
        original_language="en",
        original_text=text,
        content_hash="H" * 64,
        text_source=text_source,
        status="collected",
    )


@pytest.mark.asyncio
async def test_hydration_fills_body_for_queued_article(monkeypatch):
    long_body = "Full article body " * 100
    fetcher = AsyncMock(return_value=long_body)
    monkeypatch.setattr("app.processors.pipeline.fetch_article_full_text", fetcher)

    article = _article()
    await _hydrate_article_body(article)

    assert article.text_source == "full_body"
    assert article.original_text == long_body
    fetcher.assert_awaited_once_with("https://example.com/a")


@pytest.mark.asyncio
async def test_hydration_skips_article_already_having_body(monkeypatch):
    fetcher = AsyncMock(return_value="Full article body " * 100)
    monkeypatch.setattr("app.processors.pipeline.fetch_article_full_text", fetcher)

    article = _article(text="Body stored by an older run", text_source="full_body")
    await _hydrate_article_body(article)

    fetcher.assert_not_awaited()
    assert article.original_text == "Body stored by an older run"


@pytest.mark.asyncio
async def test_hydration_keeps_summary_when_extraction_fails(monkeypatch):
    monkeypatch.setattr("app.processors.pipeline.fetch_article_full_text", AsyncMock(return_value=None))

    article = _article()
    await _hydrate_article_body(article)

    assert article.text_source == "rss_summary"
    assert article.original_text == "RSS summary text"


@pytest.mark.asyncio
async def test_hydration_gives_up_after_the_body_timeout(monkeypatch):
    """A slow publisher keeps its slot for at most ``article_body_fetch_timeout_seconds``."""

    async def _slow_fetch(url: str) -> str:
        await asyncio.sleep(5)
        return "Full article body " * 100

    monkeypatch.setattr("app.processors.pipeline.fetch_article_full_text", _slow_fetch)
    monkeypatch.setattr(settings, "article_body_fetch_timeout_seconds", 0.05)
    article = _article()
    started = time.perf_counter()

    await _hydrate_article_body(article)

    assert time.perf_counter() - started < 1.0
    assert article.text_source == "rss_summary"
    assert article.original_text == "RSS summary text"


@pytest.mark.asyncio
async def test_hydration_ignores_shorter_extraction(monkeypatch):
    monkeypatch.setattr(
        "app.processors.pipeline.fetch_article_full_text",
        AsyncMock(return_value="short"),
    )

    article = _article(text="A much longer RSS summary already present")
    await _hydrate_article_body(article)

    assert article.text_source == "rss_summary"
    assert article.original_text == "A much longer RSS summary already present"

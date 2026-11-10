"""Unit tests for Phase 3 pipeline processing and deduplication logic."""

from unittest.mock import AsyncMock
import pytest

from app.collectors.rss_collector import canonicalize_url, make_hash
from app.processors.pipeline import is_duplicate_article


@pytest.mark.asyncio
async def test_is_duplicate_article_matches_canonical_url():
    session = AsyncMock()
    # Mock database finding a match (returns existing article ID)
    session.scalar.return_value = 42

    canonical = "https://news.un.org/en/story/2026/11/1150000"
    content_hash = "0000000000000000000000000000000000000000000000000000000000000000"

    is_dup = await is_duplicate_article(session, canonical, content_hash)
    assert is_dup is True
    assert session.scalar.called


@pytest.mark.asyncio
async def test_is_duplicate_article_matches_content_hash():
    session = AsyncMock()
    session.scalar.return_value = 99

    canonical = "https://www.aljazeera.com/news/2026/11/reprint-story"
    matched_hash = "e5ddfe4815afa859e45f78685e3b85d81fe1f3ef253f0fa3a70a6bd3bf77728c"

    is_dup = await is_duplicate_article(session, canonical, matched_hash)
    assert is_dup is True
    assert session.scalar.called


@pytest.mark.asyncio
async def test_is_duplicate_article_returns_false_when_no_match():
    session = AsyncMock()
    session.scalar.return_value = None

    canonical = "https://news.un.org/en/story/2026/11/brand-new-event"
    new_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    is_dup = await is_duplicate_article(session, canonical, new_hash)
    assert is_dup is False
    assert session.scalar.called


def test_scenario_a_tracking_parameters_canonicalized():
    raw_url = "https://news.un.org/feed/view/en/story/2026/11/1168769?utm_source=twitter&utm_medium=social&fbclid=XYZ"
    canonical = canonicalize_url(raw_url)
    assert canonical == "https://news.un.org/en/story/2026/11/1168769"
    assert "utm_source" not in canonical
    assert "fbclid" not in canonical
    assert "/feed/view/" not in canonical


def test_scenario_b_different_url_same_hash_computation():
    title = "At least 56 migrants die in weekend shipwrecks off Senegal and Greece"
    body = "At least 56 people died in a series of shipwrecks off Senegal and Greece over the weekend."

    # Source 1 (UN News)
    hash_un = make_hash(title, body)

    # Source 2 (Syndicated reprint on another network with different formatting)
    hash_syndicated = make_hash(
        "  At least 56 migrants die in weekend shipwrecks off Senegal and Greece  ",
        "At least 56 people died in a series of shipwrecks\noff Senegal and Greece over the weekend.  "
    )

    # Hashes must match identically despite whitespace/source difference
    assert hash_un == hash_syndicated

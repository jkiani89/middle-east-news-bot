"""Unit tests for collectors, normalizers, retry behavior, and content extraction."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import tenacity

from app.collectors.rss_collector import (
    DEFAULT_HEADERS,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    CollectorError,
    FeedFetchError,
    _http_get_with_retry,
    canonicalize_url,
    extract_full_text_from_html,
    fetch_article_full_text,
    fetch_feed,
    is_retryable_http_error,
    make_hash,
)

SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>UN News - Middle East</title>
    <link>https://news.un.org</link>
    <description>Latest news from the United Nations</description>
    <item>
      <title>Security Council Adopts Resolution on Middle East</title>
      <link>https://news.un.org/en/story/2026/11/1150000?utm_source=twitter&amp;utm_campaign=un</link>
      <description>The Security Council convened today in New York to deliberate on regional peace and security.</description>
      <pubDate>Tue, 10 Nov 2026 07:30:00 GMT</pubDate>
      <guid isPermaLink="true">https://news.un.org/en/story/2026/11/1150000</guid>
    </item>
    <item>
      <title>Humanitarian Aid Convoys Enter Relief Corridor</title>
      <link>https://news.un.org/en/story/2026/11/1150001</link>
      <description>Emergency supplies were dispatched to provide assistance to affected civilians.</description>
      <pubDate>Tue, 10 Nov 2026 06:15:00 +0000</pubDate>
      <guid isPermaLink="true">https://news.un.org/en/story/2026/11/1150001</guid>
    </item>
  </channel>
</rss>
"""

MALFORMED_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Malformed Feed</title>
    <item>
      <title>Unclosed Tags Article</title>
      <link>https://news.un.org/en/story/2026/11/malformed</link>
      <description>Text without proper closing tags
  </channel>
</rss>
"""

SAMPLE_ARTICLE_HTML = """<!DOCTYPE html>
<html>
<head><title>Middle East Summit Held in Doha</title></head>
<body>
  <header><nav><a href="/home">Home</a> | <a href="/ads">Sponsored Links</a></nav></header>
  <main>
    <article>
      <h1>Middle East Summit Concludes in Doha</h1>
      <p>Diplomatic delegates from across the region concluded high-level talks today in Doha.</p>
      <p>The participants agreed on a joint framework for future diplomatic cooperation and regular working group meetings.</p>
    </article>
  </main>
  <aside>
    <div>Click here for stock tips and discounts!</div>
  </aside>
  <footer><p>Copyright 2026 News Organization</p></footer>
</body>
</html>
"""


# --- URL Normalization Tests ---

def test_canonicalize_url_strips_utm_and_tracking():
    raw = "https://WWW.AlJazeera.com/news/2026/11/10/event?utm_source=twitter&utm_medium=social&utm_campaign=breaking&fbclid=abc123xyz"
    canonical = canonicalize_url(raw)
    assert canonical == "https://www.aljazeera.com/news/2026/11/10/event"


def test_canonicalize_url_preserves_legitimate_query_params():
    raw = "https://news.un.org/en/story/2026/11/12345?id=99&lang=en&gclid=track"
    canonical = canonicalize_url(raw)
    assert "id=99" in canonical
    assert "lang=en" in canonical
    assert "gclid" not in canonical


def test_canonicalize_url_strips_trailing_slash():
    raw = "https://example.com/middle-east/news/"
    canonical = canonicalize_url(raw)
    assert canonical == "https://example.com/middle-east/news"


def test_canonicalize_url_handles_empty():
    assert canonicalize_url("") == ""


# --- Content Hashing Tests ---

def test_make_hash_deterministic_and_whitespace_invariant():
    title = "Breaking: Diplomatic Talks in Geneva"
    text1 = "Delegates met on Tuesday to discuss regional security."
    text2 = "  Delegates  met\non Tuesday to discuss regional   security.  "

    hash1 = make_hash(title, text1)
    hash2 = make_hash(title, text2)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 length in hex


def test_make_hash_differentiates_distinct_content():
    title1 = "Ceasefire Agreement Signed"
    title2 = "Ceasefire Agreement Violated"
    text = "The parties made announcements regarding the ceasefire."

    assert make_hash(title1, text) != make_hash(title2, text)


# --- HTTP Error Retry Classification Tests ---

def test_is_retryable_http_error_network_and_transport():
    req = httpx.Request("GET", "https://example.com/feed.xml")
    assert is_retryable_http_error(httpx.ConnectError("Connection refused", request=req))
    assert is_retryable_http_error(httpx.ConnectTimeout("Connect timed out", request=req))
    assert is_retryable_http_error(httpx.ReadTimeout("Read timed out", request=req))


def test_is_retryable_http_error_status_codes():
    req = httpx.Request("GET", "https://example.com/feed.xml")

    # Retryable statuses: 429, 500, 502, 503, 504
    for status in [429, 500, 502, 503, 504]:
        resp = httpx.Response(status, request=req)
        err = httpx.HTTPStatusError(f"Error {status}", request=req, response=resp)
        assert is_retryable_http_error(err) is True

    # Non-retryable statuses: 400, 401, 403, 404
    for status in [400, 401, 403, 404]:
        resp = httpx.Response(status, request=req)
        err = httpx.HTTPStatusError(f"Error {status}", request=req, response=resp)
        assert is_retryable_http_error(err) is False

    # Generic non-HTTP errors are not retryable by default
    assert is_retryable_http_error(ValueError("Invalid argument")) is False


# --- Feed Fetching and Retry Execution Tests ---

@pytest.mark.asyncio
async def test_fetch_feed_successful_mock():
    mock_client = AsyncMock()
    req = httpx.Request("GET", "https://news.un.org/feed.xml")
    mock_client.get.return_value = httpx.Response(200, request=req, text=SAMPLE_RSS_XML)

    items = await fetch_feed("https://news.un.org/feed.xml", client=mock_client)

    assert len(items) == 2
    item1 = items[0]
    assert item1["title"] == "Security Council Adopts Resolution on Middle East"
    # Canonical URL should strip utm tracking
    assert item1["canonical_url"] == "https://news.un.org/en/story/2026/11/1150000"
    assert "Security Council convened" in item1["text"]
    assert item1["published_at"] == datetime(2026, 11, 10, 7, 30, tzinfo=timezone.utc)
    assert len(item1["content_hash"]) == 64

    item2 = items[1]
    assert item2["title"] == "Humanitarian Aid Convoys Enter Relief Corridor"
    assert item2["canonical_url"] == "https://news.un.org/en/story/2026/11/1150001"
    assert item2["published_at"] == datetime(2026, 11, 10, 6, 15, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_fetch_feed_http_500_retries_and_raises_feed_fetch_error():
    orig_wait = _http_get_with_retry.retry.wait
    _http_get_with_retry.retry.wait = tenacity.wait_none()

    attempts = 0
    req = httpx.Request("GET", "https://example.com/fail.xml")

    async def mock_failing_get(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        resp = httpx.Response(500, request=req)
        raise httpx.HTTPStatusError("500 Internal Server Error", request=req, response=resp)

    mock_client = AsyncMock()
    mock_client.get.side_effect = mock_failing_get

    try:
        with pytest.raises(FeedFetchError) as exc_info:
            await fetch_feed("https://example.com/fail.xml", client=mock_client)
        assert "500 Internal Server Error" in str(exc_info.value)
        # Should have attempted 3 times according to stop_after_attempt(3)
        assert attempts == 3
    finally:
        _http_get_with_retry.retry.wait = orig_wait


@pytest.mark.asyncio
async def test_fetch_feed_timeout_retries_and_raises_feed_fetch_error():
    orig_wait = _http_get_with_retry.retry.wait
    _http_get_with_retry.retry.wait = tenacity.wait_none()

    attempts = 0
    req = httpx.Request("GET", "https://example.com/timeout.xml")

    async def mock_timeout_get(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("Server took too long", request=req)

    mock_client = AsyncMock()
    mock_client.get.side_effect = mock_timeout_get

    try:
        with pytest.raises(FeedFetchError) as exc_info:
            await fetch_feed("https://example.com/timeout.xml", client=mock_client)
        assert isinstance(exc_info.value.__cause__, httpx.ReadTimeout)
        assert attempts == 3
    finally:
        _http_get_with_retry.retry.wait = orig_wait


@pytest.mark.asyncio
async def test_fetch_feed_404_fails_fast_without_retrying():
    orig_wait = _http_get_with_retry.retry.wait
    _http_get_with_retry.retry.wait = tenacity.wait_none()

    attempts = 0
    req = httpx.Request("GET", "https://example.com/notfound.xml")

    async def mock_404_get(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    mock_client = AsyncMock()
    mock_client.get.side_effect = mock_404_get

    try:
        with pytest.raises(FeedFetchError):
            await fetch_feed("https://example.com/notfound.xml", client=mock_client)
        # Non-retryable error must fail fast after 1 attempt
        assert attempts == 1
    finally:
        _http_get_with_retry.retry.wait = orig_wait


@pytest.mark.asyncio
async def test_fetch_feed_malformed_xml_resilience():
    mock_client = AsyncMock()
    req = httpx.Request("GET", "https://news.un.org/malformed.xml")
    mock_client.get.return_value = httpx.Response(200, request=req, text=MALFORMED_RSS_XML)

    # Should not raise exception; feedparser recovers whatever it can
    items = await fetch_feed("https://news.un.org/malformed.xml", client=mock_client)
    assert isinstance(items, list)


# --- Trafilatura Content Extraction Tests ---

def test_extract_full_text_from_html_cleans_boilerplate():
    extracted = extract_full_text_from_html(SAMPLE_ARTICLE_HTML, url="https://example.com/article")
    assert extracted is not None
    assert "Middle East Summit Concludes in Doha" in extracted
    assert "framework for future diplomatic cooperation" in extracted
    # Ads and navigation links should be filtered out
    assert "Sponsored Links" not in extracted
    assert "Click here for stock tips" not in extracted


def test_extract_full_text_from_html_empty_or_whitespace():
    assert extract_full_text_from_html("") is None
    assert extract_full_text_from_html("   ") is None


@pytest.mark.asyncio
async def test_fetch_article_full_text_success():
    req = httpx.Request("GET", "https://example.com/article")
    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(200, request=req, text=SAMPLE_ARTICLE_HTML)

    text = await fetch_article_full_text("https://example.com/article", client=mock_client)
    assert text is not None
    assert "diplomatic cooperation" in text


@pytest.mark.asyncio
async def test_fetch_feed_with_full_text_extraction():
    # RSS with short summary that triggers Trafilatura full-text extraction
    rss_short = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>News</title>
        <item>
          <title>Doha Summit Report</title>
          <link>https://example.com/doha</link>
          <description>Short teaser.</description>
          <pubDate>Tue, 10 Nov 2026 08:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """
    mock_client = AsyncMock()

    async def mock_get(url, *args, **kwargs):
        req = httpx.Request("GET", url)
        if "rss" in url or url.endswith("feed"):
            return httpx.Response(200, request=req, text=rss_short)
        else:
            return httpx.Response(200, request=req, text=SAMPLE_ARTICLE_HTML)

    mock_client.get.side_effect = mock_get

    items = await fetch_feed("https://example.com/feed", extract_full_text=True, client=mock_client)
    assert len(items) == 1
    # Full article text should have been extracted and replaced the short teaser
    assert "framework for future diplomatic cooperation" in items[0]["text"]

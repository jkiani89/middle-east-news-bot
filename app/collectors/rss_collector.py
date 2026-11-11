"""RSS feed collector with exponential backoff retry, timeout handling, and Trafilatura integration."""

import hashlib
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
import trafilatura
from dateutil import parser as date_parser
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

logger = logging.getLogger(__name__)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
}

DEFAULT_USER_AGENT = (
    "MiddleEastNewsBot/1.0 (+https://github.com/middle-east-news-bot; bot@middleeastnews.internal)"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, text/html;q=0.9, */*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

DEFAULT_ARTICLE_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class CollectorError(Exception):
    """Base exception for collector operations."""
    pass


class FeedFetchError(CollectorError):
    """Raised when an RSS feed cannot be retrieved after all retries."""
    pass


class ArticleExtractionError(CollectorError):
    """Raised when full-text article extraction fails."""
    pass


def canonicalize_url(url: str) -> str:
    """Normalize URL by stripping tracking parameters, normalizing host case, and resolving known feed redirect paths."""
    if not url:
        return ""
    p = urlsplit(url)
    path = p.path
    # Normalize known feed tracking paths (e.g. UN News /feed/view/en/story/... -> /en/story/...)
    if "news.un.org" in p.netloc.lower() and path.startswith("/feed/view/"):
        path = path.replace("/feed/view/", "/", 1)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    path = path.rstrip("/")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(query), ""))


def make_hash(title: str, text: str) -> str:
    """Compute SHA-256 hash of normalized title and text for deduplication."""
    normalized = " ".join((title + " " + text).lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_retryable_http_error(exc: BaseException) -> bool:
    """Determine whether an HTTP/network exception warrants an exponential backoff retry."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        # Retry server errors and rate limits; do not retry 4xx client errors
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return False


@retry(
    retry=retry_if_exception(is_retryable_http_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _http_get_with_retry(
    url: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    """Execute HTTP GET with exponential backoff retry on transient failures."""
    if client is not None:
        response = await client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        return response

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as local_client:
        response = await local_client.get(url, headers=headers)
        response.raise_for_status()
        return response


def extract_full_text_from_html(html_content: str, url: str | None = None) -> str | None:
    """Extract main article body from raw HTML using Trafilatura, stripping ads and navigation."""
    if not html_content or not html_content.strip():
        return None
    try:
        extracted = trafilatura.extract(
            html_content,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            output_format="txt",
        )
        if extracted and extracted.strip():
            return extracted.strip()
    except Exception as exc:
        logger.warning("Trafilatura extraction error for %s: %s", url, exc)
    return None


async def fetch_article_full_text(
    url: str,
    client: httpx.AsyncClient | None = None,
    timeout: httpx.Timeout | None = None,
) -> str | None:
    """Fetch article web page and extract clean body text using Trafilatura."""
    req_timeout = timeout or DEFAULT_TIMEOUT
    try:
        response = await _http_get_with_retry(
            url=url,
            headers=DEFAULT_ARTICLE_HEADERS,
            timeout=req_timeout,
            client=client,
        )
        return extract_full_text_from_html(response.text, url=url)
    except Exception as exc:
        logger.warning("Failed to fetch/extract article body for %s: %s", url, exc)
        return None


async def fetch_feed(
    feed_url: str,
    extract_full_text: bool | None = None,
    min_summary_length: int | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: httpx.Timeout | None = None,
) -> list[dict]:
    """Fetch and parse an RSS feed with retry, timeout, error isolation, and Trafilatura full-text extraction.

    Returns a list of article dicts with keys:
    - title: str
    - url: str
    - canonical_url: str
    - published_at: datetime | None (UTC)
    - text: str (extracted body or summary)
    - text_source: str ('full_body' or 'rss_summary')
    - content_hash: str (SHA-256)
    """
    req_timeout = timeout or DEFAULT_TIMEOUT
    should_extract_full = (
        extract_full_text if extract_full_text is not None else settings.require_full_text_for_ai
    )
    threshold_len = (
        min_summary_length if min_summary_length is not None else settings.min_summary_len_for_full_text
    )
    logger.info(
        "Fetching RSS feed from %s (require_full_text=%s, min_summary_len=%d)",
        feed_url,
        should_extract_full,
        threshold_len,
    )

    try:
        response = await _http_get_with_retry(
            url=feed_url,
            headers=DEFAULT_HEADERS,
            timeout=req_timeout,
            client=client,
        )
    except Exception as exc:
        msg = f"Failed to fetch RSS feed {feed_url} after retries: {exc}"
        logger.error(msg)
        raise FeedFetchError(msg) from exc

    parsed = feedparser.parse(response.text)
    if parsed.bozo:
        logger.warning(
            "Feedparser reported bozo exception for %s: %s",
            feed_url,
            getattr(parsed, "bozo_exception", "Unknown XML parse warning"),
        )

    results: list[dict] = []
    missing_pubdate_count = 0
    for entry in parsed.entries:
        raw_url = entry.get("link")
        title = entry.get("title", "").strip()
        if not raw_url or not title:
            continue

        canonical = canonicalize_url(raw_url)

        # Parse publication date to UTC timezone
        published = None
        raw_date = entry.get("published") or entry.get("updated")
        if raw_date:
            try:
                published = date_parser.parse(raw_date)
                if published and not published.tzinfo:
                    published = published.replace(tzinfo=UTC)
                elif published and published.tzinfo:
                    published = published.astimezone(UTC)
            except Exception:
                published = None

        if not published and hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published = datetime(*entry.published_parsed[:6], tzinfo=UTC)
            except Exception:
                published = None

        if published is None:
            missing_pubdate_count += 1

        summary = (entry.get("summary") or entry.get("description") or "").strip()
        article_text = summary
        text_source = "rss_summary"

        # Fallback to Trafilatura full-text extraction if requested or summary is too short (< threshold_len)
        target_url = canonical or raw_url
        if should_extract_full or (len(summary) < threshold_len and bool(target_url)):
            full_text = await fetch_article_full_text(target_url, client=client, timeout=req_timeout)
            len_summary = len(summary)
            len_extracted = len(full_text) if full_text else 0
            logger.info(
                "Trafilatura triggered: url=%s, rss_summary_len=%d, extracted_len=%d",
                target_url,
                len_summary,
                len_extracted,
            )
            if full_text and len(full_text) > len_summary:
                article_text = full_text
                text_source = "full_body"

        content_hash = make_hash(title, article_text)

        results.append({
            "title": title,
            "url": raw_url,
            "canonical_url": canonical,
            "published_at": published,
            "text": article_text,
            "text_source": text_source,
            "content_hash": content_hash,
        })

    if missing_pubdate_count > 0:
        logger.warning(
            "pubDate missing for %d entries from %s (published_at will remain NULL)",
            missing_pubdate_count,
            feed_url,
        )

    logger.info("Successfully fetched %d entries from %s", len(results), feed_url)
    return results

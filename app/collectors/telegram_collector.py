"""Collector for public Telegram channels via t.me/s/ pages."""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.rss_collector import (
    DEFAULT_HEADERS,
    DEFAULT_TIMEOUT,
    _http_get_with_retry,
    make_hash,
)
from app.config import settings
from app.database.models import Article, Source

logger = logging.getLogger(__name__)

TELEGRAM_MEDIA_MARKERS = (
    "tgme_widget_message_photo",
    "tgme_widget_message_video",
)

MAX_PAGES_PER_CHANNEL = 3
PAGE_SIZE = 20


def html_to_text(fragment: str) -> str:
    """Convert a Telegram message HTML fragment to plain text."""
    text = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_posts(page_html: str, channel: str) -> list[dict[str, Any]]:
    """Parse posts from one t.me/s page. Posts are returned in ascending order."""
    positions = [m.start() for m in re.finditer(r'data-post="', page_html)]
    posts: list[dict[str, Any]] = []

    for index, start in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(page_html)
        block = page_html[start:end]

        post_match = re.search(r'data-post="([^"]+)"', block)
        if not post_match:
            continue
        post_ref = post_match.group(1)
        if "/" not in post_ref:
            continue
        parsed_channel, post_id = post_ref.split("/", 1)
        if parsed_channel != channel:
            continue

        text_match = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block,
            re.DOTALL | re.IGNORECASE,
        )
        text = html_to_text(text_match.group(1)) if text_match else ""

        time_match = re.search(r"<time[^>]*datetime=\"([^\"]+)\"", block, re.IGNORECASE)
        published_at = parse_datetime(time_match.group(1) if time_match else None)

        has_media = any(marker in block for marker in TELEGRAM_MEDIA_MARKERS)

        canonical_url = f"https://t.me/{channel}/{post_id}"
        title = text.splitlines()[0][:120].strip() if text else ""
        if not title:
            title = f"Telegram post {post_id}"

        posts.append(
            {
                "channel": channel,
                "post_id": post_id,
                "title": title,
                "text": text,
                "published_at": published_at,
                "has_media": has_media,
                "canonical_url": canonical_url,
                "content_hash": make_hash(title, text),
            }
        )

    return posts


async def fetch_channel_page(channel: str, before: str | None = None) -> str:
    url = f"https://t.me/s/{channel}"
    if before:
        url = f"{url}?before={before}"
    response = await _http_get_with_retry(url, DEFAULT_HEADERS, DEFAULT_TIMEOUT)
    return response.text


async def classify_telegram_duplicate(
    session: AsyncSession,
    canonical_url: str,
    content_hash: str,
) -> tuple[str | None, int | None]:
    """Return (reason, existing_article_id)."""
    result = await session.execute(
        select(Article.id, Article.content_hash).where(Article.canonical_url == canonical_url).limit(1)
    )
    row = result.first()
    if row is not None:
        existing_id, existing_hash = row
        if existing_hash == content_hash:
            return "exact_duplicate", int(existing_id)
        logger.info(
            "telegram_post_content_changed: url=%s old_hash=%s new_hash=%s",
            canonical_url,
            existing_hash,
            content_hash,
        )
        return "telegram_post_content_changed", int(existing_id)

    result = await session.execute(
        select(Article.id).where(Article.content_hash == content_hash).limit(1)
    )
    existing_id = result.scalar_one_or_none()
    if existing_id is not None:
        logger.info(
            "cross_channel_duplicate: url=%s existing_article_id=%s",
            canonical_url,
            existing_id,
        )
        return "cross_channel_duplicate", int(existing_id)

    return None, None


async def collect_telegram_posts(session: AsyncSession) -> int:
    """Fetch Telegram channels and insert non-duplicate articles."""
    inserted_total = 0

    sources = (
        await session.scalars(
            select(Source).where(
                Source.is_active.is_(True),
                Source.source_type == "telegram",
            )
        )
    ).all()

    source_by_channel: dict[str, Source] = {}
    for source in sources:
        if source.feed_url and "t.me/s/" in source.feed_url:
            channel = source.feed_url.rstrip("/").split("/")[-1]
            source_by_channel[channel] = source

    for channel, source in source_by_channel.items():
        oldest_id: str | None = None
        known_seen = False
        expired_seen = False

        for _ in range(MAX_PAGES_PER_CHANNEL):
            if known_seen or expired_seen:
                break

            try:
                page_html = await fetch_channel_page(channel, oldest_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to collect Telegram channel %s: %s", channel, exc)
                break

            posts = parse_posts(page_html, channel)
            if not posts:
                break

            page_known = False
            page_expired = False

            for post in posts:
                if not post["text"]:
                    logger.warning(
                        "Skipping Telegram post without text: %s", post["canonical_url"]
                    )
                    continue

                reason, _existing_id = await classify_telegram_duplicate(
                    session, post["canonical_url"], post["content_hash"]
                )
                if reason is not None:
                    page_known = True
                    continue

                published_at = post["published_at"]
                age_hours = 0.0
                if published_at is not None:
                    reference = published_at
                    if reference.tzinfo is None:
                        reference = reference.replace(tzinfo=UTC)
                    age_hours = max(
                        0.0,
                        (datetime.now(UTC) - reference).total_seconds() / 3600.0,
                    )

                if age_hours > settings.article_max_age_hours:
                    page_expired = True

                article = Article(
                    source_id=source.id,
                    title=post["title"],
                    original_url=post["canonical_url"],
                    canonical_url=post["canonical_url"],
                    original_language=source.language,
                    original_text=post["text"],
                    published_at=post["published_at"],
                    content_hash=post["content_hash"],
                    text_source="telegram_post",
                    has_media=post["has_media"],
                    status="expired"
                    if age_hours > settings.article_max_age_hours
                    else "collected",
                )
                try:
                    async with session.begin_nested():
                        session.add(article)
                        await session.flush()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Duplicate article skipped via DB constraint: %s",
                        post["canonical_url"],
                    )
                    continue
                inserted_total += 1

            oldest_id = posts[0]["post_id"]
            if page_known:
                known_seen = True
            if page_expired:
                expired_seen = True

    return inserted_total

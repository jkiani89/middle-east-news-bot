import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import BaseAIProvider, get_ai_provider
from app.ai.summarizer import PROMPT_VERSION, analyze_article
from app.collectors.rss_collector import fetch_feed
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, ArticleEvent, Event, JobLog, Source, Summary
from app.processors.event_matcher import EventMatcher

logger = logging.getLogger(__name__)


async def is_duplicate_article(
    session: AsyncSession,
    canonical_url: str,
    content_hash: str,
) -> bool:
    """Check if an article already exists in the database by canonical_url OR content_hash.

    Implements Phase 3 deduplication (ISS-004 Part B).
    """
    stmt = (
        select(Article.id)
        .where(
            or_(
                Article.canonical_url == canonical_url,
                Article.content_hash == content_hash,
            )
        )
        .limit(1)
    )
    existing_id = await session.scalar(stmt)
    return existing_id is not None


async def process_collected_articles(
    session: AsyncSession,
    limit: int = 50,
    provider: BaseAIProvider | None = None,
) -> int:
    """Process unanalyzed articles from database with AI Provider.

    Strict financial guardrail (ISS-013 / Re-processing Prevention):
    ONLY queries articles with status == 'collected'.
    Articles with status IN ('summarized', 'rejected', 'failed', 'flagged_for_review')
    are STRICTLY EXCLUDED, preventing redundant LLM calls and unnecessary costs.
    """
    stmt = (
        select(Article)
        .where(Article.status == "collected")
        .order_by(Article.collected_at.asc())
        .limit(limit)
    )
    res = await session.scalars(stmt)
    articles_list = res.all() if hasattr(res, "all") else list(res)
    if asyncio.iscoroutine(articles_list):
        articles_list = await articles_list

    if not articles_list:
        logger.info("No unanalyzed articles found. Skipping AI processing (Gemini calls = 0).")
        return 0

    ai_provider = provider or get_ai_provider()
    processed_count = 0

    # Cache source names to minimize DB roundtrips
    sources_stmt = select(Source.id, Source.name)
    s_res = await session.execute(sources_stmt)
    s_rows = s_res.all() if hasattr(s_res, "all") else list(s_res)
    if asyncio.iscoroutine(s_rows):
        s_rows = await s_rows
    sources_map = dict(s_rows)

    for article in articles_list:
        source_name = sources_map.get(article.source_id, "Unknown Source")
        try:
            result = await analyze_article(
                title=article.title,
                body=article.original_text or "",
                source_name=source_name,
                provider=ai_provider,
            )
        except Exception as exc:
            logger.error("AI analysis failed for article ID %d (%s): %s", article.id, article.title, exc)
            article.status = "failed"
            processed_count += 1
            continue

        payload_dict = result.model_dump(mode="json")
        payload_dict["model_name"] = ai_provider.model_name
        article.ai_payload = payload_dict

        if result.is_injection_attempt:
            logger.warning(
                "Security Alert: Article ID %d contains potential prompt injection. "
                "Setting status to 'flagged_for_review' (preserving intelligence for human review).",
                article.id,
            )
            article.status = "flagged_for_review"
            # Intelligence is preserved in DB, but blocked from automatic publishing
            event = Event(
                title=f"[FLAGGED] {result.event_title}",
                event_type=result.categories[0] if result.categories else "SEC",
                countries=result.countries,
                actors=result.actors,
                location=result.location,
                importance=result.importance,
                status="flagged",
            )
            session.add(event)
            await session.flush()
            session.add(ArticleEvent(article_id=article.id, event_id=event.id, match_type="initial", confidence=0.50))
            session.add(
                Summary(
                    event_id=event.id,
                    headline_fa=result.headline_fa,
                    summary_fa=result.summary_fa,
                    details_fa={"source": source_name, "url": article.original_url, "flag": "injection_detected"},
                    verification_status="disputed",
                    model_name=ai_provider.model_name,
                    prompt_version=PROMPT_VERSION,
                )
            )
            processed_count += 1
            continue

        if not result.is_relevant:
            logger.info("Article ID %d deemed irrelevant to Middle East intelligence. Marked 'rejected'.", article.id)
            article.status = "rejected"
            processed_count += 1
            continue

        # Phase 5: Event Matching & Clustering
        matcher = EventMatcher()
        match_res = await matcher.match_or_create_event(
            session=session,
            article=article,
            analysis_data=result.model_dump(mode="json"),
            source_name=source_name,
        )

        session.add(
            ArticleEvent(
                article_id=article.id,
                event_id=match_res.event_id,
                match_type=match_res.match_type,
                confidence=match_res.confidence,
            )
        )

        if match_res.is_new_event:
            session.add(
                Summary(
                    event_id=match_res.event_id,
                    headline_fa=result.headline_fa,
                    summary_fa=result.summary_fa,
                    details_fa={
                        "source": source_name,
                        "url": article.original_url,
                        "sources": [{
                            "source": source_name,
                            "url": article.original_url,
                            "article_id": article.id,
                            "added_at": (article.collected_at or datetime.now(UTC)).isoformat(),
                        }],
                    },
                    verification_status=result.verification_status,
                    model_name=ai_provider.model_name,
                    prompt_version=PROMPT_VERSION,
                )
            )

        article.status = "summarized"
        processed_count += 1

    await session.commit()
    logger.info("Successfully processed %d articles via %s.", processed_count, ai_provider.model_name)
    return processed_count


async def collect_new_articles(session: AsyncSession) -> int:
    """Fetch every active feed and insert new, non-duplicate articles.

    Performs no AI work and leaves the transaction to the caller, so ingestion
    keeps running even when the AI models are unavailable or the queue is empty.
    Returns the number of newly inserted articles.
    """
    inserted_count = 0
    sources = (
        await session.scalars(select(Source).where(Source.is_active.is_(True), Source.feed_url.is_not(None)))
    ).all()
    for source in sources:
        try:
            entries = await fetch_feed(source.feed_url)
        except Exception as exc:
            logger.warning("Failed to collect feed for source %s: %s", source.name, exc)
            continue

        for item in entries[: settings.max_articles_per_batch]:
            if await is_duplicate_article(session, item["canonical_url"], item["content_hash"]):
                logger.debug("Skipping duplicate article: %s (%s)", item["title"], item["canonical_url"])
                continue

            article = Article(
                source_id=source.id,
                title=item["title"],
                original_url=item["url"],
                canonical_url=item["canonical_url"],
                original_language=source.language,
                original_text=item["text"],
                published_at=item["published_at"],
                content_hash=item["content_hash"],
                text_source=item.get("text_source", "rss_summary"),
                status="collected",
            )
            try:
                async with session.begin_nested():
                    session.add(article)
                    await session.flush()
            except IntegrityError:
                logger.debug(
                    "Duplicate article skipped via DB constraint: %s",
                    item["canonical_url"],
                )
                continue
            inserted_count += 1

    return inserted_count


async def collect_and_process(provider: BaseAIProvider | None = None):
    count = 0
    async with SessionLocal() as session:
        log = JobLog(job_name="hourly_collection")
        session.add(log)
        await session.flush()
        try:
            await collect_new_articles(session)

            # Process collected articles adhering strictly to status filtering
            count = await process_collected_articles(session, limit=settings.max_articles_per_batch, provider=provider)
            log.status, log.processed_count = "success", count
            log.finished_at = datetime.now(UTC)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.status, log.error_message = "failed", str(exc)[:3000]
            log.finished_at = datetime.now(UTC)
            session.add(log)
            await session.commit()
            raise
    return count

from datetime import datetime, timezone
import logging
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.connection import SessionLocal
from app.database.models import Source, Article, Event, ArticleEvent, Summary, JobLog
from app.collectors.rss_collector import fetch_feed
from app.ai.summarizer import analyze_article
from app.config import settings

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


async def collect_and_process():
    count = 0
    async with SessionLocal() as session:
        log = JobLog(job_name="hourly_collection")
        session.add(log)
        await session.flush()
        try:
            sources = (await session.scalars(select(Source).where(
                Source.is_active.is_(True), Source.feed_url.is_not(None)))).all()
            for source in sources:
                try:
                    entries = await fetch_feed(source.feed_url)
                except Exception as exc:
                    logger.warning("Failed to collect feed for source %s: %s", source.name, exc)
                    continue

                for item in entries[:settings.max_articles_per_batch]:
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
                        status="collected",
                    )
                    session.add(article)
                    try:
                        await session.flush()
                    except IntegrityError:
                        await session.rollback()
                        continue

                    try:
                        result = await analyze_article(article.title, article.original_text or "", source.name)
                    except Exception:
                        article.status = "failed"
                        continue

                    article.ai_payload = result.model_dump(mode="json")
                    if not result.is_relevant:
                        article.status = "rejected"
                        count += 1
                        continue

                    event = Event(
                        title=result.event_title,
                        event_type=result.categories[0] if result.categories else None,
                        countries=result.countries,
                        actors=result.actors,
                        location=result.location,
                        importance=result.importance,
                    )
                    session.add(event)
                    await session.flush()
                    session.add(ArticleEvent(article_id=article.id, event_id=event.id,
                                             match_type="initial", confidence=0.60))
                    session.add(Summary(
                        event_id=event.id,
                        headline_fa=result.headline_fa,
                        summary_fa=result.summary_fa,
                        details_fa={"source": source.name, "url": article.original_url},
                        verification_status=result.verification_status,
                        model_name=settings.gemini_model,
                        prompt_version="v1",
                    ))
                    article.status = "summarized"
                    count += 1

            log.status, log.processed_count = "success", count
            log.finished_at = datetime.now(timezone.utc)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.status, log.error_message = "failed", str(exc)[:3000]
            log.finished_at = datetime.now(timezone.utc)
            session.add(log)
            await session.commit()
            raise
    return count

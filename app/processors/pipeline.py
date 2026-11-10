from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database.connection import SessionLocal
from app.database.models import Source, Article, Event, ArticleEvent, Summary, JobLog
from app.collectors.rss_collector import fetch_feed
from app.ai.summarizer import analyze_article
from app.config import settings

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
                except Exception:
                    continue
                for item in entries[:settings.max_articles_per_batch]:
                    exists = await session.scalar(select(Article.id).where(
                        Article.canonical_url == item["canonical_url"]))
                    if exists:
                        continue
                    article = Article(source_id=source.id, title=item["title"],
                        original_url=item["url"], canonical_url=item["canonical_url"],
                        original_language=source.language, original_text=item["text"],
                        published_at=item["published_at"], content_hash=item["content_hash"])
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
                    event = Event(title=result.event_title,
                        event_type=result.categories[0] if result.categories else None,
                        countries=result.countries, actors=result.actors,
                        location=result.location, importance=result.importance)
                    session.add(event)
                    await session.flush()
                    session.add(ArticleEvent(article_id=article.id, event_id=event.id,
                                             match_type="initial", confidence=0.60))
                    session.add(Summary(event_id=event.id, headline_fa=result.headline_fa,
                        summary_fa=result.summary_fa,
                        details_fa={"source": source.name, "url": article.original_url},
                        verification_status=result.verification_status,
                        model_name=settings.gemini_model, prompt_version="v1"))
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

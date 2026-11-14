import asyncio
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import and_, distinct, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import BaseAIProvider, get_ai_provider, is_retryable_ai_error
from app.ai.summarizer import PROMPT_VERSION, analyze_article, analyze_perspectives
from app.collectors.rss_collector import fetch_article_full_text, fetch_feed
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, ArticleEvent, Event, JobLog, Source, Summary
from app.editorial.perspectives import load_orientations, orientation_for
from app.editorial.regions import has_middle_east_nexus, is_primary_actor
from app.editorial.topics import match_primary_topic
from app.processors.event_matcher import EventMatcher
from app.telegram.publisher import ImmediateAnalysis, publish_immediate_analysis

logger = logging.getLogger(__name__)

# Set when a batch stopped early because every AI call was failing transiently.
_circuit_breaker_tripped = False

# Monotonic timestamp of the last immediate Telegram message, used to pace sends.
_last_immediate_publish_at = 0.0


async def _count_event_sources(session: AsyncSession, event_id: int) -> int:
    """Count the distinct sources backing an event (two independent sources = confirmed)."""
    stmt = (
        select(func.count(distinct(Article.source_id)))
        .select_from(ArticleEvent)
        .join(Article, Article.id == ArticleEvent.article_id)
        .where(ArticleEvent.event_id == event_id)
    )
    count = await session.scalar(stmt)
    return count if isinstance(count, int) else 1


def resolve_verification_status(model_status: str, source_count: int) -> str:
    """Derive the published verification label from real corroboration.

    A single source can never be presented as confirmed, regardless of how
    confident the model was; two or more independent sources allow the model's
    own ``confirmed``/``partially_confirmed`` verdict to stand.
    """
    if model_status in {"disputed", "false"}:
        return model_status
    if source_count >= 2:
        if model_status in {"confirmed", "partially_confirmed"}:
            return model_status
        return "partially_confirmed"
    return "unconfirmed"


async def _refresh_event_perspectives(
    session: AsyncSession,
    event_id: int,
    event_title: str,
    provider: BaseAIProvider,
) -> bool:
    """Rebuild the narrative matrix of an event after a new editorial camp joined it.

    One call per event, and only when the event is covered by at least two camps: a
    single-camp story has nothing to compare, so spending a model call on it would
    add cost without adding information. The matrix is skipped when the stored one
    already covers every camp, which keeps repeated merges idempotent.
    """
    if not settings.perspectives_enabled:
        return False

    try:
        rows = (
            await session.execute(
                select(Article.id, Article.title, Article.original_text, Source.name)
                .join(ArticleEvent, ArticleEvent.article_id == Article.id)
                .join(Source, Source.id == Article.source_id)
                .where(ArticleEvent.event_id == event_id)
                .order_by(ArticleEvent.confidence.desc().nullslast(), Article.collected_at.asc())
            )
        ).all()
    except Exception:
        logger.exception("Could not load the coverage of event #%d for a perspective matrix.", event_id)
        return False

    # One excerpt per camp is enough to show the difference and keeps the prompt small.
    chosen: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 4:
            continue
        _article_id, title, text, source_name = row
        if not source_name:
            continue
        orientation = orientation_for(source_name)
        if orientation.key in chosen or len(chosen) >= settings.max_perspective_articles:
            continue
        excerpt = (f"{title}\n{text or ''}").strip()[: settings.perspective_excerpt_chars]
        chosen[orientation.key] = {
            "source_name": source_name,
            "orientation": orientation.label_fa,
            "text": excerpt,
        }

    if len(chosen) < 2:
        return False

    summary = (
        await session.scalars(select(Summary).where(Summary.event_id == event_id))
    ).first()
    if summary is None:
        return False

    details = dict(summary.details_fa or {})
    covered = sorted(chosen)
    if sorted(details.get("perspectives_orientations") or []) == covered and details.get("perspectives"):
        return False

    try:
        result = await analyze_perspectives(
            event_title=event_title,
            articles=list(chosen.values()),
            provider=provider,
            max_items=settings.max_perspective_articles,
        )
    except Exception:
        # The matrix is an enrichment: a model failure must never lose the event itself.
        logger.exception(
            "Perspective comparison failed for event #%d; the summary stays without a matrix.",
            event_id,
        )
        return False

    if not result.perspectives:
        return False

    # Silence is derived from the data, never guessed by the model: an absent camp has
    # no excerpt to reason about, so the only verifiable statement is that it did not
    # cover this event inside the window.
    absent = [
        f"{orientation.label_fa} ({'، '.join(orientation.sources[:3])})"
        if orientation.sources
        else orientation.label_fa
        for orientation in load_orientations()
        if orientation.key not in chosen
    ]
    silent = list(dict.fromkeys([*result.silent_parties_fa, *absent]))[
        : settings.max_perspective_articles
    ]

    details["perspectives"] = [item.model_dump(mode="json") for item in result.perspectives]
    details["perspectives_orientations"] = covered
    details["disagreement_fa"] = result.disagreement_fa
    details["silent_parties_fa"] = silent
    summary.details_fa = details
    logger.info(
        "Narrative matrix for event #%d rebuilt from %d camp(s): %d claim(s), disagreement=%s.",
        event_id,
        len(covered),
        len(result.perspectives),
        "yes" if result.disagreement_fa else "no",
    )
    return True


async def _publish_immediate_safely(analysis: ImmediateAnalysis, admin_only: bool = False) -> bool:
    """Send one analysis to Telegram without ever breaking the processing batch.

    Honors the enable flag, the test environment, and the inter-message delay, and
    swallows delivery errors: a Telegram outage must never roll back stored work.
    """
    global _last_immediate_publish_at

    if not settings.immediate_publish_enabled or settings.app_env == "test":
        return False

    pause = max(0.0, settings.immediate_publish_delay_seconds)
    elapsed = time.monotonic() - _last_immediate_publish_at
    if 0 < elapsed < pause:
        await asyncio.sleep(pause - elapsed)

    try:
        sent = await publish_immediate_analysis(analysis, admin_only=admin_only)
        _last_immediate_publish_at = time.monotonic()
        logger.info(
            "Immediate Telegram publish for '%s' completed (%d message(s)%s).",
            analysis.headline_fa,
            len(sent),
            ", admin-only" if admin_only else "",
        )
        return True
    except Exception:
        _last_immediate_publish_at = time.monotonic()
        logger.exception(
            "Immediate Telegram publish failed for '%s'; the article stays summarized.",
            analysis.headline_fa,
        )
        return False


def build_processing_query(limit: int):
    """Select articles eligible for AI analysis.

    Fresh articles (``collected``) come first, followed by transiently failed
    articles that still have attempts left (``failed`` with
    ``attempt_count < ai_max_attempts``). Articles that exhausted their
    attempts are never selected again.
    """
    return (
        select(Article)
        .where(
            or_(
                Article.status == "collected",
                and_(
                    Article.status == "failed",
                    Article.attempt_count < settings.ai_max_attempts,
                ),
            )
        )
        .order_by(
            Article.attempt_count.asc(),
            Article.last_attempt_at.asc().nullsfirst(),
            Article.collected_at.asc(),
        )
        .limit(limit)
    )


def last_batch_stopped_by_circuit_breaker() -> bool:
    """Whether the most recent processing batch was halted by the circuit breaker."""
    return _circuit_breaker_tripped


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


async def _hydrate_article_body(article: Article) -> None:
    """Extract the full article body for an article that is about to be analyzed.

    Ingestion stores RSS metadata only, so full-text extraction happens here, once
    per article that actually reaches the AI queue instead of once per feed entry.
    A failed extraction is not fatal: the stored RSS summary is analyzed instead.
    """
    if article.text_source == "full_body" or not settings.require_full_text_for_ai:
        return
    target_url = article.canonical_url or article.original_url
    if not target_url:
        return
    full_text = await fetch_article_full_text(target_url)
    if full_text and len(full_text) > len(article.original_text or ""):
        article.original_text = full_text
        article.text_source = "full_body"
        logger.info("Extracted full body for article ID %s (%d chars).", article.id, len(full_text))


async def process_collected_articles(
    session: AsyncSession,
    limit: int = 50,
    provider: BaseAIProvider | None = None,
) -> int:
    """Process unanalyzed articles from database with AI Provider.

    Strict financial guardrail (ISS-013 / Re-processing Prevention):
    ONLY queries articles with status 'collected', plus 'failed' articles that
    still have retry attempts left. Articles with status IN
    ('summarized', 'rejected', 'flagged_for_review') and exhausted failures are
    STRICTLY EXCLUDED, preventing redundant LLM calls and unnecessary costs.

    Durability: every article is committed on its own, so a cancelled or
    timed-out run keeps all the work it already finished.

    Resilience: transient provider errors (503/429/timeouts) mark the article
    'failed' with an incremented ``attempt_count`` so a later run retries it,
    and ``ai_max_consecutive_failures`` consecutive transient errors stop the
    batch leaving the remaining queue untouched.
    """
    global _circuit_breaker_tripped
    _circuit_breaker_tripped = False

    res = await session.scalars(build_processing_query(limit))
    articles_list = res.all() if hasattr(res, "all") else list(res)
    if asyncio.iscoroutine(articles_list):
        articles_list = await articles_list

    if not articles_list:
        logger.info("No unanalyzed articles found. Skipping AI processing (Gemini calls = 0).")
        return 0

    ai_provider = provider or get_ai_provider()
    processed_count = 0
    consecutive_failures = 0
    immediate_published = 0

    # Cache source names to minimize DB roundtrips
    sources_stmt = select(Source.id, Source.name)
    s_res = await session.execute(sources_stmt)
    s_rows = s_res.all() if hasattr(s_res, "all") else list(s_res)
    if asyncio.iscoroutine(s_rows):
        s_rows = await s_rows
    sources_map = dict(s_rows)

    for article in articles_list:
        source_name = sources_map.get(article.source_id, "Unknown Source")
        article.attempt_count = (article.attempt_count or 0) + 1
        article.last_attempt_at = datetime.now(UTC)
        immediate_analysis: ImmediateAnalysis | None = None
        immediate_admin_only = False

        await _hydrate_article_body(article)

        try:
            result = await analyze_article(
                title=article.title,
                body=article.original_text or "",
                source_name=source_name,
                provider=ai_provider,
            )
        except Exception as exc:
            if is_retryable_ai_error(exc):
                article.status = "failed"
                consecutive_failures += 1
                logger.warning(
                    "Transient AI failure for article ID %d (%s), attempt %d/%d; "
                    "it stays queued for a later run: %s",
                    article.id,
                    article.title,
                    article.attempt_count,
                    settings.ai_max_attempts,
                    exc,
                )
                await session.commit()
                if consecutive_failures >= settings.ai_max_consecutive_failures:
                    _circuit_breaker_tripped = True
                    logger.error(
                        "Circuit breaker tripped after %d consecutive transient AI failures; "
                        "stopping this batch and leaving the rest of the queue untouched.",
                        consecutive_failures,
                    )
                    break
                continue

            logger.error("AI analysis failed for article ID %d (%s): %s", article.id, article.title, exc)
            article.status = "failed"
            processed_count += 1
            await session.commit()
            continue

        consecutive_failures = 0
        payload_dict = result.model_dump(mode="json")
        payload_dict["model_name"] = ai_provider.model_name

        # The channel now covers several briefings, not only the Middle East: a story
        # is accepted when it is the primary subject of a configured topic. Without a
        # topic it falls back to the audited regional rule below, unchanged.
        topic = match_primary_topic(
            result.countries,
            result.actors,
            result.location,
            result.event_title or article.title,
        )
        payload_dict["topic_key"] = topic.key if topic else None
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
        elif topic is None and (not result.is_relevant or not has_middle_east_nexus(result.countries, result.location)):
            if result.is_relevant:
                logger.info(
                    "Article ID %d rejected by the editorial gate: no Middle East country or waterway in countries=%s location=%r.",
                    article.id,
                    result.countries,
                    result.location,
                )
            else:
                logger.info("Article ID %d deemed irrelevant to Middle East intelligence. Marked 'rejected'.", article.id)
            article.status = "rejected"
        elif topic is None and not is_primary_actor(result.countries, result.actors, result.location):
            logger.info(
                "Article ID %d rejected by the editorial gate: Middle East is only a passing mention "
                "(countries=%s actors=%s location=%r).",
                article.id,
                result.countries,
                result.actors,
                result.location,
            )
            article.status = "rejected"
        else:
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
            await session.flush()

            source_count = await _count_event_sources(session, match_res.event_id)
            verification_status = resolve_verification_status(result.verification_status, source_count)
            payload_dict["verification_status_resolved"] = verification_status

            if source_count >= 2:
                await _refresh_event_perspectives(
                    session=session,
                    event_id=match_res.event_id,
                    event_title=result.event_title or article.title,
                    provider=ai_provider,
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
                            "topic": topic.key if topic else None,
                            "sources": [{
                                "source": source_name,
                                "url": article.original_url,
                                "article_id": article.id,
                                "added_at": (article.collected_at or datetime.now(UTC)).isoformat(),
                            }],
                        },
                                            verification_status=verification_status,
                    model_name=ai_provider.model_name,
                    prompt_version=PROMPT_VERSION,
                )
            )
            else:
                # A second source corroborates an existing event: refresh its label.
                await session.execute(
                    update(Summary)
                    .where(Summary.event_id == match_res.event_id)
                    .values(verification_status=verification_status)
                )

            article.status = "summarized"

            should_publish = match_res.is_new_event or not settings.immediate_publish_only_new_events
            if should_publish and result.importance >= settings.immediate_publish_min_importance:
                immediate_analysis = ImmediateAnalysis.from_analysis_result(
                    result, source_name, article.original_url
                )
                immediate_analysis.verification_status = verification_status
                # High-impact single-source stories go to the admin chat only (fake-news guard).
                immediate_admin_only = result.importance >= 4 and source_count < 2

        processed_count += 1
        await session.commit()

        if immediate_analysis is not None:
            if immediate_published < settings.immediate_publish_max_per_run:
                await _publish_immediate_safely(immediate_analysis, admin_only=immediate_admin_only)
                immediate_published += 1
            else:
                logger.info(
                    "Immediate publish cap (%d) reached; article ID %d stays for the daily report only.",
                    settings.immediate_publish_max_per_run,
                    article.id,
                )

    logger.info("Successfully processed %d articles via %s.", processed_count, ai_provider.model_name)
    return processed_count


async def collect_new_articles(session: AsyncSession) -> int:
    """Fetch every active feed and insert new, non-duplicate articles.

    Performs no AI work and leaves the transaction to the caller, so ingestion
    keeps running even when the AI models are unavailable or the queue is empty.
    Feed parsing is metadata-only: extracting full bodies during collection meant
    hundreds of page fetches per run for entries that never reached the AI queue,
    so bodies are now extracted lazily by ``_hydrate_article_body``.
    Returns the number of newly inserted articles.
    """
    inserted_count = 0
    sources = (
        await session.scalars(select(Source).where(Source.is_active.is_(True), Source.feed_url.is_not(None)))
    ).all()
    for source in sources:
        try:
            entries = await fetch_feed(
                source.feed_url,
                extract_full_text=False,
                min_summary_length=0,
            )
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
            if last_batch_stopped_by_circuit_breaker():
                log.status = "stopped_unavailable"
            else:
                log.status = "success"
            log.processed_count = count
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

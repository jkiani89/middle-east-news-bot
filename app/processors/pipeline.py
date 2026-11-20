import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, distinct, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.number_check import safe_check_numbers
from app.ai.persian_lint import lint_persian_output
from app.ai.provider import BaseAIProvider, get_ai_provider, is_retryable_ai_error
from app.ai.summarizer import (
    MODEL_BODY_CHAR_LIMIT,
    PROMPT_VERSION,
    NewsAnalysis,
    analyze_article,
    analyze_perspectives,
)
from app.collectors.rss_collector import fetch_article_full_text, fetch_feed
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, ArticleEvent, Event, JobLog, Source, Summary
from app.editorial.content_quality import classify_content_quality
from app.editorial.glossary import canonicalize_approved_names
from app.editorial.liveblog import is_live_digest
from app.editorial.perspectives import check_perspectives_gate, load_orientations, orientation_for
from app.editorial.prefilter import prefilter_analysis
from app.editorial.regions import has_middle_east_nexus, is_primary_actor
from app.editorial.topics import match_topics
from app.processors.event_matcher import EventMatcher
from app.telegram.publisher import (
    ImmediateAnalysis,
    publish_flag_alert,
    publish_immediate_analysis,
)

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


YEMEN_TOPIC_KEY = "ye"

# The Yemen war has three sides the reader named explicitly; when one of them has no
# excerpt, the silence line must still name it in those words rather than a generic label.
YEMEN_REQUIRED_SIDES: dict[str, str] = {
    "houthi": "انصارالله (حوثی)",
    "saudi": "رسانه‌های رسمی عربستان (سعودی)",
    "yemen_gov": "دولت یمن (یمن گو)",
}


async def _refresh_event_perspectives(
    session: AsyncSession,
    event_id: int,
    event_title: str,
    provider: BaseAIProvider,
    topic_key: str | None = None,
    topic_fa: str | None = None,
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
                select(
                    Article.id,
                    Article.title,
                    Article.original_text,
                    Source.name,
                    Article.ai_payload,
                    Article.canonical_url,
                    Article.original_url,
                )
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
    chosen: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(row) < 4:
            continue
        _article_id, title, text, source_name = row[:4]
        payload = row[4] if len(row) > 4 and isinstance(row[4], dict) else {}
        can_u = row[5] if len(row) > 5 else None
        orig_u = row[6] if len(row) > 6 else None

        if payload.get("is_live_digest") or is_live_digest(can_u or orig_u, title):
            continue
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
            "actors": payload.get("actors") or [],
            "location": payload.get("location") or "",
        }

    logger.info(
        "Event #%d coverage before the matrix: %d linked article(s), camps=%s.",
        event_id,
        len(rows),
        sorted(chosen),
    )
    if len(chosen) < 2:
        logger.info(
            "Event #%d stays without a perspective matrix: one editorial camp only (%s).",
            event_id,
            sorted(chosen),
        )
        return False

    # Phase Y-13: Perspectives Gate — Require shared non-country actors/locations
    has_any_actor_data = any(bool(art.get("actors") or art.get("location")) for art in chosen.values())
    passed_gate, shared_entities = check_perspectives_gate(
        list(chosen.values()),
        min_shared_actors=settings.perspective_min_shared_actors,
    )
    if not passed_gate:
        reason = "insufficient_data" if not has_any_actor_data else "disjoint_entities"
        logger.info(
            "Event #%d rejected by perspective gate (reason=%s): camps %s share no non-country actor or city.",
            event_id,
            reason,
            sorted(chosen),
        )
        return False

    summary = None
    try:
        res = await session.scalars(select(Summary).where(Summary.event_id == event_id))
        if hasattr(res, "first"):
            summary = res.first()
        elif hasattr(res, "all"):
            items = res.all()
            summary = items[0] if items else None
    except Exception:
        logger.exception("Could not load summary for event #%d for perspectives.", event_id)
        return False

    if summary is None:
        return False

    details = dict(summary.details_fa or {})
    # Phase Y-13: Live digests never receive a perspective matrix
    if details.get("is_live_digest") or details.get("article_kind") == "live_digest":
        logger.info("Event #%d is a live digest; skipping perspective matrix.", event_id)
        return False

    covered = sorted(chosen)
    if sorted(details.get("perspectives_orientations") or []) == covered and details.get("perspectives"):
        return False

    try:
        result = await analyze_perspectives(
            event_title=event_title,
            articles=list(chosen.values()),
            provider=provider,
            max_items=settings.max_perspective_articles,
            topic_fa=topic_fa,
        )
    except Exception:
        # The matrix is an enrichment: a model failure must never lose the event itself.
        logger.exception(
            "Perspective comparison failed for event #%d; the summary stays without a matrix.",
            event_id,
        )
        return False

    if not result.perspectives:
        logger.warning(
            "Perspective comparison for event #%d returned no usable claim from %s; "
            "the summary keeps its single voice.",
            event_id,
            covered,
        )
        return False

    # Silence is derived from the data, never guessed by the model: an absent camp has
    # no excerpt to reason about, so the only verifiable statement is that it did not
    # cover this event inside the window.
    absent_pairs: list[tuple[str, str]] = [
        (
            orientation.key,
            f"{orientation.label_fa} ({'، '.join(orientation.sources[:3])})"
            if orientation.sources
            else orientation.label_fa,
        )
        for orientation in load_orientations()
        if orientation.key not in chosen
    ]
    if topic_key == YEMEN_TOPIC_KEY:
        # Name the three sides in the reader's own terms instead of their registry labels.
        absent_pairs = [
            (key, text) for key, text in absent_pairs if key not in YEMEN_REQUIRED_SIDES
        ]
        absent_pairs += [
            (key, label)
            for key, label in YEMEN_REQUIRED_SIDES.items()
            if key not in chosen
        ]
    # A camp with no excerpt is a fact and comes first; the model's own list follows it.
    # For Yemen the three named sides must never be pushed out of the line by the cap.
    required = [text for key, text in absent_pairs if key in YEMEN_REQUIRED_SIDES]
    others = [text for key, text in absent_pairs if key not in YEMEN_REQUIRED_SIDES]
    limit = settings.max_perspective_articles
    if topic_key == YEMEN_TOPIC_KEY:
        limit = max(limit, len(YEMEN_REQUIRED_SIDES) + 1)
    silent = list(dict.fromkeys([*required, *others, *result.silent_parties_fa]))[:limit]

    details["perspectives"] = [
        {
            **item.model_dump(mode="json"),
            "claim_fa": canonicalize_approved_names(item.claim_fa),
        }
        for item in result.perspectives
    ]
    details["perspectives_orientations"] = covered
    details["disagreement_fa"] = canonicalize_approved_names(result.disagreement_fa)
    details["silent_parties_fa"] = silent
    details["shared_actors"] = sorted(shared_entities)
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


def article_age_hours(
    published_at: datetime | None,
    collected_at: datetime | None = None,
    now: datetime | None = None,
) -> float:
    """Age of an article in hours, from publication time when it is known (Phase Y-9).

    ``COALESCE(published_at, collected_at)`` is the reference: a stale publication
    date keeps governing even when the row was collected minutes ago (a feed that
    re-serves week-old stories), while a future ``published_at`` (a broken feed
    clock) counts as age 0 instead of a negative age.
    """
    moment = now or datetime.now(UTC)
    reference = published_at or collected_at
    if reference is None:
        return 0.0
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0.0, (moment - reference).total_seconds() / 3600.0)


def article_reference_at():
    """SQL reference timestamp for freshness: publication time, else collection time."""
    return func.coalesce(Article.published_at, Article.collected_at)


def freshness_cutoff(now: datetime | None = None) -> datetime:
    """The oldest reference timestamp still worth an AI call."""
    return (now or datetime.now(UTC)) - timedelta(hours=settings.article_max_age_hours)


async def expire_stale_collected(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> tuple[int, float | None]:
    """Park collected articles past the age limit as ``expired`` (one statement).

    Returns ``(expired_count, oldest_age_hours)``. The write is a single UPDATE of
    ``status`` only, so it is reversible and never loses the article. A dry run only
    measures the queue and writes nothing.

    Y-12: ``oldest_age_h`` now reflects the *oldest* (minimum reference timestamp)
    row across the full ``articles`` table where ``status='collected'``, not the
    newest. ``func.min`` gives us the article furthest back in time; ``func.max``
    would give the most-recently-collected row (age ≈ 0), which is the bug that
    caused the log to show ``oldest_age_h=0.1`` while 36-hour-old rows sat in the DB.
    """
    moment = now or datetime.now(UTC)
    cutoff = freshness_cutoff(moment)
    reference = article_reference_at()

    # Y-12 fix: MIN of the reference timestamp across the whole collected queue
    # gives the article that has been waiting longest (highest age).
    oldest = await session.scalar(
        select(func.min(reference)).where(Article.status == "collected")
    )
    oldest_age: float | None = None
    if isinstance(oldest, datetime):
        oldest_age = round(article_age_hours(oldest, oldest, moment), 1)

    if dry_run:
        return 0, oldest_age

    result = await session.execute(
        update(Article)
        .where(Article.status == "collected", reference < cutoff)
        .values(status="expired")
    )
    rowcount = getattr(result, "rowcount", 0)
    return (rowcount if isinstance(rowcount, int) and rowcount > 0 else 0), oldest_age


def build_processing_query(limit: int):
    """Select articles eligible for AI analysis.

    Y-12: Gives failed/retry articles priority over fresh articles to prevent
    Retry Starvation.

    When new articles (``collected``, ``attempt_count=0``) exceed batch capacity,
    a simple ``attempt_count ASC`` sort puts all new articles first and starves
    transiently failed articles (``failed``, ``attempt_count >= 1``) until they
    age out and expire.

    By ordering with ``CASE WHEN status = 'failed' THEN 0 ELSE 1 END ASC``,
    any retryable failed articles are served first before fresh articles fill
    the remainder of the batch, guaranteeing slots for retries.
    Within each group, articles drain oldest-first via ``reference ASC`` so that
    the queue is FIFO: a story that has been waiting the longest is analysed (or
    rejected) before a newer one, which is what keeps it from silently reaching
    ``article_max_age_hours`` and being parked as ``expired`` without an AI call.
    """
    reference = article_reference_at()
    return (
        select(Article)
        .where(
            or_(
                Article.status == "collected",
                and_(
                    Article.status == "failed",
                    Article.attempt_count < settings.ai_max_attempts,
                ),
            ),
            reference >= freshness_cutoff(),
        )
        .order_by(
            case((Article.status == "failed", 0), else_=1).asc(),
            reference.asc(),
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

    The fetch is capped by ``article_body_fetch_timeout_seconds`` (6s by default) so a
    slow or hanging publisher cannot occupy a worker slot for the retry chain of the
    HTTP client; on timeout the article falls back to its stored RSS summary.
    """
    if article.text_source == "full_body" or not settings.require_full_text_for_ai:
        return
    target_url = article.canonical_url or article.original_url
    if not target_url:
        return
    try:
        full_text = await asyncio.wait_for(
            fetch_article_full_text(target_url),
            timeout=settings.article_body_fetch_timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "Full-text extraction for article ID %s exceeded %.1fs; analyzing the RSS summary instead.",
            article.id,
            settings.article_body_fetch_timeout_seconds,
        )
        return
    if full_text and len(full_text) > len(article.original_text or ""):
        article.original_text = full_text
        article.text_source = "full_body"
        logger.info("Extracted full body for article ID %s (%d chars).", article.id, len(full_text))


@dataclass(slots=True)
class _AnalysisAttempt:
    """The network half of one article, finished off the batch's critical path."""

    article: Article
    source_name: str
    prefilter: dict[str, object] | None = None
    result: NewsAnalysis | None = None
    error: Exception | None = None
    transient: bool = False


class _ConcurrentAnalysis:
    """Hydrate and analyze articles concurrently, hand the results over in queue order.

    The slow half of a batch is network wait (full-text fetch plus one model call per
    article), so up to ``ai_processing_concurrency`` (4) tasks run it in parallel.

    The database half is deliberately *not* concurrent: an ``AsyncSession`` must never
    be used by two tasks at once, and the editorial rules are order-dependent (an
    article's event matching sees the events created by the articles before it). Every
    database step therefore stays in the caller's single loop, which receives one
    finished attempt per iteration, in the same order the queue returned them.

    The circuit breaker keeps its exact meaning: the pool may only hold
    ``ai_max_consecutive_failures - transient_failures`` requests, so a batch that
    trips after N consecutive failures has spent exactly N model calls and the rest of
    the queue is never touched.
    """

    def __init__(
        self,
        articles: list[Article],
        provider: BaseAIProvider,
        sources_map: dict[int, str],
    ) -> None:
        self._articles = articles
        self._provider = provider
        self._sources_map = sources_map
        self._concurrency = max(1, settings.ai_processing_concurrency)
        self._failure_budget = max(1, settings.ai_max_consecutive_failures)
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._tasks: dict[int, asyncio.Task[_AnalysisAttempt]] = {}
        self._next_index = 0
        self._consume_index = 0

    async def _attempt(self, article: Article) -> _AnalysisAttempt:
        attempt = _AnalysisAttempt(
            article=article,
            source_name=self._sources_map.get(article.source_id, "Unknown Source"),
        )
        async with self._semaphore:
            try:
                await _hydrate_article_body(article)
                # Phase Y-11: cheap, log-only relevance check over exactly the text the
                # model is about to see. It never rejects, changes a status or calls the model.
                attempt.prefilter = prefilter_analysis(
                    article.title or "", (article.original_text or "")[:MODEL_BODY_CHAR_LIMIT]
                )
                attempt.result = await analyze_article(
                    title=article.title,
                    body=article.original_text or "",
                    source_name=attempt.source_name,
                    provider=self._provider,
                )
            except Exception as exc:  # noqa: BLE001 - the caller owns status handling
                attempt.error = exc
                attempt.transient = is_retryable_ai_error(exc)
        return attempt

    def _dispatch(self, transient_failures: int) -> None:
        while (
            self._next_index < len(self._articles)
            and len(self._tasks) + transient_failures < self._failure_budget
            and len(self._tasks) < self._concurrency
        ):
            index = self._next_index
            self._tasks[index] = asyncio.create_task(self._attempt(self._articles[index]))
            self._next_index += 1

    async def next_attempt(self, transient_failures: int) -> _AnalysisAttempt | None:
        """Await the next article's attempt, keeping the pool full within the breaker budget."""
        self._dispatch(transient_failures)
        task = self._tasks.pop(self._consume_index, None)
        if task is None:
            return None
        self._consume_index += 1
        return await task

    async def aclose(self) -> None:
        """Cancel anything still in flight so an aborted batch leaves no orphaned request."""
        pending = list(self._tasks.values())
        self._tasks.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def process_collected_articles(
    session: AsyncSession,
    limit: int = 50,
    provider: BaseAIProvider | None = None,
    queue_stats: dict[str, object] | None = None,
) -> int:
    """Process unanalyzed articles from database with AI Provider.

    Strict financial guardrail (ISS-013 / Re-processing Prevention):
    ONLY queries articles with status 'collected', plus 'failed' articles that
    still have retry attempts left. Articles with status IN
    ('summarized', 'rejected', 'flagged_for_review') and exhausted failures are
    STRICTLY EXCLUDED, preventing redundant LLM calls and unnecessary costs.

    Durability: every article is committed on its own, so a cancelled or
    timed-out run keeps all the work it already finished.

    Speed: full-text extraction and the model call run up to
    ``ai_processing_concurrency`` articles at a time, while every database write
    (event matching, summaries, commits) still happens serially on this session.

    Resilience: transient provider errors (503/429/timeouts) mark the article
    'failed' with an incremented ``attempt_count`` so a later run retries it,
    and ``ai_max_consecutive_failures`` consecutive transient errors stop the
    batch leaving the remaining queue untouched.
    """
    global _circuit_breaker_tripped
    _circuit_breaker_tripped = False

    # Phase Y-9 (A4): drain the stale tail of the queue before selecting anything. A
    # dry run performs the same measurement but writes nothing.
    expired_now, oldest_age = await expire_stale_collected(
        session, dry_run=settings.immediate_publish_dry_run
    )
    if queue_stats is not None:
        queue_stats["expired_now"] = expired_now
        queue_stats["oldest_age_h"] = oldest_age
    if expired_now:
        logger.info(
            "Expired %d collected article(s) older than %d hours; they will not reach the model.",
            expired_now,
            settings.article_max_age_hours,
        )

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
    # Phase Z: this run's parked articles, for a single admin alert at the end.
    flag_alerts: list[dict[str, object]] = []

    # Cache source names to minimize DB roundtrips
    sources_stmt = select(Source.id, Source.name)
    s_res = await session.execute(sources_stmt)
    s_rows = s_res.all() if hasattr(s_res, "all") else list(s_res)
    if asyncio.iscoroutine(s_rows):
        s_rows = await s_rows
    sources_map = dict(s_rows)

    analysis = _ConcurrentAnalysis(articles_list, ai_provider, sources_map)
    for _ in range(len(articles_list)):
        attempt = await analysis.next_attempt(consecutive_failures)
        if attempt is None:
            break

        article = attempt.article
        source_name = attempt.source_name
        # Attempt bookkeeping stays in the serial half: a request the breaker cancels
        # must not consume one of the article's remaining retries.
        article.attempt_count = (article.attempt_count or 0) + 1
        article.last_attempt_at = datetime.now(UTC)
        immediate_analysis: ImmediateAnalysis | None = None
        immediate_admin_only = False

        if attempt.error is not None:
            exc = attempt.error
            if attempt.transient:
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

        result = attempt.result
        if result is None:
            # Defensive: a worker returns either a result or an error. A silent gap must
            # never leave the article looking analyzed.
            logger.error("Analysis of article ID %d produced no result; marking it failed.", article.id)
            article.status = "failed"
            processed_count += 1
            await session.commit()
            continue

        # Phase Y-11 prefilter, computed by the worker over exactly the text the model saw.
        prefilter = attempt.prefilter or {}
        consecutive_failures = 0
        payload_dict = result.model_dump(mode="json")
        payload_dict["model_name"] = ai_provider.model_name

        # Phase Y-13: Liveblog / Rolling Digest detection
        is_digest = is_live_digest(article.canonical_url or article.original_url, article.title)
        article_kind = "live_digest" if is_digest else "standard"
        payload_dict["article_kind"] = article_kind
        payload_dict["is_live_digest"] = is_digest
        if is_digest:
            result.importance = min(result.importance, getattr(settings, "liveblog_max_importance", 3))
            payload_dict["importance"] = result.importance

        # Phase Y-13: Closed-set canonicalization of approved names
        result.headline_fa = canonicalize_approved_names(result.headline_fa)
        result.summary_fa = canonicalize_approved_names(result.summary_fa)
        if result.event_title:
            result.event_title = canonicalize_approved_names(result.event_title)
        if result.actors:
            result.actors = [canonicalize_approved_names(a) for a in result.actors]
        if result.countries:
            result.countries = [canonicalize_approved_names(c) for c in result.countries]
        payload_dict["headline_fa"] = result.headline_fa
        payload_dict["summary_fa"] = result.summary_fa
        payload_dict["event_title"] = result.event_title
        payload_dict["actors"] = result.actors
        payload_dict["countries"] = result.countries

        # Phase Y-1: every number in the Persian output must be grounded in the exact
        # text the model saw (title + body truncated by the prompt builder). A checker
        # failure is contained (Y-1b) so one odd article cannot abort the batch.
        number_check = safe_check_numbers(
            headline=result.headline_fa,
            summary=result.summary_fa,
            source_title=article.title or "",
            source_body=article.original_text or "",
            mode=settings.number_check_mode,
        )
        payload_dict["number_check"] = number_check
        # Phase Y-8: log-only watchlist for Arabic-flavoured Persian words. It never
        # changes the status or blocks a publication.
        payload_dict["persian_lint"] = lint_persian_output(
            result.headline_fa, result.summary_fa
        )
        payload_dict["prefilter"] = prefilter
        content_quality = classify_content_quality(
            article.title, article.original_text, article.text_source
        )
        payload_dict["content_quality"] = content_quality
        if result.prompt_meta:
            payload_dict["normalization"] = result.prompt_meta.get("normalization")
            payload_dict["glossary"] = result.prompt_meta.get("glossary")

        # The channel now covers several briefings, not only the Middle East: a story
        # is accepted when it is the primary subject of a configured topic. Without a
        # topic it falls back to the audited regional rule below, unchanged.
        topics = match_topics(
            result.countries,
            result.actors,
            result.location,
            result.event_title or article.title,
        )
        topic = topics[0] if topics else None
        payload_dict["topic_key"] = topic.key if topic else None
        payload_dict["topic_keys"] = [t.key for t in topics]
        article.ai_payload = payload_dict

        if result.is_injection_attempt:
            logger.warning(
                "Security Alert: Article ID %d contains potential prompt injection. "
                "Setting status to 'flagged_for_review' (preserving intelligence for human review).",
                article.id,
            )
            article.status = "flagged_for_review"
            flag_alerts.append(
                {
                    "article_id": article.id,
                    "reason": "injection_detected",
                    "headline_fa": result.headline_fa,
                    "source_name": source_name,
                    "source_url": article.original_url,
                    "numbers": [],
                }
            )
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
        elif settings.number_check_mode == "flag" and number_check["blocking"]:
            logger.warning(
                "Number grounding check failed for article ID %d (%d ungrounded number(s): %s); "
                "status set to 'flagged_for_review' and automatic publishing is blocked.",
                article.id,
                len(number_check["unmatched"]),
                ", ".join(item["value"] for item in number_check["unmatched"]),
            )
            article.status = "flagged_for_review"
            flag_alerts.append(
                {
                    "article_id": article.id,
                    "reason": "number_mismatch",
                    "headline_fa": result.headline_fa,
                    "source_name": source_name,
                    "source_url": article.original_url,
                    "numbers": [item["value"] for item in number_check["unmatched"]],
                }
            )
            # Intelligence is preserved like the injection path, but the event is parked
            # with status='flagged' so EventMatcher never merges a later article into it.
            event = Event(
                title=f"[FLAGGED: NUM] {result.event_title}",
                event_type=result.categories[0] if result.categories else "GEN",
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
                    details_fa={
                        "source": source_name,
                        "url": article.original_url,
                        "flag": "number_mismatch",
                        "number_check": number_check,
                    },
                    verification_status="unconfirmed",
                    model_name=ai_provider.model_name,
                    prompt_version=PROMPT_VERSION,
                )
            )
        elif not topics and (not result.is_relevant or not has_middle_east_nexus(result.countries, result.location)):
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
        elif not topics and not is_primary_actor(result.countries, result.actors, result.location):
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
                    topic_key=topic.key if topic else None,
                    topic_fa=topic.name_fa if topic else None,
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
                            "topics": [t.key for t in topics],
                            "content_quality": content_quality,
                            "article_kind": payload_dict.get("article_kind", "standard"),
                            "is_live_digest": is_digest,
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
                if content_quality:
                    immediate_analysis.content_quality = content_quality.get("status", "full_text")
                    immediate_analysis.content_quality_fa = content_quality.get("label_fa", "متن کامل")
                    immediate_analysis.is_insufficient = content_quality.get("is_insufficient", False)
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

    await analysis.aclose()

    if flag_alerts:
        try:
            await publish_flag_alert(flag_alerts, total=len(flag_alerts))
        except Exception:  # noqa: BLE001 - the alert is a convenience, never a gate
            logger.exception("Failed to send the flag alert; the batch continues.")

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

            # Phase Y-9 (A2): a story past the age limit is stored as 'expired' rather
            # than skipped, so the dedup indexes refuse to insert it on the next run.
            insert_age = article_age_hours(item.get("published_at"))
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
                status="expired" if insert_age > settings.article_max_age_hours else "collected",
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
            inserted = await collect_new_articles(session)
            # Commit the collected rows before any AI call. Collection must survive a
            # processing failure: the except block below rolls the session back, and
            # without this commit every article ingested in the same run would be lost
            # with it (the next run would see it as never collected).
            await session.commit()

            # Process collected articles adhering strictly to status filtering
            queue_stats: dict[str, object] = {}
            count = await process_collected_articles(
                session,
                limit=settings.max_articles_per_batch,
                provider=provider,
                queue_stats=queue_stats,
            )
            # Phase Y-9 (A5): one line per run makes the queue measurable.
            pending = await session.scalar(
                select(func.count(Article.id)).where(Article.status == "collected")
            )
            oldest_age = queue_stats.get("oldest_age_h")
            logger.info(
                "Queue: inserted=%d collected=%d expired_now=%d oldest_age_h=%s",
                inserted,
                pending if isinstance(pending, int) else 0,
                queue_stats.get("expired_now", 0),
                f"{oldest_age:.1f}" if isinstance(oldest_age, (int, float)) else "—",
            )
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

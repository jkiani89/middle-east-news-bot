"""Script to reprocess collected articles with real Google Gemini AI.

Transfers database state from mock/test summaries to real Gemini-generated intelligence:
1. Queries articles that currently have mock payloads (or all with --all / --force).
2. Analyzes each article using real GeminiProvider with rate limiting and exponential retry.
3. Gracefully handles 429 RESOURCE_EXHAUSTED / quota exhaustion, saving partial progress.
4. Performs real Event Matching and Clustering (Phase 5).
5. Generates real Daily Intelligence Report (Phase 6) with content_quality = "real".
6. Computes authentic False Positive rate and updated 95% Confidence Interval.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import AIRateLimitError, GeminiProvider
from app.ai.summarizer import PROMPT_VERSION, analyze_article
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, ArticleEvent, DailyReport, Event, Source, Summary
from app.processors.event_matcher import EventMatcher
from app.reports.daily_report import generate_and_save_daily_report, is_summary_mock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reprocess_real_gemini")


async def reprocess_articles(
    limit: int | None = None,
    force_all: bool = False,
    delay_seconds: float = 6.0,
    dry_run: bool = False,
):
    print("=" * 80)
    print("  REPROCESS ARTICLES WITH REAL GOOGLE GEMINI AI")
    print("=" * 80)
    print(f"Model: {settings.gemini_model}")
    print(f"Delay between calls: {delay_seconds}s")
    print(f"Dry run: {dry_run}")
    print(f"Force all articles: {force_all}")

    if not settings.gemini_api_key or "YOUR_GEMINI_API_KEY" in settings.gemini_api_key:
        print("\n[ERROR] GEMINI_API_KEY is not configured in .env or environment!")
        sys.exit(1)

    provider = GeminiProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        max_rpm=10,
    )

    async with SessionLocal() as session:
        # Load all sources
        sources = (await session.execute(select(Source))).scalars().all()
        sources_map = {s.id: s.name for s in sources}

        # Query articles
        stmt = select(Article).order_by(Article.id.asc())
        articles: list[Article] = list((await session.execute(stmt)).scalars().all())
        print(f"\nTotal articles in database: {len(articles)}")

        # Filter articles to process
        to_process: list[Article] = []
        for art in articles:
            if force_all:
                to_process.append(art)
                continue

            # Check if current payload is mock
            payload = art.ai_payload or {}
            event_title = payload.get("event_title", "")
            if event_title == "Regional Diplomatic Activity" or not payload:
                to_process.append(art)
            else:
                logger.info("Article #%d already has real processing ('%s'). Skipping.", art.id, event_title)

        if limit:
            to_process = to_process[:limit]

        print(f"Articles selected for Gemini processing: {len(to_process)}")
        if not to_process:
            print("No articles need reprocessing. All articles have real AI analysis.")
            return

        if dry_run:
            print("\n[DRY RUN] Would process the following articles:")
            for art in to_process:
                print(f"  - Article #{art.id}: {art.title[:60]}")
            return

        successful_count = 0
        fp_count = 0
        rate_limited = False

        for idx, article in enumerate(to_process, start=1):
            source_name = sources_map.get(article.source_id, "UN News")
            body_text = article.original_text or article.title

            print(f"\n[{idx}/{len(to_process)}] Calling Gemini for Article #{article.id}...")
            print(f"  Title: {article.title[:70]}")
            print(f"  Body length: {len(body_text)} chars")

            start_t = time.perf_counter()
            try:
                result = await analyze_article(
                    title=article.title,
                    body=body_text,
                    source_name=source_name,
                    provider=provider,
                )
                latency = time.perf_counter() - start_t
                print(f"  ✓ Gemini Success in {latency:.2f}s | Title: {result.event_title[:50]} | Cat: {result.categories}")
                print(f"    Headline FA: {result.headline_fa[:60]}")
                print(f"    Importance: {result.importance} | Injection Attempt: {result.is_injection_attempt}")

                article.ai_payload = result.model_dump(mode="json")
                if result.is_injection_attempt:
                    article.status = "flagged_for_review"
                    fp_count += 1
                elif not result.is_relevant:
                    article.status = "rejected"
                else:
                    article.status = "summarized"

                successful_count += 1
                await session.commit()

            except AIRateLimitError as rle:
                print(f"\n[ALERT] Rate limit / Quota reached: {rle}")
                print(f"Progress saved up to Article #{article.id}. You can resume later.")
                rate_limited = True
                break
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    print(f"\n[ALERT] Quota exhausted (429 RESOURCE_EXHAUSTED): {err_str}")
                    print(f"Progress saved up to Article #{article.id}. You can resume later.")
                    rate_limited = True
                    break
                else:
                    logger.error("Error analyzing Article #%d: %s", article.id, exc)

            if idx < len(to_process) and not rate_limited:
                await asyncio.sleep(delay_seconds)

        print("\n" + "=" * 80)
        print("  GEMINI PROCESSING BATCH COMPLETED")
        print("=" * 80)
        print(f"Total processed in this run: {successful_count} / {len(to_process)}")
        print(f"Potential prompt injection flags: {fp_count}")

        # If any articles were successfully processed, rebuild Events, Clustering & Daily Report
        if successful_count > 0:
            print("\nRe-clustering events with real metadata...")
            # Truncate existing events, article_events, summaries
            await session.execute(delete(DailyReport))
            await session.execute(delete(ArticleEvent))
            await session.execute(delete(Summary))
            await session.execute(delete(Event))
            await session.commit()

            matcher = EventMatcher(merge_threshold=0.70)
            all_articles = (await session.execute(select(Article).order_by(Article.id.asc()))).scalars().all()

            for art in all_articles:
                payload = art.ai_payload or {}
                if not payload or not payload.get("is_relevant", True):
                    continue
                match_res = await matcher.match_or_create_event(session, art, payload)
                if match_res.match_type == "initial":
                    summary = Summary(
                        event_id=match_res.event_id,
                        headline_fa=payload.get("headline_fa", art.title),
                        summary_fa=payload.get("summary_fa", art.title),
                        details_fa={"source": sources_map.get(art.source_id, "UN News"), "url": art.original_url},
                        verification_status=payload.get("verification_status", "confirmed"),
                        model_name=settings.gemini_model,
                        prompt_version=PROMPT_VERSION,
                    )
                    session.add(summary)
            await session.commit()

            # Re-generate Daily Report
            print("\nRe-generating Daily Intelligence Report...")
            rep_date, items, text = await generate_and_save_daily_report()
            print(f"Daily Report generated: {len(items)} items | Status: generated")

            # Check total real articles in DB now
            real_articles_count = sum(
                1 for a in all_articles
                if (a.ai_payload or {}).get("event_title") != "Regional Diplomatic Activity"
            )
            print("\n" + "=" * 80)
            print("  UPDATED FALSE POSITIVE STATISTICAL BENCHMARK")
            print("=" * 80)
            print(f"Real articles analyzed by Gemini in DB: {real_articles_count} / {len(all_articles)}")
            print(f"Real False Positives detected: {fp_count} / {real_articles_count}")
            if real_articles_count > 0:
                upper_bound = 3.0 / real_articles_count
                print(f"Rule of Three 95% CI Upper Bound: {upper_bound:.1%}")


def main():
    parser = argparse.ArgumentParser(description="Reprocess articles with real Google Gemini AI")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of articles to process")
    parser.add_argument("--force-all", action="store_true", help="Reprocess all articles, even if already real")
    parser.add_argument("--delay", type=float, default=6.0, help="Delay in seconds between Gemini API calls")
    parser.add_argument("--dry-run", action="store_true", help="Show which articles would be processed without calling API")
    args = parser.parse_args()

    asyncio.run(
        reprocess_articles(
            limit=args.limit,
            force_all=args.force_all,
            delay_seconds=args.delay,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()

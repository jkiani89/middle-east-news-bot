"""Live PostgreSQL verification of Phase 3 deduplication scenarios.

Tests both deduplication paths against the live PostgreSQL database:
- Scenario A: Same article with tracking query parameters (e.g. ?utm_source=twitter) -> Skipped
- Scenario B: Same content from another source/URL -> Skipped via content_hash
- Scenario C: Distinct news article -> Allowed
"""

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from app.database.connection import SessionLocal
from app.database.models import Article
from app.collectors.rss_collector import canonicalize_url, make_hash
from app.processors.pipeline import is_duplicate_article

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("phase3_dedup_verifier")


async def main():
    print("=" * 65)
    print("  Phase 3 Live Deduplication Verification against PostgreSQL")
    print("=" * 65)

    async with SessionLocal() as session:
        # 1. Fetch an existing article from DB
        existing = await session.scalar(select(Article).order_by(Article.id.asc()).limit(1))
        if not existing:
            print("ERROR: No articles in database. Please run scripts/fetch_live_un_news.py first.")
            return

        print(f"\n[Baseline Article from DB]:")
        print(f"  ID: {existing.id}")
        print(f"  Title: {existing.title}")
        print(f"  Canonical URL: {existing.canonical_url}")
        print(f"  Content Hash: {existing.content_hash}")

        # ---------------------------------------------------------
        # Scenario A: Modified URL with tracking query parameters
        # ---------------------------------------------------------
        print("\n--- Scenario A: Modified URL with tracking parameter ---")
        modified_url = f"{existing.canonical_url}?utm_source=twitter&utm_medium=social&utm_campaign=breaking_news&fbclid=XYZ123"
        canonical_a = canonicalize_url(modified_url)
        dummy_hash_a = "0000000000000000000000000000000000000000000000000000000000000000"

        print(f"  Incoming Raw URL: {modified_url}")
        print(f"  Canonicalized URL: {canonical_a}")
        print(f"  Dummy Hash (testing URL match only): {dummy_hash_a}")

        is_dup_a = await is_duplicate_article(session, canonical_a, dummy_hash_a)
        print(f"  Deduplication Check Result: is_duplicate = {is_dup_a}")
        if is_dup_a:
            print("  [PASS] Scenario A: Successfully detected duplicate via canonical_url -> SKIPPED")
        else:
            print("  [FAIL] Scenario A: Failed to detect duplicate URL")

        # ---------------------------------------------------------
        # Scenario B: Same text published under different URL
        # ---------------------------------------------------------
        print("\n--- Scenario B: Same text published under different URL ---")
        different_source_url = "https://www.aljazeera.com/news/2026/11/syndicated-coverage-reprint-9999"
        canonical_b = canonicalize_url(different_source_url)
        # Identical hash from the existing article
        matched_hash_b = existing.content_hash

        print(f"  Incoming Raw URL: {different_source_url}")
        print(f"  Canonicalized URL: {canonical_b}")
        print(f"  Content Hash (matching baseline): {matched_hash_b}")

        is_dup_b = await is_duplicate_article(session, canonical_b, matched_hash_b)
        print(f"  Deduplication Check Result: is_duplicate = {is_dup_b}")
        if is_dup_b:
            print("  [PASS] Scenario B: Successfully detected duplicate via content_hash -> SKIPPED")
        else:
            print("  [FAIL] Scenario B: Failed to detect duplicate content_hash")

        # ---------------------------------------------------------
        # Scenario C: Truly new article (different URL and Hash)
        # ---------------------------------------------------------
        print("\n--- Scenario C: Completely new article ---")
        new_url = "https://news.un.org/en/story/2026/11/unique-new-story-8888"
        new_canonical = canonicalize_url(new_url)
        new_hash = make_hash("Brand New Event Headline", "Entirely new story text about regional summit.")

        print(f"  Incoming URL: {new_url}")
        print(f"  Incoming Hash: {new_hash}")

        is_dup_c = await is_duplicate_article(session, new_canonical, new_hash)
        print(f"  Deduplication Check Result: is_duplicate = {is_dup_c}")
        if not is_dup_c:
            print("  [PASS] Scenario C: Correctly recognized as non-duplicate -> ALLOWED")
        else:
            print("  [FAIL] Scenario C: Incorrectly flagged novel article as duplicate")

    print("\n" + "=" * 65)
    print("  Phase 3 Live Deduplication Verification Complete!")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())

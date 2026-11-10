"""Verification script proving that summarized articles are strictly excluded from AI processing.

Addresses Requirement 1 (Financial / Re-processing Prevention - ISS-013).
"""

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("reprocessing_test")

from sqlalchemy import select, func
from app.database.connection import SessionLocal
from app.database.models import Article
from app.processors.pipeline import process_collected_articles
from app.ai.provider import MockAIProvider


async def run_verification():
    print("=" * 70)
    print("  VERIFICATION: Prevention of Redundant Summarization (Financial Guardrail)")
    print("=" * 70)

    async with SessionLocal() as session:
        # Step 0: Check current database state
        art1 = await session.get(Article, 1)
        print(f"\n[Baseline Status Check]:")
        print(f"  Article ID 1 Status: {art1.status if art1 else 'Not Found'}")

        total_collected = await session.scalar(
            select(func.count(Article.id)).where(Article.status == "collected")
        )
        total_summarized = await session.scalar(
            select(func.count(Article.id)).where(Article.status == "summarized")
        )
        print(f"  Total Articles with status='collected': {total_collected}")
        print(f"  Total Articles with status='summarized': {total_summarized}")

        # Step 1: Run process_collected_articles with a tracking MockAIProvider
        canned_response = {
            "is_relevant": True,
            "is_injection_attempt": False,
            "categories": ["DIP"],
            "event_title": "Regional Diplomatic Activity",
            "headline_fa": "رایزنی‌های دیپلماتیک منطقه‌ای",
            "summary_fa": "به گزارش منابع موثق، مذاکرات دیپلماتیک منطقه‌ای ادامه دارد.",
            "countries": ["منطقه"],
            "actors": ["نمایندگان دیپلماتیک"],
            "location": None,
            "importance": 3,
            "verification_status": "unconfirmed",
        }
        mock_provider = MockAIProvider(canned_response=canned_response, model="gemini-2.5-flash-tracker")

        print(f"\n--- Execution Run #1: Processing pending collected articles ---")
        processed_count_1 = await process_collected_articles(session, limit=100, provider=mock_provider)
        print(f"  Articles processed in Run #1: {processed_count_1}")
        print(f"  AI Provider Call Count in Run #1: {mock_provider.call_count}")

        # Check that Article 1 was NEVER touched/called again
        assert mock_provider.call_count == total_collected, (
            f"Expected calls to match pending collected ({total_collected}), got {mock_provider.call_count}"
        )

        # Step 2: Run process_collected_articles a SECOND TIME on the same DB
        print(f"\n--- Execution Run #2: Immediate re-run on fully summarized DB ---")
        mock_provider_2 = MockAIProvider(canned_response=canned_response, model="gemini-2.5-flash-tracker")
        processed_count_2 = await process_collected_articles(session, limit=100, provider=mock_provider_2)
        print(f"  Articles processed in Run #2: {processed_count_2}")
        print(f"  AI Provider Call Count in Run #2: {mock_provider_2.call_count}")

        # Final Verification Assertions
        assert processed_count_2 == 0, f"Expected 0 processed articles in Run #2, got {processed_count_2}"
        assert mock_provider_2.call_count == 0, f"CRITICAL LEAK: AI Provider was called {mock_provider_2.call_count} times on summarized articles!"

        print("\n" + "=" * 70)
        print("  SUCCESS: Re-processing prevention verified! (Run #2 Gemini calls = 0)")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_verification())

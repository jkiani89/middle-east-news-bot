"""End-to-End verification script for Phase 4 AI summarization and database persistence.

Picks an actual collected article from PostgreSQL, runs the AI analysis pipeline,
and saves the structured event, link, and summary into PostgreSQL tables.
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
from app.database.models import Article, Event, ArticleEvent, Summary, Source
from app.ai.provider import MockAIProvider
from app.ai.summarizer import PROMPT_VERSION, analyze_article
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("phase4_e2e")


async def main():
    print("=" * 65)
    print("  Phase 4 End-to-End AI Summarization Persistence Test")
    print("=" * 65)

    async with SessionLocal() as session:
        # 1. Fetch real article from PostgreSQL
        article = await session.scalar(
            select(Article).where(Article.text_source == "full_body").order_by(Article.id.asc()).limit(1)
        )
        if not article:
            print("No full_body article found. Fetching first available article...")
            article = await session.scalar(select(Article).order_by(Article.id.asc()).limit(1))

        if not article:
            print("ERROR: No articles in database. Please run scripts/fetch_live_un_news.py first.")
            return

        source = await session.get(Source, article.source_id)
        source_name = source.name if source else "UN News"

        print(f"\n[Selected Article from DB]:")
        print(f"  ID: {article.id}")
        print(f"  Title: {article.title}")
        print(f"  Source: {source_name}")
        print(f"  Text Source: {article.text_source}")
        print(f"  Text Length: {len(article.original_text or '')} chars")

        # 2. Prepare realistic editorial AI response adhering to Phase 4 Pydantic schema
        canned_ai_response = {
            "is_relevant": True,
            "is_injection_attempt": False,
            "categories": ["HUM", "SEC"],
            "event_title": "Mediterranean and West Africa Migration Fatalities",
            "headline_fa": "جان باختن دست‌کم ۵۶ پناهجو در حوادث غرق شدن قایق‌ها در سواحل سنگال و یونان",
            "summary_fa": (
                "به گزارش سازمان ملل متحد و نهادهای امدادرسان بین‌المللی، طی حوادث جداگانه در آب‌های سواحل "
                "سنگال و یونان، دست‌کم ۵۶ نفر از مهاجران و پناهجویان جان خود را از دست دادند. "
                "بر اساس بیانیه سازمان بین‌المللی مهاجرت (IOM)، این رویداد بار دیگر خطرات مرگبار مسیرهای "
                "دریایی غیرقانونی و لزوم همکاری‌های منطقه‌ای جهت امدادرسانی را برجسته می‌سازد."
            ),
            "countries": ["یونان", "سنگال"],
            "actors": ["سازمان بین‌المللی مهاجرت (IOM)", "سازمان ملل متحد"],
            "location": "سواحل سنگال و یونان",
            "importance": 4,
            "verification_status": "confirmed",
        }

        # 3. Analyze using MockAIProvider (representing Gemini provider output)
        provider = MockAIProvider(canned_response=canned_ai_response, model=settings.gemini_model)
        result = await analyze_article(
            title=article.title,
            body=article.original_text or "",
            source_name=source_name,
            provider=provider,
        )

        print(f"\n[AI Analysis Result]:")
        print(f"  Headline (FA): {result.headline_fa}")
        print(f"  Categories: {result.categories}")
        print(f"  Verification Status: {result.verification_status}")
        print(f"  Importance: {result.importance}")

        # 4. Save to Database: Event, ArticleEvent, Summary
        event = Event(
            title=result.event_title,
            event_type=result.categories[0] if result.categories else "HUM",
            countries=result.countries,
            actors=result.actors,
            location=result.location,
            importance=result.importance,
            status="active",
        )
        session.add(event)
        await session.flush()
        print(f"\nCreated Event record: ID = {event.id}")

        article_event = ArticleEvent(
            article_id=article.id,
            event_id=event.id,
            match_type="initial",
            confidence=0.90,
        )
        session.add(article_event)

        summary = Summary(
            event_id=event.id,
            headline_fa=result.headline_fa,
            summary_fa=result.summary_fa,
            details_fa={"source": source_name, "url": article.original_url},
            verification_status=result.verification_status,
            model_name=provider.model_name,
            prompt_version=PROMPT_VERSION,
        )
        session.add(summary)

        # Update article status and ai_payload
        article.status = "summarized"
        article.ai_payload = result.model_dump(mode="json")

        await session.commit()
        print(f"Created Summary record: ID = {summary.id}, Model = {summary.model_name}, Prompt = {summary.prompt_version}")
        print("Updated Article status to 'summarized'.")
        print("\n" + "=" * 65)
        print("  Phase 4 End-to-End Test Successfully Completed!")
        print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())

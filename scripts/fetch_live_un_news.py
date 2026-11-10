"""Live RSS collector verification script for UN News.

Fetches live entries from UN News RSS, ensures the source exists in the database,
and saves the collected articles into the PostgreSQL `articles` table.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from app.database.connection import SessionLocal
from app.database.models import Source, Article
from app.collectors.rss_collector import fetch_feed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("live_un_fetcher")

UN_FEED_URL = "https://news.un.org/feed/subscribe/en/news/all/rss.xml"


async def main():
    print(f"Connecting to database and fetching live feed from {UN_FEED_URL}...")
    
    # 1. Fetch live entries from UN News
    entries = await fetch_feed(UN_FEED_URL)
    print(f"Successfully retrieved {len(entries)} articles from UN News RSS.")

    async with SessionLocal() as session:
        # 2. Ensure UN News source exists
        source = await session.scalar(select(Source).where(Source.feed_url == UN_FEED_URL))
        if not source:
            source = Source(
                name="UN News",
                base_url="https://news.un.org/",
                feed_url=UN_FEED_URL,
                source_type="official",
                country="International",
                language="en",
                is_active=True,
                reliability_note="منبع رسمی سازمان ملل؛ برای مواضع و گزارش‌های سازمان ملل"
            )
            session.add(source)
            await session.flush()
            print(f"Created new source record: id={source.id}, name={source.name}")
        else:
            print(f"Found existing source record: id={source.id}, name={source.name}")

        # 3. Insert new articles
        inserted_count = 0
        existing_count = 0
        for item in entries:
            exists = await session.scalar(
                select(Article.id).where(Article.canonical_url == item["canonical_url"])
            )
            if exists:
                existing_count += 1
                continue

            article = Article(
                source_id=source.id,
                title=item["title"],
                original_url=item["url"],
                canonical_url=item["canonical_url"],
                original_language="en",
                original_text=item["text"],
                published_at=item["published_at"],
                content_hash=item["content_hash"],
                status="collected",
            )
            session.add(article)
            inserted_count += 1

        await session.commit()
        print(f"\nPersistence Summary:")
        print(f"  Total entries fetched: {len(entries)}")
        print(f"  New articles inserted: {inserted_count}")
        print(f"  Pre-existing articles: {existing_count}")


if __name__ == "__main__":
    asyncio.run(main())

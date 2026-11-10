from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Event, Summary, ArticleEvent, Article, Source, DailyReport

async def build_daily_report():
    tz = ZoneInfo(settings.timezone)
    report_day = datetime.now(tz).date() - timedelta(days=1)
    start = datetime.combine(report_day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Event, Summary, Article, Source)
            .join(Summary, Summary.event_id == Event.id)
            .join(ArticleEvent, ArticleEvent.event_id == Event.id)
            .join(Article, Article.id == ArticleEvent.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(Article.collected_at >= start, Article.collected_at < end)
            .order_by(Event.importance.desc(), Article.collected_at.desc())
        )).all()
        seen, items = set(), []
        for event, summary, article, source in rows:
            if event.id in seen or event.importance < settings.min_importance:
                continue
            seen.add(event.id)
            items.append({"event_id": event.id, "headline": summary.headline_fa,
                "summary": summary.summary_fa, "verification_status": summary.verification_status,
                "source": source.name, "url": article.original_url,
                "categories": event.event_type, "countries": event.countries,
                "importance": event.importance})
        report = await session.scalar(select(DailyReport).where(DailyReport.report_date == report_day))
        payload = {"date": str(report_day), "items": items}
        if report:
            report.content, report.status = payload, "generated"
        else:
            session.add(DailyReport(report_date=report_day, content=payload, status="generated"))
        await session.commit()
        return report_day, items

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from app.config import settings
from app.processors.pipeline import collect_and_process
from app.reports.daily_report import build_daily_report
from app.telegram.publisher import publish_report
from app.database.connection import SessionLocal
from app.database.models import DailyReport

async def hourly_job():
    await collect_and_process()

async def daily_job():
    report_day, items = await build_daily_report()
    ids = await publish_report(report_day, items)
    async with SessionLocal() as session:
        report = await session.scalar(select(DailyReport).where(DailyReport.report_date == report_day))
        if report:
            report.telegram_message_ids = ids
            report.status = "published"
            report.published_at = datetime.now(timezone.utc)
            await session.commit()

def build_scheduler():
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
    scheduler.add_job(hourly_job, "interval", minutes=settings.collection_interval_minutes,
                      id="hourly_collection", max_instances=1, coalesce=True)
    scheduler.add_job(daily_job, "cron", hour=settings.daily_report_hour,
                      minute=settings.daily_report_minute, id="daily_report",
                      max_instances=1, coalesce=True)
    return scheduler

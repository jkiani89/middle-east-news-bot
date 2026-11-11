import asyncio
import logging

from app.collectors.source_manager import sync_sources
from app.processors.pipeline import collect_and_process
from app.scheduler.jobs import build_scheduler, reconcile_interrupted_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

async def main():
    await sync_sources()
    await reconcile_interrupted_jobs()
    try:
        await collect_and_process()
    except Exception:
        logging.exception("Initial collection failed.")
    scheduler = build_scheduler()
    scheduler.start()
    logging.info("Scheduler started.")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())

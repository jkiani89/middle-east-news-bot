import json
from pathlib import Path

from sqlalchemy import select

from app.database.connection import SessionLocal
from app.database.models import Source


async def sync_sources():
    sources_file = Path("editorial/sources.json")
    records = json.loads(sources_file.read_text(encoding="utf-8"))
    async with SessionLocal() as session:
        for record in records:
            source = await session.scalar(select(Source).where(Source.name == record["name"]))
            if source:
                for key, value in record.items():
                    setattr(source, key, value)
            else:
                session.add(Source(**record))
        await session.commit()

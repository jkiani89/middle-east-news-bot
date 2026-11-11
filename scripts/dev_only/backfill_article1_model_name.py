import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.config import settings

async def main():
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        res = await conn.execute(text("""
            UPDATE articles 
            SET ai_payload = ai_payload || '{"model_name": "gemini-2.5-flash"}'::jsonb 
            WHERE id = 1
        """))
        print(f"Updated rows: {res.rowcount}")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())

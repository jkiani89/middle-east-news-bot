from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url_for_asyncpg,
    connect_args=settings.asyncpg_connect_args,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

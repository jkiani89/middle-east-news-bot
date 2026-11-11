import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Ensure app is discoverable on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.database.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set database URL dynamically from app settings
config.set_main_option("sqlalchemy.url", settings.database_url)

# Model metadata for autogenerate support
target_metadata = Base.metadata

# Rotator-managed tables (raw DDL, not in ORM metadata).
# These are defined in alembic/versions/f1a2b3c4d5e6_add_gemini_rotator_tables.py
# and used by app/ai/postgres_adapter.py via psycopg (not SQLAlchemy ORM).
# See docs/PHASE_F1_PLAN.md section "تغییر ۱".
_GEMINI_ROTATOR_TABLES = frozenset({
    "gemini_suspended_keys",
    "gemini_key_stats",
    "gemini_request_log",
})


def include_object(object, name, type_, reflected, compare_to):
    """Exclude rotator tables from autogenerate diff.

    These tables are created by Alembic migration and used via raw SQL
    (psycopg) in PostgresAdapter, not via ORM. Without this filter,
    autogenerate would try to DROP them on every check.
    """
    if type_ == "table" and name in _GEMINI_ROTATOR_TABLES:
        return False
    return not (
        type_ == "index"
        and getattr(object, "table", None) is not None
        and object.table.name in _GEMINI_ROTATOR_TABLES
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with AsyncEngine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

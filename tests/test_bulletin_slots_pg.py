"""Real-PostgreSQL tests for the bulletin slot lock.

These tests need a reachable PostgreSQL (CI provides one as a service container; locally
they skip with the same message as the other database tests). They exist because the claim
must be atomic in the database: a mock can only prove the shape of the call, not that two
concurrent sessions cannot both win.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.reports.daily_report import (
    SLOT_CLAIMED,
    SLOT_IN_FLIGHT,
    SLOT_PUBLISHED,
    claim_slot,
    slot_status,
)

TEST_DATE = date(2031, 1, 1)


@pytest.fixture
async def pg_sessions(sync_test_dsn, ensure_test_schema):
    """Async session factory on the test database, with this test's rows cleared."""
    dsn = sync_test_dsn.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(dsn, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("DELETE FROM bulletin_slots WHERE report_date = :d"), {"d": TEST_DATE}
        )
        await session.commit()

    yield factory

    async with factory() as session:
        await session.execute(
            text("DELETE FROM bulletin_slots WHERE report_date = :d"), {"d": TEST_DATE}
        )
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_slot_claim(pg_sessions):
    """Two runs racing for the same slot: exactly one of them may claim it."""

    async def attempt() -> str:
        async with pg_sessions() as session:
            return await claim_slot(session, TEST_DATE, 8)

    first, second = await asyncio.gather(attempt(), attempt())

    assert sorted([first, second]) == [SLOT_CLAIMED, SLOT_IN_FLIGHT]


@pytest.mark.asyncio
async def test_published_slot_cannot_be_claimed_again(pg_sessions):
    """A slot already recorded as published is never reopened."""
    async with pg_sessions() as session:
        assert await claim_slot(session, TEST_DATE, 20) == SLOT_CLAIMED
        await session.execute(
            text(
                """
                UPDATE bulletin_slots
                   SET status = 'published', message_ids = '[7, 8]'::jsonb
                 WHERE report_date = :d AND slot_hour = 20
                """
            ),
            {"d": TEST_DATE},
        )
        await session.commit()

    async with pg_sessions() as session:
        assert await claim_slot(session, TEST_DATE, 20) == SLOT_PUBLISHED
        state = await slot_status(session, TEST_DATE, 20)
        assert state is not None
        assert state["status"] == "published"


@pytest.mark.asyncio
async def test_unique_constraint_protects_the_slot(pg_sessions):
    """The database itself rejects a duplicate (report_date, slot_hour) row."""
    insert = text(
        """
        INSERT INTO bulletin_slots (report_date, slot_hour, status, message_ids)
        VALUES (:d, 8, 'pending', '[]'::jsonb)
        """
    )
    async with pg_sessions() as session:
        await session.execute(insert, {"d": TEST_DATE})
        await session.commit()

    with pytest.raises(IntegrityError):
        async with pg_sessions() as session:
            await session.execute(insert, {"d": TEST_DATE})
            await session.commit()

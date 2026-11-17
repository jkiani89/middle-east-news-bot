"""Production must never create a throwaway Telegraph account for a missing token."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

import app.reports.telegraph_report as telegraph_report
from app.config import settings
from app.reports.telegraph_report import (
    TELEGRAPH_DISABLED,
    publish_telegraph_report,
)


@pytest.mark.asyncio
async def test_production_without_a_token_refuses_to_create_an_account(monkeypatch):
    built: list[bool] = []

    def _build_client():
        built.append(True)
        return AsyncMock()

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "telegraph_enabled", True)
    monkeypatch.setattr(settings, "telegraph_access_token", "")
    monkeypatch.setattr(telegraph_report, "build_client", _build_client)

    result = await publish_telegraph_report(
        AsyncMock(),
        report_date=date(2026, 11, 14),
        slot_hour=8,
        items=[],
        persist=False,
    )

    assert result["status"] == TELEGRAPH_DISABLED
    assert result["failures"] == [
        {"scope": "config", "error": "TELEGRAPH_ACCESS_TOKEN missing"}
    ]
    assert built == []  # no client, therefore no createAccount call


@pytest.mark.asyncio
async def test_production_with_a_token_is_not_blocked(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "telegraph_enabled", True)
    monkeypatch.setattr(settings, "telegraph_access_token", "stored-token")
    client = AsyncMock()
    client.ensure_token.return_value = "stored-token"

    result = await publish_telegraph_report(
        AsyncMock(),
        report_date=date(2026, 11, 14),
        slot_hour=8,
        client=client,
        items=[],
        persist=False,
    )

    assert result["failures"] == []
    client.ensure_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_development_keeps_the_temporary_account_behaviour(monkeypatch):
    """Only production is guarded; local runs may still create a throwaway account."""
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "telegraph_enabled", True)
    monkeypatch.setattr(settings, "telegraph_access_token", "")
    client = AsyncMock()
    client.ensure_token.return_value = "temporary-token"

    result = await publish_telegraph_report(
        AsyncMock(),
        report_date=date(2026, 11, 14),
        slot_hour=8,
        client=client,
        items=[],
        persist=False,
    )

    assert result["failures"] == []
    client.ensure_token.assert_awaited_once()

"""Unit tests for Iranian Solar Hijri (شمسی) calendar conversions."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.reports.persian_dates import (
    format_jalali_date,
    format_jalali_datetime,
    format_jalali_long,
    gregorian_to_jalali,
)


def test_gregorian_to_jalali_handles_leap_and_nowruz_boundaries() -> None:
    assert gregorian_to_jalali(date(2024, 3, 20)) == (1403, 1, 1)
    assert gregorian_to_jalali(date(2025, 3, 20)) == (1403, 12, 30)  # 1403 is leap
    assert gregorian_to_jalali(date(2025, 3, 21)) == (1404, 1, 1)
    assert gregorian_to_jalali(date(2026, 3, 21)) == (1405, 1, 1)
    assert gregorian_to_jalali(date(2026, 11, 10)) == (1405, 8, 19)
    assert gregorian_to_jalali(date(2026, 11, 14)) == (1405, 8, 23)
    assert gregorian_to_jalali(date(2026, 11, 25)) == (1405, 9, 4)


def test_persian_date_formatters() -> None:
    d = date(2026, 11, 14)
    assert format_jalali_date(d) == "۱۴۰۵/۰۸/۲۳"
    assert format_jalali_long(d) == "شنبه ۲۳ آبان ۱۴۰۵"

    moment = datetime(2026, 11, 14, 7, 30, tzinfo=UTC)
    assert format_jalali_datetime(moment, "Asia/Tehran") == "۱۴۰۵/۰۸/۲۳ ۱۱:۰۰"

"""Phase R-4: PDF bulletin rendering and Telegram document delivery."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.reports.pdf_report import build_daily_report_pdf
from app.telegram.publisher import MockPublishRefusedError, publish_daily_report_pdf


def _items() -> list[dict]:
    return [
        {
            "event_id": 1,
            "event_type": "MIL",
            "category_fa": "تحولات نظامی و امنیتی",
            "importance": 5,
            "countries": ["فلسطین"],
            "actors": ["ارتش اسرائیل"],
            "location": "خان یونس",
            "headline_fa": "گزارش انفجار نزدیک بیمارستان ناصر در خان یونس",
            "summary_fa": "منابع محلی از وقوع انفجار در نزدیکی بیمارستان ناصر خبر دادند.",
            "verification_status": "confirmed",
            "verification_status_fa": "✅ تأییدشده",
            "primary_source": "IRNA",
            "primary_url": "https://irna.ir/news/1",
            "additional_sources": [{"source": "Reuters", "url": "https://reuters.com/x"}],
            "total_sources_count": 2,
        },
        {
            "event_id": 2,
            "event_type": "ECO",
            "category_fa": "اقتصاد، تجارت و بازرگانی",
            "importance": 3,
            "countries": ["ایران"],
            "actors": [],
            "location": "تهران",
            "headline_fa": "افزایش صادرات غیرنفتی در ماه گذشته",
            "summary_fa": "گمرک از رشد صادرات غیرنفتی در ماه گذشته خبر داد.",
            "verification_status": "unconfirmed",
            "verification_status_fa": "❓ تأییدنشده (تک‌منبعی)",
            "primary_source": "IRNA",
            "primary_url": "https://irna.ir/news/2",
            "additional_sources": [],
            "total_sources_count": 1,
        },
    ]


def test_pdf_bulletin_is_rendered(tmp_path):
    out = build_daily_report_pdf(
        date(2026, 11, 14),
        _items(),
        {"content_quality": "real", "min_importance": 2, "language_rejected_count": 2},
        tmp_path / "bulletin.pdf",
    )

    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    assert data.rstrip().endswith(b"%%EOF")
    assert len(data) > 5000


def test_pdf_bulletin_handles_an_empty_report(tmp_path):
    out = build_daily_report_pdf(date(2026, 11, 14), [], {}, tmp_path / "empty.pdf")

    assert out.read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_dry_run_renders_without_sending():
    result = await publish_daily_report_pdf(
        date(2026, 11, 14),
        _items(),
        {"content_quality": "real"},
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["type"] == "document"
    assert result["bytes"] > 5000


@pytest.mark.asyncio
async def test_document_is_sent_to_the_channel_with_a_caption(tmp_path):
    bot = AsyncMock()
    message = MagicMock()
    message.message_id = 4242
    bot.send_document.return_value = message

    info = await publish_daily_report_pdf(
        date(2026, 11, 14),
        _items(),
        {"content_quality": "real"},
        content_quality="real",
        bot=bot,
        output_path=tmp_path / "bulletin.pdf",
    )

    assert info["message_id"] == 4242
    kwargs = bot.send_document.call_args.kwargs
    assert kwargs["chat_id"] == settings.telegram_channel_id
    assert "بولتن روزانه خاورمیانه" in kwargs["caption"]
    assert len(kwargs["caption"]) <= 1024
    assert kwargs["document"] is not None


@pytest.mark.asyncio
async def test_mock_content_is_refused_outside_the_test_environment():
    with patch.object(settings, "app_env", "development"):
        with pytest.raises(MockPublishRefusedError, match="content_quality is 'mock'"):
            await publish_daily_report_pdf(
                date(2026, 11, 14),
                _items(),
                {"content_quality": "mock"},
                content_quality="mock",
            )

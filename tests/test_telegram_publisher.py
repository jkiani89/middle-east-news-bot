"""Unit tests for Telegram publishing utilities and security guardrails."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import settings
from app.telegram.publisher import (
    MockPublishRefusedError,
    esc,
    publish_report,
    split_text,
)


def test_esc_sanitizes_html_characters():
    assert esc("Tehran & Baghdad <update>") == "Tehran &amp; Baghdad &lt;update&gt;"
    assert esc("Normal text without special characters") == "Normal text without special characters"


def test_split_text_single_chunk():
    text = "Line 1\nLine 2\nLine 3"
    chunks = split_text(text, limit=100)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_text_multiple_chunks():
    lines = [f"News update item number {i}" for i in range(50)]
    text = "\n".join(lines)
    chunks = split_text(text, limit=150)
    assert len(chunks) > 1
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_text_preserves_html_tags_iss_008():
    """Verify that splitting a long text with HTML tags balances open/close tags in every chunk."""
    text = "<b>" + "x" * 5000 + "</b> <a href='https://news.un.org'>link</a>"
    chunks = split_text(text, limit=4000)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 4000
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<a ") == chunk.count("</a>")


def test_split_text_nested_tags():
    """Verify nested tags balance correctly across chunks."""
    text = "<b>Header <i>italic section " + "word " * 2000 + "end italic</i> end bold</b>"
    chunks = split_text(text, limit=3000)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 3000
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<i>") == chunk.count("</i>")


# ==============================================================================
# Security Policy: Fail-Closed content_quality checks in non-test environments
# ==============================================================================

@pytest.mark.asyncio
async def test_publish_report_refuses_mock_in_development():
    """Fail-closed: content_quality='mock' in development must raise MockPublishRefusedError."""
    with patch.object(settings, "app_env", "development"):
        with pytest.raises(MockPublishRefusedError, match="content_quality is 'mock'"):
            await publish_report(
                report_date="2026-11-10",
                items=[{"headline": "Mock", "summary": "Mock"}],
                content_quality="mock",
            )


@pytest.mark.asyncio
async def test_publish_report_refuses_mixed_in_development():
    """Fail-closed: content_quality='mixed' in development must raise MockPublishRefusedError."""
    with patch.object(settings, "app_env", "development"):
        with pytest.raises(MockPublishRefusedError, match="content_quality is 'mixed'"):
            await publish_report(
                report_date="2026-11-10",
                items=[{"headline": "Mixed", "summary": "Mixed"}],
                content_quality="mixed",
            )


@pytest.mark.asyncio
async def test_publish_report_refuses_null_or_none_in_development():
    """Fail-closed: content_quality=None in development must raise MockPublishRefusedError."""
    with patch.object(settings, "app_env", "development"):
        with pytest.raises(MockPublishRefusedError, match="content_quality is 'None'"):
            await publish_report(
                report_date="2026-11-10",
                items=[{"headline": "Unknown", "summary": "Unknown"}],
                content_quality=None,
            )


@pytest.mark.asyncio
async def test_publish_report_allows_mock_in_test_environment():
    """In test environment (app_env='test'), mock reports are allowed for integration tests."""
    mock_bot = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.message_id = 999
    mock_bot.send_message.return_value = mock_msg
    mock_bot.session.close = AsyncMock()

    with patch.object(settings, "app_env", "test"):
        with patch("app.telegram.publisher.Bot", return_value=mock_bot):
            sent = await publish_report(
                report_date="2026-11-10",
                items=[{"headline": "Test Item", "summary": "Test Summary", "source": "Test", "url": "http://x"}],
                content_quality="mock",
            )
            assert len(sent) > 0
            assert mock_bot.send_message.called


@pytest.mark.asyncio
async def test_publish_report_allows_real_in_development():
    """In development environment, content_quality='real' proceeds without refusal."""
    mock_bot = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.message_id = 1001
    mock_bot.send_message.return_value = mock_msg
    mock_bot.session.close = AsyncMock()

    with patch.object(settings, "app_env", "development"):
        with patch("app.telegram.publisher.Bot", return_value=mock_bot):
            sent = await publish_report(
                report_date="2026-11-10",
                items=[{"headline": "Real Headline", "summary": "Real Summary", "source": "Reuters", "url": "http://r"}],
                content_quality="real",
            )
            assert len(sent) > 0
            assert mock_bot.send_message.called

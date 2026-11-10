"""Unit tests for Telegram publishing utilities."""

import pytest
from app.telegram.publisher import esc, split_text


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
    # Verify reconstructed lines match original
    reconstructed = "\n".join(chunks)
    assert reconstructed == text


@pytest.mark.asyncio
async def test_publish_report_refuses_mock_content():
    from app.telegram.publisher import MockPublishRefusedError, publish_report
    with pytest.raises(MockPublishRefusedError, match="Publish refused: Daily report content_quality is 'mock'"):
        await publish_report("2026-11-10", [{"headline": "Mock", "summary": "Mock"}], content_quality="mock")

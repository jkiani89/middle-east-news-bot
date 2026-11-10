"""Unit tests for Telegram publishing utilities."""

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

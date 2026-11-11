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


def test_old_split_text_would_have_failed():
    """Counterfactual proof test for ISS-008:
    Demonstrates that naive string splitting across chunks produces unbalanced HTML tags
    (which causes Telegram 400 Bad Request: can't parse entities: Tag 'b' is not closed),
    whereas the stack-based split_text safely closes tags in chunk 1 and reopens them in chunk 2.
    """
    long_section = "<b>" + "Word " * 600 + "</b>"

    # 1. Simulate naive line/character slicing across chunk boundary
    naive_chunk_1 = long_section[:2000]
    naive_chunk_2 = long_section[2000:]

    # In naive chunking, chunk 1 has <b> without </b>, chunk 2 has </b> without <b>
    assert naive_chunk_1.count("<b>") == 1
    assert naive_chunk_1.count("</b>") == 0  # FAILS: Tag 'b' is not closed!
    assert naive_chunk_2.count("<b>") == 0
    assert naive_chunk_2.count("</b>") == 1  # FAILS: Unmatched closing tag 'b'!

    # 2. In contrast, stack-aware split_text balances every chunk
    safe_chunks = split_text(long_section, limit=2000)
    assert len(safe_chunks) >= 2
    for chunk in safe_chunks:
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<b>") > 0


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


# ==============================================================================
# Phase 7 Comprehensive Integration Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_publish_report_dual_destination_real_4_calls():
    """Test real report sends 3 chunks to channel + 1 summary to admin (4 calls total)."""
    # Create exactly 3 chunks
    report_text = "\n".join([f"<b>Item {i}</b>: " + "analysis content " * 15 for i in range(36)])
    assert len(split_text(report_text, limit=3900)) == 3

    mock_bot = AsyncMock()
    msg_ids = [101, 102, 103, 104]
    mock_messages = [MagicMock(message_id=mid) for mid in msg_ids]
    mock_bot.send_message.side_effect = mock_messages
    mock_bot.session.close = AsyncMock()

    sent = await publish_report(
        report_date="2026-11-10",
        content_quality="real",
        report_text=report_text,
        bot=mock_bot,
        channel_id="@middle_east_channel",
        admin_chat_id="987654321",
    )

    assert mock_bot.send_message.call_count == 4
    assert len(sent) == 4

    # Verify first 3 calls went to channel
    for i in range(3):
        call_kwargs = mock_bot.send_message.call_args_list[i].kwargs
        assert call_kwargs["chat_id"] == "@middle_east_channel"
        assert sent[i]["type"] == "channel"
        assert sent[i]["message_id"] == msg_ids[i]

    # Verify 4th call went to admin with channel IDs
    admin_call = mock_bot.send_message.call_args_list[3].kwargs
    assert admin_call["chat_id"] == "987654321"
    assert "گزارش وضعیت انتشار در کانال تلگرام" in admin_call["text"]
    assert "[101, 102, 103]" in admin_call["text"]
    assert sent[3]["type"] == "admin"
    assert sent[3]["message_id"] == 104


@pytest.mark.asyncio
async def test_publish_report_flood_wait_retry():
    """Test TelegramRetryAfter triggers backoff sleep and retries successfully."""
    from aiogram.exceptions import TelegramRetryAfter

    # Create exactly 3 chunks
    report_text = "\n".join([f"<b>Item {i}</b>: " + "analysis content " * 15 for i in range(36)])
    assert len(split_text(report_text, limit=3900)) == 3

    mock_bot = AsyncMock()
    retry_exc = TelegramRetryAfter(method=MagicMock(), message="Flood wait", retry_after=0.001)

    # Chunk 1 succeeds (101), Chunk 2 fails once with FloodWait then succeeds (102), Chunk 3 succeeds (103), Admin succeeds (104)
    mock_bot.send_message.side_effect = [
        MagicMock(message_id=101),
        retry_exc,
        MagicMock(message_id=102),
        MagicMock(message_id=103),
        MagicMock(message_id=104),
    ]
    mock_bot.session.close = AsyncMock()

    sent = await publish_report(
        report_date="2026-11-10",
        content_quality="real",
        report_text=report_text,
        bot=mock_bot,
        channel_id="@channel",
        admin_chat_id="12345",
        base_backoff=0.001,
    )

    assert mock_bot.send_message.call_count == 5
    assert len(sent) == 4
    assert [s["message_id"] for s in sent] == [101, 102, 103, 104]


@pytest.mark.asyncio
async def test_publish_report_network_error_retry():
    """Test TelegramNetworkError retries with exponential backoff and succeeds."""
    from aiogram.exceptions import TelegramNetworkError

    mock_bot = AsyncMock()
    net_exc = TelegramNetworkError(method=MagicMock(), message="Connection reset by peer")

    mock_bot.send_message.side_effect = [
        net_exc,
        MagicMock(message_id=201),
        MagicMock(message_id=202),
    ]
    mock_bot.session.close = AsyncMock()

    sent = await publish_report(
        report_date="2026-11-10",
        content_quality="real",
        report_text="<b>Single short report</b>",
        bot=mock_bot,
        channel_id="@channel",
        admin_chat_id="12345",
        base_backoff=0.001,
    )

    assert mock_bot.send_message.call_count == 3
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_publish_report_permanent_failure_saves_partial_progress():
    """Test permanent failure marks DailyReport status='failed' and preserves partial message_ids."""
    from aiogram.exceptions import TelegramNetworkError

    chunk_block = "<b>Headline</b>\n" + ("Content text " * 120) + "\n\n"
    report_text = chunk_block * 2

    mock_db_report = MagicMock()
    mock_db_report.telegram_message_ids = []
    mock_db_report.status = "generated"

    mock_session = AsyncMock()
    mock_session.scalar.return_value = mock_db_report

    mock_bot = AsyncMock()
    net_exc = TelegramNetworkError(method=MagicMock(), message="Server Down")

    # Chunk 1 succeeds (101), Chunk 2 exhausts 3 retries
    mock_bot.send_message.side_effect = [
        MagicMock(message_id=101),
        net_exc,
        net_exc,
        net_exc,
    ]
    mock_bot.session.close = AsyncMock()

    with pytest.raises(TelegramNetworkError):
        await publish_report(
            report_date="2026-11-10",
            content_quality="real",
            report_text=report_text,
            bot=mock_bot,
            session=mock_session,
            channel_id="@channel",
            admin_chat_id="12345",
            base_backoff=0.001,
        )

    # Verify partial delivery state saved to database
    assert mock_db_report.status == "failed"
    assert mock_db_report.telegram_message_ids == [101]
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_publish_report_dry_run_mode():
    """Test dry_run=True produces simulated delivery without network calls."""
    mock_bot = AsyncMock()

    sent = await publish_report(
        report_date="2026-11-10",
        content_quality="mock",
        report_text="<b>Sample dry run text</b>",
        bot=mock_bot,
        dry_run=True,
        channel_id="@test_channel",
        admin_chat_id="987654",
    )

    assert len(sent) == 2
    assert sent[0]["type"] == "channel"
    assert sent[1]["type"] == "admin"
    assert not mock_bot.send_message.called


@pytest.mark.asyncio
async def test_resilient_send_permanent_api_error_fails_immediately():
    """Test TelegramAPIError (e.g. 400 Bad Request) fails without retrying."""
    from aiogram.exceptions import TelegramAPIError

    from app.telegram.publisher import send_telegram_message_resilient

    mock_bot = AsyncMock()
    api_exc = TelegramAPIError(method=MagicMock(), message="Bad Request: chat not found")
    mock_bot.send_message.side_effect = api_exc

    with pytest.raises(TelegramAPIError, match="chat not found"):
        await send_telegram_message_resilient(
            bot=mock_bot,
            chat_id="@invalid_chat",
            text="hello",
            max_retries=3,
        )

    assert mock_bot.send_message.call_count == 1


# ==============================================================================
# Idempotency & Partial Resume Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_publish_skips_already_sent_chunks():
    """Verify that retrying after partial failure skips already delivered chunks (Idempotency)."""
    # 3-chunk report
    report_text = "\n".join([f"<b>Item {i}</b>: " + "analysis content " * 15 for i in range(36)])
    assert len(split_text(report_text, limit=3900)) == 3

    # Simulate existing DB state where chunk 1 was sent (message_id=101) but chunk 2 failed
    mock_db_report = MagicMock()
    mock_db_report.telegram_message_ids = [101]
    mock_db_report.status = "failed"

    mock_session = AsyncMock()
    mock_session.scalar.return_value = mock_db_report

    mock_bot = AsyncMock()
    # On retry, mock responses for remaining chunks: chunk 2 (102), chunk 3 (103), admin (104)
    mock_bot.send_message.side_effect = [
        MagicMock(message_id=102),
        MagicMock(message_id=103),
        MagicMock(message_id=104),
    ]
    mock_bot.session.close = AsyncMock()

    sent = await publish_report(
        report_date="2026-11-10",
        content_quality="real",
        report_text=report_text,
        bot=mock_bot,
        session=mock_session,
        channel_id="@channel",
        admin_chat_id="12345",
    )

    # Crucial assertion: send_message was called 3 times (chunk 2, chunk 3, admin), NOT 4!
    # Chunk 1 was NEVER re-sent!
    assert mock_bot.send_message.call_count == 3

    # First call must be chunk 2, NOT chunk 1
    chunk_2_call_text = mock_bot.send_message.call_args_list[0].kwargs["text"]
    assert "Item 0" not in chunk_2_call_text  # Item 0 was in chunk 1

    # Sent array and database records contain all 4 message IDs
    assert [s["message_id"] for s in sent] == [101, 102, 103, 104]
    assert mock_db_report.telegram_message_ids == [101, 102, 103, 104]
    assert mock_db_report.status == "published"


@pytest.mark.asyncio
async def test_publish_all_chunks_already_sent_skips_channel():
    """Verify that when all chunks are already sent, channel delivery is completely bypassed."""
    report_text = "\n".join([f"<b>Item {i}</b>: " + "analysis content " * 15 for i in range(36)])
    assert len(split_text(report_text, limit=3900)) == 3

    mock_db_report = MagicMock()
    mock_db_report.telegram_message_ids = [101, 102, 103]
    mock_db_report.status = "failed"

    mock_session = AsyncMock()
    mock_session.scalar.return_value = mock_db_report

    mock_bot = AsyncMock()
    mock_bot.send_message.return_value = MagicMock(message_id=104)

    await publish_report(
        report_date="2026-11-10",
        content_quality="real",
        report_text=report_text,
        bot=mock_bot,
        session=mock_session,
        channel_id="@channel",
        admin_chat_id="12345",
    )

    # Only 1 call made: to admin chat! Zero channel calls!
    assert mock_bot.send_message.call_count == 1
    assert mock_bot.send_message.call_args.kwargs["chat_id"] == "12345"
    assert mock_db_report.status == "published"


@pytest.mark.asyncio
async def test_cli_production_guardrail():
    """Verify CLI in production rejects real publish unless --confirm is specified."""
    from app.telegram.publisher import _run_cli_dry_run

    with patch.object(settings, "app_env", "production"):
        with patch("sys.argv", ["publisher.py", "--date", "2026-11-10"]):
            # Missing --confirm and --dry-run -> should exit without publishing
            with patch("app.telegram.publisher.publish_report") as mock_pub:
                await _run_cli_dry_run()
                assert not mock_pub.called


# ==============================================================================
# Additional Coverage Boosters (Target >= 90%)
# ==============================================================================

def test_split_text_edge_cases():
    """Verify split_text with empty text and unbroken long strings."""
    assert split_text("") == []
    long_unbroken = "a" * 5000
    chunks = split_text(long_unbroken, limit=3900)
    assert len(chunks) == 2
    assert len(chunks[0]) <= 3900
    assert len(chunks[1]) <= 3900


@pytest.mark.asyncio
async def test_publish_report_flood_wait_exhausts_retries():
    """Verify TelegramRetryAfter raises exception when max_retries is reached."""
    from aiogram.exceptions import TelegramRetryAfter

    mock_bot = AsyncMock()
    retry_exc = TelegramRetryAfter(method=MagicMock(), message="Flood", retry_after=0.001)
    mock_bot.send_message.side_effect = [retry_exc, retry_exc, retry_exc]

    with pytest.raises(TelegramRetryAfter):
        await publish_report(
            report_date="2026-11-10",
            content_quality="real",
            report_text="Sample text",
            bot=mock_bot,
            channel_id="@channel",
            base_backoff=0.001,
        )


@pytest.mark.asyncio
async def test_publish_report_successful_db_status_update():
    """Verify successful publish updates DailyReport status='published' and records published_at."""
    mock_db_report = MagicMock()
    mock_db_report.status = "generated"
    mock_db_report.telegram_message_ids = []
    mock_db_report.published_at = None

    mock_session = AsyncMock()
    mock_session.scalar.return_value = mock_db_report

    mock_bot = AsyncMock()
    mock_bot.send_message.return_value = MagicMock(message_id=555)

    await publish_report(
        report_date="2026-11-10",
        content_quality="real",
        report_text="<b>Report text</b>",
        bot=mock_bot,
        session=mock_session,
        channel_id="@channel",
        admin_chat_id="123",
    )

    assert mock_db_report.status == "published"
    assert mock_db_report.published_at is not None
    assert mock_db_report.telegram_message_ids == [555, 555]
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_publish_report_empty_report_text():
    """Verify publish_report with no items and no DB record generates placeholder."""
    mock_bot = AsyncMock()
    mock_bot.send_message.return_value = MagicMock(message_id=777)

    with patch("app.telegram.publisher.SessionLocal", side_effect=Exception("DB offline")):
        sent = await publish_report(
            report_date="2026-11-10",
            content_quality="real",
            items=None,
            report_text=None,
            bot=mock_bot,
            channel_id="@channel",
        )
        assert len(sent) > 0


@pytest.mark.asyncio
async def test_cli_dry_run_execution():
    """Verify _run_cli_dry_run CLI command execution."""
    from app.telegram.publisher import _run_cli_dry_run

    mock_report = MagicMock()
    mock_report.id = 1
    mock_report.status = "generated"
    mock_report.content = {"content_quality": "mock", "formatted_text": "<b>Bulletin</b>"}

    mock_session = AsyncMock()
    mock_session.scalar.return_value = mock_report

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None

    with patch("app.telegram.publisher.SessionLocal", return_value=mock_ctx):
        with patch("sys.argv", ["publisher.py", "--dry-run", "--date", "2026-11-10"]):
            await _run_cli_dry_run()

    # Also test CLI when no report found
    mock_session.scalar.return_value = None
    with patch("app.telegram.publisher.SessionLocal", return_value=mock_ctx):
        with patch("sys.argv", ["publisher.py", "--dry-run", "--date", "2026-11-10"]):
            await _run_cli_dry_run()

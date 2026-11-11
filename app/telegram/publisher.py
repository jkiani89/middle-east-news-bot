"""Telegram Publisher Service for Middle East News Bot.

Implements tag-aware message splitting (resolving ISS-008) to ensure all HTML tags
are properly balanced across chunk boundaries, preventing Telegram ParseMode errors.
Strictly enforces fail-closed publishing guards based on content_quality and app_env.
Provides resilient delivery with FloodWait (429) backoff, network error retries,
dual destination routing (Channel + Admin Chat), and partial delivery tracking.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import UTC, date, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import DailyReport
from app.reports.daily_report import format_persian_daily_report

logger = logging.getLogger(__name__)

TAG_RE = re.compile(r"<(/)?([a-zA-Z0-9_-]+)(?:\s+[^>]*)?>")


class MockPublishRefusedError(RuntimeError):
    """Raised when attempting to publish mock, mixed, or unverified content to public Telegram channels."""
    pass


def esc(s: Any) -> str:
    """Sanitize HTML special characters for Telegram HTML parse mode."""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_tag_name(tag_str: str) -> str:
    """Extract lowercase tag name from HTML tag string."""
    m = re.match(r"<(/)?([a-zA-Z0-9_-]+)", tag_str)
    return m.group(2).lower() if m else ""


def split_text(text: str, limit: int = 3900) -> list[str]:
    """Split HTML text into chunks <= limit while ensuring all HTML tags are balanced.

    Resolves ISS-008:
    - Every chunk has length <= limit.
    - All tags opened within a chunk are cleanly closed before the chunk boundary.
    - Any tags active at a boundary are re-opened at the start of the next chunk.
    - Extremely long lines/words are safely chunked.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    lines = text.split("\n")
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    open_tags: list[str] = []

    def get_closing_tags_str(tags: list[str]) -> str:
        return "".join(f"</{get_tag_name(t)}>" for t in reversed(tags))

    def get_opening_tags_str(tags: list[str]) -> str:
        return "".join(tags)

    def scan_tags(s: str, stack: list[str]) -> None:
        for match in TAG_RE.finditer(s):
            is_closing = match.group(1) == "/"
            tag_name = match.group(2).lower()
            raw_tag = match.group(0)
            if is_closing:
                for idx in range(len(stack) - 1, -1, -1):
                    if get_tag_name(stack[idx]) == tag_name:
                        stack.pop(idx)
                        break
            else:
                stack.append(raw_tag)

    max_content_len = limit - 100

    for line in lines:
        sub_lines: list[str] = []
        if len(line) > max_content_len:
            if " " in line:
                words = line.split(" ")
                curr_sub = ""
                for w in words:
                    if len(w) > max_content_len:
                        if curr_sub:
                            sub_lines.append(curr_sub)
                            curr_sub = ""
                        for i in range(0, len(w), max_content_len):
                            sub_lines.append(w[i:i + max_content_len])
                    elif len(curr_sub) + len(w) + 1 > max_content_len and curr_sub:
                        sub_lines.append(curr_sub)
                        curr_sub = w
                    else:
                        curr_sub = (curr_sub + " " + w) if curr_sub else w
                if curr_sub:
                    sub_lines.append(curr_sub)
            else:
                sub_lines = [line[i:i + max_content_len] for i in range(0, len(line), max_content_len)]
        else:
            sub_lines = [line]

        for sub_line in sub_lines:
            test_stack = list(open_tags)
            scan_tags(sub_line, test_stack)
            closing_str = get_closing_tags_str(test_stack)

            line_addition = ("\n" if current_lines else "") + sub_line
            needed_len = current_len + len(line_addition) + len(closing_str)

            if needed_len > limit and current_lines:
                chunk_closing = get_closing_tags_str(open_tags)
                chunk_body = "\n".join(current_lines) + chunk_closing
                chunks.append(chunk_body)

                chunk_opening = get_opening_tags_str(open_tags)
                reopened_line = (chunk_opening + sub_line) if chunk_opening else sub_line
                current_lines = [reopened_line]
                scan_tags(sub_line, open_tags)
                current_len = len(reopened_line)
            else:
                current_lines.append(sub_line)
                scan_tags(sub_line, open_tags)
                current_len += len(line_addition)

    if current_lines:
        chunk_closing = get_closing_tags_str(open_tags)
        chunk_body = "\n".join(current_lines) + chunk_closing
        chunks.append(chunk_body)

    return chunks


async def send_telegram_message_resilient(
    bot: Bot,
    chat_id: str | int,
    text: str,
    max_retries: int = 3,
    base_backoff: float = 1.0,
    **kwargs: Any,
) -> Any:
    """Send Telegram message with exponential backoff and FloodWait handling."""
    for attempt in range(1, max_retries + 1):
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs,
            )
        except TelegramRetryAfter as e:
            wait_time = float(getattr(e, "retry_after", base_backoff * (2 ** (attempt - 1))))
            logger.warning(
                "Telegram FloodWait encountered for chat %s. Waiting %s seconds before retry (attempt %d/%d)...",
                chat_id,
                wait_time,
                attempt,
                max_retries,
            )
            if attempt == max_retries:
                raise
            await asyncio.sleep(wait_time)
        except (TimeoutError, TelegramNetworkError) as e:
            wait_time = base_backoff * (2 ** (attempt - 1))
            logger.warning(
                "Telegram NetworkError for chat %s: %s. Retrying in %s seconds (attempt %d/%d)...",
                chat_id,
                e,
                wait_time,
                attempt,
                max_retries,
            )
            if attempt == max_retries:
                raise
            await asyncio.sleep(wait_time)
        except TelegramAPIError as e:
            logger.error("Permanent Telegram API error for chat %s: %s", chat_id, e)
            raise


async def publish_report(
    report_date: Any,
    items: list[dict[str, Any]] | None = None,
    content_quality: str | None = None,
    report_text: str | None = None,
    bot: Bot | None = None,
    session: AsyncSession | None = None,
    dry_run: bool = False,
    channel_id: str | None = None,
    admin_chat_id: str | int | None = None,
    base_backoff: float = 1.0,
) -> list[dict[str, Any]]:
    """Publish formatted report to Telegram channel and admin chat.

    Strict Fail-Closed Security Policy:
    - If content_quality is NOT 'real' (e.g. 'mock', 'mixed', None, or unrecognized):
      Publishing is strictly REFUSED with MockPublishRefusedError, unless settings.app_env == 'test'.
    - No caller parameter can bypass this check in non-test environments.

    Dual Destination Delivery:
    - Channel: Receives full report chunks formatted in HTML.
    - Admin Chat: Receives operational summary and telemetry info.
    """
    if not dry_run and content_quality != "real":
        if settings.app_env != "test":
            raise MockPublishRefusedError(
                f"Publish refused: Daily report content_quality is '{content_quality}'. "
                f"Refusing to publish unverified/mock content to Telegram channels in '{settings.app_env}' environment."
            )

    # Resolve destination IDs
    target_channel = channel_id or settings.telegram_channel_id
    target_admin = admin_chat_id or settings.telegram_admin_chat_id

    # Resolve report text
    final_text = report_text
    db_report: DailyReport | None = None
    managed_session = False

    if session is not None:
        db_report = await session.scalar(
            select(DailyReport).where(DailyReport.report_date == report_date)
        )
    else:
        try:
            async with SessionLocal() as local_session:
                db_report = await local_session.scalar(
                    select(DailyReport).where(DailyReport.report_date == report_date)
                )
        except Exception as e:
            logger.debug("Could not query DailyReport from DB (likely unit test env): %s", e)

    if not final_text:
        if db_report and db_report.content and "formatted_text" in db_report.content:
            final_text = db_report.content["formatted_text"]
        elif items is not None:
            # Fallback format for items list
            final_text = format_persian_daily_report(report_date, items, as_html=True)
        else:
            final_text = f"📰 <b>گزارش تحولات خاورمیانه</b>\n📅 {report_date}\n\nگزارش خالی است."

    chunks = split_text(final_text, limit=3900)

    # Dry-run execution
    if dry_run:
        print("=" * 80)
        print(" TELEGRAM PUBLISHER [DRY RUN]")
        print("=" * 80)
        print(f"Report Date: {report_date}")
        print(f"Content Quality: {content_quality or 'real'}")
        print(f"Target Channel: {target_channel}")
        print(f"Target Admin Chat: {target_admin}")
        print(f"Total Text Length: {len(final_text)} characters")
        print(f"Total Chunks: {len(chunks)}")
        print("-" * 80)
        simulated_sent: list[dict[str, Any]] = []
        for idx, ch in enumerate(chunks, 1):
            print(f"Chunk #{idx} ({len(ch)} chars) -> Would send to channel {target_channel}")
            simulated_sent.append({"chat_id": target_channel, "message_id": 1000 + idx, "type": "channel"})
        if target_admin:
            print(f"Admin Summary -> Would send to admin {target_admin}")
            simulated_sent.append({"chat_id": str(target_admin), "message_id": 9999, "type": "admin"})
        print("=" * 80)
        return simulated_sent

    own_bot = False
    if bot is None:
        bot = Bot(
            settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        own_bot = True

    sent: list[dict[str, Any]] = []

    # Check for existing partial delivery from previous failed attempts (Idempotency Resume)
    start_chunk_idx = 0
    if db_report is not None and db_report.telegram_message_ids:
        existing_ids = list(db_report.telegram_message_ids)
        if len(existing_ids) < len(chunks):
            start_chunk_idx = len(existing_ids)
            sent = [
                {"chat_id": target_channel, "message_id": mid, "type": "channel"}
                for mid in existing_ids
            ]
            logger.info(
                "Resuming partial publish for date %s: skipping %d already sent chunks, sending remaining %d chunks",
                report_date,
                start_chunk_idx,
                len(chunks) - start_chunk_idx,
            )
        elif len(existing_ids) >= len(chunks):
            start_chunk_idx = len(chunks)
            sent = [
                {"chat_id": target_channel, "message_id": mid, "type": "channel"}
                for mid in existing_ids[:len(chunks)]
            ]
            logger.info(
                "All %d chunks already sent for date %s. Skipping channel delivery.",
                len(chunks),
                report_date,
            )

    try:
        # Step 1: Send remaining chunks to public Telegram channel
        if target_channel and start_chunk_idx < len(chunks):
            for part in chunks[start_chunk_idx:]:
                msg = await send_telegram_message_resilient(
                    bot=bot,
                    chat_id=target_channel,
                    text=part,
                    base_backoff=base_backoff,
                    disable_web_page_preview=True,
                )
                sent.append({"chat_id": target_channel, "message_id": msg.message_id, "type": "channel"})
                # Record partial progress if session available
                if session is not None and db_report is not None:
                    db_report.telegram_message_ids = [s["message_id"] for s in sent]
                    await session.commit()

        # Step 2: Send operational summary to admin chat
        if target_admin:
            channel_msg_ids = [s["message_id"] for s in sent if s["type"] == "channel"]
            admin_msg = (
                "🔔 <b>گزارش وضعیت انتشار در کانال تلگرام</b>\n"
                f"📅 <b>تاریخ:</b> <code>{report_date}</code>\n"
                f"📊 <b>وضعیت:</b> {len(chunks)} بخش با موفقیت در کانال منتشر گردید.\n"
                f"🛡 <b>کیفیت محتوا:</b> {esc(content_quality or 'real')}\n"
                f"🔗 <b>شناسه‌های پیام کانال:</b> <code>{channel_msg_ids}</code>"
            )
            admin_res = await send_telegram_message_resilient(
                bot=bot,
                chat_id=str(target_admin),
                text=admin_msg,
                base_backoff=base_backoff,
                disable_web_page_preview=True,
            )
            sent.append({"chat_id": str(target_admin), "message_id": admin_res.message_id, "type": "admin"})

        # Step 3: Mark DailyReport as published
        if session is not None and db_report is not None:
            db_report.status = "published"
            db_report.published_at = datetime.now(UTC)
            db_report.telegram_message_ids = [s["message_id"] for s in sent]
            await session.commit()

        return sent

    except Exception as exc:
        logger.error("Publishing failed after partial delivery: %s", exc)
        if session is not None and db_report is not None:
            db_report.status = "failed"
            db_report.telegram_message_ids = [s["message_id"] for s in sent]
            await session.commit()
        raise
    finally:
        if own_bot:
            await bot.session.close()


async def _run_cli_dry_run() -> None:
    parser = argparse.ArgumentParser(description="Telegram Publisher - Middle East News Bot")
    parser.add_argument("--dry-run", action="store_true", help="Perform dry run without calling Telegram API")
    parser.add_argument("--confirm", action="store_true", help="Explicit confirmation for production real publish")
    parser.add_argument("--date", type=str, default=None, help="Report date (YYYY-MM-DD)")
    args = parser.parse_args()

    # Guardrail against accidental real publish in production environment
    if settings.app_env == "production" and not args.dry_run and not args.confirm:
        print("ERROR: Real publish in production environment requires explicit --confirm flag!")
        return

    target_date = date.fromisoformat(args.date) if args.date else date.today()

    async with SessionLocal() as session:
        rep = await session.scalar(
            select(DailyReport).where(DailyReport.report_date == target_date)
        )
        if not rep:
            print(f"No DailyReport found in database for date {target_date}.")
            return

        cq = rep.content.get("content_quality", "real") if rep.content else "real"
        text = rep.content.get("formatted_text", "") if rep.content else ""
        print(f"Loaded DailyReport #{rep.id} for {target_date} (content_quality='{cq}', status='{rep.status}')")

        await publish_report(
            report_date=target_date,
            content_quality=cq,
            report_text=text,
            session=session,
            dry_run=True,
        )


if __name__ == "__main__":
    asyncio.run(_run_cli_dry_run())

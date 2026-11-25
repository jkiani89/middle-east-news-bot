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
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions
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


def esc(s: Any, quote: bool = False) -> str:
    """Sanitize HTML special characters for Telegram HTML parse mode."""
    if s is None:
        return ""
    text = str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote:
        text = text.replace('"', "&quot;").replace("'", "&#x27;")
    return text


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
    persist: bool = True,
    send_admin_summary: bool = True,
) -> list[dict[str, Any]]:
    """Publish formatted report to Telegram channel and admin chat.

    Strict Fail-Closed Security Policy:
    - If content_quality is NOT 'real' (e.g. 'mock', 'mixed', None, or unrecognized):
      Publishing is strictly REFUSED with MockPublishRefusedError, unless settings.app_env == 'test'.
    - No caller parameter can bypass this check in non-test environments.

    Dual Destination Delivery:
    - Channel: Receives the report text formatted in HTML.
    - Admin Chat: Receives operational summary and telemetry info, unless the caller
      sends its own fail-safe admin report and disables this summary.
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

    if not persist:
        # Scheduled briefings live in nested per-slot records; the caller owns them.
        db_report = None
    elif session is not None:
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
        # Logged (not printed) so the dry-run evidence survives a cancelled/timed-out job,
        # where block-buffered stdout is never flushed.
        logger.info("%s", "=" * 80)
        logger.info(" TELEGRAM PUBLISHER [DRY RUN]")
        logger.info("%s", "=" * 80)
        logger.info("Report Date: %s", report_date)
        logger.info("Content Quality: %s", content_quality or "real")
        logger.info("Target Channel: %s", target_channel)
        logger.info("Target Admin Chat: %s", target_admin)
        logger.info("Total Text Length: %d characters", len(final_text))
        logger.info("Total Chunks: %d", len(chunks))
        logger.info("%s", "-" * 80)
        simulated_sent: list[dict[str, Any]] = []
        for idx, ch in enumerate(chunks, 1):
            logger.info("Chunk #%d (%d chars) -> Would send to channel %s", idx, len(ch), target_channel)
            simulated_sent.append({"chat_id": target_channel, "message_id": 1000 + idx, "type": "channel"})
        if target_admin:
            logger.info("Admin Summary -> Would send to admin %s", target_admin)
            simulated_sent.append({"chat_id": str(target_admin), "message_id": 9999, "type": "admin"})
        logger.info("%s", "=" * 80)
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
        if target_admin and send_admin_summary:
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
        if persist and session is not None and db_report is not None:
            db_report.status = "published"
            db_report.published_at = datetime.now(UTC)
            db_report.telegram_message_ids = [s["message_id"] for s in sent]
            await session.commit()

        return sent

    except Exception as exc:
        logger.error("Publishing failed after partial delivery: %s", exc)
        if persist and session is not None and db_report is not None:
            db_report.status = "failed"
            db_report.telegram_message_ids = [s["message_id"] for s in sent]
            await session.commit()
        raise
    finally:
        if own_bot:
            await bot.session.close()


async def publish_admin_report(
    report_text: str,
    bot: Bot | None = None,
    admin_chat_id: str | int | None = None,
    base_backoff: float = 1.0,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Send the full statistical/operational report to the admin's private chat.

    The caller wraps this in ``try/except``: a failure here must never block the public
    channel delivery. In the test environment an injected bot is used as-is; without an
    injected bot nothing is sent, so the suite never touches the Telegram network.
    """
    target = str(admin_chat_id or settings.telegram_admin_chat_id or "")
    if not target or target == "0":
        logger.warning("Admin report skipped: TELEGRAM_ADMIN_CHAT_ID is not configured.")
        return []

    chunks = split_text(report_text, limit=3900)
    if dry_run or (bot is None and settings.app_env == "test"):
        logger.info(
            "Admin report [%s] -> %s (%d chunk(s)).",
            "DRY RUN" if dry_run else "TEST",
            target,
            len(chunks),
        )
        return [
            {"chat_id": target, "message_id": 9000 + index, "type": "admin"}
            for index in range(1, len(chunks) + 1)
        ]

    own_bot = False
    if bot is None:
        bot = Bot(
            settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        own_bot = True

    sent: list[dict[str, Any]] = []
    try:
        for chunk in chunks:
            msg = await send_telegram_message_resilient(
                bot=bot,
                chat_id=target,
                text=chunk,
                base_backoff=base_backoff,
                disable_web_page_preview=True,
            )
            sent.append({"chat_id": target, "message_id": msg.message_id, "type": "admin"})
        return sent
    finally:
        if own_bot:
            await bot.session.close()


# Country → flag mapping for the immediate Telegram block. Keys are matched after
# casefolding and Persian letter unification, so both the English names the analyzer
# returns and their Persian equivalents resolve to the same flag. Unknown names are
# simply skipped, so an unmapped story starts with no flag prefix at all.
COUNTRY_FLAGS: dict[str, str] = {
    "ایران": "🇮🇷",
    "iran": "🇮🇷",
    "اسرائیل": "🇮🇱",
    "israel": "🇮🇱",
    "آمریکا": "🇺🇸",
    "ایالات متحده": "🇺🇸",
    "ایالات متحده آمریکا": "🇺🇸",
    "america": "🇺🇸",
    "united states": "🇺🇸",
    "united states of america": "🇺🇸",
    "usa": "🇺🇸",
    "روسیه": "🇷🇺",
    "russia": "🇷🇺",
    "اوکراین": "🇺🇦",
    "ukraine": "🇺🇦",
    "چین": "🇨🇳",
    "china": "🇨🇳",
    "عربستان": "🇸🇦",
    "عربستان سعودی": "🇸🇦",
    "saudi arabia": "🇸🇦",
    "امارات": "🇦🇪",
    "امارات متحده عربی": "🇦🇪",
    "uae": "🇦🇪",
    "united arab emirates": "🇦🇪",
    "قطر": "🇶🇦",
    "qatar": "🇶🇦",
    "یمن": "🇾🇪",
    "yemen": "🇾🇪",
    "لبنان": "🇱🇧",
    "lebanon": "🇱🇧",
    "سوریه": "🇸🇾",
    "syria": "🇸🇾",
    "عراق": "🇮🇶",
    "iraq": "🇮🇶",
    "ترکیه": "🇹🇷",
    "turkey": "🇹🇷",
    "turkiye": "🇹🇷",
    "türkiye": "🇹🇷",
    "مصر": "🇪🇬",
    "egypt": "🇪🇬",
    "اردن": "🇯🇴",
    "jordan": "🇯🇴",
    "کویت": "🇰🇼",
    "kuwait": "🇰🇼",
    "عمان": "🇴🇲",
    "oman": "🇴🇲",
    "بحرین": "🇧🇭",
    "bahrain": "🇧🇭",
    "فلسطین": "🇵🇸",
    "سرزمین‌های فلسطینی": "🇵🇸",
    "palestine": "🇵🇸",
    "palestinian territories": "🇵🇸",
    "بریتانیا": "🇬🇧",
    "انگلستان": "🇬🇧",
    "britain": "🇬🇧",
    "united kingdom": "🇬🇧",
    "uk": "🇬🇧",
    "فرانسه": "🇫🇷",
    "france": "🇫🇷",
    "آلمان": "🇩🇪",
    "germany": "🇩🇪",
    "هند": "🇮🇳",
    "india": "🇮🇳",
    "پاکستان": "🇵🇰",
    "pakistan": "🇵🇰",
}
MAX_COUNTRY_FLAGS = 3

_PERSIAN_LETTER_MAP = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه"})


def _normalize_country_name(name: Any) -> str:
    """Casefold a country name and unify Persian letter variants for lookup."""
    normalized = str(name or "").strip().casefold().translate(_PERSIAN_LETTER_MAP)
    return " ".join(normalized.split())


def country_flags(countries: list[str] | None, limit: int = MAX_COUNTRY_FLAGS) -> list[str]:
    """Return unique flag emojis for the recognized countries, in order of appearance."""
    flags: list[str] = []
    for name in countries or []:
        flag = COUNTRY_FLAGS.get(_normalize_country_name(name))
        if flag and flag not in flags:
            flags.append(flag)
        if len(flags) >= limit:
            break
    return flags


@dataclass(slots=True)
class ImmediateAnalysis:
    """A single freshly analyzed article, ready for immediate Telegram delivery."""

    event_title: str
    headline_fa: str
    summary_fa: str
    source_name: str
    source_url: str
    verification_status: str = "unconfirmed"
    importance: int = 3
    category: str | None = None
    countries: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    content_quality: str = "full_text"
    content_quality_fa: str = "متن کامل"
    is_insufficient: bool = False

    @classmethod
    def from_analysis_result(cls, result: Any, source_name: str, source_url: str) -> ImmediateAnalysis:
        """Build the payload from a validated ``NewsAnalysis`` result."""
        categories = list(getattr(result, "categories", []) or [])
        return cls(
            event_title=getattr(result, "event_title", "") or "",
            headline_fa=getattr(result, "headline_fa", "") or "",
            summary_fa=getattr(result, "summary_fa", "") or "",
            source_name=source_name,
            source_url=source_url,
            verification_status=getattr(result, "verification_status", "unconfirmed") or "unconfirmed",
            importance=int(getattr(result, "importance", 3) or 3),
            category=categories[0] if categories else None,
            countries=list(getattr(result, "countries", []) or []),
            actors=list(getattr(result, "actors", []) or []),
        )


def format_immediate_analysis(analysis: ImmediateAnalysis) -> str:
    """Render the Persian headline/summary block sent right after analysis."""
    if analysis.source_url:
        source_rendered = f"<a href=\"{esc(analysis.source_url)}\">{esc(analysis.source_name)}</a>"
    else:
        source_rendered = esc(analysis.source_name)

    footer = f"🔗 منبع: {source_rendered}"

    summary = esc(analysis.summary_fa)
    flags = country_flags(analysis.countries)
    if flags:
        summary = f"{''.join(flags)} — {summary}"

    lines: list[str] = [
        f"📌 <b>{esc(analysis.headline_fa)}</b>",
        "",
        summary,
        "",
        footer,
    ]
    return "\n".join(lines)


async def publish_immediate_analysis(
    analysis: ImmediateAnalysis,
    bot: Bot | None = None,
    dry_run: bool | None = None,
    channel_id: str | None = None,
    delay_seconds: float | None = None,
    base_backoff: float = 1.0,
    admin_only: bool = False,
) -> list[dict[str, Any]]:
    """Publish one analyzed article to the channel immediately after it is stored.

    Only the Persian headline and summary are sent (never the raw article), and
    FloodWait/network errors are handled by ``send_telegram_message_resilient``.
    In dry-run mode nothing is sent and a ``[DRY RUN]`` block is printed instead.

    ``admin_only`` routes a high-impact single-source story to the admin chat
    instead of the public channel, so unverified claims are never broadcast.
    """
    effective_dry_run = settings.immediate_publish_dry_run if dry_run is None else dry_run
    if admin_only:
        target_channel = str(settings.telegram_admin_chat_id)
        prefix = (
            "🔒 <b>پیش‌نمایش ادمین — خبر تک‌منبعی با اهمیت بالا</b>\n"
            "<i>این مورد برای بازبینی ارسال شد و در کانال عمومی منتشر نشد.</i>\n\n"
        )
    else:
        target_channel = channel_id or settings.telegram_channel_id
        prefix = ""
    text = prefix + format_immediate_analysis(analysis)
    delivery_type = "admin" if admin_only else "immediate"
    chunks = split_text(text, limit=3900)
    pause = settings.immediate_publish_delay_seconds if delay_seconds is None else delay_seconds

    if effective_dry_run:
        # Structured logging (not print) keeps the dry-run preview readable in CI logs
        # even when the job is cancelled before stdout is flushed.
        logger.info("%s", "=" * 80)
        logger.info(" TELEGRAM IMMEDIATE PUBLISHER [DRY RUN]")
        logger.info("%s", "=" * 80)
        logger.info("Headline: %s", analysis.headline_fa)
        logger.info("Summary: %s", analysis.summary_fa)
        logger.info("Verification: %s", analysis.verification_status)
        logger.info("Importance: %s", analysis.importance)
        logger.info("Delivery: %s", delivery_type)
        logger.info("Target: %s", target_channel)
        logger.info("Total Text Length: %d characters", len(text))
        logger.info("Total Chunks: %d", len(chunks))
        logger.info("%s", "-" * 80)
        simulated: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks, 1):
            logger.info(
                "Chunk #%d (%d chars) -> Would send to %s %s", idx, len(chunk), delivery_type, target_channel
            )
            simulated.append({"chat_id": target_channel, "message_id": 2000 + idx, "type": delivery_type})
        logger.info("%s", "=" * 80)
        return simulated

    own_bot = False
    if bot is None:
        bot = Bot(
            settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        own_bot = True

    sent: list[dict[str, Any]] = []
    try:
        for idx, chunk in enumerate(chunks):
            msg = await send_telegram_message_resilient(
                bot=bot,
                chat_id=target_channel,
                text=chunk,
                base_backoff=base_backoff,
            )
            sent.append({"chat_id": target_channel, "message_id": msg.message_id, "type": delivery_type})
            if pause > 0 and idx < len(chunks) - 1:
                await asyncio.sleep(pause)
        return sent
    finally:
        if own_bot:
            await bot.session.close()


FLAG_REASON_LABELS = {
    "injection_detected": "تلاش برای تزریق پرامپت در متن منبع",
    "number_mismatch": "عدد بدون پشتوانه در متن فارسی",
    "other": "دلیل دیگر (بازبینی دستی)",
}
MAX_FLAG_HEADLINE_CHARS = 80
MAX_FLAG_NUMBERS = 5


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _is_http_url(url: str) -> bool:
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return False
    return scheme in {"http", "https"}


def _source_domain(url: str) -> str:
    """Bare host for the alert, so a hostile URL is never clickable there."""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _defang_host(value: str) -> str:
    """Render a URL or bare host inert: no scheme, every dot as ``[.]``.

    Injection reports must never let Telegram auto-link the attacker's domain, and
    the source *name* can itself be a URL or a bare host, so both go through here.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    try:
        parts = urlparse(cleaned)
    except ValueError:
        return cleaned.replace(".", "[.]")
    if parts.netloc:
        cleaned = parts.netloc
    elif parts.scheme and parts.path:
        cleaned = parts.path
    cleaned = cleaned.split("/")[0].split("?")[0].split("#")[0].lstrip("@")
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    return cleaned.replace(".", "[.]")


def format_flag_alert(entries: list[dict[str, Any]], total: int, max_items: int = 5) -> str:
    """Format one admin alert for articles parked for review (Phase Z)."""
    shown = entries[:max(0, max_items)]
    lines = [
        "🚩 <b>هشدار تحریریه: مقاله‌های پارک‌شده</b>",
        "<i>این موارد در همین اجرا به بازبینی انسانی سپرده شدند و منتشر نشدند.</i>",
        "",
    ]
    for index, entry in enumerate(shown, 1):
        reason_code = entry.get("reason", "other")
        reason = FLAG_REASON_LABELS.get(reason_code, FLAG_REASON_LABELS["other"])
        lines.append(f"{index}. <b>#{entry.get('article_id', '?')}</b> — {esc(reason)}")
        # An injected article's model output may itself carry the attack text, so only
        # its identity, reason, source and domain are shown: no headline, no link.
        if reason_code != "injection_detected":
            headline = _truncate(entry.get("headline_fa") or "", MAX_FLAG_HEADLINE_CHARS)
            lines.append(f"   ▫️ تیتر: {esc(headline)}")
            numbers = [str(value) for value in (entry.get("numbers") or [])][:MAX_FLAG_NUMBERS]
            if numbers:
                lines.append(f"   ▫️ عددهای بدون پشتوانه: {esc('، '.join(numbers))}")
        source = esc(entry.get("source_name") or "منبع نامشخص", quote=True)
        url = entry.get("source_url") or ""
        domain = _source_domain(url)
        # Only number_mismatch (and other non-injection) entries may expose a clickable
        # link, and only for http(s) URLs. Injection reports stay link-free.
        if reason_code != "injection_detected" and _is_http_url(url):
            lines.append(
                f"   ▫️ منبع: <a href=\"{html.escape(url, quote=True)}\">{source}</a>"
                + (f" ({esc(domain)})" if domain else "")
            )
        else:
            # Injection reports stay link-free: defang both the host from the URL and
            # the source name (which can itself be a URL or a bare domain), so no part
            # of this line is auto-linked by Telegram.
            if reason_code == "injection_detected":
                display_source = esc(_defang_host(entry.get("source_name") or "منبع نامشخص"), quote=True)
                display_domain = esc(_defang_host(domain))
            else:
                display_source = source
                display_domain = esc(domain)
            if display_domain and display_domain != display_source:
                lines.append(f"   ▫️ منبع: {display_source} ({display_domain})")
            else:
                lines.append(f"   ▫️ منبع: {display_source}")
        lines.append("")
    remaining = total - len(shown)
    if remaining > 0:
        lines.append(f"… و {remaining} مورد دیگر در همین اجرا پارک شد.")
    return "\n".join(lines).strip()


async def publish_flag_alert(
    entries: list[dict[str, Any]],
    total: int | None = None,
    bot: Bot | None = None,
    dry_run: bool | None = None,
    chat_id: str | int | None = None,
) -> list[dict[str, Any]]:
    """Send at most one admin message per run about articles parked for review.

    Only the entries passed in (this run's flagged articles) are reported; older
    parked rows are never re-announced. ``flag_alert_enabled=False`` or a dry run
    sends nothing. Telegram failures are logged and swallowed so the batch keeps
    its normal course.
    """
    if not settings.flag_alert_enabled or not entries:
        return []
    total_count = total if total is not None else len(entries)
    effective_dry_run = settings.immediate_publish_dry_run if dry_run is None else dry_run
    target = str(chat_id or settings.telegram_admin_chat_id or "")
    text = format_flag_alert(entries, total_count, max_items=settings.flag_alert_max_per_run)

    if effective_dry_run:
        logger.info(
            "TELEGRAM FLAG ALERT [DRY RUN] target=%s shown=%d total=%d",
            target or "(unset)",
            min(len(entries), settings.flag_alert_max_per_run),
            total_count,
        )
        return [{"chat_id": target, "message_id": 3000, "type": "flag_alert_dry_run"}]
    if not target:
        logger.warning("Flag alert skipped: TELEGRAM_ADMIN_CHAT_ID is not configured.")
        return []

    own_bot = False
    if bot is None:
        bot = Bot(
            settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        own_bot = True

    sent: list[dict[str, Any]] = []
    try:
        for chunk in split_text(text, limit=3900):
            msg = await send_telegram_message_resilient(
                bot=bot,
                chat_id=target,
                text=chunk,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            sent.append({"chat_id": target, "message_id": msg.message_id, "type": "flag_alert"})
        return sent
    except Exception:  # noqa: BLE001 - a failed alert must never break a batch
        logger.exception("Flag alert delivery failed; the batch continues.")
        return sent
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

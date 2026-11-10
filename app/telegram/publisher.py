"""Telegram Publisher Service for Middle East News Bot.

Implements tag-aware message splitting (resolving ISS-008) to ensure all HTML tags
are properly balanced across chunk boundaries, preventing Telegram ParseMode errors.
Strictly enforces fail-closed publishing guards based on content_quality and app_env.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import settings

logger = logging.getLogger(__name__)

TAG_RE = re.compile(r"<(/)?([a-zA-Z0-9_-]+)(?:\s+[^>]*)?>")


class MockPublishRefusedError(RuntimeError):
    """Raised when attempting to publish mock, mixed, or unverified content to public Telegram channels."""
    pass


def esc(s: Any) -> str:
    """Sanitize HTML special characters for Telegram HTML parse mode."""
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


async def publish_report(
    report_date: Any,
    items: list[dict[str, Any]],
    content_quality: str | None = None,
) -> list[dict[str, Any]]:
    """Publish formatted report to Telegram channel and admin chat.

    Strict Fail-Closed Security Policy:
    - If content_quality is NOT 'real' (e.g. 'mock', 'mixed', None, or unrecognized):
      Publishing is strictly REFUSED with MockPublishRefusedError, unless settings.app_env == 'test'.
    - No caller parameter can bypass this check in non-test environments.
    """
    if content_quality != "real":
        if settings.app_env != "test":
            raise MockPublishRefusedError(
                f"Publish refused: Daily report content_quality is '{content_quality}'. "
                f"Refusing to publish unverified/mock content to Telegram channels in '{settings.app_env}' environment."
            )

    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        lines = [
            "📰 <b>گزارش روزانه تحولات خاورمیانه</b>",
            f"📅 {report_date}",
            "",
        ]
        if not items:
            lines.append("در بازه موردنظر خبر واجد شرایطی ثبت نشده است.")
        for i, item in enumerate(items, 1):
            lines += [
                f"<b>{i}. {esc(item.get('headline', item.get('headline_fa', '')))}</b>",
                esc(item.get("summary", item.get("summary_fa", ""))),
                f"<i>منبع: {esc(item.get('source', item.get('primary_source', '')))} | وضعیت: {esc(item.get('verification_status', ''))}</i>",
                f'<a href="{esc(item.get("url", item.get("primary_url", "")))}">لینک منبع</a>',
                "",
            ]

        destinations = [settings.telegram_channel_id, str(settings.telegram_admin_chat_id)]
        sent: list[dict[str, Any]] = []
        full_text = "\n".join(lines)
        chunks = split_text(full_text, limit=3900)

        for chat_id in destinations:
            if not chat_id:
                continue
            for part in chunks:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    disable_web_page_preview=True,
                )
                sent.append({"chat_id": chat_id, "message_id": msg.message_id})
        return sent
    finally:
        await bot.session.close()

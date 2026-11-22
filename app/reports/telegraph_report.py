"""Telegraph publication of the 12-hour briefing.

One report = one index page + one page per **topic part**. Every news item of the window
lives inside its topic, and the index links to those pages. A topic that cannot fit inside
Telegraph's 64 KB page limit is split into several parts (:mod:`telegraph_pagination`),
chained to each other, and every part is listed on the index. Nothing is ever trimmed:
an unsplittable page fails loudly instead of losing the tail of its news.

Window membership is decided by the collection time of the articles, half-open
``[start, end)``, so a story belongs to exactly one report. Re-running a report edits the
same topic pages instead of creating new ones; a story that also appeared in the previous
report is never removed from the current one, because membership is never derived from
what was published before.

``telegraph_reports`` tracks the two channels independently:

* ``status``          - pending / in_progress / telegraph_completed / telegraph_partial / failed
* ``telegram_status`` - pending / sent / failed

A failed notification therefore only retries the notification; completed pages are reused.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Article, ArticleEvent, Event, Source, TelegraphReport, utcnow
from app.editorial.headlines import first_sentence, sanitize_headline
from app.editorial.telegraph_topics import (
    UNPROCESSED_TOPIC_KEY,
    UNPROCESSED_TOPIC_NAME,
    UNPUBLISHED_TOPIC_KEY,
    UNPUBLISHED_TOPIC_NAME,
    group_items_by_telegraph_topic,
    telegraph_topic,
)
from app.editorial.topics import topic_for_event, topics_for_event
from app.reports.daily_report import (
    current_slot_hour,
    fetch_report_items,
    report_window,
    slot_label,
    slot_report_date,
)
from app.reports.telegraph_client import (
    TelegraphClient,
    TelegraphContentTooLargeError,
    TelegraphError,
    build_client,
)
from app.reports.telegraph_pagination import (
    LAYOUT_VERSION,
    PAGE_CONTENT_BUDGET_BYTES,
    layout_hash,
    measure_nodes,
    part_layout_hash,
    plan_topic_parts,
)

logger = logging.getLogger(__name__)


class AmbiguousOrphanError(TelegraphError):
    """More than one Telegraph page matches a pending create.

    The run is aborted instead of guessing which page to adopt: rewriting the wrong page
    would silently destroy someone else's content, and leaving the ambiguity unresolved
    would create a duplicate.
    """


TELEGRAPH_PENDING = "pending"
TELEGRAPH_IN_PROGRESS = "in_progress"
TELEGRAPH_COMPLETED = "telegraph_completed"
TELEGRAPH_PARTIAL = "telegraph_partial"
TELEGRAPH_FAILED = "failed"
TELEGRAPH_DISABLED = "disabled"

TELEGRAM_PENDING = "pending"
TELEGRAM_SENT = "sent"
TELEGRAM_FAILED = "failed"

_TELEGRAPH_FINAL_STATES = {TELEGRAPH_COMPLETED, TELEGRAPH_PARTIAL}
SUMMARY_MISSING_LABEL = "خلاصه در دسترس نیست"
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_number(value: int) -> str:
    """Render a number with Persian digits, as the rest of the report does."""
    return str(int(value)).translate(_FA_DIGITS)


def stable_title_prefix(topic_name: str, report_date: date, slot_hour: int, part: int) -> str:
    """Title prefix that identifies a part without depending on the total part count.

    The display title ends with ``— بخش k از n`` (or with nothing when there is a single
    part), so matching on the full title would break whenever the number of parts changes.
    This prefix stops at the part number and is what recovery matches against.
    """
    label = slot_label(slot_hour)
    return f"{topic_name} — گزارش {label} {report_date.isoformat()} — بخش {fa_number(part)}"


def page_identity_stamp(report_date: date, slot_hour: int) -> str:
    """Compact slot stamp used inside the page identity line (``20261117T2000``)."""
    return f"{report_date.strftime('%Y%m%d')}T{int(slot_hour):02d}00"


def page_identity_line(stamp: str, topic_key: str, part: int, layout_hash: str) -> str:
    """The single compact line that identifies a page and its content.

    ``شناسه: 20261117T2000|ir|1|sha1:<40 hex>`` carries everything recovery needs (slot,
    subject, part and the full content fingerprint) in one paragraph instead of two.
    Nothing is shortened: the fingerprint keeps its algorithm prefix and all 40 hex
    characters, because a truncated hash would weaken the duplicate guard.

    The title is display text (it may be reworded, and the number of parts changes), so
    the page itself has to carry this line. Recovery reads it back with
    ``getPage(return_content=True)``; a page without it, or with a different one, is never
    adopted.
    """
    return f"شناسه: {stamp}|{topic_key}|{int(part)}|{layout_hash}"


def title_matches_part(title: str, topic_name: str, report_date: date, slot_hour: int, part: int) -> bool:
    """Whether a Telegraph title belongs to the given slot/topic/part.

    A single-part page is published without the ``— بخش …`` suffix, so it is recognised
    as part 1 by its bare title as well as by the prefixed form. The part number needs a
    boundary: ``بخش ۱`` must not match ``بخش ۱۰``.
    """
    candidate = str(title or "")
    label = slot_label(slot_hour)
    base = f"{topic_name} — گزارش {label} {report_date.isoformat()}"
    if part == 1 and candidate == base:
        return True
    prefix = stable_title_prefix(topic_name, report_date, slot_hour, part)
    if candidate == prefix:
        return True
    return candidate.startswith(f"{prefix} از ")


def _is_http_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _format_local_time(value: Any) -> str:
    """Render a stored ISO timestamp in the project timezone."""
    if not value:
        return "نامشخص"
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    tz = ZoneInfo(settings.timezone)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _anchor(label: str, url: str) -> dict[str, Any] | None:
    if not url or not _is_http_url(url):
        return None
    return {"tag": "a", "attrs": {"href": url}, "children": [label]}


def _paragraph(*children: Any) -> dict[str, Any]:
    return {"tag": "p", "children": [child for child in children if child]}


def _article_status(article: Article) -> str:
    """The stored status as a plain string (empty when the row carries none)."""
    raw = getattr(article, "status", "")
    return raw if isinstance(raw, str) else ""


# D-9 decision (2026-11-21): a story the editorial gate reviewed and turned down is
# reported as a count in the transparency note, never as a page of raw, untranslated
# headlines. The same applies to an analysis error. They are still counted as
# represented so the completeness check keeps meaning "no news silently lost".
COUNT_ONLY_STATUSES: frozenset[str] = frozenset({"rejected", "failed"})


def _unsaved_status_labels(status: str) -> tuple[str, str, str]:
    """Section and label for an article that produced no editorial summary.

    The three outcomes are not the same story and must not share one section or one
    label: a ``collected``/``expired`` row was never analysed, a ``rejected`` row was
    reviewed and turned down, and a ``failed`` row hit an error. Mixing them under
    "سایر تحولات" made the report claim analysis that never happened.
    """
    if status == "rejected":
        return (
            UNPUBLISHED_TOPIC_KEY,
            UNPUBLISHED_TOPIC_NAME,
            "⛔ ردشده در بررسی تحریریه",
        )
    if status == "failed":
        return (
            UNPUBLISHED_TOPIC_KEY,
            UNPUBLISHED_TOPIC_NAME,
            "⚠️ خطا در تحلیل (بدون خلاصه تحریریه)",
        )
    if status == "expired":
        return (
            UNPROCESSED_TOPIC_KEY,
            UNPROCESSED_TOPIC_NAME,
            "⌛ منقضی پیش از تحلیل (بدون خلاصه تحریریه)",
        )
    return (
        UNPROCESSED_TOPIC_KEY,
        UNPROCESSED_TOPIC_NAME,
        "⏳ در انتظار تحلیل (بدون خلاصه تحریریه)",
    )


def _article_item(article: Article, source: Source | None) -> dict[str, Any]:
    """A collected article that has no editorial summary yet."""
    moment = article.published_at or article.collected_at
    status = _article_status(article)
    topic_key, topic_fa, verification_fa = _unsaved_status_labels(status)
    return {
        "event_id": None,
        "article_id": article.id,
        "headline_fa": str(article.title or "").strip() or "خبر بدون عنوان",
        # A feed title that is only the outlet name is caught at render time; this is the
        # first replacement candidate (the RSS body's own lead sentence).
        "headline_alt_fa": first_sentence(article.original_text),
        "summary_fa": "",
        "summary_missing": True,
        "primary_source": getattr(source, "name", "") or "منبع نامشخص",
        "primary_url": article.original_url or "",
        "has_media": bool(getattr(article, "has_media", False)),
        "first_seen_at": moment.isoformat() if moment else "",
        "verification_status": "unconfirmed",
        "verification_status_fa": verification_fa,
        "processing_status": status,
        "topic_key": topic_key,
        "topic_keys": [topic_key],
        "topic_fa": topic_fa,
        "additional_sources": [],
        "is_insufficient": True,
        "content_quality_fa": SUMMARY_MISSING_LABEL,
        "review_pending": False,
    }


def _event_item_without_summary(
    event: Event, article: Article, source: Source | None
) -> dict[str, Any]:
    """A collected event whose summary is not ready: publish its own facts, flagged."""
    item = _article_item(article, source)
    topic = topic_for_event(event)
    item_topics = topics_for_event(event)
    # The event exists but its editorial summary is not ready: it has a subject, so it
    # stays in that subject's section; only an event with no resolvable topic falls back
    # to the "not analysed" heading.
    fallback_key = UNPROCESSED_TOPIC_KEY
    fallback_name = UNPROCESSED_TOPIC_NAME
    item.update(
        {
            "event_id": int(event.id),
            "headline_fa": str(event.title or item["headline_fa"]).strip(),
            "headline_alt_fa": str(event.title or "").strip(),
            "verification_status_fa": "❓ تأییدنشده (بدون خلاصه تحریریه)",
            "topic_key": topic.key if topic else fallback_key,
            "topic_keys": (
                [t.key for t in item_topics]
                if item_topics
                else ([topic.key] if topic else [fallback_key])
            ),
            "topic_fa": topic.name_fa if topic else fallback_name,
            "importance": event.importance,
            "countries": list(event.countries or []),
            "actors": list(event.actors or []),
            "location": event.location or "منطقه",
        }
    )
    return item


async def fetch_complete_window_items(
    session: AsyncSession,
    report_date: date | None = None,
    slot_hour: int | None = None,
) -> tuple[date, list[dict[str, Any]], dict[str, Any]]:
    """Every article collected in the window, with or without a summary.

    The report cannot silently lose collected news: an article whose analysis never ran
    (no event), or whose event has no summary yet, is published with its original title,
    source, link and the ``خلاصه در دسترس نیست`` label. Counts of collected articles and
    represented articles are returned so the two are directly reconcilable.
    """
    if slot_hour is None:
        slot_hour = current_slot_hour()
    rep_date, items, metadata = await fetch_report_items(
        session, report_date=report_date, slot_hour=slot_hour, include_all=True
    )
    _resolved, window_start, window_end = report_window(rep_date, slot_hour)

    rows = (
        await session.execute(
            select(Article, Source, ArticleEvent.event_id)
            .join(Source, Source.id == Article.source_id)
            .outerjoin(ArticleEvent, ArticleEvent.article_id == Article.id)
            .where(
                Article.collected_at >= window_start,
                Article.collected_at < window_end,
            )
            .order_by(Article.collected_at.asc())
        )
    ).all()

    represented_events = {
        int(item["event_id"]) for item in items if item.get("event_id") is not None
    }
    missing_event_ids = {
        int(event_id)
        for _article, _source, event_id in rows
        if event_id is not None and int(event_id) not in represented_events
    }
    events_by_id: dict[int, Event] = {}
    if missing_event_ids:
        event_rows = (
            await session.execute(select(Event).where(Event.id.in_(missing_event_ids)))
        ).scalars().all()
        events_by_id = {int(event.id): event for event in event_rows}

    synthetic: list[dict[str, Any]] = []
    seen_events: set[int] = set()
    articles_represented = 0
    unlinked_articles = 0
    events_without_summary = 0
    status_counts: dict[str, int] = {}

    for article, source, event_id in rows:
        status = _article_status(article)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status in COUNT_ONLY_STATUSES:
            articles_represented += 1
            continue
        if event_id is None:
            unlinked_articles += 1
            synthetic.append(_article_item(article, source))
            articles_represented += 1
            continue
        event_key = int(event_id)
        articles_represented += 1
        if event_key in represented_events or event_key in seen_events:
            continue
        event = events_by_id.get(event_key)
        seen_events.add(event_key)
        events_without_summary += 1
        if event is not None:
            synthetic.append(_event_item_without_summary(event, article, source))
        else:
            item = _article_item(article, source)
            item["event_id"] = event_key
            synthetic.append(item)

    all_items = items + synthetic
    # The window is split by what actually happened to each article, so the report can
    # count "never analysed" separately from "reviewed and rejected" and "errored".
    unprocessed = status_counts.get("collected", 0) + status_counts.get("expired", 0)
    metadata = {
        **metadata,
        "collected_articles": len(rows),
        "articles_represented": articles_represented,
        "articles_without_summary": events_without_summary,
        "unlinked_articles": unlinked_articles,
        "articles_by_status": dict(status_counts),
        "articles_unprocessed": unprocessed,
        "articles_rejected": status_counts.get("rejected", 0),
        "articles_failed": status_counts.get("failed", 0),
        "event_items": len(items),
        "synthetic_items": len(synthetic),
        "news_included": len(all_items),
    }
    if articles_represented != len(rows):
        logger.error(
            "Window %s..%s: %d of %d collected article(s) are not represented in the report.",
            window_start.isoformat(),
            window_end.isoformat(),
            len(rows) - articles_represented,
            len(rows),
        )
    logger.info(
        "Window %s..%s: %d collected article(s) -> %d report item(s) "
        "(%d without summary, %d without event).",
        window_start.isoformat(),
        window_end.isoformat(),
        len(rows),
        len(all_items),
        events_without_summary,
        unlinked_articles,
    )
    return rep_date, all_items, metadata


def build_item_block(
    item: dict[str, Any], stats: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Nodes for one news item inside a topic page.

    Only the system summary is published - never the source article body - and the title,
    source, original link, time and verification label are always present. A headline that
    carries no information (a bare outlet name such as ``- رادیو فردا``) is replaced through
    :func:`app.editorial.headlines.sanitize_headline`, and the substitution is counted in
    ``stats`` so the run reports it instead of hiding it.
    """
    source = str(item.get("primary_source") or "منبع نامشخص").strip()
    url = str(item.get("primary_url") or "").strip()
    verification = str(
        item.get("verification_status_fa") or item.get("verification_status") or "نامشخص"
    ).strip()
    published_at = _format_local_time(item.get("first_seen_at") or item.get("collected_at"))

    resolved = sanitize_headline(
        item.get("headline_fa"),
        source=source if source != "منبع نامشخص" else None,
        alt=item.get("headline_alt_fa") or item.get("title"),
        summary=item.get("summary_fa"),
        local_time=published_at,
    )
    headline = resolved.text
    if resolved.replaced and stats is not None:
        stats["headline_fallbacks"] = int(stats.get("headline_fallbacks") or 0) + 1
        stats.setdefault("headline_reasons", {})
        stats["headline_reasons"][resolved.reason] = (
            int(stats["headline_reasons"].get(resolved.reason) or 0) + 1
        )

    nodes: list[dict[str, Any]] = [{"tag": "h4", "children": [headline]}]
    summary = str(item.get("summary_fa") or "").strip()
    if summary:
        nodes.append(_paragraph(summary))
    else:
        nodes.append(
            _paragraph(
                f"⚠️ {SUMMARY_MISSING_LABEL}. عنوان، منبع و لینک اصلی خبر در ادامه آمده است."
            )
        )

    nodes.append(_paragraph("📡 منبع: ", source))
    nodes.append(_paragraph("🕒 زمان انتشار: ", published_at))
    nodes.append(_paragraph("🛡 وضعیت راستی‌آزمایی: ", verification))

    link = _anchor(url, url)
    nodes.append(_paragraph("🔗 لینک اصلی خبر: ", link if link else "در دسترس نیست"))
    if item.get("has_media"):
        nodes.append(_paragraph("🖼 دارای تصویر/ویدئو"))

    if item.get("review_pending"):
        nodes.append(
            _paragraph("🚩 این خبر برای بازبینی تحریریه علامت‌گذاری شده و وضعیت آن شفاف اعلام می‌شود.")
        )
    if item.get("language_warning"):
        nodes.append(
            _paragraph("⚠️ هشدار زبانی: متن این مورد توسط سامانه بازبینی زبان تأیید نشده است.")
        )
    if item.get("is_insufficient") and summary:
        nodes.append(
            _paragraph(
                "⚠️ کیفیت محتوا: ",
                str(item.get("content_quality_fa") or "اطلاعات ناکافی"),
                " (گزارش اولیه / نیازمند تکمیل جزئیات)",
            )
        )

    perspectives = [p for p in item.get("perspectives") or [] if isinstance(p, dict)]
    if perspectives:
        nodes.append(_paragraph("🗣 مقایسه روایت‌ها:"))
        for claim in perspectives:
            source_name = str(claim.get("source_name") or "").strip()
            claim_text = str(claim.get("claim_fa") or "").strip()
            if claim_text:
                nodes.append(_paragraph(f"{source_name}: " if source_name else "", claim_text))
        disagreement = str(item.get("disagreement_fa") or "").strip()
        if disagreement:
            nodes.append(_paragraph("اختلاف روایت‌ها: ", disagreement))

    additional = [s for s in item.get("additional_sources") or [] if isinstance(s, dict)]
    if additional:
        nodes.append(_paragraph("🔗 پوشش‌های موازی و منابع تکمیلی:"))
        for source_entry in additional:
            name = str(source_entry.get("source") or "منبع پیگیری").strip()
            link = _anchor(name, str(source_entry.get("url") or ""))
            nodes.append(_paragraph("• ", link if link else name))
    return nodes


def _part_links(
    part_urls: dict[int, str], total_parts: int, current_part: int
) -> list[Any]:
    """Inline list of every part of a topic; the current part is rendered as text."""
    children: list[Any] = []
    for part in range(1, total_parts + 1):
        if children:
            children.append(" | ")
        label = f"بخش {fa_number(part)}"
        if part == current_part:
            children.append({"tag": "b", "children": [label]})
            continue
        link = _anchor(label, part_urls.get(part) or "")
        children.append(link if link else label)
    return children


def build_part_navigation(
    *,
    position: str,
    current_part: int,
    total_parts: int,
    part_urls: dict[int, str],
    topic_item_count: int,
    part_item_count: int,
    index_url: str = "",
) -> list[dict[str, Any]]:
    """Navigation block that chains the parts of one topic together.

    The top block tells the reader how many pages the topic has and where this page sits;
    the bottom block walks to the neighbouring parts and back to the report index. A
    single-part topic renders no navigation at all, so its page keeps the original shape.
    """
    if total_parts <= 1:
        return []
    if position == "top":
        return [
            _paragraph(
                f"این موضوع {topic_item_count} خبر دارد و در {fa_number(total_parts)} صفحه "
                f"منتشر شده است؛ این صفحه بخش {fa_number(current_part)} از "
                f"{fa_number(total_parts)} است."
            ),
            _paragraph(f"تعداد اخبار این صفحه: {part_item_count}"),
            _paragraph("بخش‌های این موضوع: ", *_part_links(part_urls, total_parts, current_part)),
        ]

    previous = _anchor("بخش پیشین", part_urls.get(current_part - 1) or "")
    following = _anchor("بخش بعدی", part_urls.get(current_part + 1) or "")
    nodes: list[dict[str, Any]] = []
    walk: list[Any] = []
    if previous:
        walk.extend(["➡️ ", previous])
    if following:
        walk.extend([" | " if walk else "", "⬅️ ", following])
    if walk:
        nodes.append(_paragraph(*walk))
    index_link = _anchor("صفحه اصلی گزارش", index_url)
    if index_link:
        nodes.append(_paragraph("📑 فهرست کامل گزارش: ", index_link))
    return nodes


def build_topic_part(
    topic_name: str,
    report_date: date,
    slot_hour: int,
    items: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    *,
    part: int = 1,
    total_parts: int = 1,
    topic_item_count: int | None = None,
    topic_key: str = "",
    nav_top: list[dict[str, Any]] | None = None,
    nav_bottom: list[dict[str, Any]] | None = None,
    stats: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build one page of one topic: its header, its news blocks, its footer.

    ``items`` is a contiguous slice of the topic in publication order, so the pages of a
    topic always reproduce the original sequence when read in order.
    """
    label = slot_label(slot_hour)
    title = f"{topic_name} — گزارش {label} {report_date.isoformat()}"
    if total_parts > 1:
        title = f"{title} — بخش {fa_number(part)} از {fa_number(total_parts)}"
    start_text = window_start.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")
    end_text = window_end.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")

    total_items = len(items) if topic_item_count is None else topic_item_count
    nodes: list[dict[str, Any]] = [
        _paragraph(f"بازه گزارش: از {start_text} تا {end_text} (به وقت {settings.timezone})"),
        _paragraph(f"تعداد اخبار این موضوع: {total_items}"),
    ]
    nodes.extend(nav_top or [])
    for index, item in enumerate(items):
        if index:
            nodes.append({"tag": "hr"})
        nodes.extend(build_item_block(item, stats))

    nodes.append({"tag": "hr"})
    nodes.extend(nav_bottom or [])
    # One compact machine-readable line: slot, subject, part and the content fingerprint.
    # It is what lets a later run prove that an orphan page is really this slot/topic/part
    # - and still holds exactly these news items - before adopting it instead of creating
    # a duplicate.
    if topic_key:
        nodes.append(
            _paragraph(
                page_identity_line(
                    page_identity_stamp(report_date, slot_hour),
                    topic_key,
                    part,
                    part_layout_hash(items),
                )
            )
        )
    nodes.append(
        _paragraph(
            "🛡 این بخش خودکار و بر پایه اصول بی‌طرفی و انتساب دقیق منابع تدوین شده است. "
            "خبرهای تک‌منبعی و تأییدنشده با برچسب شفاف منتشر شده‌اند."
        )
    )
    return title, nodes


def build_topic_page(
    topic_name: str,
    report_date: date,
    slot_hour: int,
    items: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> tuple[str, list[dict[str, Any]]]:
    """Build a single-page topic (the layout every topic used before pagination)."""
    return build_topic_part(
        topic_name, report_date, slot_hour, items, window_start, window_end
    )


def build_page_overhead_bytes(
    report_date: date,
    slot_hour: int,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Upper bound of everything on a topic page that is not a news block.

    Measured with placeholder numbers wider than any real value (four-digit counts,
    two-digit part numbers), so the real header and footer can only be smaller.
    """
    _title, nodes = build_topic_part(
        "موضوع",
        report_date,
        slot_hour,
        [],
        window_start,
        window_end,
        part=99,
        total_parts=99,
        topic_item_count=9999,
        # Longest real topic slug, so the reserved overhead covers every page.
        topic_key="us-cn",
    )
    return measure_nodes(nodes)


def build_index_page(
    report_date: date,
    slot_hour: int,
    sections: list[tuple[str, str, list[dict[str, Any]]]],
    topic_pages: dict[str, dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build the index page linking to every topic page of one report."""
    metadata = metadata or {}
    label = slot_label(slot_hour)
    # Unique news items across all sections (multi-topic items are counted once).
    unique_ids: set[Any] = set()
    for _key, _name, topic_items in sections:
        for item in topic_items:
            uid = item.get("event_id")
            if uid is None:
                uid = id(item)
            unique_ids.add(uid)
    total = len(unique_ids)
    title = f"گزارش {label} — {report_date.isoformat()}"

    start_text = window_start.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")
    end_text = window_end.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")

    nodes: list[dict[str, Any]] = [
        _paragraph(f"بازه گزارش: از {start_text} تا {end_text} (به وقت {settings.timezone})"),
        _paragraph(f"تعداد اخبار این بازه: {total}"),
        _paragraph(
            "وضعیت راستی‌آزمایی هر خبر در صفحه موضوع همان خبر آمده است. "
            "خبرهای تک‌منبعی، تأییدنشده و خبرهای بدون خلاصه نیز با برچسب شفاف منتشر شده‌اند."
        ),
    ]

    if not sections:
        nodes.append(_paragraph("در این بازه خبری برای انتشار ثبت نشده است."))
    for key, name, topic_items in sections:
        entry = normalize_topic_entry(topic_pages.get(key))
        page_url = str(entry.get("url") or "")
        icon = telegraph_topic(key).icon
        display_name = f"{icon} {name}".strip() if icon else name
        if page_url and _is_http_url(page_url):
            link = {"tag": "a", "attrs": {"href": page_url}, "children": [{"tag": "b", "children": [display_name]}]}
        else:
            link = None
        part_count = int(entry.get("part_count") or 1)
        suffix = f" — {len(topic_items)} خبر"
        if part_count > 1:
            suffix = f" — {len(topic_items)} خبر در {fa_number(part_count)} صفحه"
        if link is not None:
            nodes.append(_paragraph("• ", link, suffix))
        else:
            nodes.append(_paragraph("• ", {"tag": "b", "children": [display_name]}, suffix))
        if part_count > 1:
            # Every part is linked from the index itself, so a reader never has to walk a
            # chain to find the news of a long subject.
            nodes.append(
                _paragraph(
                    "   ",
                    *_part_links(part_urls_of(entry), part_count, 0),
                )
            )

    transparency: list[str] = []
    if metadata.get("articles_unprocessed"):
        transparency.append(
            f"{fa_number(int(metadata['articles_unprocessed']))} خبر هنوز تحلیل نشده بود "
            f"(در صف یا منقضی پیش از تحلیل) و در بخش «{UNPROCESSED_TOPIC_NAME}» با برچسب "
            f"«{SUMMARY_MISSING_LABEL}» منتشر شد."
        )
    if metadata.get("articles_rejected"):
        transparency.append(
            f"{fa_number(int(metadata['articles_rejected']))} خبر بررسی و در گیت تحریریه رد شد؛ "
            f"این موارد فقط به‌صورت شمارشی در همین یادداشت گزارش می‌شوند و جزو اخبار «تحلیل‌نشده» نیستند."
        )
    if metadata.get("articles_failed"):
        transparency.append(
            f"{fa_number(int(metadata['articles_failed']))} خبر در مرحله تحلیل با خطا مواجه شد؛ "
            f"این موارد فقط به‌صورت شمارشی در همین یادداشت گزارش می‌شوند."
        )
    if metadata.get("articles_without_summary"):
        transparency.append(
            f"{fa_number(int(metadata['articles_without_summary']))} رویداد هنوز خلاصه تحریریه نداشت و "
            f"با برچسب «{SUMMARY_MISSING_LABEL}» منتشر شد."
        )
    if metadata.get("unlinked_articles"):
        transparency.append(
            f"{fa_number(int(metadata['unlinked_articles']))} خبر هنوز به رویدادی متصل نشده بود و "
            f"با اطلاعات اصلی خود منتشر شد."
        )
    if metadata.get("flagged_events_included"):
        transparency.append(
            f"{metadata['flagged_events_included']} رویداد برای بازبینی تحریریه علامت‌گذاری شده است."
        )
    for note in transparency:
        nodes.append(_paragraph("یادداشت شفافیت: ", note))

    nodes.append(
        _paragraph("🛡 این گزارش خودکار و بر پایه اصول بی‌طرفی و انتساب دقیق منابع تدوین شده است.")
    )
    return title, nodes


def format_telegram_notification(
    report_date: date,
    slot_hour: int,
    index_url: str,
    sections: list[tuple[str, str, list[dict[str, Any]]]],
    metadata: dict[str, Any] | None = None,
    topic_pages: dict[str, dict[str, Any]] | None = None,
) -> str:
    """HTML notification that points Telegram readers at the full Telegraph report.

    The index link leads to the whole 12-hour report and every topic is listed with a
    direct link to its own page, so a reader reaches a subject in one tap instead of
    going through the index first. A topic whose page URL is missing (dry run, partial
    publication) is still listed by name with its item count, never dropped.
    """
    metadata = metadata or {}
    pages = topic_pages or {}
    unique_ids: set[Any] = set()
    for _key, _name, topic_items in sections:
        for item in topic_items:
            uid = item.get("event_id")
            if uid is None:
                uid = id(item)
            unique_ids.add(uid)
    total = len(unique_ids)
    label = slot_label(slot_hour)
    window_start = metadata.get("window_start")
    window_end = metadata.get("window_end")
    window_text = ""
    if window_start and window_end:
        window_text = (
            f"\n🕒 بازه: {_format_local_time(window_start)} تا {_format_local_time(window_end)}"
        )

    section_lines: list[str] = []
    for key, name, topic_items in sections:
        entry = normalize_topic_entry(pages.get(key))
        page_url = str(entry.get("url") or "")
        icon = telegraph_topic(key).icon
        icon_prefix = f"{icon} " if icon else ""
        if _is_http_url(page_url):
            label_html = f'<a href="{page_url}"><b>{icon_prefix}{name}</b></a>'
        else:
            label_html = f"{icon_prefix}<b>{name}</b>"
        part_count = int(entry.get("part_count") or 1)
        suffix = f": {len(topic_items)} خبر"
        if part_count > 1:
            suffix += f" در {fa_number(part_count)} صفحه"
            ordered_parts = sorted(
                entry.get("parts") or [], key=lambda part: int(part.get("part") or 0)
            )
            links = [
                f'<a href="{str(part.get("url") or "")}">{fa_number(int(part.get("part") or 0))}</a>'
                for part in ordered_parts
                if not part.get("retired") and _is_http_url(str(part.get("url") or ""))
            ]
            if links:
                suffix += f" (بخش‌ها: {'، '.join(links)})"
        section_lines.append(f"• {label_html}{suffix}")
    link_html = (
        f'<a href="{index_url}">صفحه اصلی گزارش در Telegraph</a>'
        if _is_http_url(index_url)
        else "لینک گزارش در دسترس نیست."
    )

    lines = [
        f"📊 <b>گزارش ۱۲ ساعته تحولات — نوبت {label}</b>",
        f"📅 تاریخ: {report_date.isoformat()}{window_text}",
        f"📰 {total} خبر در {len(sections)} موضوع",
        *section_lines,
        "",
        f"🔗 {link_html}",
        "🛡 خبرهای تک‌منبعی و بدون خلاصه با برچسب شفاف در گزارش باقی مانده‌اند.",
    ]
    return "\n".join(lines)


def _new_row(report_date: date, slot_hour: int) -> TelegraphReport:
    return TelegraphReport(
        report_date=report_date,
        slot_hour=slot_hour,
        status=TELEGRAPH_PENDING,
        telegram_status=TELEGRAM_PENDING,
        topic_pages={},
        window_start=None,
        window_end=None,
    )


async def get_report_row(
    session: AsyncSession, report_date: date, slot_hour: int
) -> TelegraphReport | None:
    return await session.scalar(
        select(TelegraphReport).where(
            TelegraphReport.report_date == report_date,
            TelegraphReport.slot_hour == slot_hour,
        )
    )


async def _get_or_create_row(
    session: AsyncSession, report_date: date, slot_hour: int
) -> TelegraphReport:
    row = await get_report_row(session, report_date, slot_hour)
    if row is not None:
        return row
    row = _new_row(report_date, slot_hour)
    session.add(row)
    await session.commit()
    return row


def normalize_topic_entry(raw: Any) -> dict[str, Any]:
    """Read a stored topic entry, upgrading a flat pre-pagination record.

    A record written before pagination carries one page and no part list; it is exactly
    part 1 of today's layout, so it must never be treated as missing (that would publish a
    second copy of the same topic).
    """
    entry = dict(raw) if isinstance(raw, dict) else {}
    stored = entry.get("parts")
    parts: list[dict[str, Any]] = []
    if isinstance(stored, list) and stored:
        parts = [dict(part) for part in stored if isinstance(part, dict)]
    else:
        legacy_path = str(entry.get("path") or "")
        legacy_url = str(entry.get("url") or "")
        if legacy_path or legacy_url:
            parts = [
                {
                    "part": 1,
                    "path": legacy_path,
                    "url": legacy_url,
                    "item_count": entry.get("item_count"),
                    "legacy": True,
                }
            ]
    entry["parts"] = sorted(parts, key=lambda part: int(part.get("part") or 0))
    entry["layout_version"] = int(entry.get("layout_version") or (LAYOUT_VERSION if stored else 1))
    active = [part for part in entry["parts"] if not part.get("retired")]
    entry["part_count"] = int(entry.get("part_count") or len(active) or 1)
    active = [part for part in entry["parts"] if not part.get("retired")]
    chosen = active[0] if active else (entry["parts"][0] if entry["parts"] else {})
    entry["path"] = str(chosen.get("path") or entry.get("path") or "")
    entry["url"] = str(chosen.get("url") or entry.get("url") or "")
    return entry


def part_urls_of(entry: dict[str, Any]) -> dict[int, str]:
    return {
        int(part.get("part") or 0): str(part.get("url") or "")
        for part in entry.get("parts") or []
        if isinstance(part, dict)
    }


def entry_has_unresolved_part(entry: Any) -> bool:
    """Whether the record still carries an unresolved create or a refused orphan."""
    normalised = normalize_topic_entry(entry)
    return any(
        part.get("pending_create") or part.get("orphan") for part in normalised["parts"]
    )


def report_layout_is_current(topic_pages: Any) -> bool:
    """Whether every stored topic is a complete, fully paginated publication.

    Used to decide that a completed report really has nothing left to repair: a record
    written by the old trimming code, or one whose published count does not match what it
    planned, is rebuilt even when the caller did not pass ``force``.
    """
    if not isinstance(topic_pages, dict) or not topic_pages:
        return False
    for raw in topic_pages.values():
        entry = normalize_topic_entry(raw)
        active = [part for part in entry["parts"] if not part.get("retired")]
        if not active or any(not part.get("path") for part in active):
            return False
        if any(part.get("pending_create") or part.get("orphan") for part in entry["parts"]):
            return False
        if entry.get("layout_version") != LAYOUT_VERSION:
            return False
        if entry.get("links_complete") is not True:
            return False
        if int(entry.get("published_item_count") or -1) != int(
            entry.get("planned_item_count") or 0
        ):
            return False
    return True


def _topic_entry(
    name: str,
    parts: dict[int, dict[str, Any]],
    *,
    layout: str,
    planned: int,
    links_complete: bool,
) -> dict[str, Any]:
    """Assemble the stored record of one topic, with its part 1 as the topic's URL."""
    ordered = [parts[number] for number in sorted(parts)]
    active = [part for part in ordered if not part.get("retired")]
    # Only a part with a path is really online; a pending create is not published news.
    published = sum(
        int(part.get("item_count") or 0) for part in active if part.get("path")
    )
    first = active[0] if active else {}
    return {
        "name_fa": name,
        "layout_version": LAYOUT_VERSION,
        "layout_hash": layout,
        "planned_item_count": planned,
        "published_item_count": published,
        "part_count": len(active),
        "links_complete": links_complete,
        "path": str(first.get("path") or ""),
        "url": str(first.get("url") or ""),
        # Kept for readers written before pagination (item_count was the topic's total).
        "item_count": planned,
        "parts": ordered,
    }


def _retired_part_nodes(
    total_parts: int, first_part_url: str, index_url: str
) -> list[dict[str, Any]]:
    """Stand-in content for a part that a smaller layout no longer needs.

    Telegraph cannot delete a page, and leaving yesterday's slice online would show news
    twice (or out of date). The page therefore becomes a pointer to the current layout.
    """
    nodes = [
        _paragraph(
            f"این بخش در به‌روزرسانی گزارش ادغام شد. موضوع اکنون در "
            f"{fa_number(total_parts)} صفحه منتشر شده است."
        )
    ]
    first_link = _anchor("بخش نخست این موضوع", first_part_url)
    nodes.append(_paragraph("↩️ ", first_link if first_link else "بخش نخست در دسترس نیست"))
    index_link = _anchor("صفحه اصلی گزارش", index_url)
    if index_link:
        nodes.append(_paragraph("📑 فهرست کامل گزارش: ", index_link))
    return nodes


async def _recover_pending_page(
    client: TelegraphClient,
    record: dict[str, Any],
    *,
    topic_key: str,
    topic_name: str,
    report_date: date,
    slot_hour: int,
    part: int,
    expected_item_count: int | None,
    expected_layout_hash: str | None,
) -> dict[str, Any] | None:
    """Find the page a crashed run created but never recorded.

    Identity is proven against the *stable* identity of the part (slot + topic_key + part
    number) and then against the page's own content. If any proof fails, the page is
    reported as an orphan and is never adopted or rewritten.
    """
    pending = record.get("pending_create")
    if not isinstance(pending, dict):
        return None

    expected = (f"{report_date.isoformat()} {slot_hour:02d}:00", str(topic_key), int(part))
    marker = (
        str(pending.get("slot") or ""),
        str(pending.get("topic_key") or ""),
        int(pending.get("part") or 0),
    )
    if marker != expected:
        # A marker that does not identify this slot/topic/part proves nothing about the
        # page that may exist; publishing anyway could duplicate it.
        return {
            "orphan": "",
            "url": "",
            "reason": f"pending marker identity {marker} does not match {expected}",
        }

    expected_items = (
        expected_item_count if expected_item_count is not None else pending.get("item_count")
    )
    expected_hash = (
        expected_layout_hash if expected_layout_hash is not None else pending.get("layout_hash")
    )
    if not isinstance(expected_items, int) or not expected_items:
        return {"orphan": "", "url": "", "reason": "missing expected item count in marker"}
    if not isinstance(expected_hash, str) or not expected_hash:
        return {"orphan": "", "url": "", "reason": "missing expected layout hash in marker"}

    try:
        # The whole account, not one window: a page outside the newest 200 would otherwise
        # look absent and be republished as a duplicate.
        pages, listing_complete = await client.list_pages_until_end()
    except TelegraphError as exc:
        # Without the listing the absence of an orphan cannot be proven, so creating a page
        # here could publish the same news twice.
        logger.warning(
            "Could not list Telegraph pages to recover slot %s topic %s part %s: %s",
            report_date,
            topic_key,
            part,
            exc,
        )
        return {"orphan": "", "url": "", "reason": f"getPageList failed: {exc}"}
    if not listing_complete:
        return {
            "orphan": "",
            "url": "",
            "reason": "getPageList ended without proving the full page list",
        }

    candidates = [
        page for page in pages
        if title_matches_part(str(page.get("title") or ""), topic_name, report_date, slot_hour, part)
        and str(page.get("path") or "")
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise AmbiguousOrphanError(
            f"ambiguous orphan: {len(candidates)} Telegraph pages match slot "
            f"{report_date.isoformat()} {slot_hour:02d}:00 topic {topic_key!r} part {part}; "
            f"paths={[str(p.get('path')) for p in candidates]}"
        )

    page = candidates[0]
    path = str(page.get("path") or "")
    url = str(page.get("url") or f"https://telegra.ph/{path}")

    # Structural proof: read the page back and verify its content.
    try:
        full = await client.get_page(path, return_content=True)
    except TelegraphError as exc:
        logger.warning("Could not read orphan candidate %s: %s", path, exc)
        return {"orphan": path, "url": url, "reason": f"getPage failed: {exc}"}

    content = full.get("content")
    try:
        serialised = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        serialised = ""
    parsed = content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            parsed = []
        else:
            # Telegraph may hand the content back with \uXXXX escapes, so the proof is
            # matched against the decoded Persian text rather than the wire form.
            serialised = json.dumps(parsed, ensure_ascii=False)

    expected_identity = page_identity_line(
        page_identity_stamp(report_date, slot_hour), topic_key, part, expected_hash
    )
    identity_prefix = expected_identity[: expected_identity.rfind("|") + 1]
    if identity_prefix not in serialised:
        # Slot, subject and part are proven by the page itself, never by its title alone: a
        # page published for another slot or subject can carry a similar display title.
        return {
            "orphan": path,
            "url": url,
            "reason": "page identity line for this slot/topic/part not found in content",
        }
    if expected_identity not in serialised:
        # Right page, wrong content: it must not be adopted (nor rewritten) on a stale
        # fingerprint.
        return {
            "orphan": path,
            "url": url,
            "reason": "content fingerprint missing or mismatched",
        }
    if f"بخش {fa_number(part)}" not in serialised:
        return {"orphan": path, "url": url, "reason": "part number not found in content"}
    h4_count = sum(
        1 for node in (parsed if isinstance(parsed, list) else [])
        if isinstance(node, dict) and node.get("tag") == "h4"
    )
    if h4_count != int(expected_items):
        return {"orphan": path, "url": url, "reason": f"h4 count {h4_count} != expected {expected_items}"}

    logger.warning(
        "Adopting the verified orphan page %s for slot %s topic %s part %s.",
        path,
        report_date,
        topic_key,
        part,
    )
    return {"path": path, "url": url, "adopted": True}


async def publish_telegraph_report(
    session: AsyncSession,
    report_date: date | None = None,
    slot_hour: int | None = None,
    client: TelegraphClient | None = None,
    items: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    dry_run: bool = False,
    force: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    """Publish (or resume) the topic pages of one report slot.

    One page per topic: a topic whose page already exists is edited in place, so retries
    and re-runs never create duplicate pages. Progress is committed after every page, so
    a crash halfway leaves the finished topics recorded for the next attempt.
    """
    if slot_hour is None:
        slot_hour = current_slot_hour()
    rep_date = report_date or slot_report_date(slot_hour)
    _resolved_date, window_start, window_end = report_window(rep_date, slot_hour)
    label = slot_label(slot_hour)

    if not settings.telegraph_enabled:
        return {
            "status": TELEGRAPH_DISABLED,
            "index_url": "",
            "index_path": "",
            "pages": {},
            "created": 0,
            "reused": 0,
            "failures": [],
            "skipped": True,
            "item_count": 0,
            "window_start": window_start,
            "window_end": window_end,
            "sections": [],
        }

    if settings.app_env == "production" and not settings.telegraph_access_token:
        # A throwaway account cannot edit yesterday's pages, so in production a missing
        # token means "no Telegraph this run", never "create one more account". The
        # caller treats TELEGRAPH_DISABLED exactly like the operator switch: the full
        # bulletin goes to Telegram, so the 12-hour report still ships.
        logger.error(
            "TELEGRAPH_ACCESS_TOKEN is not configured; publishing the bulletin as full "
            "text instead of creating a throwaway account that cannot edit earlier pages."
        )
        return {
            "status": TELEGRAPH_DISABLED,
            "index_url": "",
            "index_path": "",
            "pages": {},
            "created": 0,
            "reused": 0,
            "failures": [{"scope": "config", "error": "TELEGRAPH_ACCESS_TOKEN missing"}],
            "skipped": True,
            "item_count": 0,
            "window_start": window_start,
            "window_end": window_end,
            "sections": [],
        }

    row = await _get_or_create_row(session, rep_date, slot_hour) if persist else _new_row(rep_date, slot_hour)

    # A completed report is only skipped when its stored layout is the current one; a
    # record written by the trimming code (or by a run that left a part unrecorded) is
    # repaired instead of being trusted.
    if (
        row.status == TELEGRAPH_COMPLETED
        and row.index_url
        and not force
        and report_layout_is_current(row.topic_pages)
    ):
        logger.info("Telegraph report %s for %s is already complete; skipping.", label, rep_date)
        return {
            "status": row.status,
            "index_url": row.index_url or "",
            "index_path": row.index_path or "",
            "pages": dict(row.topic_pages or {}),
            "created": 0,
            "updated": 0,
            "reused": len(row.topic_pages or {}),
            "recovered": 0,
            "failures": [],
            "skipped": True,
            "item_count": 0,
            "planned_item_count": 0,
            "published_item_count": 0,
            "part_count": 0,
            "max_part_bytes": 0,
            "headline_fallbacks": 0,
            "part_plans": [],
            "window_start": row.window_start or window_start,
            "window_end": row.window_end or window_end,
            "sections": [],
        }

    if items is None:
        _date, items, fetched_metadata = await fetch_complete_window_items(
            session, report_date=rep_date, slot_hour=slot_hour
        )
        metadata = {**fetched_metadata, **(metadata or {})}
    metadata = dict(metadata or {})
    metadata.setdefault("window_start", window_start.isoformat())
    metadata.setdefault("window_end", window_end.isoformat())

    row.window_start = window_start
    row.window_end = window_end
    row.status = TELEGRAPH_IN_PROGRESS
    row.error_message = None
    if persist:
        await session.commit()

    sections = group_items_by_telegraph_topic(items)
    pages: dict[str, dict[str, Any]] = {
        key: normalize_topic_entry(value) for key, value in (row.topic_pages or {}).items()
    }
    created = 0
    updated = 0
    reused = 0
    recovered = 0
    planned_placements = 0
    abort_run = False
    stats: dict[str, Any] = {"headline_fallbacks": 0}
    failures: list[dict[str, Any]] = []

    effective_client = client or build_client()
    if effective_client is not None and not dry_run:
        try:
            await effective_client.ensure_token()
        except TelegraphError as exc:
            logger.exception("Could not authenticate with Telegraph for %s.", label)
            effective_client = None
            failures.append({"scope": "account", "error": str(exc)})

    if effective_client is None and not dry_run:
        row.status = TELEGRAPH_FAILED
        row.error_message = json.dumps(failures or [{"scope": "client", "error": "unavailable"}])[:2000]
        row.updated_at = utcnow()
        if persist:
            await session.commit()
        return {
            "status": row.status,
            "index_url": "",
            "index_path": "",
            "pages": pages,
            "created": created,
            "reused": reused,
            "failures": failures,
            "skipped": False,
            "item_count": len(items),
            "window_start": window_start,
            "window_end": window_end,
            "sections": sections,
            "metadata": metadata,
        }

    overhead_bytes = build_page_overhead_bytes(rep_date, slot_hour, window_start, window_end)
    index_url = str(row.index_url or "")
    index_path = str(row.index_path or "")
    part_plans: list[dict[str, Any]] = []
    max_part_bytes = 0

    for key, name, topic_items in sections:
        if abort_run:
            break
        planned_placements += len(topic_items)
        try:
            planned = plan_topic_parts(
                topic_items,
                block_builder=lambda item: build_item_block(item, stats),
                overhead_bytes=overhead_bytes,
                topic=name,
            )
        except TelegraphContentTooLargeError as exc:
            logger.error("Topic %s in slot %s cannot be split: %s", key, label, exc)
            failures.append({"scope": "layout", "topic": key, "error": str(exc)})
            continue

        planned_hash = layout_hash(planned)
        existing = normalize_topic_entry(pages.get(key))
        stored_parts: dict[int, dict[str, Any]] = {
            int(part.get("part") or 0): dict(part) for part in existing["parts"]
        }
        part_plans.append(
            {
                "topic": key,
                "name_fa": name,
                "item_count": len(topic_items),
                "part_count": len(planned),
                "part_items": [part.item_count for part in planned],
                "layout_hash": planned_hash,
            }
        )
        unchanged = (
            existing.get("layout_hash") == planned_hash
            and int(existing.get("part_count") or 0) == len(planned)
            and existing.get("links_complete") is True
            and all(str(stored_parts.get(part.part, {}).get("path") or "") for part in planned)
        )
        if unchanged and not force:
            reused += 1
            continue

        if dry_run:
            dry_parts: dict[int, dict[str, Any]] = {}
            for plan in planned:
                previous = stored_parts.get(plan.part) or {}
                dry_parts[plan.part] = {
                    "part": plan.part,
                    "path": str(previous.get("path") or "")
                    or f"dry-run-{rep_date.isoformat()}-{slot_hour:02d}-{key}-{plan.part}",
                    "url": str(previous.get("url") or ""),
                    "item_count": plan.item_count,
                }
                if previous.get("path"):
                    updated += 1
                else:
                    created += 1
            pages[key] = _topic_entry(
                name, dry_parts, layout=planned_hash, planned=len(topic_items), links_complete=True
            )
            continue

        entry_parts: dict[int, dict[str, Any]] = {
            number: dict(part) for number, part in stored_parts.items()
        }
        nav_incomplete = False
        topic_failed = False
        for plan in planned:
            urls = {number: str(part.get("url") or "") for number, part in entry_parts.items()}
            if any(not urls.get(number) for number in range(1, len(planned) + 1)):
                nav_incomplete = True
            nav_top = build_part_navigation(
                position="top",
                current_part=plan.part,
                total_parts=len(planned),
                part_urls=urls,
                topic_item_count=len(topic_items),
                part_item_count=plan.item_count,
            )
            nav_bottom = build_part_navigation(
                position="bottom",
                current_part=plan.part,
                total_parts=len(planned),
                part_urls=urls,
                topic_item_count=len(topic_items),
                part_item_count=plan.item_count,
                index_url=index_url,
            )
            title, nodes = build_topic_part(
                name,
                rep_date,
                slot_hour,
                list(plan.items),
                window_start,
                window_end,
                part=plan.part,
                total_parts=len(planned),
                topic_item_count=len(topic_items),
                topic_key=key,
                nav_top=nav_top,
                nav_bottom=nav_bottom,
                stats=stats,
            )
            size = measure_nodes(nodes)
            max_part_bytes = max(max_part_bytes, size)
            if size > PAGE_CONTENT_BUDGET_BYTES:
                failures.append(
                    {
                        "scope": "layout",
                        "topic": key,
                        "part": plan.part,
                        "error": f"rendered part is {size} bytes, over the {PAGE_CONTENT_BUDGET_BYTES}-byte budget",
                    }
                )
                topic_failed = True
                break

            record = dict(entry_parts.get(plan.part) or {"part": plan.part})
            page_path = str(record.get("path") or "")
            is_new_page = False
            try:
                assert effective_client is not None
                if page_path:
                    page = await effective_client.edit_page(
                        path=page_path, title=title, content=nodes
                    )
                else:
                    attempt = await _recover_pending_page(
                        effective_client,
                        record,
                        topic_key=key,
                        topic_name=name,
                        report_date=rep_date,
                        slot_hour=slot_hour,
                        part=plan.part,
                        expected_item_count=plan.item_count,
                        expected_layout_hash=part_layout_hash(plan.items),
                    )
                    if attempt and attempt.get("adopted"):
                        record.update({"path": attempt["path"], "url": attempt["url"]})
                        record.pop("orphan", None)
                        recovered += 1
                        page = await effective_client.edit_page(
                            path=attempt["path"], title=title, content=nodes
                        )
                    elif attempt is not None:
                        # Identity could not be proven: record the page as an orphan and
                        # write nothing, so a suspected page is never rewritten and a
                        # duplicate is never created.
                        orphan = {
                            "path": str(attempt.get("orphan") or ""),
                            "url": str(attempt.get("url") or ""),
                            "reason": str(attempt.get("reason") or "identity not proven"),
                            "at": utcnow().isoformat(),
                        }
                        entry_parts[plan.part] = {
                            **record,
                            "path": "",
                            "item_count": plan.item_count,
                            "bytes": size,
                            "orphan": orphan,
                        }
                        pages[key] = _topic_entry(
                            name,
                            entry_parts,
                            layout=planned_hash,
                            planned=len(topic_items),
                            links_complete=False,
                        )
                        row.topic_pages = dict(pages)
                        if persist:
                            await session.commit()
                        logger.error(
                            "Orphan page for topic %s part %s in slot %s: %s",
                            key,
                            plan.part,
                            label,
                            orphan["reason"],
                        )
                        failures.append(
                            {
                                "scope": "orphan",
                                "topic": key,
                                "part": plan.part,
                                "path": orphan["path"],
                                "error": orphan["reason"],
                            }
                        )
                        topic_failed = True
                        break
                    else:
                        # Record the intent before the write: if this process dies between
                        # the create and its commit, the next run proves this page's
                        # identity from constraint-free data instead of publishing a
                        # duplicate.
                        entry_parts[plan.part] = {
                            **record,
                            "path": "",
                            "item_count": plan.item_count,
                            "bytes": size,
                            "pending_create": {
                                "title": title,
                                "slot": f"{rep_date.isoformat()} {slot_hour:02d}:00",
                                "topic_key": key,
                                "part": plan.part,
                                "item_count": plan.item_count,
                                "layout_hash": part_layout_hash(plan.items),
                                "at": utcnow().isoformat(),
                            },
                        }
                        pages[key] = _topic_entry(
                            name,
                            entry_parts,
                            layout=planned_hash,
                            planned=len(topic_items),
                            links_complete=False,
                        )
                        row.topic_pages = dict(pages)
                        if persist:
                            await session.commit()
                        page = await effective_client.create_page(title=title, content=nodes)
                        is_new_page = True
            except AmbiguousOrphanError as exc:
                # Two pages claim the same part: nothing can be proven, so the whole run
                # stops instead of publishing more pages on top of the ambiguity.
                logger.error("Ambiguous orphan in slot %s: %s", label, exc)
                failures.append(
                    {"scope": "orphan_ambiguous", "topic": key, "part": plan.part, "error": str(exc)}
                )
                topic_failed = True
                abort_run = True
                break
            except Exception as exc:  # one bad page must not lose the rest of the report
                logger.exception(
                    "Telegraph page failed for topic %s part %s in slot %s.", key, plan.part, label
                )
                failures.append(
                    {"scope": "topic", "topic": key, "part": plan.part, "error": str(exc)}
                )
                topic_failed = True
                break

            published_part = {
                **record,
                "part": plan.part,
                "path": page.path or page_path,
                "url": page.url or str(record.get("url") or ""),
                "item_count": plan.item_count,
                "bytes": size,
            }
            published_part.pop("pending_create", None)
            entry_parts[plan.part] = published_part
            pages[key] = _topic_entry(
                name,
                entry_parts,
                layout=planned_hash,
                planned=len(topic_items),
                links_complete=False,
            )
            row.topic_pages = dict(pages)
            if persist:
                await session.commit()
            if is_new_page:
                created += 1
            else:
                updated += 1

        if topic_failed:
            continue

        if nav_incomplete and len(planned) > 1:
            # Second pass: every part exists now, so the chain can be completed. Nobody
            # reads the pages between the two passes (this runs before the index and the
            # notification), so a half-linked chain is never visible to readers.
            final_urls = {
                number: str(part.get("url") or "") for number, part in entry_parts.items()
            }
            for plan in planned:
                nav_top = build_part_navigation(
                    position="top",
                    current_part=plan.part,
                    total_parts=len(planned),
                    part_urls=final_urls,
                    topic_item_count=len(topic_items),
                    part_item_count=plan.item_count,
                )
                nav_bottom = build_part_navigation(
                    position="bottom",
                    current_part=plan.part,
                    total_parts=len(planned),
                    part_urls=final_urls,
                    topic_item_count=len(topic_items),
                    part_item_count=plan.item_count,
                    index_url=index_url,
                )
                title, nodes = build_topic_part(
                    name,
                    rep_date,
                    slot_hour,
                    list(plan.items),
                    window_start,
                    window_end,
                    part=plan.part,
                    total_parts=len(planned),
                    topic_item_count=len(topic_items),
                    topic_key=key,
                    nav_top=nav_top,
                    nav_bottom=nav_bottom,
                )
                size = measure_nodes(nodes)
                max_part_bytes = max(max_part_bytes, size)
                if size > PAGE_CONTENT_BUDGET_BYTES:
                    failures.append(
                        {
                            "scope": "layout",
                            "topic": key,
                            "part": plan.part,
                            "error": f"part with full navigation is {size} bytes",
                        }
                    )
                    topic_failed = True
                    break
                try:
                    assert effective_client is not None
                    page = await effective_client.edit_page(
                        path=str(entry_parts[plan.part]["path"]), title=title, content=nodes
                    )
                except Exception as exc:
                    logger.exception(
                        "Telegraph navigation failed for topic %s part %s in slot %s.",
                        key,
                        plan.part,
                        label,
                    )
                    failures.append(
                        {"scope": "navigation", "topic": key, "part": plan.part, "error": str(exc)}
                    )
                    topic_failed = True
                    break
                entry_parts[plan.part] = {
                    **entry_parts[plan.part],
                    "url": page.url or str(entry_parts[plan.part].get("url") or ""),
                    "bytes": size,
                }
            if topic_failed:
                pages[key] = _topic_entry(
                    name,
                    entry_parts,
                    layout=planned_hash,
                    planned=len(topic_items),
                    links_complete=False,
                )
                row.topic_pages = dict(pages)
                if persist:
                    await session.commit()
                continue

        surplus = [number for number in sorted(entry_parts) if number > len(planned)]
        for number in surplus:
            stale_path = str(entry_parts[number].get("path") or "")
            if not stale_path:
                # A part with an unresolved create may still be online: dropping its marker
                # would lose the only trace of an orphan page.
                if entry_parts[number].get("pending_create") or entry_parts[number].get("orphan"):
                    failures.append(
                        {
                            "scope": "surplus",
                            "topic": key,
                            "part": number,
                            "error": "unresolved pending/orphan part is no longer in the layout",
                        }
                    )
                    continue
                entry_parts.pop(number, None)
                continue
            stub_title = f"{name} — بخش {fa_number(number)} (ادغام‌شده)"
            try:
                assert effective_client is not None
                await effective_client.edit_page(
                    path=stale_path,
                    title=stub_title,
                    content=_retired_part_nodes(
                        len(planned),
                        str(entry_parts.get(1, {}).get("url") or ""),
                        index_url,
                    ),
                )
            except Exception as exc:
                logger.exception("Could not retire part %s of topic %s.", number, key)
                failures.append(
                    {"scope": "retire", "topic": key, "part": number, "error": str(exc)}
                )
                continue
            entry_parts[number] = {**entry_parts[number], "retired": True, "item_count": 0}

        pages[key] = _topic_entry(
            name, entry_parts, layout=planned_hash, planned=len(topic_items), links_complete=True
        )
        row.topic_pages = dict(pages)
        if persist:
            await session.commit()

    index_title, index_nodes = build_index_page(
        rep_date, slot_hour, sections, pages, window_start, window_end, metadata
    )

    if abort_run:
        # The run stopped on an unresolved ambiguity: publishing an index for a report that
        # was deliberately left half-written would point readers at an incomplete layout.
        logger.error("Skipping the index page for slot %s after an aborted run.", label)
    elif dry_run:
        index_path = index_path or f"dry-run-{rep_date.isoformat()}-{slot_hour:02d}-index"
    else:
        try:
            assert effective_client is not None
            if index_path:
                index_page = await effective_client.edit_page(
                    path=index_path, title=index_title, content=index_nodes
                )
            else:
                index_page = await effective_client.create_page(
                    title=index_title, content=index_nodes
                )
            index_path = index_page.path
            index_url = index_page.url
        except Exception as exc:
            logger.exception("Telegraph index page failed for slot %s.", label)
            failures.append({"scope": "index", "error": str(exc)})

    published_placements = sum(
        int(pages.get(key, {}).get("published_item_count") or 0)
        for key, _name, _topic_items in sections
    )
    # Success is counted, not assumed: a slot is only complete when every planned
    # placement of every planned part is online.
    if planned_placements != published_placements:
        failures.append(
            {
                "scope": "completeness",
                "planned": planned_placements,
                "published": published_placements,
            }
        )

    if failures and pages:
        status = TELEGRAPH_PARTIAL
    elif failures:
        status = TELEGRAPH_FAILED
    else:
        status = TELEGRAPH_COMPLETED
    if dry_run:
        status = TELEGRAPH_COMPLETED

    row.topic_pages = dict(pages)
    row.index_path = index_path or None
    row.index_url = index_url or None
    row.status = status
    row.error_message = json.dumps(failures)[:2000] if failures else None
    row.updated_at = utcnow()
    if persist:
        await session.commit()

    total_parts = sum(int(entry.get("part_count") or 0) for entry in pages.values())
    logger.info(
        "Telegraph report %s for %s: %s (%d created, %d updated, %d reused, %d recovered, "
        "%d failure(s), %d/%d item(s) published on %d page(s), max %d bytes).",
        label,
        rep_date,
        status,
        created,
        updated,
        reused,
        recovered,
        len(failures),
        published_placements,
        planned_placements,
        total_parts,
        max_part_bytes,
    )
    return {
        "status": status,
        "index_url": index_url,
        "index_path": index_path,
        "pages": pages,
        "created": created,
        "updated": updated,
        "reused": reused,
        "recovered": recovered,
        "failures": failures,
        "skipped": False,
        "item_count": len(items),
        "planned_item_count": planned_placements,
        "published_item_count": published_placements,
        "part_count": total_parts,
        "max_part_bytes": max_part_bytes,
        "headline_fallbacks": int(stats.get("headline_fallbacks") or 0),
        "part_plans": part_plans,
        "window_start": window_start,
        "window_end": window_end,
        "metadata": metadata,
        "sections": sections,
    }


async def record_telegram_status(
    session: AsyncSession,
    report_date: date,
    slot_hour: int,
    status: str,
    message_id: int | None = None,
    error: str | None = None,
) -> None:
    """Store Telegram delivery state separately from the Telegraph state."""
    row = await get_report_row(session, report_date, slot_hour)
    if row is None:
        row = _new_row(report_date, slot_hour)
        session.add(row)
    row.telegram_status = status
    if message_id:
        row.telegram_message_id = message_id
    if error:
        row.error_message = f"telegram: {error}"[:2000]
    row.updated_at = utcnow()
    await session.commit()


def notification_ready(result: dict[str, Any]) -> bool:
    """Whether a report has an index page worth notifying Telegram readers about."""
    return bool(result.get("index_url")) and result.get("status") in _TELEGRAPH_FINAL_STATES

"""Telegraph publication of the 12-hour briefing.

One report = one index page + one page per **topic**. Every news item of the window lives
inside its topic page, and the index links to those topic pages.

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
from app.editorial.telegraph_topics import group_items_by_telegraph_topic
from app.editorial.topics import topic_for_event
from app.reports.daily_report import (
    current_slot_hour,
    fetch_report_items,
    report_window,
    slot_label,
    slot_report_date,
)
from app.reports.telegraph_client import TelegraphClient, TelegraphError, build_client

logger = logging.getLogger(__name__)

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


def _article_item(article: Article, source: Source | None) -> dict[str, Any]:
    """A collected article that has no editorial summary yet."""
    moment = article.published_at or article.collected_at
    return {
        "event_id": None,
        "article_id": article.id,
        "headline_fa": str(article.title or "").strip() or "خبر بدون عنوان",
        "summary_fa": "",
        "summary_missing": True,
        "primary_source": getattr(source, "name", "") or "منبع نامشخص",
        "primary_url": article.original_url or "",
        "first_seen_at": moment.isoformat() if moment else "",
        "verification_status": "unconfirmed",
        "verification_status_fa": "❓ تأییدنشده (بدون خلاصه تحریریه)",
        "topic_key": None,
        "topic_fa": "سایر تحولات",
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
    item.update(
        {
            "event_id": int(event.id),
            "headline_fa": str(event.title or item["headline_fa"]).strip(),
            "topic_key": topic.key if topic else None,
            "topic_fa": topic.name_fa if topic else "سایر تحولات",
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

    for article, source, event_id in rows:
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
    metadata = {
        **metadata,
        "collected_articles": len(rows),
        "articles_represented": articles_represented,
        "articles_without_summary": events_without_summary,
        "unlinked_articles": unlinked_articles,
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


def build_item_block(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Nodes for one news item inside a topic page.

    Only the system summary is published - never the source article body - and the title,
    source, original link, time and verification label are always present.
    """
    headline = str(item.get("headline_fa") or item.get("title") or "خبر بدون عنوان").strip()
    source = str(item.get("primary_source") or "منبع نامشخص").strip()
    url = str(item.get("primary_url") or "").strip()
    verification = str(
        item.get("verification_status_fa") or item.get("verification_status") or "نامشخص"
    ).strip()

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
    nodes.append(
        _paragraph(
            "🕒 زمان انتشار: ",
            _format_local_time(item.get("first_seen_at") or item.get("collected_at")),
        )
    )
    nodes.append(_paragraph("🛡 وضعیت راستی‌آزمایی: ", verification))

    link = _anchor(url, url)
    nodes.append(_paragraph("🔗 لینک اصلی خبر: ", link if link else "در دسترس نیست"))

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


def build_topic_page(
    topic_name: str,
    report_date: date,
    slot_hour: int,
    items: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> tuple[str, list[dict[str, Any]]]:
    """Build one topic page holding every news item of that topic."""
    label = slot_label(slot_hour)
    title = f"{topic_name} — گزارش {label} {report_date.isoformat()}"
    start_text = window_start.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")
    end_text = window_end.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")

    nodes: list[dict[str, Any]] = [
        _paragraph(f"بازه گزارش: از {start_text} تا {end_text} (به وقت {settings.timezone})"),
        _paragraph(f"تعداد اخبار این موضوع: {len(items)}"),
    ]
    for index, item in enumerate(items):
        if index:
            nodes.append({"tag": "hr"})
        nodes.extend(build_item_block(item))

    nodes.append({"tag": "hr"})
    nodes.append(
        _paragraph(
            "🛡 این بخش خودکار و بر پایه اصول بی‌طرفی و انتساب دقیق منابع تدوین شده است. "
            "خبرهای تک‌منبعی و تأییدنشده با برچسب شفاف منتشر شده‌اند."
        )
    )
    return title, nodes


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
    total = sum(len(topic_items) for _key, _name, topic_items in sections)
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
        entry = topic_pages.get(key) or {}
        page_url = str(entry.get("url") or "")
        link = _anchor(name, page_url)
        nodes.append(_paragraph("• ", link if link else name, f" — {len(topic_items)} خبر"))

    transparency: list[str] = []
    if metadata.get("articles_without_summary"):
        transparency.append(
            f"{metadata['articles_without_summary']} خبر هنوز خلاصه تحریریه نداشت و با برچسب "
            f"«{SUMMARY_MISSING_LABEL}» منتشر شد."
        )
    if metadata.get("unlinked_articles"):
        transparency.append(
            f"{metadata['unlinked_articles']} خبر هنوز به رویدادی متصل نشده بود و با اطلاعات اصلی خود منتشر شد."
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
) -> str:
    """HTML notification that points Telegram readers at the full Telegraph report."""
    metadata = metadata or {}
    total = sum(len(topic_items) for _key, _name, topic_items in sections)
    label = slot_label(slot_hour)
    window_start = metadata.get("window_start")
    window_end = metadata.get("window_end")
    window_text = ""
    if window_start and window_end:
        window_text = (
            f"\n🕒 بازه: {_format_local_time(window_start)} تا {_format_local_time(window_end)}"
        )

    section_lines = [f"• {name}: {len(topic_items)} خبر" for _key, name, topic_items in sections]
    link_html = (
        f'<a href="{index_url}">مطالعه کامل گزارش در Telegraph</a>'
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

    if row.status == TELEGRAPH_COMPLETED and row.index_url and not force:
        logger.info("Telegraph report %s for %s is already complete; skipping.", label, rep_date)
        return {
            "status": row.status,
            "index_url": row.index_url or "",
            "index_path": row.index_path or "",
            "pages": dict(row.topic_pages or {}),
            "created": 0,
            "reused": len(row.topic_pages or {}),
            "failures": [],
            "skipped": True,
            "item_count": 0,
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
    pages: dict[str, dict[str, Any]] = dict(row.topic_pages or {})
    created = 0
    updated = 0
    reused = 0
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

    for key, name, topic_items in sections:
        title, nodes = build_topic_page(
            name, rep_date, slot_hour, topic_items, window_start, window_end
        )
        existing = pages.get(key)
        page_path = str((existing or {}).get("path") or "")

        if dry_run:
            pages[key] = {
                "path": page_path or f"dry-run-{rep_date.isoformat()}-{slot_hour:02d}-{key}",
                "url": str((existing or {}).get("url") or ""),
                "name_fa": name,
                "item_count": len(topic_items),
            }
            created += 1
            continue

        is_new_page = False
        try:
            assert effective_client is not None
            if page_path and (force or int((existing or {}).get("item_count") or 0) != len(topic_items)):
                # The page exists: update it instead of publishing a second copy.
                page = await effective_client.edit_page(
                    path=page_path, title=title, content=nodes
                )
            elif page_path:
                reused += 1
                continue
            else:
                page = await effective_client.create_page(title=title, content=nodes)
                is_new_page = True
        except Exception as exc:  # one bad topic must not lose the rest of the report
            logger.exception("Telegraph page failed for topic %s in slot %s.", key, label)
            failures.append({"scope": "topic", "topic": key, "error": str(exc)})
            continue

        pages[key] = {
            "path": page.path,
            "url": page.url,
            "name_fa": name,
            "item_count": len(topic_items),
        }
        if is_new_page:
            created += 1
        else:
            updated += 1
        row.topic_pages = dict(pages)
        if persist:
            await session.commit()

    index_title, index_nodes = build_index_page(
        rep_date, slot_hour, sections, pages, window_start, window_end, metadata
    )

    index_url = str(row.index_url or "")
    index_path = str(row.index_path or "")
    if dry_run:
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

    logger.info(
        "Telegraph report %s for %s: %s (%d created, %d updated, %d reused, %d failure(s), %d item(s)).",
        label,
        rep_date,
        status,
        created,
        updated,
        reused,
        len(failures),
        len(items),
    )
    return {
        "status": status,
        "index_url": index_url,
        "index_path": index_path,
        "pages": pages,
        "created": created,
        "updated": updated,
        "reused": reused,
        "failures": failures,
        "skipped": False,
        "item_count": len(items),
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

"""Audit script for bulletin events, clustering integrity, and perspective gates (Phase Y-13).

READ-ONLY utility for inspection of events, source articles, live digest tagging,
perspective gating decisions, and bulletin section placement.

Usage on Windows PowerShell:
    $env:DATABASE_URL="postgresql+asyncpg://user:pass@host:port/dbname"
    python scripts/dev_only/audit_bulletin_events.py --date 2026-11-17 [--slot morning/evening]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Add project root to sys.path so app modules are resolvable
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, ArticleEvent, Event, Source, Summary
from app.editorial.glossary import canonicalize_approved_names
from app.editorial.liveblog import is_live_digest
from app.editorial.perspectives import check_perspectives_gate, orientation_for
from app.editorial.regions import is_middle_east_country, mentions_middle_east_place


def _url_path(url: str | None) -> str:
    if not url:
        return "-"
    parsed = urllib.parse.urlsplit(str(url))
    return parsed.path or "/"


def _is_regional(item: dict[str, Any]) -> bool:
    countries = item.get("countries") or []
    location = item.get("location") or ""
    for c in countries:
        if is_middle_east_country(c):
            return True
    if mentions_middle_east_place(location):
        return True
    return False


async def audit_events(target_date: date, slot: str | None = None) -> None:
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)

    # Compute 24h window
    target_dt = datetime(target_date.year, target_date.month, target_date.day, 18, 0, tzinfo=tz)
    window_end_utc = target_dt.astimezone(UTC)
    window_start_utc = window_end_utc - timedelta(hours=24)

    print("=" * 120)
    print(f"  BULLETIN EVENT INTEGRITY AUDIT — Date: {target_date} (Slot: {slot or 'default'})")
    print(f"  Window: {window_start_utc.isoformat()} to {window_end_utc.isoformat()} (UTC)")
    print("=" * 120)

    async with SessionLocal() as session:
        # Enforce read-only transaction
        await session.execute(text("SET default_transaction_read_only = 'on'"))

        stmt_events = (
            select(Event, Summary)
            .outerjoin(Summary, Summary.event_id == Event.id)
            .where(
                Event.first_seen_at >= window_start_utc,
                Event.first_seen_at <= window_end_utc,
                Event.importance >= settings.min_importance,
            )
            .order_by(Event.importance.desc(), Event.first_seen_at.desc())
        )
        event_rows = (await session.execute(stmt_events)).all()

        if not event_rows:
            print("No qualifying events found for this window.")
            return

        print(f"Found {len(event_rows)} qualifying events.\n")

        # Prepare records for section assignment
        audit_records = []
        for ev, sm in event_rows:
            # Query linked articles
            stmt_arts = (
                select(Article, Source.name)
                .join(ArticleEvent, ArticleEvent.article_id == Article.id)
                .join(Source, Source.id == Article.source_id)
                .where(ArticleEvent.event_id == ev.id)
                .order_by(Article.collected_at.asc())
            )
            art_rows = (await session.execute(stmt_arts)).all()

            details = sm.details_fa if sm and isinstance(sm.details_fa, dict) else {}
            is_digest = bool(details.get("is_live_digest") or details.get("article_kind") == "live_digest")

            item_dict = {
                "event": ev,
                "summary": sm,
                "articles": art_rows,
                "is_live_digest": is_digest,
                "countries": ev.countries or [],
                "actors": ev.actors or [],
                "location": ev.location or "",
                "importance": ev.importance,
                "sources_count": len(art_rows),
            }
            audit_records.append(item_dict)

        # Determine section placement (matching DailyReport rules)
        regional_present = [r for r in audit_records if _is_regional(r)]
        effective_list = regional_present if settings.bulletin_require_regional else audit_records

        # Sort: importance DESC, sources DESC, recency DESC
        def _sort_key(it: dict[str, Any]) -> tuple[int, int, float]:
            ts = it["event"].first_seen_at.timestamp() if it["event"].first_seen_at else 0.0
            return (int(it["importance"] or 3), it["sources_count"], ts)

        effective_sorted = sorted(effective_list, key=_sort_key, reverse=True)
        sec1_max = getattr(settings, "bulletin_section1_max", 12)
        sec1_ids = {r["event"].id for r in effective_sorted[:sec1_max] if r["importance"] >= 4}
        sec2_ids = {r["event"].id for r in effective_sorted if r["event"].id not in sec1_ids}

        # Print detailed audit table
        headers = [
            "EvID", "Imp", "Arts", "ArtID", "Source", "Kind",
            "URL Path", "Title (80 chars)", "Actors", "Shared Actors",
            "Gate", "Section"
        ]
        row_fmt = "{:<5} {:<4} {:<5} {:<6} {:<15} {:<12} {:<25} {:<40} {:<25} {:<20} {:<6} {:<8}"
        print(row_fmt.format(*headers))
        print("-" * 175)

        for rec in audit_records:
            ev = rec["event"]
            arts = rec["articles"]
            ev_id = ev.id
            ev_imp = ev.importance
            art_count = len(arts)

            # Determine section
            if ev_id in sec1_ids:
                section_str = "بخش ۱"
            elif ev_id in sec2_ids:
                section_str = "بخش ۲"
            else:
                section_str = "حذف‌شده"

            # Check perspectives gate if multi-source
            camp_articles = []
            seen_camps = set()
            for art, src_name in arts:
                o = orientation_for(src_name)
                if o.key in seen_camps:
                    continue
                seen_camps.add(o.key)
                payload = art.ai_payload if isinstance(art.ai_payload, dict) else {}
                camp_articles.append({
                    "source_name": src_name,
                    "actors": payload.get("actors") or [],
                    "location": payload.get("location") or "",
                })

            if len(camp_articles) >= 2 and not rec["is_live_digest"]:
                gate_ok, shared = check_perspectives_gate(
                    camp_articles, min_shared_actors=settings.perspective_min_shared_actors
                )
                gate_str = "قبول" if gate_ok else "رد"
                shared_str = "، ".join(sorted(shared))[:20] if shared else "—"
            elif rec["is_live_digest"]:
                gate_str = "لایو"
                shared_str = "—"
            else:
                gate_str = "تک‌منبع"
                shared_str = "—"

            if not arts:
                print(row_fmt.format(
                    ev_id, ev_imp, 0, "-", "-", "-", "-",
                    canonicalize_approved_names(ev.title)[:38],
                    "—", "—", gate_str, section_str
                ))
                continue

            for idx, (art, src_name) in enumerate(arts):
                p_load = art.ai_payload if isinstance(art.ai_payload, dict) else {}
                art_is_digest = is_live_digest(art.canonical_url or art.original_url, art.title)
                kind_str = "live_digest" if art_is_digest else "standard"
                path_str = _url_path(art.canonical_url or art.original_url)[:24]
                art_title = canonicalize_approved_names(art.title)[:38]
                actors_str = "، ".join(p_load.get("actors") or [])[:24] or "—"

                # Show event-level columns on first row, blank thereafter
                c_ev = str(ev_id) if idx == 0 else ""
                c_imp = str(ev_imp) if idx == 0 else ""
                c_cnt = str(art_count) if idx == 0 else ""
                c_gate = gate_str if idx == 0 else ""
                c_sec = section_str if idx == 0 else ""
                c_shared = shared_str if idx == 0 else ""

                print(row_fmt.format(
                    c_ev, c_imp, c_cnt, art.id, src_name[:14], kind_str,
                    path_str, art_title, actors_str, c_shared, c_gate, c_sec
                ))
            print("." * 175)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit bulletin events and clustering integrity.")
    parser.add_argument("--date", type=str, default=str(date.today()), help="Target report date (YYYY-MM-DD)")
    parser.add_argument("--slot", type=str, choices=["morning", "evening"], default=None, help="Bulletin slot")
    args = parser.parse_args()

    try:
        t_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(audit_events(t_date, args.slot))


if __name__ == "__main__":
    main()

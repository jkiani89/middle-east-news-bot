"""Multi-part Telegraph publication: no news is trimmed, no page is duplicated."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.reports.telegraph_client import (
    PAGE_LIST_PAGE_SIZE,
    TelegraphClient,
    TelegraphError,
    TelegraphPage,
)
from app.reports.telegraph_pagination import (
    PAGE_CONTENT_BUDGET_BYTES,
    layout_hash,
    measure_nodes,
    nav_reserve_bytes,
    part_layout_hash,
    plan_topic_parts,
)
from app.reports.telegraph_report import (
    TELEGRAPH_COMPLETED,
    TELEGRAPH_PARTIAL,
    AmbiguousOrphanError,
    _recover_pending_page,
    _topic_entry,
    build_item_block,
    fa_number,
    normalize_topic_entry,
    page_identity_line,
    page_identity_stamp,
    publish_telegraph_report,
    title_matches_part,
)

REPORT_DATE = date(2026, 11, 17)
SLOT_HOUR = 20
# Long enough that a handful of items overflow one page.
BIG_SUMMARY_CHARS = 1200


def _item(event_id: int, *, topic_key: str = "ir", summary_chars: int = BIG_SUMMARY_CHARS) -> dict:
    return {
        "event_id": event_id,
        "topic_key": topic_key,
        "topic_fa": "ایران و آمریکا" if topic_key == "ir" else "سایر تحولات",
        "headline_fa": f"تیتر خبر شماره {event_id}",
        "summary_fa": "خ" * summary_chars,
        "primary_source": "IRNA",
        "primary_url": f"https://irna.ir/news/{event_id}",
        "first_seen_at": "2026-11-17T19:30:00+00:00",
        "verification_status": "unconfirmed",
        "verification_status_fa": "❓ تأییدنشده (تک‌منبعی)",
        "additional_sources": [],
    }


class _Session:
    def __init__(self, row=None):
        self.row = row

    async def scalar(self, stmt):  # noqa: ARG002 - only the slot lookup is used here
        return self.row

    def add(self, obj):
        self.row = obj

    async def commit(self):
        return None


class _Client(TelegraphClient):
    def __init__(
        self,
        *,
        fail_titles: set[str] | None = None,
        existing_pages: list[dict] | None = None,
        remote_pages: dict[str, list[dict]] | None = None,
        list_error: str | None = None,
        path_prefix: str = "p",
    ):
        super().__init__(access_token="token")
        self.created: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.contents: dict[str, list[dict]] = {}
        self.titles: dict[str, str] = {}
        self.fail_titles = fail_titles or set()
        self.existing_pages = existing_pages or []
        self.remote_pages = remote_pages or {}
        self.list_error = list_error
        self.path_prefix = path_prefix
        self.read_pages: list[str] = []
        self.list_offsets: list[int] = []
        # When False the walk stops at the safety valve with the listing unproven.
        self.listing_complete = True
        # The real API returns content as a JSON string, sometimes with \uXXXX escapes.
        self.content_as_json = False
        # When set, getPageList reports this as total_count instead of len(existing_pages).
        self.listing_total_count: int | None = None
        # A getPageList that never reports total_count at all.
        self.listing_total_missing = False
        # Number of full walks started so far (offset 0, full-size window).
        self.walks = 0
        # Full override: (client, walk, offset, limit) -> (pages, total).
        self.window_script = None

    async def ensure_token(self) -> str:
        return "token"

    async def list_page_window(self, offset: int = 0, limit: int = 200):
        """One window plus the account's reported total, like the real getPageList."""
        if self.list_error:
            raise TelegraphError(self.list_error)
        if limit == 200 and offset == 0:
            self.walks += 1
        self.list_offsets.append(offset)
        if self.window_script is not None:
            return self.window_script(self, self.walks, offset, limit)
        if not self.listing_complete:
            # A listing that never ends: the client's safety valve must report it.
            return (
                [{"path": f"filler{offset + index}"} for index in range(limit)],
                len(self.existing_pages) + 10_000,
            )
        batch = list(self.existing_pages)[offset : offset + limit]
        if self.listing_total_missing:
            return batch, None
        total = len(self.existing_pages) if self.listing_total_count is None else self.listing_total_count
        return batch, total

    async def list_pages(self, offset: int = 0, limit: int = 200) -> list[dict]:
        pages, _total = await self.list_page_window(offset=offset, limit=limit)
        return pages

    async def get_page(self, path: str, return_content: bool = False) -> dict:
        """Read a page back: the only way a recovery can prove what it is adopting."""
        self.read_pages.append(path)
        nodes = self.remote_pages.get(path)
        if nodes is None:
            nodes = self.contents.get(path)
        if nodes is None:
            raise TelegraphError(f"page {path} not found")
        payload = {
            "path": path,
            "url": f"https://telegra.ph/{path}",
            "title": self.titles.get(path, ""),
        }
        if return_content:
            payload["content"] = (
                json.dumps(nodes, ensure_ascii=True) if self.content_as_json else nodes
            )
        return payload

    async def create_page(self, title, content, author_name=None, author_url=None):
        if title in self.fail_titles:
            raise TelegraphError(f"create failed for {title}")
        # Telegraph never reuses a page path, so a new page must not land on one that is
        # already in the account (the crashed orphan's path, for example).
        taken = set(self.contents) | {str(page.get("path") or "") for page in self.existing_pages}
        index = len(self.created) + 1
        path = f"{self.path_prefix}{index}"
        while path in taken:
            index += 1
            path = f"{self.path_prefix}{index}"
        self.created.append((path, title))
        self.contents[path] = content
        self.titles[path] = title
        return TelegraphPage(path=path, url=f"https://telegra.ph/{path}", title=title)

    async def edit_page(self, path, title, content, author_name=None):
        if title in self.fail_titles:
            raise TelegraphError(f"edit failed for {title}")
        self.edited.append((path, title))
        self.contents[path] = content
        self.titles[path] = title
        return TelegraphPage(path=path, url=f"https://telegra.ph/{path}", title=title)


def _rendered_sizes(client: _Client) -> dict[str, int]:
    return {path: measure_nodes(nodes) for path, nodes in client.contents.items()}


def _published_headlines(client: _Client) -> list[str]:
    """Every ``<h4>`` headline that is online, page by page."""
    found: list[str] = []
    for nodes in client.contents.values():
        for node in nodes:
            if node.get("tag") == "h4":
                found.extend(str(child) for child in node.get("children") or [])
    return found


async def _publish(client: _Client, items: list[dict], session: _Session, **kwargs) -> dict:
    return await publish_telegraph_report(
        session,
        report_date=REPORT_DATE,
        slot_hour=SLOT_HOUR,
        client=client,
        items=items,
        **kwargs,
    )


def test_planner_keeps_every_item_once_and_in_order():
    items = [_item(n) for n in range(1, 41)]

    parts = plan_topic_parts(
        items,
        block_builder=build_item_block,
        overhead_bytes=800,
        topic="ایران و آمریکا",
    )

    assert len(parts) > 1
    assert [plan.part for plan in parts] == list(range(1, len(parts) + 1))
    flattened = [item["event_id"] for plan in parts for item in plan.items]
    assert flattened == [item["event_id"] for item in items]
    assert all(plan.item_count > 0 for plan in parts)


def test_planner_respects_the_budget_for_every_page():
    items = [_item(n) for n in range(1, 41)]

    parts = plan_topic_parts(items, block_builder=build_item_block, overhead_bytes=800)

    for plan in parts:
        assert measure_nodes(build_item_block_chain(plan.items)) <= PAGE_CONTENT_BUDGET_BYTES - nav_reserve_bytes(len(parts))


def build_item_block_chain(items) -> list[dict]:
    """Blocks of one planned page, joined the way the renderer joins them."""
    nodes: list[dict] = []
    for index, item in enumerate(items):
        if index:
            nodes.append({"tag": "hr"})
        nodes.extend(build_item_block(item))
    return nodes


def test_a_single_unfittable_block_fails_instead_of_being_dropped():
    items = [_item(1, summary_chars=40000)]

    with pytest.raises(Exception) as excinfo:
        plan_topic_parts(items, block_builder=build_item_block, overhead_bytes=800)

    assert "does not fit" in str(excinfo.value)


def test_layout_hash_is_stable_and_order_sensitive():
    items = [_item(n) for n in range(1, 6)]
    first = plan_topic_parts(items, block_builder=build_item_block, overhead_bytes=800)
    again = plan_topic_parts(items, block_builder=build_item_block, overhead_bytes=800)
    reordered = plan_topic_parts(
        list(reversed(items)), block_builder=build_item_block, overhead_bytes=800
    )

    assert layout_hash(first) == layout_hash(again)
    assert layout_hash(first) != layout_hash(reordered)


def test_more_parts_reserve_more_navigation_room():
    assert nav_reserve_bytes(2) > nav_reserve_bytes(1)
    assert nav_reserve_bytes(1) > 0


@pytest.mark.asyncio
async def test_a_long_topic_is_split_without_losing_a_single_news_item():
    session = _Session()
    client = _Client()
    items = [_item(n) for n in range(1, 31)]
    headlines = {item["headline_fa"] for item in items}

    result = await _publish(client, items, session)

    assert result["status"] == TELEGRAPH_COMPLETED
    assert result["part_count"] >= 2
    assert result["planned_item_count"] == len(items)
    assert result["published_item_count"] == len(items)
    assert result["failures"] == []
    # Every news item is on exactly one page of the topic, and no page is over budget.
    assert sorted(_published_headlines(client)) == sorted(headlines)
    assert max(_rendered_sizes(client).values()) <= PAGE_CONTENT_BUDGET_BYTES
    # The index links every part and names the page count.
    index_nodes = client.contents[session.row.index_path]
    index_text = json.dumps(index_nodes, ensure_ascii=False)
    assert "بخش" in index_text
    assert f"{fa_number(result['part_count'])} صفحه" in index_text
    assert session.row.status == TELEGRAPH_COMPLETED


@pytest.mark.asyncio
async def test_every_part_ends_up_with_the_complete_navigation_chain():
    session = _Session()
    client = _Client()
    items = [_item(n) for n in range(1, 31)]

    result = await _publish(client, items, session)

    parts = session.row.topic_pages["ir"]["parts"]
    assert len(parts) == result["part_count"]
    for part in parts:
        rendered = json.dumps(client.contents[part["path"]], ensure_ascii=False)
        # The first page was written before later URLs existed; the second pass fixes it.
        for other in parts:
            if other["part"] == part["part"]:
                continue
            assert other["url"] in rendered
        assert "این صفحه بخش" in rendered


@pytest.mark.asyncio
async def test_republishing_a_complete_report_creates_nothing():
    """A6: after a full successful publication, a forced re-run must create zero pages."""
    session = _Session()
    first = _Client()
    items = [_item(n) for n in range(1, 31)]
    await _publish(first, items, session)

    # A plain re-run is skipped entirely.
    skipped = await _publish(_Client(), items, session)
    assert skipped["skipped"] is True
    assert skipped["created"] == 0

    # A forced re-run re-renders the existing pages and still creates nothing.
    forced = _Client()
    result = await _publish(forced, items, session, force=True)

    assert result["created"] == 0
    assert forced.created == []
    assert len(forced.edited) == result["part_count"] + 1  # parts + index
    assert result["status"] == TELEGRAPH_COMPLETED
    assert result["published_item_count"] == len(items)


TOPIC_NAME = "ایران و آمریکا"
SLOT_STAMP = f"{REPORT_DATE.isoformat()} {SLOT_HOUR:02d}:00"
COMPACT_STAMP = page_identity_stamp(REPORT_DATE, SLOT_HOUR)


def _marker(
    *,
    title: str,
    part: int = 1,
    item_count: int,
    layout: str,
    topic_key: str = "ir",
) -> dict:
    return {
        "title": title,
        "slot": SLOT_STAMP,
        "topic_key": topic_key,
        "part": part,
        "item_count": item_count,
        "layout_hash": layout,
    }


def _online_part_nodes(
    items,
    *,
    part: int = 1,
    total: int = 2,
    layout: str,
    stamp: str = COMPACT_STAMP,
    topic_key: str = "ir",
) -> list[dict]:
    """Content of a page as the crashed run left it: part label, blocks, identity line."""
    nodes: list[dict] = [
        {"tag": "p", "children": [f"این صفحه بخش {fa_number(part)} از {fa_number(total)}"]},
    ]
    for index, item in enumerate(items):
        if index:
            nodes.append({"tag": "hr"})
        nodes.extend(build_item_block(item))
    nodes.append({"tag": "p", "children": [page_identity_line(stamp, topic_key, part, layout)]})
    return nodes


def _as_crashed(session, items, *, old_total: int = 2, part: int = 1, overrides: dict | None = None):
    """Rewrite a published part into the state left by a crash between create and commit."""
    entry = session.row.topic_pages["ir"]
    stored = entry["parts"][part - 1]
    count = stored["item_count"]
    layout = part_layout_hash(items[:count]) if part == 1 else stored.get("layout_hash")
    title = f"{TOPIC_NAME} — گزارش 20:00 {REPORT_DATE.isoformat()} — بخش ۱ از {fa_number(old_total)}"
    marker = _marker(title=title, part=part, item_count=count, layout=layout or "sha1:unknown")
    marker.update(overrides or {})
    entry["parts"][part - 1] = {
        "part": part,
        "path": "",
        "url": "",
        "item_count": count,
        "layout_hash": marker["layout_hash"],
        "pending_create": marker,
    }
    session.row.index_url = None
    session.row.index_path = None
    session.row.status = "telegraph_in_progress"
    return stored["path"], count, marker


@pytest.mark.asyncio
async def test_a_verified_orphan_is_adopted_instead_of_duplicated():
    """A crash between createPage and the commit must not publish the topic twice."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    retry = _Client(
        existing_pages=[{"path": path, "url": f"https://telegra.ph/{path}", "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=1, total=2, layout=marker["layout_hash"])
        },
    )
    second = await _publish(retry, items, session)

    assert second["recovered"] == 1
    # Identity is proven by reading the page back, not by its title alone.
    assert retry.read_pages == [path]
    assert path in [edited for edited, _title in retry.edited]
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    assert session.row.topic_pages["ir"]["parts"][0]["path"] == path
    assert second["status"] == TELEGRAPH_COMPLETED


@pytest.mark.asyncio
async def test_recovery_identity_does_not_depend_on_the_total_part_count():
    """R2: the recovery marker is keyed by slot/topic/part, never by the layout size."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items, old_total=3)

    retry = _Client(
        existing_pages=[{"path": path, "url": f"https://telegra.ph/{path}", "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=1, total=2, layout=marker["layout_hash"])
        },
    )
    second = await _publish(retry, items, session)

    written_title = next(title for edited, title in retry.edited if edited == path)
    assert second["recovered"] == 1
    assert second["part_count"] == 2
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    # The old layout had three parts, today's has two: the page is still recognised and
    # written with today's display title, so the part total is not part of its identity.
    assert "از ۳" in marker["title"]
    assert written_title != marker["title"]
    assert "بخش ۱ از ۲" in written_title


def test_title_identity_ignores_the_part_total_but_not_the_part():
    base = f"{TOPIC_NAME} — گزارش 20:00 {REPORT_DATE.isoformat()}"

    assert title_matches_part(base, TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 1)  # legacy single page
    assert title_matches_part(f"{base} — بخش ۱ از ۲", TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 1)
    assert title_matches_part(f"{base} — بخش ۱ از ۹", TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 1)
    assert not title_matches_part(f"{base} — بخش ۲ از ۲", TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 1)
    assert not title_matches_part(
        f"{TOPIC_NAME} — گزارش 08:00 {REPORT_DATE.isoformat()}", TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 1
    )


def test_the_part_number_needs_a_digit_boundary():
    """R-A: part 1 must never match part 10, 11, … 19 nor 21, 31 …."""
    base = f"{TOPIC_NAME} — گزارش 20:00 {REPORT_DATE.isoformat()}"

    for other in (10, 11, 12, 19, 21, 31, 40):
        title = f"{base} — بخش {fa_number(other)} از ۴۰"
        assert not title_matches_part(title, TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 1), title
    for other in (1, 10, 11, 19, 21, 31):
        title = f"{base} — بخش {fa_number(other)} از ۴۰"
        assert title_matches_part(title, TOPIC_NAME, REPORT_DATE, SLOT_HOUR, other), title
    # A page without the total suffix is still part 1 only.
    assert title_matches_part(f"{base} — بخش ۱", TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 1)
    assert not title_matches_part(f"{base} — بخش ۱", TOPIC_NAME, REPORT_DATE, SLOT_HOUR, 10)


@pytest.mark.asyncio
async def test_a_marker_for_another_topic_is_recorded_as_an_orphan_not_adopted():
    """Requirement 2: the marker must prove slot + topic_key + part, not just a title."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, _count, _marker_used = _as_crashed(session, items, overrides={"topic_key": "ye"})

    retry = _Client(existing_pages=[{"path": path, "title": f"{TOPIC_NAME} — گزارش 20:00 2026-11-17 — بخش ۱ از ۲"}])
    second = await _publish(retry, items, session)

    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    assert retry.edited == []
    assert any(failure["scope"] == "orphan" for failure in second["failures"])
    stored = session.row.topic_pages["ir"]["parts"][0]
    assert stored["path"] == ""
    assert "does not match" in stored["orphan"]["reason"]
    assert second["status"] == TELEGRAPH_PARTIAL


@pytest.mark.asyncio
async def test_content_that_does_not_match_the_marker_is_left_alone():
    """Requirement 4: an unprovable page is recorded as an orphan and never rewritten."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    # The online page holds one news item less than the marker promised.
    retry = _Client(
        existing_pages=[{"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[: count - 1], part=1, total=2, layout=marker["layout_hash"])
        },
    )
    second = await _publish(retry, items, session)

    assert retry.edited == []
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    stored = session.row.topic_pages["ir"]["parts"][0]
    assert stored["orphan"]["path"] == path
    assert "h4 count" in stored["orphan"]["reason"]
    assert second["status"] == TELEGRAPH_PARTIAL


@pytest.mark.asyncio
async def test_published_parts_carry_their_slot_and_topic_identity():
    """The identity proof has to exist on the page, or recovery can never verify it."""
    session = _Session()
    client = _Client()
    items = [_item(n) for n in range(1, 31)]
    await _publish(client, items, session)

    parts = session.row.topic_pages["ir"]["parts"]
    lines = [
        child
        for part in parts
        for node in client.contents[part["path"]]
        for child in (node.get("children") or [])
        if isinstance(child, str) and child.startswith("شناسه: ")
    ]
    assert len(lines) == len(parts)  # one compact line per part, no extras
    for line, part in zip(lines, parts, strict=True):
        stamp, topic, number, fingerprint = line.removeprefix("شناسه: ").split("|")
        assert stamp == COMPACT_STAMP
        assert topic == "ir"
        assert number == str(part["part"])
        assert re.fullmatch(r"sha1:[0-9a-f]{40}", fingerprint), fingerprint
        rendered = json.dumps(client.contents[part["path"]], ensure_ascii=False)
        # The old two-paragraph markers are gone.
        assert "شناسه صفحه:" not in rendered
        assert "شناسه طرح:" not in rendered


@pytest.mark.asyncio
async def test_content_identity_must_name_this_slot_and_topic():
    """R2: slot and topic are proven by the page content, never by the title alone."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    # Same display title and same layout, but the page identifies another subject.
    retry = _Client(
        existing_pages=[{"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(
                items[:count], part=1, total=2, layout=marker["layout_hash"], topic_key="ye"
            )
        },
    )
    second = await _publish(retry, items, session)

    stored = session.row.topic_pages["ir"]["parts"][0]
    assert stored["path"] == ""
    assert "identity line" in stored["orphan"]["reason"]
    assert retry.edited == []
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    assert second["status"] == TELEGRAPH_PARTIAL


@pytest.mark.asyncio
async def test_escaped_content_from_get_page_is_still_verified():
    """The API returns content as a JSON string; escaped Persian must still prove identity."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    retry = _Client(
        existing_pages=[{"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=1, total=2, layout=marker["layout_hash"])
        },
    )
    retry.content_as_json = True
    second = await _publish(retry, items, session)

    assert second["recovered"] == 1
    assert path in [edited for edited, _title in retry.edited]
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []


@pytest.mark.asyncio
async def test_an_orphan_beyond_the_first_listing_page_is_still_adopted():
    """R-B: a page outside the newest 200 results must not be republished."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    fillers = [{"path": f"old{i}", "title": f"صفحه قدیمی {i}"} for i in range(240)]
    retry = _Client(
        existing_pages=[*fillers, {"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=1, total=2, layout=marker["layout_hash"])
        },
    )
    second = await _publish(retry, items, session)

    assert second["recovered"] == 1
    # The first walk went past the first window; the account is walked twice.
    assert retry.list_offsets[:2] == [0, PAGE_LIST_PAGE_SIZE]
    assert retry.walks == 2
    assert path in [edited for edited, _title in retry.edited]
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_total", "reason"),
    [
        (4, "without proving the full page list"),  # fewer pages than total_count
        (1, "without proving the full page list"),  # more pages than total_count
        (None, "without proving the full page list"),  # total_count missing
    ],
)
async def test_a_listing_that_contradicts_total_count_never_creates_a_page(reported_total, reason):
    """C: only an exact match between delivered pages and total_count proves absence."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    _as_crashed(session, items)

    retry = _Client(existing_pages=[{"path": f"o{i}", "title": f"صفحه {i}"} for i in range(2)])
    if reported_total is None:
        retry.listing_total_missing = True
    else:
        retry.listing_total_count = reported_total
    second = await _publish(retry, items, session)

    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    assert retry.edited == []
    assert second["failures"][0]["scope"] == "orphan"
    assert reason in second["failures"][0]["error"]
    assert session.row.topic_pages["ir"]["parts"][0]["path"] == ""


@pytest.mark.asyncio
async def test_the_compact_identity_line_is_read_from_the_actual_page_content():
    """C: slot, topic, part and the full fingerprint come from one line on the page."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    retry = _Client(
        existing_pages=[{"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=1, total=2, layout=marker["layout_hash"])
        },
    )
    second = await _publish(retry, items, session)

    assert second["recovered"] == 1
    assert path in [edited for edited, _title in retry.edited]
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    # The adopted page is re-rendered with the same compact identity line.
    line = next(
        child
        for node in retry.contents[path]
        for child in (node.get("children") or [])
        if isinstance(child, str) and child.startswith("شناسه: ")
    )
    stamp, topic, number, fingerprint = line.removeprefix("شناسه: ").split("|")
    assert (stamp, topic, number) == (COMPACT_STAMP, "ir", "1")
    assert re.fullmatch(r"sha1:[0-9a-f]{40}", fingerprint)


@pytest.mark.asyncio
async def test_a_part_10_page_is_never_mistaken_for_part_1():
    """R-A inside recovery: the fingerprint line carries the part number boundary too."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    # The page on Telegraph really belongs to part 10: same slot and topic, wrong part.
    retry = _Client(
        existing_pages=[{"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=10, total=40, layout=marker["layout_hash"])
        },
    )
    second = await _publish(retry, items, session)

    assert retry.edited == []
    assert "identity line" in second["failures"][0]["error"]
    assert session.row.topic_pages["ir"]["parts"][0]["path"] == ""


@pytest.mark.asyncio
async def test_an_incomplete_listing_never_creates_a_page():
    """R-B: an unproven absence is reported, never turned into a second page."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    _as_crashed(session, items)

    retry = _Client(existing_pages=[{"path": "x", "title": "صفحه دیگر"}])
    retry.listing_complete = False
    second = await _publish(retry, items, session)

    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    assert retry.edited == []
    assert second["failures"][0]["scope"] == "orphan"
    assert "without proving the full page list" in second["failures"][0]["error"]
    assert session.row.topic_pages["ir"]["parts"][0]["path"] == ""


def _changed_total(client: _Client):
    """total_count differs between two windows of the same walk."""

    def script(c, walk, offset, limit):
        total = len(c.existing_pages) + (1 if offset else 0)
        return list(c.existing_pages)[offset : offset + limit], total

    client.window_script = script


def _deleted_page(client: _Client):
    """A page disappears between the first and the second window."""
    done = {"value": False}

    def script(c, walk, offset, limit):
        if walk == 1 and offset and not done["value"]:
            done["value"] = True
            c.existing_pages.pop(0)
        return list(c.existing_pages)[offset : offset + limit], len(c.existing_pages)

    client.window_script = script


def _created_page(client: _Client):
    """A page appears between the first and the second window."""
    done = {"value": False}

    def script(c, walk, offset, limit):
        if walk == 1 and offset and not done["value"]:
            done["value"] = True
            c.existing_pages.insert(0, {"path": "just-created", "title": "صفحه تازه"})
        return list(c.existing_pages)[offset : offset + limit], len(c.existing_pages)

    client.window_script = script


def _repeated_path(client: _Client):
    """One path is served twice while the count still matches."""

    def script(c, walk, offset, limit):
        window = [dict(page) for page in c.existing_pages[offset : offset + limit]]
        if limit == PAGE_LIST_PAGE_SIZE and not offset and len(window) > 1:
            window[-1] = dict(window[-2])
        return window, len(c.existing_pages)

    client.window_script = script


def _walks_disagree(client: _Client):
    """Each walk is internally proven but the two page sets differ."""

    def script(c, walk, offset, limit):
        window = [dict(page) for page in c.existing_pages[offset : offset + limit]]
        if walk == 2:
            window = [
                {"path": "swapped-path"} if page["path"] == "old0" else page for page in window
            ]
        return window, len(c.existing_pages)

    client.window_script = script


def _total_changes_at_final_control(client: _Client):
    """The read after the walk reports a different total_count."""

    def script(c, walk, offset, limit):
        total = len(c.existing_pages) + (0 if limit == PAGE_LIST_PAGE_SIZE else 1)
        return list(c.existing_pages)[offset : offset + limit], total

    client.window_script = script


def _second_walk_fails(client: _Client):
    """The confirming walk hits a network error."""

    def script(c, walk, offset, limit):
        if walk == 2:
            raise TelegraphError("getPageList request failed")
        return list(c.existing_pages)[offset : offset + limit], len(c.existing_pages)

    client.window_script = script


UNKNOWN_LISTING_STATES = {
    "total_changes_between_windows": _changed_total,
    "page_deleted_during_walk": _deleted_page,
    "page_created_during_walk": _created_page,
    "repeated_path": _repeated_path,
    "walks_disagree_on_pages": _walks_disagree,
    "total_changes_at_final_control": _total_changes_at_final_control,
    "second_walk_network_error": _second_walk_fails,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", sorted(UNKNOWN_LISTING_STATES))
async def test_an_unproven_page_list_never_creates_or_rewrites_a_page(state):
    """Every unproven listing means: no adoption, no createPage for the crashed part."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    fillers = [{"path": f"old{i}", "title": f"صفحه قدیمی {i}"} for i in range(400)]
    retry = _Client(
        existing_pages=[*fillers, {"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=1, total=2, layout=marker["layout_hash"])
        },
    )
    UNKNOWN_LISTING_STATES[state](retry)

    second = await _publish(retry, items, session)

    # No page of the crashed topic part is created (the report index still is, by design,
    # for a partial report), and the candidate page is never even read for adoption.
    assert [title for _path, title in retry.created if title.startswith(TOPIC_NAME)] == []
    assert retry.edited == []
    assert retry.read_pages == []
    assert second["failures"][0]["scope"] == "orphan"
    assert "without proving the full page list" in second["failures"][0]["error"]
    assert session.row.topic_pages["ir"]["parts"][0]["path"] == ""


@pytest.mark.asyncio
async def test_a_wrong_layout_marker_is_not_adopted():
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    retry = _Client(
        existing_pages=[{"path": path, "title": marker["title"]}],
        remote_pages={
            path: _online_part_nodes(items[:count], part=1, total=2, layout="sha1:someone-else")
        },
    )
    second = await _publish(retry, items, session)

    stored = session.row.topic_pages["ir"]["parts"][0]
    assert stored["orphan"]["path"] == path
    assert "content fingerprint" in stored["orphan"]["reason"]
    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    assert second["status"] == TELEGRAPH_PARTIAL


@pytest.mark.asyncio
async def test_two_pages_claiming_one_part_stop_the_whole_run():
    """Requirement 5: ambiguity is reported and nothing else is published."""
    session = _Session()
    items = [_item(n) for n in range(1, 31)] + [_item(n, topic_key="ye") for n in range(100, 104)]
    probe = _Client()
    await _publish(probe, items, session)
    path, count, marker = _as_crashed(session, items)

    retry = _Client(
        existing_pages=[
            {"path": path, "title": marker["title"]},
            {"path": "twin", "title": marker["title"]},
        ]
    )
    second = await _publish(retry, items, session)

    assert retry.created == []
    assert retry.edited == []
    assert second["failures"][0]["scope"] == "orphan_ambiguous"
    assert "ambiguous" in second["failures"][0]["error"]
    assert second["status"] == TELEGRAPH_PARTIAL


@pytest.mark.asyncio
async def test_ambiguity_is_reported_as_a_telegraph_error():
    title = f"{TOPIC_NAME} — گزارش 20:00 {REPORT_DATE.isoformat()} — بخش ۱ از ۲"
    record = {
        "path": "",
        "pending_create": _marker(title=title, item_count=2, layout="sha1:h"),
    }
    client = _Client(
        existing_pages=[{"path": "a", "title": title}, {"path": "b", "title": title}]
    )

    with pytest.raises(AmbiguousOrphanError) as excinfo:
        await _recover_pending_page(
            client,
            record,
            topic_key="ir",
            topic_name=TOPIC_NAME,
            report_date=REPORT_DATE,
            slot_hour=SLOT_HOUR,
            part=1,
            expected_item_count=2,
            expected_layout_hash="sha1:h",
        )

    assert isinstance(excinfo.value, TelegraphError)
    assert "['a', 'b']" in str(excinfo.value)
    assert client.read_pages == []  # nothing was read, so nothing could be adopted


@pytest.mark.asyncio
async def test_a_failed_page_listing_never_silently_creates_a_duplicate():
    session = _Session()
    items = [_item(n) for n in range(1, 31)]
    probe = _Client()
    await _publish(probe, items, session)
    _as_crashed(session, items)

    retry = _Client(list_error="telegraph is down")
    second = await _publish(retry, items, session)

    assert [title for _path, title in retry.created if TOPIC_NAME in title] == []
    assert second["failures"][0]["scope"] == "orphan"
    assert "getPageList failed" in second["failures"][0]["error"]


@pytest.mark.asyncio
async def test_an_unresolved_marker_is_never_dropped_as_surplus():
    """R6: a smaller layout may not silently forget a page that may still be online."""
    session = _Session()
    many = [_item(n) for n in range(1, 31)]
    first = await _publish(_Client(), many, session)
    assert first["part_count"] >= 2
    last_number = session.row.topic_pages["ir"]["parts"][-1]["part"]
    session.row.topic_pages["ir"]["parts"][-1] = {
        "part": last_number,
        "path": "",
        "item_count": 2,
        "pending_create": _marker(
            title="x", part=last_number, item_count=2, layout="sha1:pending"
        ),
    }

    second = await _publish(_Client(), many[:3], session, force=True)

    stored = session.row.topic_pages["ir"]["parts"]
    assert [part["part"] for part in stored if part.get("pending_create")] == [last_number]
    assert [failure["scope"] for failure in second["failures"]] == ["surplus"]
    assert second["status"] == TELEGRAPH_PARTIAL


def test_a_retired_first_part_never_becomes_the_topic_url():
    """R5: normalization and record building must pick the same active part."""
    parts = {
        1: {"part": 1, "path": "old1", "url": "https://telegra.ph/old1", "retired": True, "item_count": 0},
        2: {"part": 2, "path": "live2", "url": "https://telegra.ph/live2", "item_count": 3},
    }

    entry = _topic_entry(TOPIC_NAME, dict(parts), layout="sha1:h", planned=3, links_complete=True)
    normalised = normalize_topic_entry(entry)

    assert entry["path"] == "live2"
    assert normalised["path"] == "live2"
    assert normalised["url"] == entry["url"] == "https://telegra.ph/live2"
    # A record with no top-level path behaves the same way.
    stripped = normalize_topic_entry({"name_fa": TOPIC_NAME, "parts": list(parts.values())})
    assert stripped["path"] == "live2"


@pytest.mark.asyncio
async def test_a_part_that_is_no_longer_needed_becomes_a_pointer_page():
    session = _Session()
    big = _Client()
    many = [_item(n) for n in range(1, 31)]
    first = await _publish(big, many, session)
    assert first["part_count"] >= 2
    surplus = session.row.topic_pages["ir"]["parts"][-1]

    small = _Client()
    result = await _publish(small, many[:3], session, force=True)

    assert result["part_count"] == 1
    retired_titles = [title for path, title in small.edited if path == surplus["path"]]
    assert retired_titles and "ادغام" in retired_titles[-1]
    stored = session.row.topic_pages["ir"]
    assert stored["part_count"] == 1
    assert stored["parts"][-1]["retired"] is True
    assert stored["published_item_count"] == 3


@pytest.mark.asyncio
async def test_legacy_single_page_record_is_upgraded_in_place():
    """A record written before pagination is part 1, never a page to recreate."""
    session = _Session()
    row = _new_row()
    row.topic_pages = {
        "ir": {"path": "legacy1", "url": "https://telegra.ph/legacy1", "name_fa": "ایران و آمریکا", "item_count": 3}
    }
    row.status = TELEGRAPH_COMPLETED
    row.index_url = "https://telegra.ph/legacy-index"
    row.index_path = "legacy-index"
    session.row = row

    client = _Client()
    result = await _publish(client, [_item(n) for n in range(1, 4)], session)

    assert client.created == []  # the legacy page already holds part 1
    assert "legacy1" in [path for path, _title in client.edited]
    assert result["status"] == TELEGRAPH_COMPLETED
    assert session.row.topic_pages["ir"]["layout_version"] == 2


def _new_row():
    from app.reports.telegraph_report import _new_row as build_row

    return build_row(REPORT_DATE, SLOT_HOUR)


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _window_start() -> datetime:
    return datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day, 20, 0, tzinfo=_tz())

"""Phase T-2: one event, several camps, and the disagreement between them."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.provider import MockAIProvider
from app.ai.summarizer import (
    analyze_perspectives,
    build_perspectives_prompt,
    coerce_perspectives,
)
from app.config import settings
from app.processors.pipeline import _refresh_event_perspectives
from app.reports.daily_report import (
    _backfill_perspectives,
    fetch_report_items,
    format_persian_daily_report,
    perspective_lines,
)
from app.reports.pdf_report import build_daily_report_pdf

VALID_MATRIX = {
    "perspectives": [
        {"source_name": "Reuters", "orientation": "west", "claim_fa": "به گزارش رویترز پنج کشتی هدف قرار گرفتند."},
        {"source_name": "TASS", "orientation": "russian", "claim_fa": "به ادعای تاس هیچ کشتی روسی آسیب ندیده است."},
        {"source_name": "Saba News Agency", "orientation": "party", "claim_fa": "به گفته سبا، نیروهای یمنی یک نفتکش را متوقف کردند."},
    ],
    "disagreement_fa": "رویترز از پنج کشتی و تاس از صفر آسیب سخن می‌گوید.",
    "silent_parties_fa": ["هاآرتص"],
}


def _item(**overrides: object) -> dict:
    item = {
        "event_id": 1,
        "topic_key": "ye",
        "topic_fa": "یمن و دریای سرخ",
        "importance": 4,
        "event_type": "MAR",
        "category_fa": "دریانوردی و امنیت ترانزیت",
        "countries": ["یمن"],
        "actors": ["انصارالله"],
        "location": "باب‌المندب",
        "headline_fa": "توقف نفتکش در دریای سرخ",
        "summary_fa": "رویدادی در دریای سرخ رخ داد و رسانه‌ها روایت‌های متفاوتی دارند.",
        "verification_status": "disputed",
        "verification_status_fa": "⚖️ مورد مناقشه",
        "primary_source": "Reuters",
        "primary_url": "https://reuters.com/1",
        "additional_sources": [],
        "total_sources_count": 2,
        "perspectives": VALID_MATRIX["perspectives"],
        "disagreement_fa": VALID_MATRIX["disagreement_fa"],
        "silent_parties_fa": VALID_MATRIX["silent_parties_fa"],
    }
    item.update(overrides)
    return item


def test_prompt_carries_every_camp_behind_untrusted_delimiters():
    prompt = build_perspectives_prompt(
        "حمله به نفتکش",
        [
            {"source_name": "Reuters", "orientation": "غربی", "text": "پنج کشتی هدف قرار گرفت"},
            {"source_name": "TASS", "orientation": "روسی", "text": "هیچ آسیبی گزارش نشده"},
        ],
    )
    assert "Reuters" in prompt and "TASS" in prompt
    assert "گرایش: غربی" in prompt
    assert prompt.count("<<<UNTRUSTED_EXCERPT>>>") == 2
    assert "silent_parties_fa" in prompt


@pytest.mark.asyncio
async def test_matrix_is_parsed_from_the_model_response():
    provider = MockAIProvider(canned_response=VALID_MATRIX)

    result = await analyze_perspectives(
        "حمله به نفتکش",
        [
            {"source_name": "Reuters", "orientation": "غربی", "text": "پنج کشتی"},
            {"source_name": "TASS", "orientation": "روسی", "text": "هیچ آسیب"},
        ],
        provider=provider,
    )

    assert [item.source_name for item in result.perspectives] == ["Reuters", "TASS", "Saba News Agency"]
    assert "رویترز" in result.disagreement_fa
    assert result.silent_parties_fa == ["هاآرتص"]
    assert provider.calls_by_purpose["perspectives"] == 1


@pytest.mark.asyncio
async def test_single_camp_never_costs_a_model_call():
    provider = MockAIProvider(canned_response=VALID_MATRIX)
    result = await analyze_perspectives(
        "رویداد تک‌منبعی",
        [{"source_name": "Reuters", "orientation": "غربی", "text": "خبر"}],
        provider=provider,
    )
    assert result.perspectives == []
    assert provider.calls_by_purpose.get("perspectives", 0) == 0


def test_a_non_persian_claim_is_dropped_but_the_rest_survives():
    result = coerce_perspectives(
        {
            "perspectives": [
                {"source_name": "Reuters", "orientation": "west", "claim_fa": "Four ships were hit near the strait."},
                {"source_name": "TASS", "orientation": "russian", "claim_fa": "تاس از نبود آسیب گزارش می‌دهد."},
            ],
            "disagreement_fa": "The sides disagree on the number of ships.",
            "silent_parties_fa": ["هاآرتص"],
        }
    )
    assert [item.source_name for item in result.perspectives] == ["TASS"]
    assert result.disagreement_fa == ""
    assert result.silent_parties_fa == ["هاآرتص"]


def test_matrix_renders_camps_in_display_order_with_disagreement_and_silence():
    lines = "\n".join(perspective_lines(_item()))
    assert "مقایسه روایت‌ها" in lines
    assert lines.index("غربی") < lines.index("روسی") < lines.index("طرف درگیر")
    assert "نقاط اختلاف" in lines
    assert "بی‌واکنش" in lines
    assert "هاآرتص" in lines


def test_bulletin_text_shows_the_matrix_and_hides_it_when_empty():
    with_matrix = format_persian_daily_report(
        "2026-11-14", [_item()], {"total_events": 1, "min_importance": 2}
    )
    without_matrix = format_persian_daily_report(
        "2026-11-14",
        [_item(perspectives=[], disagreement_fa="", silent_parties_fa=[])],
        {"total_events": 1, "min_importance": 2},
    )
    assert "مقایسه روایت‌ها" in with_matrix
    assert "به ادعای تاس" in with_matrix
    assert "مقایسه روایت‌ها" not in without_matrix


def test_pdf_renders_the_matrix_block(tmp_path):
    output = build_daily_report_pdf(
        "2026-11-14",
        [_item()],
        {"min_importance": 2},
        tmp_path / "matrix.pdf",
    )
    assert output.stat().st_size > 0


@pytest.mark.asyncio
async def test_backfill_gives_a_matrix_to_an_event_two_camps_already_cover(monkeypatch):
    covered = _item(
        perspectives=[],
        disagreement_fa="",
        silent_parties_fa=[],
        primary_source="Euronews",
        additional_sources=[{"source": "Al Jazeera English", "url": "https://aj/1"}],
    )
    single_camp = _item(event_id=2, perspectives=[], primary_source="Reuters", additional_sources=[])
    calls: list[int] = []

    async def fake_refresh(**kwargs: object) -> bool:
        calls.append(int(kwargs["event_id"]))
        return True

    summary = SimpleNamespace(
        details_fa={
            "perspectives": VALID_MATRIX["perspectives"],
            "disagreement_fa": VALID_MATRIX["disagreement_fa"],
            "silent_parties_fa": VALID_MATRIX["silent_parties_fa"],
        }
    )
    session = _FakeSession([], summary)
    monkeypatch.setattr("app.processors.pipeline._refresh_event_perspectives", fake_refresh)

    changed = await _backfill_perspectives(session, [covered, single_camp])

    assert changed is True
    assert calls == [1]
    assert covered["perspectives"] == VALID_MATRIX["perspectives"]
    assert covered["disagreement_fa"] == VALID_MATRIX["disagreement_fa"]
    assert single_camp["perspectives"] == []


@pytest.mark.asyncio
async def test_backfill_respects_its_per_briefing_budget(monkeypatch):
    monkeypatch.setattr(settings, "max_perspective_backfill_per_briefing", 1)
    items = [
        _item(
            event_id=index,
            perspectives=[],
            primary_source="Euronews",
            additional_sources=[{"source": "TASS", "url": f"https://tass.ru/{index}"}],
        )
        for index in (1, 2)
    ]
    calls: list[int] = []

    async def fake_refresh(**kwargs: object) -> bool:
        calls.append(int(kwargs["event_id"]))
        return False

    monkeypatch.setattr("app.processors.pipeline._refresh_event_perspectives", fake_refresh)

    await _backfill_perspectives(_FakeSession([], None), items)

    assert calls == [1]


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class _First:
    def __init__(self, value: object) -> None:
        self._value = value

    def first(self) -> object:
        return self._value


class _FakeSession:
    def __init__(self, rows: list[tuple], summary: object | None) -> None:
        self._rows = rows
        self._summary = summary
        self._commits = 0

    async def execute(self, _stmt: object) -> _Rows:
        return _Rows(self._rows)

    async def scalars(self, _stmt: object) -> _First:
        return _First(self._summary)

    async def scalar(self, _stmt: object) -> object | None:
        return self._summary

    async def commit(self) -> None:
        self._commits += 1


@pytest.mark.asyncio
async def test_refresh_builds_and_caches_the_matrix_per_camp():
    summary = SimpleNamespace(details_fa={}, event_id=7)
    rows = [
        (11, "Five ships hit in the Red Sea", "Houthi attack damaged five vessels.", "Reuters"),
        (12, "No Russian ship damaged", "TASS reports nothing was hit.", "TASS"),
    ]
    session = _FakeSession(rows, summary)
    provider = MockAIProvider(canned_response=VALID_MATRIX)

    assert await _refresh_event_perspectives(session, 7, "Red Sea tanker", provider) is True
    assert summary.details_fa["perspectives_orientations"] == ["russian", "west"]
    assert summary.details_fa["perspectives"]
    assert summary.details_fa["disagreement_fa"]
    # Camps without an excerpt are reported as absent instead of being silently dropped.
    silent = " ".join(summary.details_fa["silent_parties_fa"])
    assert "روسی" not in silent  # TASS covered this event
    assert "عربی" in silent and "اسرائیلی" in silent
    assert provider.calls_by_purpose["perspectives"] == 1

    # A second merge from the same two camps must not spend another model call.
    assert await _refresh_event_perspectives(session, 7, "Red Sea tanker", provider) is False
    assert provider.calls_by_purpose["perspectives"] == 1


@pytest.mark.asyncio
async def test_refresh_skips_an_event_covered_by_a_single_camp():
    summary = SimpleNamespace(details_fa={}, event_id=8)
    rows = [(11, "One report", "Only one outlet covered it.", "Reuters")]
    provider = MockAIProvider(canned_response=VALID_MATRIX)

    assert await _refresh_event_perspectives(_FakeSession(rows, summary), 8, "title", provider) is False
    assert provider.calls_by_purpose.get("perspectives", 0) == 0
    assert summary.details_fa == {}


@pytest.mark.asyncio
async def test_model_failure_leaves_the_event_without_a_matrix():
    class _Boom(MockAIProvider):
        async def generate_json(self, *args: object, **kwargs: object) -> dict:
            raise RuntimeError("model down")

    summary = SimpleNamespace(details_fa={}, event_id=9)
    rows = [
        (11, "A", "Text A", "Reuters"),
        (12, "B", "Text B", "TASS"),
    ]
    assert await _refresh_event_perspectives(_FakeSession(rows, summary), 9, "title", _Boom()) is False
    assert summary.details_fa == {}


@pytest.mark.asyncio
async def test_primary_article_is_not_listed_as_parallel_coverage():
    event = MagicMock()
    event.id, event.title, event.event_type, event.importance = 1, "test event", "MIL", 4
    event.first_seen_at = datetime.now(UTC)
    event.countries, event.actors, event.location = ["یمن"], ["انصارالله"], "عدن"

    summary = MagicMock()
    summary.headline_fa = "توقف یک نفتکش در دریای سرخ گزارش شد"
    summary.summary_fa = "منابع محلی از توقف یک نفتکش در دریای سرخ خبر دادند و مقام‌ها آن را تأیید نکرده‌اند."
    summary.verification_status = "unconfirmed"
    summary.details_fa = {
        "source": "Reuters",
        "url": "https://reuters.com/1",
        "sources": [{"source": "Reuters", "url": "https://reuters.com/1"}],
    }

    article = MagicMock()
    article.id, article.source_id, article.original_url = 10, 1, "https://reuters.com/1"
    article.collected_at = datetime.now(UTC)

    source = MagicMock()
    source.id, source.name = 1, "Reuters"

    session = AsyncMock()
    first, second = MagicMock(), MagicMock()
    first.all.return_value = [(event, summary)]
    second.all.return_value = [(1, article, source)]
    session.execute.side_effect = [first, second]

    _, items, _meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert items[0]["primary_source"] == "Reuters"
    assert items[0]["additional_sources"] == []
    assert items[0]["total_sources_count"] == 1

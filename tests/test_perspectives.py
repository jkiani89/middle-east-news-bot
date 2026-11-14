"""Phase T-2: one event, several camps, and the disagreement between them."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.provider import MockAIProvider
from app.ai.summarizer import (
    analyze_perspectives,
    build_perspectives_prompt,
    coerce_perspectives,
)
from app.processors.pipeline import _refresh_event_perspectives
from app.reports.daily_report import format_persian_daily_report, perspective_lines
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

    async def execute(self, _stmt: object) -> _Rows:
        return _Rows(self._rows)

    async def scalars(self, _stmt: object) -> _First:
        return _First(self._summary)


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

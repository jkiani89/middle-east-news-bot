"""Phase T-3: the registry must carry the voices that counterweight the default feed mix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.editorial.perspectives import (
    UNKNOWN_ORIENTATION,
    distinct_orientations,
    group_perspectives,
    orientation_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES = json.loads((PROJECT_ROOT / "editorial" / "sources.json").read_text(encoding="utf-8"))
NAMES = {source["name"] for source in SOURCES}


def test_every_source_carries_the_fields_the_sync_expects():
    required = {
        "name",
        "base_url",
        "feed_url",
        "source_type",
        "country",
        "language",
        "is_active",
        "reliability_note",
    }
    for source in SOURCES:
        assert required <= set(source), f"{source.get('name')} is missing {required - set(source)}"


def test_source_names_and_feeds_are_unique():
    assert len(NAMES) == len(SOURCES)
    feeds = [source["feed_url"] for source in SOURCES]
    assert len(set(feeds)) == len(feeds)


def test_every_feed_is_https():
    for source in SOURCES:
        assert source["feed_url"].startswith("https://"), source["name"]


@pytest.mark.parametrize(
    "name",
    [
        "TASS",
        "Radio Farda",
        "Haaretz",
        "Saba News Agency",
        "Mehr News",
        "Saudi Press Agency (SPA)",
        "Saba News (Aden)",
    ],
)
def test_counterweight_sources_are_active(name: str):
    source = next(source for source in SOURCES if source["name"] == name)
    assert source["is_active"] is True
    assert source["reliability_note"].strip()


@pytest.mark.parametrize(
    ("source_name", "orientation_key"),
    [
        ("TASS", "russian"),
        ("Radio Farda", "west"),
        ("Haaretz", "israeli"),
        ("Saba News Agency", "houthi"),
        ("Saudi Press Agency (SPA)", "saudi"),
        ("Saba News (Aden)", "yemen_gov"),
        ("Mehr News", "iranian"),
        ("Reuters", "west"),
        ("Al Jazeera English", "arab"),
    ],
)
def test_camps_are_labelled(source_name: str, orientation_key: str):
    assert orientation_for(source_name).key == orientation_key


def test_orientation_lookup_ignores_case_and_spacing():
    assert orientation_for("  tass  ").key == "russian"


def test_unlisted_source_falls_back_instead_of_disappearing():
    orientation = orientation_for("Some Brand New Wire")
    assert orientation is UNKNOWN_ORIENTATION
    assert orientation.label_fa


def test_distinct_orientations_counts_only_real_camps():
    assert distinct_orientations(["Reuters", "TASS"]) == {"west", "russian"}
    assert distinct_orientations(["Reuters", "Unknown Outlet"]) == {"west"}
    assert distinct_orientations([]) == set()


def test_grouping_orders_camps_and_drops_claims_without_text():
    grouped = group_perspectives(
        [
            {"source_name": "TASS", "claim_fa": "ادعای مسکو درباره حمله"},
            {"source_name": "Reuters", "claim_fa": "ادعای رویترز درباره حمله"},
            {"source_name": "Haaretz", "claim_fa": "   "},
        ]
    )
    assert [orientation.key for orientation, _claims in grouped] == ["west", "russian"]
    assert grouped[0][1][0]["source_name"] == "Reuters"

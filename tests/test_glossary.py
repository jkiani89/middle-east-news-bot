"""Phase Y-3: editorial glossary loader and prompt injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.editorial.glossary import (
    DEFAULT_GLOSSARY_PATH,
    MAX_BLOCK_CHARS,
    MAX_ENTRIES_PER_PROMPT,
    GlossaryError,
    build_glossary_block,
    glossary_for_prompt,
    load_glossary,
    match_entries,
)

ENTRY = {
    "id": "hezbollah",
    "kind": "organization",
    "canonical_fa": "حزب‌الله",
    "source_forms": ["Hezbollah", "Hizbullah"],
    "variants_fa": ["حزب الله"],
    "review_required": False,
}


def _write_glossary(tmp_path: Path, entries: list[dict], version: int = 7) -> Path:
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps({"version": version, "entries": entries}), encoding="utf-8")
    return path


def test_real_glossary_file_loads_in_ci() -> None:
    glossary = load_glossary()
    assert DEFAULT_GLOSSARY_PATH.exists()
    assert glossary.version >= 1
    assert all(entry.review_required for entry in glossary.entries)
    # Nothing awaiting approval may reach a prompt.
    assert glossary.usable_entries() == ()


def test_content_sha_tracks_the_file_contents() -> None:
    first = load_glossary()
    assert len(first.content_sha) == 12
    payload = json.loads(DEFAULT_GLOSSARY_PATH.read_text(encoding="utf-8"))
    payload["entries"][0]["canonical_fa"] += " (ویرایش)"
    # Same version, edited entry: the fingerprint must differ.
    from app.editorial.glossary import parse_glossary

    edited = parse_glossary(payload, DEFAULT_GLOSSARY_PATH)
    from app.editorial.glossary import _content_sha

    assert _content_sha(edited) != first.content_sha


def test_prompt_metadata_carries_the_content_sha(tmp_path: Path) -> None:
    path = _write_glossary(tmp_path, [ENTRY])
    _, meta = glossary_for_prompt("Hezbollah", path=path)
    assert len(meta["content_sha"]) == 12
    assert meta["entries"] == ["hezbollah"]


def test_missing_file_is_tolerated_without_entries(tmp_path: Path) -> None:
    glossary = load_glossary(tmp_path / "absent.json")
    assert glossary.entries == ()


# Acceptance Y-3.12
def test_invalid_json_raises_an_explicit_error(tmp_path: Path) -> None:
    path = tmp_path / "glossary.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(GlossaryError) as excinfo:
        load_glossary(path)
    assert str(path) in str(excinfo.value)
    assert "invalid JSON" in str(excinfo.value)


def test_malformed_entry_names_the_entry_and_file(tmp_path: Path) -> None:
    path = _write_glossary(tmp_path, [{**ENTRY, "canonical_fa": ""}])
    with pytest.raises(GlossaryError) as excinfo:
        load_glossary(path)
    assert "hezbollah" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = _write_glossary(tmp_path, [ENTRY, ENTRY])
    with pytest.raises(GlossaryError):
        load_glossary(path)


# Acceptance Y-3.13
def test_only_matching_entries_reach_the_prompt(tmp_path: Path) -> None:
    other = {
        "id": "unifil",
        "kind": "organization",
        "canonical_fa": "یونیفیل",
        "source_forms": ["UNIFIL"],
        "review_required": False,
    }
    path = _write_glossary(tmp_path, [ENTRY, other])
    block, meta = glossary_for_prompt("Hezbollah leader speaks", "The group denied the report.", path=path)
    assert meta["entries"] == ["hezbollah"]
    assert "حزب‌الله" in block
    assert "یونیفیل" not in block


def test_entry_limit_is_enforced(tmp_path: Path) -> None:
    entries = [
        {**ENTRY, "id": f"e{index}", "canonical_fa": f"نام {index}", "source_forms": ["shared"]}
        for index in range(MAX_ENTRIES_PER_PROMPT + 5)
    ]
    path = _write_glossary(tmp_path, entries)
    glossary = load_glossary(path)
    matched = match_entries(glossary, "shared text")
    assert len(matched) == MAX_ENTRIES_PER_PROMPT


def test_review_required_entries_are_never_injected(tmp_path: Path) -> None:
    path = _write_glossary(tmp_path, [{**ENTRY, "review_required": True}])
    block, meta = glossary_for_prompt("Hezbollah leader speaks", path=path)
    assert block == ""
    assert meta["entries"] == []
    assert meta["review_required_skipped"] == 1


def test_arabic_clitics_are_tolerated(tmp_path: Path) -> None:
    entry = {
        "id": "army",
        "kind": "organization",
        "canonical_fa": "ارتش لبنان",
        "source_forms": ["الجيش"],
        "review_required": False,
    }
    path = _write_glossary(tmp_path, [entry])
    glossary = load_glossary(path)
    for text in (
        "قال الجيش إنه يتابع",       # bare definite article
        "وأعلن والجيش انتشاره",       # conjunction + article
        "وأبلغ للجيش عن تحرك",        # preposition + article (assimilated)
    ):
        assert [e.id for e in match_entries(glossary, text)] == ["army"], text
    # A different word that merely starts with the same letters must not match.
    assert [e.id for e in match_entries(glossary, "الجيشيون تظاهروا")] == []
    # A form stored without the article still matches the article-bearing text.
    entry_no_article = {**entry, "id": "army2", "source_forms": ["جيش"]}
    path2 = _write_glossary(tmp_path, [entry_no_article])
    assert [e.id for e in match_entries(load_glossary(path2), "قال الجيش إنه يتابع")] == ["army2"]


def test_block_respects_the_character_cap(tmp_path: Path) -> None:
    entries = [
        {
            "id": f"e{index}",
            "kind": "place",
            "canonical_fa": "نام بسیار طولانی مکانی " * 2,
            "source_forms": ["trigger"],
            "review_required": False,
        }
        for index in range(MAX_ENTRIES_PER_PROMPT)
    ]
    path = _write_glossary(tmp_path, entries)
    block, _ = glossary_for_prompt("trigger", path=path)
    assert 0 < len(block) <= MAX_BLOCK_CHARS


def test_build_block_is_empty_without_entries() -> None:
    assert build_glossary_block([]).startswith("راهنمای املای نام‌ها")

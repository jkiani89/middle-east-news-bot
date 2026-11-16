"""Phase Y-11: log-only lexical prefilter (agreement with the model is measured in SQL)."""

from __future__ import annotations

from app.editorial.prefilter import (
    REASON_NO_REGIONAL_MENTION,
    REASON_REGIONAL_MENTION,
    prefilter_analysis,
)


def test_story_without_any_regional_name_would_be_rejected() -> None:
    verdict = prefilter_analysis(
        "Apple reports quarterly earnings",
        "The company beat analyst expectations on services revenue in Europe.",
    )
    assert verdict == {"would_reject": True, "reason": REASON_NO_REGIONAL_MENTION}


def test_regional_country_keeps_the_story() -> None:
    verdict = prefilter_analysis(
        "Talks resume in Vienna",
        "Officials from Iran and the agency met again on Tuesday.",
    )
    assert verdict == {"would_reject": False, "reason": REASON_REGIONAL_MENTION}


def test_configured_topic_actor_keeps_the_story() -> None:
    verdict = prefilter_analysis("Statement from the Guards", "The IRGC published a statement.")
    assert verdict["would_reject"] is False


def test_regional_waterway_keeps_the_story() -> None:
    verdict = prefilter_analysis(
        "Tanker reports an incident", "A vessel was approached near the Strait of Hormuz."
    )
    assert verdict["would_reject"] is False


def test_empty_text_is_treated_as_would_reject() -> None:
    assert prefilter_analysis("", "")["would_reject"] is True

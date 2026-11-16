"""Tests for content quality and information sufficiency classification."""

from __future__ import annotations

from app.editorial.content_quality import (
    QUALITY_LABELS_FA,
    classify_content_quality,
)


def test_full_text_classification() -> None:
    """Articles with 400+ characters of body text classify as full_text."""
    text = "این یک گزارش تفصیلی از تحولات منطقه است. " * 15  # ~600 chars
    result = classify_content_quality(
        title="نشست دیپلماتیک در ژنو",
        text=text,
        text_source="full_body",
    )
    assert result["status"] == "full_text"
    assert result["label_fa"] == QUALITY_LABELS_FA["full_text"]
    assert result["is_insufficient"] is False
    assert result["char_count"] == len(text.strip())


def test_short_substantive_summary_classification() -> None:
    """Short text (< 400 chars) with genuine factual substance is short_summary, not insufficient."""
    short_substantive = (
        "وزارت دفاع سوریه اعلام کرد پدافند هوایی این کشور چند فروند موشک شلیک شده "
        "از بلندی‌های جولان را بر فراز دمشق رهگیری و سرنگون کرده است."
    )
    assert len(short_substantive) < 400
    assert len(short_substantive) > 80

    result = classify_content_quality(
        title="حمله موشکی به دمشق و واکنش پدافند هوایی",
        text=short_substantive,
        text_source="rss_summary",
    )
    assert result["status"] == "short_summary"
    assert result["label_fa"] == QUALITY_LABELS_FA["short_summary"]
    assert result["is_insufficient"] is False


def test_insufficient_content_empty_or_too_short() -> None:
    """Extremely short texts (< 80 chars) classify as insufficient_content."""
    result = classify_content_quality(
        title="خبر فوری از غزه",
        text="حمله هوایی رخ داد.",
    )
    assert result["status"] == "insufficient_content"
    assert result["label_fa"] == QUALITY_LABELS_FA["insufficient_content"]
    assert result["is_insufficient"] is True
    assert result["reason"] == "text_too_short_or_empty"


def test_insufficient_content_headline_mirror() -> None:
    """Text that merely repeats the headline without additional substance is insufficient."""
    title = "سازمان ملل خواستار توقف فوری درگیری‌ها در سودان شد"
    text = "سازمان ملل خواستار توقف فوری درگیری‌ها در سودان شد."
    result = classify_content_quality(title=title, text=text)
    assert result["status"] == "insufficient_content"
    assert result["is_insufficient"] is True
    assert result["reason"] == "headline_mirror_no_substance"


def test_insufficient_content_placeholder_and_filler_phrases() -> None:
    """Texts containing empty filler phrases classify as insufficient_content."""
    samples = [
        "طبق گزارش خبرگزاری، این خبر به وضعیت حمل‌ونقل انرژی در تنگه هرمز می‌پردازد.",
        "یک سازمان بین‌المللی بیانیه‌ای صادر کرد. جزئیات بیشتری درباره این بیانیه ذکر نشده است.",
        "تحولات اقتصادی در منطقه ادامه دارد اما اطلاعات بیشتری منتشر نشده است.",
    ]
    for sample in samples:
        result = classify_content_quality(title="گزارش روز", text=sample)
        assert result["status"] == "insufficient_content", f"Failed for: {sample}"
        assert result["is_insufficient"] is True
        assert result["reason"] == "filler_or_missing_details"

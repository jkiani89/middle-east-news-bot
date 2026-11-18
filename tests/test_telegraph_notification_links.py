"""The Telegram notification must point at every Telegraph page, not only the index.

One report = one index page + one page per topic. Readers should reach a topic in one
tap, so each section line carries the topic's own URL with the topic name as its label.
A section whose page URL is missing (dry run, partial publication) is still listed by
name with its item count, because the notification must never lose a subject.
"""

from datetime import date

from app.reports.telegraph_report import format_telegram_notification

REPORT_DATE = date(2026, 11, 18)
WINDOW = {
    "window_start": "2026-11-17T20:00:00+00:00",
    "window_end": "2026-11-18T08:00:00+00:00",
}


def _sections() -> list[tuple[str, str, list[dict[str, object]]]]:
    return [
        ("ir", "ایران و مذاکرات", [{"headline_fa": "خبر یک"}, {"headline_fa": "خبر دو"}]),
        ("ye", "یمن و دریای سرخ", [{"headline_fa": "خبر سه"}]),
    ]


def _pages() -> dict[str, dict[str, object]]:
    return {
        "ir": {"url": "https://telegra.ph/iran-11-18", "name_fa": "ایران و مذاکرات", "item_count": 2},
        "ye": {"url": "https://telegra.ph/yemen-11-18", "name_fa": "یمن و دریای سرخ", "item_count": 1},
    }


def test_notification_links_the_index_and_every_topic_page() -> None:
    text = format_telegram_notification(
        REPORT_DATE,
        8,
        "https://telegra.ph/report-index-11-18",
        _sections(),
        WINDOW,
        topic_pages=_pages(),
    )

    assert '<a href="https://telegra.ph/report-index-11-18">' in text
    assert "صفحه اصلی گزارش در Telegraph" in text
    assert '<a href="https://telegra.ph/iran-11-18">ایران و مذاکرات</a>' in text
    assert '<a href="https://telegra.ph/yemen-11-18">یمن و دریای سرخ</a>' in text


def test_every_section_keeps_its_item_count_next_to_its_link() -> None:
    text = format_telegram_notification(
        REPORT_DATE,
        8,
        "https://telegra.ph/report-index-11-18",
        _sections(),
        WINDOW,
        topic_pages=_pages(),
    )

    assert '<a href="https://telegra.ph/iran-11-18">ایران و مذاکرات</a>: 2 خبر' in text
    assert '<a href="https://telegra.ph/yemen-11-18">یمن و دریای سرخ</a>: 1 خبر' in text
    assert "3 خبر در 2 موضوع" in text


def test_topic_without_a_page_url_is_still_listed_by_name() -> None:
    pages = _pages()
    pages.pop("ye")

    text = format_telegram_notification(
        REPORT_DATE,
        8,
        "https://telegra.ph/report-index-11-18",
        _sections(),
        WINDOW,
        topic_pages=pages,
    )

    assert '<a href="https://telegra.ph/iran-11-18">ایران و مذاکرات</a>' in text
    assert "• یمن و دریای سرخ: 1 خبر" in text
    assert "telegra.ph/yemen" not in text


def test_no_topic_pages_degrades_to_names_only_and_keeps_the_index_link() -> None:
    text = format_telegram_notification(
        REPORT_DATE,
        8,
        "https://telegra.ph/report-index-11-18",
        _sections(),
        WINDOW,
    )

    assert text.count("<a href=") == 1
    assert '<a href="https://telegra.ph/report-index-11-18">' in text
    assert "• ایران و مذاکرات: 2 خبر" in text
    assert "• یمن و دریای سرخ: 1 خبر" in text


def test_missing_index_url_states_it_instead_of_emitting_a_broken_link() -> None:
    text = format_telegram_notification(
        REPORT_DATE,
        8,
        "",
        _sections(),
        WINDOW,
        topic_pages=_pages(),
    )

    assert "لینک گزارش در دسترس نیست." in text
    assert "telegra.ph/report-index" not in text
    # The topic links are independent of the index page and must survive it.
    assert '<a href="https://telegra.ph/iran-11-18">ایران و مذاکرات</a>' in text

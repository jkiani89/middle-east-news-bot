"""Persian right-to-left PDF rendering of the daily intelligence bulletin.

The text formatter in :mod:`app.reports.daily_report` targets Telegram's HTML
parse mode; this module renders the same items into a styled A4 document.
Vazirmatn is registered from ``assets/fonts`` because a PDF without an Arabic
script font draws every Persian letter in its isolated form. Letters are joined
by ``arabic-reshaper`` and ordered by ``python-bidi`` before layout; both are
pure Python, so nothing extra is needed in the slim runtime image.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.editorial.perspectives import group_perspectives

logger = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
REGULAR_FONT = "Vazirmatn"
BOLD_FONT = "Vazirmatn-Bold"
FALLBACK_FONT = "Helvetica"
FALLBACK_BOLD_FONT = "Helvetica-Bold"

PAGE_WIDTH, PAGE_HEIGHT = A4
SIDE_MARGIN = 16 * mm

NAVY = colors.HexColor("#132a45")
GOLD = colors.HexColor("#b8892b")
SLATE = colors.HexColor("#4a5a6a")
INK = colors.HexColor("#1c1c1c")
LIGHT_RULE = colors.HexColor("#d8dee6")
PAPER_TINT = colors.HexColor("#f6f8fa")
STRATEGIC_ACCENT = colors.HexColor("#9b2226")
REGIONAL_ACCENT = colors.HexColor("#1d5b79")
WARNING = colors.HexColor("#9b2226")

# Emoji in the Telegram labels cannot be drawn by a text font, so the PDF keeps
# the same five statuses in plain Persian words.
PDF_STATUS_LABELS: dict[str, str] = {
    "confirmed": "تأییدشده",
    "partially_confirmed": "تا حدی تأییدشده",
    "unconfirmed": "تأییدنشده (تک‌منبعی)",
    "disputed": "مورد مناقشه",
    "false": "ردشده",
}

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_fonts_ready: bool | None = None


def _digits(value: Any) -> str:
    """Render a number with Persian digits."""
    return str(value).translate(_PERSIAN_DIGITS)


def _register_fonts() -> bool:
    """Register Vazirmatn once per process; fall back to Helvetica without it."""
    global _fonts_ready
    if _fonts_ready is not None:
        return _fonts_ready

    regular = FONT_DIR / "Vazirmatn-Regular.ttf"
    bold = FONT_DIR / "Vazirmatn-Bold.ttf"
    if not regular.exists() or not bold.exists():
        logger.warning(
            "Persian fonts are missing from %s; the PDF renders without Persian shaping.",
            FONT_DIR,
        )
        _fonts_ready = False
        return False

    pdfmetrics.registerFont(TTFont(REGULAR_FONT, str(regular)))
    pdfmetrics.registerFont(TTFont(BOLD_FONT, str(bold)))
    pdfmetrics.registerFontFamily(
        REGULAR_FONT,
        normal=REGULAR_FONT,
        bold=BOLD_FONT,
        italic=REGULAR_FONT,
        boldItalic=BOLD_FONT,
    )
    _fonts_ready = True
    return True


def _visual(text: Any) -> str:
    """Return letter-joined, visual-order text for a right-to-left run."""
    raw = "" if text is None else str(text)
    if not _fonts_ready:
        return raw
    return get_display(arabic_reshaper.reshape(raw))


def _fa(text: Any) -> str:
    """Visual-order Persian text that is safe inside a ReportLab Paragraph."""
    return escape(_visual(text))


def _wrap_rtl(text: Any, style: ParagraphStyle, width: float, pad: float = 8.0) -> str:
    """Wrap right-to-left text into explicit lines, then shape each line.

    Reversing a whole paragraph and letting ReportLab wrap it silently puts the
    lines in the wrong order: the logical last line ends up on top. Wrapping in
    logical order and shaping line by line keeps both the line order and the
    word order correct.
    """
    raw = "" if text is None else str(text).strip()
    if not raw:
        return ""
    if not _fonts_ready:
        return escape(raw)

    available = max(20.0, width - pad)
    lines: list[list[str]] = []
    current: list[str] = []
    for word in raw.split():
        candidate = " ".join([*current, word])
        if current and pdfmetrics.stringWidth(_visual(candidate), style.fontName, style.fontSize) > available:
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)

    return "<br/>".join(escape(_visual(" ".join(line))) for line in lines)


def _date_label(report_date: Any) -> str:
    if hasattr(report_date, "strftime"):
        return _digits(report_date.strftime("%Y/%m/%d"))
    return _digits(report_date)


def _styles() -> dict[str, ParagraphStyle]:
    regular = REGULAR_FONT if _fonts_ready else FALLBACK_FONT
    bold = BOLD_FONT if _fonts_ready else FALLBACK_BOLD_FONT
    return {
        "title": ParagraphStyle(
            "BulletinTitle",
            fontName=bold,
            fontSize=19,
            leading=27,
            textColor=colors.white,
            alignment=2,  # right
        ),
        "subtitle": ParagraphStyle(
            "BulletinSubtitle",
            fontName=regular,
            fontSize=9.5,
            leading=14,
            textColor=colors.HexColor("#dbe4ee"),
            alignment=2,
        ),
        "h2": ParagraphStyle(
            "SectionTitle",
            fontName=bold,
            fontSize=12.5,
            leading=18,
            textColor=NAVY,
            alignment=2,
            spaceAfter=3,
        ),
        "stat_value": ParagraphStyle(
            "StatValue",
            fontName=bold,
            fontSize=13,
            leading=17,
            textColor=NAVY,
            alignment=1,  # center
        ),
        "stat_label": ParagraphStyle(
            "StatLabel",
            fontName=regular,
            fontSize=8,
            leading=12,
            textColor=SLATE,
            alignment=1,
        ),
        "headline": ParagraphStyle(
            "ItemHeadline",
            fontName=bold,
            fontSize=10.8,
            leading=17,
            textColor=NAVY,
            alignment=2,
        ),
        "meta": ParagraphStyle(
            "ItemMeta",
            fontName=regular,
            fontSize=8.4,
            leading=13,
            textColor=SLATE,
            alignment=2,
        ),
        "persp_label": ParagraphStyle(
            "PerspLabel",
            fontName=bold,
            fontSize=8.6,
            leading=13,
            textColor=NAVY,
            alignment=2,
        ),
        "persp": ParagraphStyle(
            "PerspClaim",
            fontName=regular,
            fontSize=8.4,
            leading=13,
            textColor=INK,
            alignment=2,
        ),
        "body": ParagraphStyle(
            "ItemBody",
            fontName=regular,
            fontSize=9.6,
            leading=15.5,
            textColor=INK,
            alignment=2,
        ),
        "source": ParagraphStyle(
            "ItemSource",
            fontName=regular,
            fontSize=7.8,
            leading=12,
            textColor=colors.HexColor("#2f4f6f"),
            alignment=2,
        ),
        "warning": ParagraphStyle(
            "QualityWarning",
            fontName=regular,
            fontSize=8.6,
            leading=13,
            textColor=WARNING,
            alignment=2,
        ),
        "footer": ParagraphStyle(
            "BulletinFooter",
            fontName=regular,
            fontSize=8,
            leading=12,
            textColor=SLATE,
            alignment=2,
        ),
    }


def _rule(width: float, color: colors.Color, height: float = 1.2) -> Table:
    rule = Table([[""]], colWidths=[width], rowHeights=[height])
    rule.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), color),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return rule


def _header_flowables(
    styles: dict[str, ParagraphStyle],
    report_date: Any,
    items: list[dict[str, Any]],
    metadata: dict[str, Any],
    width: float,
) -> list[Any]:
    inner_width = width - 28  # the band adds 14pt of padding on each side
    title = Paragraph(
        _wrap_rtl("بولتن روزانه پایش تحولات خاورمیانه و جهان اسلام", styles["title"], inner_width, pad=0),
        styles["title"],
    )
    subtitle = Paragraph(
        _wrap_rtl(
            f"تاریخ گزارش: {_date_label(report_date)} • بازه پایش ۲۴ ساعته",
            styles["subtitle"],
            inner_width,
            pad=0,
        ),
        styles["subtitle"],
    )
    band = Table([[title], [subtitle]], colWidths=[width])
    band.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (0, 0), 13),
                ("BOTTOMPADDING", (0, 0), (0, 0), 2),
                ("TOPPADDING", (0, 1), (0, 1), 0),
                ("BOTTOMPADDING", (0, 1), (0, 1), 13),
                ("LINEBELOW", (0, -1), (-1, -1), 2.4, GOLD),
            ]
        )
    )

    strategic = sum(1 for item in items if int(item.get("importance") or 3) >= 4)
    regional = len(items) - strategic
    min_importance = metadata.get("min_importance", "")
    stats = [
        ("رویدادهای منتشرشده", _digits(len(items))),
        ("تحولات راهبردی", _digits(strategic)),
        ("تحولات منطقه‌ای", _digits(regional)),
        ("حد اهمیت", _digits(min_importance) if min_importance != "" else "—"),
    ]
    # Right-to-left reading order: the first stat sits in the rightmost column.
    ordered = list(reversed(stats))
    stat_table = Table(
        [
            [Paragraph(value, styles["stat_value"]) for _label, value in ordered],
            [
                Paragraph(_wrap_rtl(label, styles["stat_label"], width / len(stats), pad=14),
                          styles["stat_label"])
                for label, _value in ordered
            ],
        ],
        colWidths=[width / len(stats)] * len(stats),
    )
    stat_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PAPER_TINT),
                ("BOX", (0, 0), (-1, -1), 0.6, LIGHT_RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.6, LIGHT_RULE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    flowables: list[Any] = [band, Spacer(1, 8), stat_table]

    rejected = metadata.get("language_rejected_count") or 0
    if rejected:
        flowables.extend(
            [
                Spacer(1, 4),
                Paragraph(
                    _wrap_rtl(
                        f"{_digits(rejected)} رویداد به دلیل رعایت‌نشدن استاندارد زبان فارسی از این بولتن حذف شد.",
                        styles["warning"],
                        width,
                        pad=0,
                    ),
                    styles["warning"],
                ),
            ]
        )

    quality = metadata.get("content_quality")
    if quality and quality != "real":
        flowables.extend(
            [
                Spacer(1, 4),
                Paragraph(
                    _wrap_rtl(
                        f"توجه: کیفیت محتوای این بولتن «{quality}» است و برای انتشار عمومی تأیید نشده است.",
                        styles["warning"],
                        width,
                        pad=0,
                    ),
                    styles["warning"],
                ),
            ]
        )

    flowables.append(Spacer(1, 12))
    return flowables


def _sources_text(item: dict[str, Any]) -> str:
    entries: list[str] = []
    primary = " — ".join(
        part for part in (item.get("primary_source") or "", item.get("primary_url") or "") if part
    )
    if primary:
        entries.append(primary)
    for supp in item.get("additional_sources") or []:
        if not isinstance(supp, dict):
            continue
        entry = " — ".join(part for part in (supp.get("source") or "", supp.get("url") or "") if part)
        if entry:
            entries.append(entry)
    if not entries:
        return ""
    if len(entries) == 1:
        return f"منبع: {entries[0]}"
    return "منابع: " + "؛ ".join(entries)


def _perspective_blocks(
    styles: dict[str, ParagraphStyle],
    item: dict[str, Any],
    width: float,
) -> list[Any]:
    """Render the per-camp claim table of one item, or nothing when absent (T-2)."""
    grouped = group_perspectives(item.get("perspectives") or [])
    if not grouped:
        return []

    label_width = width * 0.28
    claim_width = width * 0.72
    rows: list[list[Any]] = []
    for orientation, claims in grouped:
        sources = "، ".join(
            dict.fromkeys(str(claim.get("source_name") or "").strip() for claim in claims)
        ).strip("، ")
        text = " / ".join(str(claim.get("claim_fa") or "").strip() for claim in claims)
        head = orientation.label_fa + (f" ({sources})" if sources else "")
        rows.append(
            [
                Paragraph(_wrap_rtl(text, styles["persp"], claim_width, pad=6), styles["persp"]),
                Paragraph(_wrap_rtl(head, styles["persp_label"], label_width, pad=6), styles["persp_label"]),
            ]
        )

    disagreement = str(item.get("disagreement_fa") or "").strip()
    if disagreement:
        rows.append(
            [
                Paragraph(_wrap_rtl(disagreement, styles["persp"], claim_width, pad=6), styles["persp"]),
                Paragraph(_wrap_rtl("نقاط اختلاف", styles["persp_label"], label_width, pad=6), styles["persp_label"]),
            ]
        )

    silent = "، ".join(
        str(party).strip() for party in item.get("silent_parties_fa") or [] if str(party).strip()
    )
    if silent:
        rows.append(
            [
                Paragraph(_wrap_rtl(silent, styles["persp"], claim_width, pad=6), styles["persp"]),
                Paragraph(_wrap_rtl("بی‌واکنش", styles["persp_label"], label_width, pad=6), styles["persp_label"]),
            ]
        )

    table = Table(rows, colWidths=[claim_width, label_width])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PAPER_TINT),
                ("BOX", (0, 0), (-1, -1), 0.6, LIGHT_RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, LIGHT_RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return [
        Spacer(1, 4),
        Paragraph(_wrap_rtl("مقایسه روایت‌ها", styles["persp_label"], width, pad=0), styles["persp_label"]),
        Spacer(1, 2),
        table,
    ]


def _item_flowables(
    styles: dict[str, ParagraphStyle],
    index: int,
    item: dict[str, Any],
    accent: colors.Color,
    width: float,
) -> list[Any]:
    headline = item.get("headline_fa") or item.get("headline") or item.get("title") or "بدون تیتر"
    summary = item.get("summary_fa") or item.get("summary") or ""
    importance = max(1, min(5, int(item.get("importance") or 3)))
    category = item.get("category_fa") or "عمومی"
    status = PDF_STATUS_LABELS.get(item.get("verification_status") or "", "نامشخص")
    geography = "، ".join(item.get("countries") or []) or item.get("location") or "منطقه"
    actors = "، ".join(item.get("actors") or [])

    meta_bits = [
        f"اهمیت {_digits(importance)} از ۵",
        f"حوزه: {category}",
        f"وضعیت راستی‌آزمایی: {status}",
    ]
    if item.get("event_type"):
        meta_bits.append(f"کد رویداد: {item['event_type']}")

    geo_bits = [f"جغرافیا: {geography}"]
    if actors:
        geo_bits.append(f"بازیگران: {actors}")

    # The wrapper adds 9pt of left and 7pt of right padding inside the text cell.
    text_width = width - 3 - 16
    cells: list[Any] = [
        Paragraph(
            _wrap_rtl(f"{_digits(index)}. {headline}", styles["headline"], text_width, pad=0),
            styles["headline"],
        ),
        Paragraph(_wrap_rtl(" • ".join(meta_bits), styles["meta"], text_width, pad=0), styles["meta"]),
        Paragraph(_wrap_rtl(" • ".join(geo_bits), styles["meta"], text_width, pad=0), styles["meta"]),
        Spacer(1, 3),
        Paragraph(_wrap_rtl(summary, styles["body"], text_width, pad=0), styles["body"]),
    ]
    cells.extend(_perspective_blocks(styles, item, text_width))

    sources = _sources_text(item)
    if sources:
        cells.extend(
            [Spacer(1, 3), Paragraph(_wrap_rtl(sources, styles["source"], text_width, pad=0), styles["source"])]
        )

    wrapper = Table([[cells, ""]], colWidths=[width - 3, 3])
    wrapper.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (1, 0), (1, 0), accent),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 9),
                ("RIGHTPADDING", (0, 0), (0, 0), 7),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return [wrapper]


def _section_flowables(
    styles: dict[str, ParagraphStyle],
    title: str,
    section_items: list[dict[str, Any]],
    accent: colors.Color,
    width: float,
) -> list[Any]:
    flowables: list[Any] = [
        Paragraph(_wrap_rtl(title, styles["h2"], width, pad=0), styles["h2"]),
        _rule(width, accent, 1.6),
        Spacer(1, 7),
    ]
    for index, item in enumerate(section_items, start=1):
        flowables.append(KeepTogether(_item_flowables(styles, index, item, accent, width)))
        flowables.append(Spacer(1, 9))
    return flowables


def _draw_page_furniture(canvas: Any, doc: Any) -> None:
    """Draw the footer rule, the product mark and the page number."""
    canvas.saveState()
    canvas.setStrokeColor(LIGHT_RULE)
    canvas.setLineWidth(0.6)
    canvas.line(SIDE_MARGIN, 14 * mm, PAGE_WIDTH - SIDE_MARGIN, 14 * mm)
    canvas.setFillColor(SLATE)
    canvas.setFont(REGULAR_FONT if _fonts_ready else FALLBACK_FONT, 8)
    canvas.drawString(SIDE_MARGIN, 9 * mm, "Middle East News Bot — Daily Bulletin")
    canvas.setFont(BOLD_FONT if _fonts_ready else FALLBACK_BOLD_FONT, 8)
    canvas.drawRightString(PAGE_WIDTH - SIDE_MARGIN, 9 * mm, _visual(f"صفحه {_digits(doc.page)}"))
    canvas.restoreState()


def build_daily_report_pdf(
    report_date: date | str,
    items: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
    output_path: str | Path | None = None,
) -> Path:
    """Render the bulletin into ``output_path`` and return that path.

    ``items`` are the dictionaries produced by
    :func:`app.reports.daily_report.fetch_report_items`, so the PDF and the
    Telegram text always describe the same events.
    """
    if output_path is None:
        raise ValueError("build_daily_report_pdf requires output_path")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _register_fonts()
    styles = _styles()
    metadata = metadata or {}
    content_width = PAGE_WIDTH - 2 * SIDE_MARGIN

    doc = BaseDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=SIDE_MARGIN,
        rightMargin=SIDE_MARGIN,
        topMargin=14 * mm,
        bottomMargin=20 * mm,
        title="Middle East Daily Bulletin",
        author="Middle East News Bot",
        subject="Daily intelligence bulletin",
    )
    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="bulletin",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    doc.addPageTemplates(
        [PageTemplate(id="bulletin", frames=[frame], onPage=_draw_page_furniture)]
    )

    story: list[Any] = _header_flowables(styles, report_date, items, metadata, content_width)

    strategic = [item for item in items if int(item.get("importance") or 3) >= 4]
    regional = [item for item in items if int(item.get("importance") or 3) < 4]

    if not items:
        story.append(
            Paragraph(
                _wrap_rtl(
                    "در بازه ۲۴ ساعته گذشته هیچ رویدادی با شرایط مدنظر ثبت نگردیده است.",
                    styles["body"],
                    content_width,
                    pad=0,
                ),
                styles["body"],
            )
        )

    if strategic:
        story.extend(
            _section_flowables(
                styles,
                "بخش اول: اهم تحولات راهبردی",
                strategic,
                STRATEGIC_ACCENT,
                content_width,
            )
        )
    if regional:
        story.extend(
            _section_flowables(
                styles,
                "بخش دوم: سایر تحولات منطقه‌ای و موضوعی",
                regional,
                REGIONAL_ACCENT,
                content_width,
            )
        )

    story.extend(
        [
            Spacer(1, 6),
            _rule(content_width, LIGHT_RULE, 0.8),
            Spacer(1, 6),
            Paragraph(
                _wrap_rtl(
                    "پایان بولتن — این گزارش به‌صورت خودکار و بر پایه اصول بی‌طرفی، انتساب دقیق "
                    "مواضع رسمی و تجمیع رویدادهای مستقل تدوین شده است.",
                    styles["footer"],
                    content_width,
                    pad=0,
                ),
                styles["footer"],
            ),
        ]
    )

    doc.build(story)
    logger.info("Daily bulletin PDF written to %s (%d item(s)).", out_path, len(items))
    return out_path

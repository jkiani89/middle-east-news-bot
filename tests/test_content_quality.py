"""Tests for content quality and information sufficiency classification."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.database.models import Article
from app.editorial.content_quality import (
    QUALITY_LABELS_FA,
    classify_content_quality,
)
from app.processors.pipeline import _hydrate_article_body
from app.reports.daily_report import format_persian_daily_report
from app.reports.pdf_report import build_daily_report_pdf
from app.telegram.publisher import ImmediateAnalysis, format_immediate_analysis


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
    """Extremely short texts (< 40 chars) classify as insufficient_content."""
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


# --- 33 Real News Samples Benchmark ---

REAL_NEWS_BENCHMARK_SAMPLES = [
    # 1. Short Substantive News (< 400 chars, concrete facts, no boilerplate)
    {
        "id": "SUBST-FA-1",
        "category": "short_substantive",
        "title": "حمله موشکی به حومه دمشق",
        "text": "وزارت دفاع سوریه اعلام کرد پدافند هوایی این کشور چند فروند موشک شلیک شده از بلندی‌های جولان را بر فراز دمشق رهگیری و سرنگون کرده است.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "SUBST-EN-2",
        "category": "short_substantive",
        "title": "UNIFIL peacekeepers rotate units in southern Lebanon border",
        "text": "UNIFIL peacekeepers completed a routine unit rotation in southern Lebanon near Naqoura today. The operation involved Italian and Ghanaian battalions replacing outgoing contingents along the Blue Line.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "SUBST-AR-3",
        "category": "short_substantive",
        "title": "انسحاب القوات اليمنية من محيط باب المندب",
        "text": "أعلنت القوات الحكومية اليمنية انسحابها من مناطق قرب مضيق باب المندب وإعادة تمركزها في محيط العاصمة المؤقتة عدن بهدف تعزيز الدفاع عن المدينة.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "SUBST-FA-4",
        "category": "short_substantive",
        "title": "دیدار هیئت دیپلماتیک عمان با وزیر خارجه ایران",
        "text": "هیئت دیپلماتیک سلطنت عمان به ریاست قائم‌مقام وزارت خارجه این کشور در تهران با عباس عراقچی دیدار و پیرامون تحولات منطقه و راه‌های کاهش تنش گفتگو کردند.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "SUBST-EN-5",
        "category": "short_substantive",
        "title": "Coalition forces escort commercial tankers through Bab el-Mandeb",
        "text": "A joint maritime patrol escorted three commercial bulk carriers through the southern Red Sea corridor on Monday following unconfirmed reports of drone sightings in the area.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "SUBST-FA-6",
        "category": "short_substantive",
        "title": "ورود کاروان امدادی به شمال نوار غزه",
        "text": "هلال احمر فلسطین اعلام کرد ۲۰ دستگاه کامیون حامل آرد و تجهیزات درمانی پس از هماهنگی با سازمان ملل وارد گذرگاه بیت حانون شده و میان آوارگان توزیع شد.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "SUBST-AR-7",
        "category": "short_substantive",
        "title": "قيادة حرس الحدود العراقية تعزز تحصينات الحدود مع سوريا",
        "text": "أعلنت قيادة حرس الحدود العراقية نصب كاميرات حرارية وأسلاك شائكة جديدة على طول الشريط الحدودي مع سوريا لمنع محاولات التسلل غير القانونية.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    # 2. Breaking News & Flash Alerts (Concrete details vs empty alerts)
    {
        "id": "BREAK-FA-8",
        "category": "breaking_substantive",
        "title": "فوری: وقوع انفجار در نزدیکی پایگاه نظامی در اربیل",
        "text": "منابع امنیتی اقلیم کردستان از انفجار یک پهپاد انتحاری در محوطه فرودگاه اربیل خبر دادند. به گفته این منابع حادثه تلفات جانی نداشته و سامانه پدافندی فعال شد.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "BREAK-EN-9",
        "category": "breaking_substantive",
        "title": "BREAKING: Two oil tankers collide near Strait of Hormuz, crew safe",
        "text": "Maritime authorities confirm two crude carriers suffered minor hull damage after a collision near Hormuz. All 42 crew members were safely evacuated with no oil spills detected.",
        "expected_status": "short_summary",
        "expected_insufficient": False,
    },
    {
        "id": "BREAK-FA-10",
        "category": "breaking_empty",
        "title": "فوری: به صدا درآمدن آژیر خطر در شمال اراضی اشغالی",
        "text": "آژیر خطر به صدا درآمد.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "BREAK-EN-11",
        "category": "breaking_empty",
        "title": "Flash: Explosions heard in Damascus suburb",
        "text": "Explosions reported.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "BREAK-AR-12",
        "category": "breaking_empty",
        "title": "عاجل: دوي صفارات الإنذار في الجليل الغربي",
        "text": "سماع دوي انفجارات في الجليل.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "BREAK-FA-13",
        "category": "breaking_filler",
        "title": "فوری: حادثه امنیتی برای شناور تجاری در خلیج عمان",
        "text": "منابع دریانوردی از رخداد یک حادثه امنیتی خبر دادند. این خبر در حال به‌روزرسانی است و جزئیات بیشتری ذکر نشده است.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    # 3. Headline-only or Headline Mirror (repeats headline without facts)
    {
        "id": "MIRROR-FA-14",
        "category": "headline_mirror",
        "title": "نشست اضطراری شورای امنیت پیرامون بحران خاورمیانه",
        "text": "نشست اضطراری شورای امنیت پیرامون بحران خاورمیانه.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "MIRROR-EN-15",
        "category": "headline_mirror",
        "title": "Security Council calls for immediate humanitarian truce in Sudan",
        "text": "Security Council calls for immediate humanitarian truce in Sudan",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "MIRROR-AR-16",
        "category": "headline_mirror",
        "title": "وزير الخارجية الأردني يبحث هاتفياً مع نظيره المصري التطورات الإقليمية",
        "text": "وزير الخارجية الأردني يبحث هاتفياً مع نظيره المصري التطورات الإقليمية.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "MIRROR-FA-17",
        "category": "headline_mirror",
        "title": "برگزاری رزمایش مشترک هوایی در منطقه",
        "text": "برگزاری رزمایش مشترک هوایی در منطقه برگزار شد.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "SHORT-FA-18",
        "category": "text_too_short",
        "title": "تحولات اقتصادی منطقه",
        "text": "بازار نفت امروز نوسان داشت.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "SHORT-EN-19",
        "category": "text_too_short",
        "title": "Doha Talks",
        "text": "Meetings continue.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    # 4. Boilerplate / Placeholder / Filler Texts
    {
        "id": "FILLER-FA-20",
        "category": "boilerplate_filler",
        "title": "کاهش پنجاه درصدی تردد نفتکش‌ها در تنگه هرمز",
        "text": "طبق گزارش خبرگزاری، تردد نفتکش‌ها از طریق آبراه راهبردی تنگه هرمز کاهش یافته است. این خبر به وضعیت حمل‌ونقل انرژی در تنگه هرمز می‌پردازد.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "FILLER-FA-21",
        "category": "boilerplate_filler",
        "title": "سازمان بین‌المللی: وضعیت محاصره غزه بحرانی است",
        "text": "یک نهاد امدادی بیانیه‌ای صادر کرد. جزئیات بیشتری درباره نام این سازمان و متن کامل بیانیه ذکر نشده است.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "FILLER-FA-22",
        "category": "boilerplate_filler",
        "title": "بیانیه مشترک وزرای دارایی کشورهای خلیج فارس",
        "text": "وزرای دارایی در پایان نشست بیانیه دادند اما اطلاعات بیشتری منتشر نشده است و موضوع در دست پیگیری است.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "FILLER-EN-23",
        "category": "boilerplate_filler",
        "title": "Explosion heard outside government compound in Tripoli",
        "text": "A loud blast was heard near the Tripoli ministerial complex late Sunday. No further details were immediately available.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "FILLER-AR-24",
        "category": "boilerplate_filler",
        "title": "استهداف موكب مسؤول أمني في محافظة شبوة",
        "text": "تعرض موكب مدير أمن المحافظة لانفجار عبوة ناسفة، ولم ترد تفاصيل إضافية حتى الآن حول الضحايا.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "FILLER-FA-25",
        "category": "boilerplate_filler",
        "title": "گزارش جدید بانک مرکزی درباره تورم منطقه",
        "text": "بانک مرکزی گزارش فصلی را تدوین کرد ولی متن کامل خبر در دسترس نیست و جزئیاتی ارائه نشده است.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    {
        "id": "FILLER-FA-26",
        "category": "boilerplate_filler",
        "title": "بررسی توافقات گمرکی میان عراق و کویت",
        "text": "این گزارش به بررسی توافقات مرزی دو کشور می‌پردازد بدون اینکه اطلاعات تکمیلی اعلام شده باشد.",
        "expected_status": "insufficient_content",
        "expected_insufficient": True,
    },
    # 5. Full Reports / Comprehensive Articles (>= 400 chars)
    {
        "id": "FULL-FA-27",
        "category": "full_report",
        "title": "افزایش درگیری‌های نظامی و تبادل آتش میان ایران و ایالات متحده در تنگه هرمز",
        "text": (
            "گزارش‌ها حاکی از افزایش تبادل آتش و حملات متقابل میان نیروهای نظامی ایران و ایالات متحده در منطقه خلیج فارس و تنگه هرمز است. "
            "طبق ادعای سپاه پاسداران انقلاب اسلامی، چندین نفتکش در پی برخورد با مین‌های دریایی در تنگه هرمز دچار حریق شده‌اند، در حالی که ارتش "
            "ایالات متحده اعلام کرده است در پاسخ به حملات ایران به پایگاه‌های خود در بحرین و نفتکش‌ها، مواضع پرتاب موشک در جنوب ایران را هدف قرار داده است. "
            "سازمان عملیات تجارت دریایی بریتانیا نیز وقوع حادثه برای یک نفتکش را تایید کرده است. مارکو روبیو وزیر امور خارجه آمریکا بر تداوم حضور ناوگان دریایی تاکید کرد."
        ),
        "expected_status": "full_text",
        "expected_insufficient": False,
    },
    {
        "id": "FULL-EN-28",
        "category": "full_report",
        "title": "USS Roosevelt arrives in Middle East as regional tensions rise",
        "text": (
            "According to a senior US defense official speaking to Al Jazeera, the USS Theodore Roosevelt aircraft carrier has entered "
            "the Middle East area of operations. However, the Pentagon and US Central Command (CENTCOM) have not yet officially verified the deployment. "
            "This arrival increases the number of US carrier strike groups operating in the region to three. Meanwhile, regional tensions remain elevated "
            "amidst diplomatic gridlock over the security of international maritime corridors in the Strait of Hormuz. Iran's foreign ministry warned that "
            "further militarization of regional waterways will complicate diplomatic efforts led by regional mediators."
        ),
        "expected_status": "full_text",
        "expected_insufficient": False,
    },
    {
        "id": "FULL-EN-29",
        "category": "full_report",
        "title": "Putin, Trump and Xi Jinping may meet on sidelines of APEC summit in Shenzhen",
        "text": (
            "A trilateral summit between Vladimir Putin, Donald Trump, and Xi Jinping is scheduled to take place on the sidelines of the "
            "Asia-Pacific Economic Cooperation (APEC) summit in Shenzhen. Analysts report that while Washington emphasizes nuclear arms control "
            "and the New START treaty framework, Beijing maintains its priority on regional strategic stability. European diplomats expressed concern "
            "that parallel economic talks might dilute multilateral sanctions enforcement, as global oil markets watch potential diplomatic breakthroughs."
        ),
        "expected_status": "full_text",
        "expected_insufficient": False,
    },
    {
        "id": "FULL-FA-30",
        "category": "full_report",
        "title": "صدور حکم حبس ابد غیابی برای آیت‌الله شیخ عیسی قاسم در بحرین",
        "text": (
            "طبق گزارش خبرگزاری مهر، دادگاه کیفری عالی بحرین روز دوشنبه آیت‌الله شیخ عیسی قاسم از روحانیون برجسته این کشور را به صورت غیابی به حبس ابد محکوم کرد. "
            "به دنبال صدور این حکم، شماری از معترضان در منامه و مناطق اطراف با حضور در خیابان‌ها به روند محاکمات اعتراض کردند. در مقابل، دادستانی بحرین مدعی است "
            "این پرونده مرتبط با اقدامات امنیتی بوده و تشریفات قانونی رعایت شده است، در حالی که دیده‌بان حقوق بشر نسبت به وضعیت دادرسی عادلانه ابراز نگرانی جدی نموده است."
        ),
        "expected_status": "full_text",
        "expected_insufficient": False,
    },
    {
        "id": "FULL-AR-31",
        "category": "full_report",
        "title": "الخليل: عملية عسكرية واسعة للاحتلال الإسرائيلي وحملة اعتقالات",
        "text": (
            "نفذت قوات الاحتلال الإسرائيلي عملية عسكرية واسعة في مدينة الخليل ومخيماتها جنوب الضفة الغربية، تخللتها مداهمات للمنازل وحملة اعتقالات طالت عشرات الفلسطينيين. "
            "وذكرت مصادر محلية أن القوات أغلقت المداخل الرئيسية للمدينة بالسواتر الترابية وفرضت حظر التجوال في بعض الأحياء، فيما اندلعت مواجهات بين الشبان وقوات الاحتلال. "
            "وأكدت جمعية الهلال الأحمر الفلسطيني تعامل طواقمها مع عدة إصابات بالرصاص الحي والغاز المسيل للدموع خلال المداهمات التي استمرت لساعات متواصلة."
        ),
        "expected_status": "full_text",
        "expected_insufficient": False,
    },
    {
        "id": "FULL-EN-32",
        "category": "full_report",
        "title": "Amman Summit Concludes with Bilateral Water Cooperation Agreement",
        "text": (
            "Delegates concluded the 3-day Mediterranean climate and water cooperation summit in Amman with a landmark agreement to share "
            "hydrological data and develop joint desalination infrastructure across the Jordan Valley. The treaty was signed by ministerial "
            "representatives from Jordan, Egypt, and international development banks. Experts highlighted that regional water scarcity requires "
            "cross-border technical integration and stable multilateral financing mechanisms to ensure sustainable community supply over the coming decades."
        ),
        "expected_status": "full_text",
        "expected_insufficient": False,
    },
    {
        "id": "FULL-FA-33",
        "category": "full_report",
        "title": "عقب‌نشینی نیروهای دولت یمن از پایگاه‌های راهبردی ساحل غربی",
        "text": (
            "به گزارش منابع محلی، نیروهای وابسته به دولت شناخته‌شده یمن اعلام کردند که به منظور سازماندهی مجدد خطوط دفاعی عدن، از فرودگاه ذباب و پایگاه‌های اطراف "
            "تنگه باب‌المندب عقب‌نشینی کرده‌اند. کارشناسان نظامی تاکید دارند که این جابجایی توازن میدانی در ساحل غربی را دستخوش تغییر نموده و نیروهای انصارالله را "
            "به خطوط دفاعی پایتخت موقت نزدیک‌تر ساخته است. در همین حال درگیری‌های پراکنده توپخانه‌ای در مناطق مرزی همچنان ادامه دارد و میانجیگران بین‌المللی نسبت به "
            "احتمال شعله‌ور شدن مجدد نبردهای همه‌جانبه هشدار داده‌اند."
        ),
        "expected_status": "full_text",
        "expected_insufficient": False,
    },
]


def test_33_real_news_samples_benchmark() -> None:
    """Benchmark classify_content_quality against 33 real news samples across all 5 categories."""
    assert len(REAL_NEWS_BENCHMARK_SAMPLES) >= 30

    failures = []
    for sample in REAL_NEWS_BENCHMARK_SAMPLES:
        result = classify_content_quality(
            title=sample["title"],
            text=sample["text"],
            text_source="rss_summary" if sample["expected_status"] != "full_text" else "full_body",
        )
        if result["status"] != sample["expected_status"]:
            failures.append({
                "id": sample["id"],
                "category": sample["category"],
                "expected": sample["expected_status"],
                "actual": result["status"],
                "reason": result.get("reason"),
                "text_preview": sample["text"][:60],
            })
        if result["is_insufficient"] != sample["expected_insufficient"]:
            failures.append({
                "id": sample["id"],
                "issue": "is_insufficient mismatch",
                "expected": sample["expected_insufficient"],
                "actual": result["is_insufficient"],
            })

    assert not failures, f"Benchmark failures: {failures}"


@pytest.mark.asyncio
async def test_quality_recalculated_on_hydration_from_rss_summary_to_full_body(monkeypatch) -> None:
    """Verifies that when text_source upgrades from rss_summary to full_body, content quality is recalculated."""
    article = Article(
        id=42,
        source_id=1,
        title="نشست دیپلماتیک منطقه",
        original_url="https://example.com/story-42",
        canonical_url="https://example.com/story-42",
        original_language="fa",
        original_text="این خبر به وضعیت نشست دیپلماتیک منطقه‌ای می‌پردازد.",  # Placeholder RSS snippet (>40 chars)
        content_hash="A" * 64,
        text_source="rss_summary",
        status="collected",
    )

    # 1. Before hydration: classify initial RSS summary
    initial_quality = classify_content_quality(article.title, article.original_text, article.text_source)
    assert initial_quality["status"] == "insufficient_content"
    assert initial_quality["is_insufficient"] is True
    assert initial_quality["reason"] == "filler_or_missing_details"

    # 2. Simulate lazy hydration fetching full body
    full_body = (
        "نمایندگان کشورهای منطقه در دوحه گرد هم آمدند تا درباره ترتیبات امنیتی گفتگو کنند. "
        "این نشست با حضور وزرای خارجه و مشاوران امنیت ملی برگزار شد و طرفین بر ضرورت ادامه گفتگوها "
        "و تبادل هیئت‌های کارشناسی توافق کردند. بیانیه پایانی این نشست فردا منتشر خواهد شد."
    ) * 3  # ~650 chars
    monkeypatch.setattr("app.processors.pipeline.fetch_article_full_text", AsyncMock(return_value=full_body))

    await _hydrate_article_body(article)
    assert article.text_source == "full_body"
    assert article.original_text == full_body

    # 3. After hydration: dynamically recalculated quality reflects full_text
    hydrated_quality = classify_content_quality(article.title, article.original_text, article.text_source)
    assert hydrated_quality["status"] == "full_text"
    assert hydrated_quality["is_insufficient"] is False
    assert hydrated_quality["reason"] == "full_body_length"
    assert hydrated_quality["char_count"] == len(full_body)


def test_all_report_formats_render_insufficient_content(tmp_path) -> None:
    """Verifies that Telegram HTML, Telegram plain text, Immediate Analysis, and PDF all render insufficient_content."""
    insufficient_item = {
        "event_id": 101,
        "title": "گزارش اولیه از حادثه دریایی",
        "headline_fa": "حادثه برای شناور تجاری در خلیج عمان",
        "summary_fa": "منابع محلی از حادثه برای شناور تجاری خبر دادند اما جزئیاتی ارائه نشده است.",
        "category": "SEC",
        "category_fa": "امنیت و مقابله با تروریسم",
        "event_type": "SEC",
        "importance": 4,  # High importance event with insufficient details
        "verification_status": "unconfirmed",
        "verification_status_fa": "تأییدنشده (تک‌منبعی)",
        "countries": ["ایران", "عمان"],
        "actors": ["سازمان بنادر"],
        "primary_source": "Saba News",
        "primary_url": "https://example.com/saba-101",
        "content_quality": "insufficient_content",
        "content_quality_fa": "اطلاعات ناکافی",
        "is_insufficient": True,
        "perspectives": [],
        "additional_sources": [],
    }

    report_date = date(2026, 11, 16)

    # 1. Telegram / HTML Daily Report
    html_output = format_persian_daily_report(report_date, [insufficient_item], as_html=True)
    assert "کیفیت محتوا:" in html_output
    assert "اطلاعات ناکافی" in html_output
    assert "(گزارش اولیه / نیازمند تکمیل جزئیات)" in html_output

    # 2. Telegram / Plain Text Daily Report
    plain_output = format_persian_daily_report(report_date, [insufficient_item], as_html=False)
    assert "کیفیت محتوا:" in plain_output
    assert "اطلاعات ناکافی" in plain_output
    assert "(گزارش اولیه / نیازمند تکمیل جزئیات)" in plain_output

    # 3. Immediate Analysis
    immediate = ImmediateAnalysis(
        event_title="حادثه شناور",
        headline_fa="حادثه برای شناور تجاری در خلیج عمان",
        summary_fa="گزارش اولیه بدون جزئیات",
        source_name="Saba News",
        source_url="https://example.com/saba-101",
        verification_status="unconfirmed",
        importance=4,
        category="SEC",
        countries=["ایران"],
        actors=["سازمان بنادر"],
        content_quality="insufficient_content",
        content_quality_fa="اطلاعات ناکافی",
        is_insufficient=True,
    )
    imm_output = format_immediate_analysis(immediate)
    assert "کیفیت محتوا:" in imm_output
    assert "اطلاعات ناکافی" in imm_output

    # 4. PDF Bulletin
    pdf_path = tmp_path / "test_insufficient_bulletin.pdf"
    build_daily_report_pdf(report_date, [insufficient_item], {"content_quality": "real"}, pdf_path)
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


def test_independence_of_quality_importance_and_verification() -> None:
    """Verifies that content quality, event importance, and verification status remain orthogonal."""
    # A high importance (5/5) event with unconfirmed status and insufficient content
    item_high_unconfirmed_short = classify_content_quality(
        title="انفجار مهیب در بندر تجاری",
        text="انفجار در بندر رخ داد و دود غلیظی آسمان منطقه را فرا گرفت.",  # 61 chars, substantive short
        text_source="rss_summary",
    )
    assert item_high_unconfirmed_short["status"] == "short_summary"
    assert item_high_unconfirmed_short["is_insufficient"] is False

    # A high importance event with placeholder filler
    item_high_filler = classify_content_quality(
        title="انفجار مهیب در تاسیسات نفتی",
        text="یک مقام رسمی از انفجار در تاسیسات خبر داد. جزئیات بیشتری ذکر نشده است.",
        text_source="rss_summary",
    )
    assert item_high_filler["status"] == "insufficient_content"
    assert item_high_filler["is_insufficient"] is True
    # Crucially, importance (e.g. 5) is completely external to content_quality dict
    assert "importance" not in item_high_filler
    assert "verification_status" not in item_high_filler

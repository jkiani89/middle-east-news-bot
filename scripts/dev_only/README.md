# scripts/dev_only/

اسکریپت‌های دستی توسعه، پژوهش و آزمون‌های Live. هیچ‌کدام بخشی از سامانه عملیاتی نیستند.

## هشدار مهم
اکثر این اسکریپت‌ها به APIهای خارجی (Gemini، UN News، RSS) متصل می‌شوند. اجرای آن‌ها مصرف سهمیه و هزینه دارد. طبق AGENT_PROTOCOL R-06، اجرای خودکار ممنوع؛ فقط با تأیید صریح کاربر.

## فایل‌ها
| فایل | هدف |
|---|---|
| fetch_live_un_news.py | دریافت نمونه خبر زنده از UN News |
| test_live_daily_report.py | آزمون زنده تولید گزارش روزانه |
| test_live_event_clustering.py | آزمون زنده خوشه‌بندی رویدادها |
| test_phase3_dedup_live.py | آزمون زنده تکرارزدایی |
| test_phase4_end_to_end.py | آزمون سرتاسری فاز 4 |
| test_prompt_injection.py | آزمون OWASP prompt injection روی Gemini زنده |
| test_false_positive_benchmark.py | بنچمارک false-positive |
| test_controls_live.py | آزمون کنترل‌های امنیتی |
| test_reprocessing_prevention.py | آزمون جلوگیری از بازپردازش |
| reprocess_with_real_gemini.py | بازپردازش دسته‌ای با Gemini واقعی |
| backfill_article1_model_name.py | backfill یکباره فیلد model_name |
| verify.ps1 | اسکریپت verify دستی |

## نحوه اجرا
    .\.venv\Scripts\python scripts\dev_only\fetch_live_un_news.py

## قبل از اجرا
- .env باید GEMINI_KEY_1 (یا GEMINI_API_KEY) معتبر داشته باشد
- PostgreSQL در حال اجرا باشد
- مطمئن شوید هزینه/سهمیه دارید (R-06)

## وضعیت
این اسکریپت‌ها در CI/CD اجرا نمی‌شوند و در tests/ نیستند. صرفاً ابزار دستی توسعه‌دهنده‌اند.

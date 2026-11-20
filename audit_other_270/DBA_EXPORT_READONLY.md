# مسیر جایگزین: استخراج read-only توسط DBA

> ایجنت هیچ‌کدام از این دستورها را اجرا نمی‌کند و هیچ رمزی در این فایل یا در چت قرار نمی‌گیرد.
> این مسیر برای زمانی است که نقش `audit_select_v3` تأییدشده آماده نیست و نباید ممیزی محتوایی را متوقف کند.

## ۱. تصمیم لازم پیش از اجرا

این مسیر برخلاف قاعدهٔ «هیچ حساب نوشتنی برای ممیزی» است: استخراج با `avnadmin` انجام می‌شود. برای اینکه این استثنا قابل‌دفاع باشد:

1. نشست با `PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=300000'` اجرا شود تا هر نوشتن احتمالی در سطح سرور رد شود.
2. فایل فقط `SELECT` و `\copy` داشته باشد (`export_other_270.sql`) و با `BEGIN TRANSACTION READ ONLY` و `ROLLBACK` تمام شود.
3. DBA تأیید کتبی بدهد که این استثنا برای همین یک استخراج پذیرفته شده است.
4. خروجی، فایل CSV است؛ هیچ داده‌ای تغییر نمی‌کند.

اگر نقش SELECT-only آماده شد، همان مسیر ترجیح دارد و این فایل استفاده نمی‌شود.

## ۲. اجرا

```bash
mkdir -p /tmp/audit_bundle
PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=300000' \
  psql "host=<host> dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=<ca>" \
       -v ON_ERROR_STOP=1 -f audit_other_270/export_other_270.sql
```

مسیرهای `/tmp/audit_bundle/...` داخل فایل باید به پوشهٔ خالی مقصد اشاره کنند.

## ۳. بررسی سلامت بسته

```bash
sha256sum /tmp/audit_bundle/*.csv | tee /tmp/audit_bundle/SHA256SUMS
wc -l /tmp/audit_bundle/*.csv
```

انتظار: چهار فایل `events.csv`، `articles.csv`، `article_events.csv`، `summaries.csv`؛ بلوک شمارش انتهایی باید با تعداد ردیف‌ها بخواند (منهای سطر سرصفحه).

**اگر بلوک ۰ مقدار `transaction_read_only` را `on` نشان نداد، بسته را دور بریزید و دوباره اجرا نکنید تا علت روشن شود.**

## ۴. آنچه تحویل داده می‌شود

| فایل | محتوا |
|---|---|
| `events.csv` | رویدادهای پنجره با فیلدهای تحلیل (`countries`/`actors` به‌صورت JSON) |
| `articles.csv` | همهٔ مقالات پنجره با نام منبع و متن کامل |
| `article_events.csv` | نگاشت مقاله↔رویداد |
| `summaries.csv` | خلاصه‌های رویدادهای پنجره |
| `SHA256SUMS` | اثر انگشت فایل‌ها برای بازتولید |

هیچ ستون برچسب موضوع یا خروجی `match_topics` در بسته نیست؛ برچسب سیستم محلی و در فایل جداگانه محاسبه می‌شود تا داوری کور بماند.

## ۵. پس از تحویل (سمت ایجنت، آفلاین)

```bash
python audit_other_270/adjudicate.py build --bundle /tmp/audit_bundle --out audit_other_270/audit_run
```

خروجی: `blind_sheet.csv` (بدون برچسب)، `blind_sheet_articles.csv` (مقالات بدون رویداد)، `labels_holdout.csv` (مهروموم‌شده)، `meta.json` (شمارش‌ها و هش ورودی‌ها).

`labels_holdout.csv` تا تکمیل `judgments.csv` باز نمی‌شود.

## ۶. نکتهٔ حاکمیت داده

این بسته متن کامل خبرهای تولید را در بر می‌گیرد. نگهداری آن باید مطابق سیاست دادهٔ پروژه باشد: محل موقت، حذف پس از پایان ممیزی، و ثبت هش برای بازتولید. هیچ محتوایی از این بسته در Git قرار نمی‌گیرد.

## ۷. چک‌لیست تحویل

این جدول را همراه بسته پر و ارسال کنید. هیچ مقدار محرمانه‌ای اینجا نوشته نمی‌شود.

| مورد | مقدار |
|---|---|
| زمان استخراج (UTC) | |
| نام دیتابیس | `defaultdb` |
| نسخهٔ PostgreSQL (`server_version`) | |
| بازهٔ زمانی | `2026-11-19 04:30:00+00` تا `2026-11-19 16:30:00+00` |
| `transaction_read_only` در بلوک ۰ | `on` / `off` |
| `events_in_window` از بلوک ۰ب | |
| تعداد ردیف `events.csv` | |
| تعداد ردیف `articles.csv` | |
| تعداد ردیف `article_events.csv` | |
| تعداد ردیف `summaries.csv` | |
| SHA256 `events.csv` | |
| SHA256 `articles.csv` | |
| SHA256 `article_events.csv` | |
| SHA256 `summaries.csv` | |
| تأیید استثنای حساب نوشتنی | نام/تاریخ تأییدکننده |

**معیار پذیرش بسته:** `transaction_read_only = on`، تعداد ردیف هر فایل با بلوک شمارش همان اجرا بخواند، و `SHA256`ها همراه بسته باشند. اگر هر کدام برقرار نبود، بسته پذیرفته نمی‌شود و استخراج تکرار می‌گردد.

## ۸. پس از تحویل

قرارداد ستون‌ها و کلیدها در `BUNDLE_CONTRACT.md` است. `build` پیش از ساخت شیت کور، کامل بودن ستون‌ها، یکتایی کلیدها و نبود لینک معلق را بررسی می‌کند؛ بستهٔ ناقص رد می‌شود و هیچ داوری‌ای آغاز نمی‌شود.

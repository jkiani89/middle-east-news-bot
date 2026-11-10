# راهنمای راه‌اندازی محیط محلی توسعه (Local Development Setup)

این سند مراحل کامل راه‌اندازی محیط توسعه محلی ربات اخبار خاورمیانه (`middle-east-news-bot`) را بر روی سیستم‌عامل ویندوز با استفاده از نسخه پرتابل PostgreSQL 16 و محیط مجازی پایتون تشریح می‌کند.

---

## ۱. پیش‌نیازها

- **Python:** نگارش 3.15.2 یا بالاتر
- **PostgreSQL:** نگارش 16.4 پرتابل (مستقر در `D:\pgsql\pgsql`)
- **دسترسی به اینترنت:** جهت دریافت وابستگی‌های پایتون و فیدهای خبری

---

## ۲. راه‌اندازی و مدیریت PostgreSQL محلی (پرتابل)

برای پرهیز از نصب پیچیده سرویس‌های ویندوزی، پایگاه داده به صورت مستقل در شاخه `D:\pgsql` قرار دارد:
- مسیر باینری‌ها: `D:\pgsql\pgsql\bin`
- مسیر داده‌های کلاستر: `D:\pgsql\data`

### ۲.۱. ایجاد کلاستر داده (فقط بار اول)
در صورتی که پوشه داده وجود نداشته باشد:
```powershell
& "D:\pgsql\pgsql\bin\initdb.exe" -D "D:\pgsql\data" -U postgres -A trust -E UTF8
```

### ۲.۲. اجرای سرور پایگاه داده (Background Daemon)
برای اجرای سرور به عنوان پروسه در پس‌زمینه:
```powershell
& "D:\pgsql\pgsql\bin\pg_ctl.exe" -D "D:\pgsql\data" -l "D:\pgsql\logfile.log" start
```
یا اجرای مستقیم `postgres.exe`:
```powershell
& "D:\pgsql\pgsql\bin\postgres.exe" -D "D:\pgsql\data" -p 5432
```

### ۲.۳. بررسی وضعیت اتصال
```powershell
& "D:\pgsql\pgsql\bin\pg_isready.exe" -h 127.0.0.1 -p 5432
```
خروجی موفقیت‌آمیز: `127.0.0.1:5432 - accepting connections`

### ۲.۴. متوقف کردن سرور پایگاه داده
```powershell
& "D:\pgsql\pgsql\bin\pg_ctl.exe" -D "D:\pgsql\data" stop
```

---

## ۳. ایجاد کاربر و پایگاه داده

کاربر و پایگاه داده پروژه طبق متغیرهای محیطی استاندارد به شرح زیر ایجاد می‌شوند:
- نام کاربری: `newsbot`
- رمز عبور: `CHANGE_THIS_PASSWORD`
- نام دیتابیس: `newsbot`

دستورات PowerShell برای ایجاد کاربر و دیتابیس:
```powershell
# ایجاد کاربر
& "D:\pgsql\pgsql\bin\psql.exe" -h 127.0.0.1 -U postgres -c "CREATE USER newsbot WITH PASSWORD 'CHANGE_THIS_PASSWORD';"

# ایجاد پایگاه داده
& "D:\pgsql\pgsql\bin\psql.exe" -h 127.0.0.1 -U postgres -c "CREATE DATABASE newsbot OWNER newsbot;"

# اعطای دسترسی‌ها
& "D:\pgsql\pgsql\bin\psql.exe" -h 127.0.0.1 -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE newsbot TO newsbot;"
```

---

## ۴. آماده‌سازی محیط پایتون و متغیرهای محیطی

### ۴.۱. فعال‌سازی محیط مجازی
```powershell
.\.venv\Scripts\Activate.ps1
```

### ۴.۲. نصب بسته‌ها
```powershell
pip install -r requirements.txt -c constraints.txt
pip install -r requirements-dev.txt -c constraints.txt
```
> **نکته:** فایل `constraints.txt` حاوی نگارش‌های دقیق و آزموده‌شده کل وابستگی‌ها در محیط ویندوز با پایتون ۳.۱۵ است تا از بروز ناسازگاری ناشی از انتشار نسخه‌های جدید جلوگیری شود.

### ۴.۳. پیکربندی فایل `.env`
یک کپی از `.env.example` با نام `.env` بسازید:
```powershell
Copy-Item .env.example .env
```
مقادیر پایگاه داده به صورت پیش‌فرض:
```env
DATABASE_URL=postgresql+asyncpg://newsbot:CHANGE_THIS_PASSWORD@127.0.0.1:5432/newsbot
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHANNEL_ID=@your_channel_handle
TELEGRAM_ADMIN_CHAT_ID=123456789
```

---

## ۵. پیکربندی استخر کلیدهای Gemini (Multi-Key Gemini Configuration)

برای پشتیبانی پایدار از زمان‌بندی ساعتی جمع‌آوری و خلاصه‌سازی اخبار بدون برخورد به سقف Quota (حل قطعی `ISS-020`)، سیستم از یک استخر چندکلیدی هوشمند با کتابخانه `gemini-api-rotator` بهره می‌برد:

### ۵.۱. درک محدودیت سهمیه Google Cloud (Per-Project Quota Constraint)
> [!IMPORTANT]
> محدودیت‌های طرح رایگان Google AI Studio (شامل ۲۰ درخواست روزانه برای مدل‌های تحلیلی و ۱۵ RPM) **به ازای هر پروژه Google Cloud (Per-Project)** اعمال می‌شوند، نه به ازای هر API Key.
> **ساخت چند کلید مختلف درون یک پروژه واحد هیچ افزایشی در سهمیه مصرفی ایجاد نمی‌کند.**
> جهت افزایش واقعی سقف پردازش، باید کلیدها از پروژه‌های مجزا در Google Cloud (یا حساب‌های کاربری مجزا) تولید شوند.

### ۵.۲. نحوه ساخت چند پروژه در Google Cloud
1. وارد کنسول [Google AI Studio](https://aistudio.google.com/) یا [Google Cloud Console](https://console.cloud.google.com/) شوید.
2. از منوی بالای صفحه روی Project Selector کلیک کرده و گزینه **New Project** را انتخاب کنید (مثلاً `newsbot-prod-01`, `newsbot-prod-02`, `newsbot-prod-03`).
3. در هر پروژه، یک API Key اختصاصی تولید و کپی کنید.

### ۵.۳. نحوه تنظیم کلیدها در فایل `.env`
کلیدها را با الگوی شماره‌گذاری‌شده `GEMINI_KEY_1` تا `GEMINI_KEY_N` در فایل `.env` قرار دهید:
```env
# ── Gemini API Keys (چند پروژه جداگانه Google Cloud) ──
GEMINI_KEY_1=AIzaSy...project1...
GEMINI_KEY_2=AIzaSy...project2...
GEMINI_KEY_3=AIzaSy...project3...

# مدل اصلی با اولویت کیفیت استدلال و فارسی‌نویسی
GEMINI_PRIMARY_MODEL=gemini-3.8-flash

# مدل جایگزین سبک (اختیاری، فقط در صورت اتمام سهمیه مدل اصلی)
GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite
GEMINI_ENABLE_FALLBACK=false

# مسیر ذخیره پایدار وضعیت و آمارهای کلیدها در SQLite
GEMINI_KEY_DB_PATH=data/gemini_keys.db
```
سیستم به صورت خودکار تمام متغیرهای منطبق بر `GEMINI_KEY_\d+` و همچنین کلید تکی سنتی `GEMINI_API_KEY` را پایش کرده و به استخر تزریق می‌کند.

### ۵.۴. پایداری وضعیت کلیدها در دیسک (SQLite Adapter)
وضعیت کلیدها و آمار تاخیر و سوختن کلیدها (مانند `API_KEY_INVALID` یا `PERMISSION_DENIED`) در مسیر زیر پایدارسازی می‌شود:
- مسیر پیش‌فرض: `data/gemini_keys.db`
- این فایل مانع از آن می‌شود که پس از هر بار ری‌استارت ربات، کلیدهای نامعتبر مجدداً فراخوانی شوند.

---

## ۶. اجرای مایگریشن‌های Alembic

برای ساخت جداول، ایندکس‌ها و ریویژن اولیه در پایگاه داده واقعی:

### روش ۱: با دستور Alembic
```powershell
alembic upgrade head
```

### روش ۲: اجرای اسکریپت راه‌انداز پروژه
```powershell
python -m app.init_db
```

### راستی‌آزمایی وجود ۷ جدول در دیتابیس:
```powershell
& "D:\pgsql\pgsql\bin\psql.exe" -h 127.0.0.1 -U newsbot -d newsbot -c "\dt"
```
خروجی باید شامل جداول زیر باشد:
1. `alembic_version`
2. `article_events`
3. `articles`
4. `daily_reports`
5. `events`
6. `job_logs`
7. `sources`
8. `summaries`

---

## ۶. راستی‌آزمایی سلامت کل سامانه (Verify Script)

یک اسکریپت خودکار در مسیر `scripts/verify.ps1` تعبیه شده است که:
1. ارتباط با PostgreSQL را بررسی می‌کند.
2. آزمون‌های جامع `pytest` را اجرا می‌کند.
3. خطای متادیتای شناخته‌شده `pip check` (ISS-009) را اعتبارسنجی و گزارش می‌کند.
4. کلیه کتابخانه‌های حساس هسته را به صورت همگام ایمپورت و سلامت آنها را تضمین می‌کند.

اجرای اعتبارسنجی:
```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify.ps1
```

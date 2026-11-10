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

## ۵. اجرای مایگریشن‌های Alembic

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

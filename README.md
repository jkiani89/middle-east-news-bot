# Middle East News Bot MVP

ربات جمع‌آوری ساعتی اخبار خاورمیانه، خلاصه‌سازی فارسی با Google Gemini و انتشار در کانال و گفت‌وگوی خصوصی تلگرام.

## پیش‌نیازها
- Windows 10/11 و Python 3.11+
- PostgreSQL نصب‌شده به‌صورت Native
- Gemini API key
- Telegram Bot token، شناسه کانال و شناسه عددی چت خصوصی

## نصب در PowerShell
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

## دیتابیس
در pgAdmin یا psql اجرا کن:
```sql
CREATE USER newsbot WITH PASSWORD 'CHANGE_THIS_PASSWORD';
CREATE DATABASE newsbot OWNER newsbot;
```
سپس `DATABASE_URL` را در `.env` تنظیم کن.

## تلگرام
1. با BotFather ربات بساز و توکن را در `.env` بگذار.
2. ربات را به کانال اضافه و Admin کن.
3. برای پیام خصوصی، ابتدا از حساب خودت به ربات `/start` بفرست.
4. شناسه عددی چت خصوصی را در `TELEGRAM_ADMIN_CHAT_ID` قرار بده.

## اجرا
```powershell
python -m app.init_db
python -m app.main
```

برنامه در شروع یک‌بار RSSها را بررسی می‌کند، سپس هر ۶۰ دقیقه جمع‌آوری و در ساعت تنظیم‌شده گزارش روز قبل را منتشر می‌کند.

## تنظیم ساعت گزارش
در `.env`:
```env
TIMEZONE=Asia/Tehran
DAILY_REPORT_HOUR=8
DAILY_REPORT_MINUTE=0
```
ساعت ۸ صبح فقط مقدار پیش‌فرض است و می‌توانی تغییرش بدهی.

## افزودن منابع
`editorial/sources.json` را ویرایش کن. نسخه اول فقط RSS را پردازش می‌کند. برای منابع بدون RSS باید بعداً استخراج‌کننده جداگانه و سازگار با شرایط دسترسی آن منبع اضافه شود.

## محدودیت‌های نسخه اول
- Gemini به‌تنهایی راستی‌آزمایی مستقل انجام نمی‌دهد.
- در نبود شواهد مستقل، خبر تأییدنشده باقی می‌ماند.
- تطبیق رویدادها در این اسکلت محافظه‌کارانه است؛ ادغام اشتباه خبرها باید در تست‌ها بررسی شود.
- متن کامل منابع را بدون بررسی مجوز بازنشر نکن؛ لینک و انتساب منبع را نگه دار.

## انتشار لحظه‌ای در تلگرام
هر خبری که تحلیل شود، بلافاصله پس از ذخیره در دیتابیس (فقط تیتر و خلاصه فارسی، نه متن خام) به کانال ارسال می‌شود و هم‌زمان در گزارش روزانه نیز باقی می‌ماند.

```env
IMMEDIATE_PUBLISH_ENABLED=true
IMMEDIATE_PUBLISH_ONLY_NEW_EVENTS=true   # رویدادهای ادغام‌شده پیام تکراری نمی‌سازند
IMMEDIATE_PUBLISH_MAX_PER_RUN=10
IMMEDIATE_PUBLISH_DELAY_SECONDS=2
```

`DRY_RUN_DAILY_REPORT=true` هم گزارش روزانه و هم ارسال لحظه‌ای را در حالت آزمایشی نگه می‌دارد و هیچ پیامی به تلگرام نمی‌فرستد.

## تنظیمات پایداری پردازش
```env
AI_MAX_ATTEMPTS=3                  # سقف تلاش مجدد برای هر مقاله ناموفق
AI_MAX_CONSECUTIVE_FAILURES=5      # توقف بچ پس از این تعداد خطای گذرای متوالی
MAX_ARTICLES_PER_BATCH=15          # مقدار پیشنهادی برای اجرای ابری
```

## Deployment

This project is designed to run on **zero-cost infrastructure with no credit card required** ($0/month):
- **Compute & Scheduler:** GitHub Actions Scheduled Cron Runner (Serverless batch processing)
- **Database:** Aiven PostgreSQL 16 Free (1 CPU / 1GB RAM)
- **Backup:** Cloudflare R2 (10GB free tier, zero egress)
- **CI/CD:** GitHub Actions (public repo)

See the full deployment guide: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

For backup and restore: **[docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md)**

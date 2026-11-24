# Middle East News Bot MVP

ربات جمع‌آوری ساعتی اخبار خاورمیانه، خلاصه‌سازی فارسی با Google Gemini و انتشار در کانال و گفت‌وگوی خصوصی تلگرام.

> **هشدار — دو حالت اجرا با زمان‌بندی متفاوت:**
> - **اجرای محلی** (`python -m app.main`): جمع‌آوری و خلاصه‌سازی **هر ۶۰ دقیقه** و گزارش یک‌بار در روز.
> - **اجرای ابری** (GitHub Actions، بخش Deployment پایین): جمع‌آوری هر ۶ ساعت و **دو بولتن ۱۲ساعته** در روز (۰۸:۰۰ و ۲۰:۰۰ تهران) با پنجرهٔ جبرانی ۳ ساعته.
>
> پس «انتشار لحظه‌ای» در ابر یعنی «در نخستین اجرای بعدی»، نه ارسال در همان ثانیه.

## پیش‌نیازها
- Windows 10/11 یا Linux و Python 3.12+ (CI روی 3.12 و 3.15 تست می‌کند)
- PostgreSQL نصب‌شده به‌صورت Native
- Gemini API key
- Telegram Bot token، شناسه کانال و شناسه عددی چت خصوصی

## نصب در PowerShell
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install --no-deps "aiogram==3.22.0"
pip install -r requirements.txt
Copy-Item .env.example .env
```
> **نسخهٔ aiogram و نصب با `--no-deps`:** متادیتای `aiogram 3.22.0` وابستگی‌های `pydantic<2.12` و `aiohttp<3.13` را اعلام می‌کند، ولی این پروژه به `pydantic>=2.14` (الزام `google-genai`) و `aiohttp 3.14.x` برای Python 3.15 نیاز دارد. `pip install aiogram` ساده این دو را downgrade و `google-genai` را می‌شکند؛ به همین دلیل aiogram دقیقاً مثل `Dockerfile` و ورک‌فلوهای CI/کران با `--no-deps` نصب می‌شود و زیروابستگی‌هایش در `requirements.txt` اعلام شده‌اند. برای اجرای تست‌ها `pip install -r requirements-dev.txt` (این فایل خودش `requirements.txt` را شامل می‌شود).

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

برنامه در شروع یک‌بار RSSها را بررسی می‌کند، سپس **هر ۶۰ دقیقه** جمع‌آوری و در ساعت تنظیم‌شده گزارش روز قبل را منتشر می‌کند. (این زمان‌بندی مخصوص اجرای محلی است؛ حالت ابری در بخش Deployment توضیح داده شده و دو نوبت در روز دارد.)

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

## زنجیره مدل‌ها و سرعت پردازش دسته

**۱) زنجیره سه‌لایه Gemini (`623afab`):**
```env
GEMINI_PRIMARY_MODEL=gemini-3.5-flash-lite     # Primary
GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite    # Fallback
GEMINI_TERTIARY_MODEL=gemini-3.8-flash         # Tertiary (last resort)
GEMINI_ENABLE_FALLBACK=true
```
هر درخواست به ترتیب primary ← fallback ← tertiary می‌رود. فقط خطای قابل‌تلاش مجدد (`429` یا `503`) باعث رفتن به مدل بعدی می‌شود؛ نام تکراری از زنجیره حذف می‌شود و با `GEMINI_ENABLE_FALLBACK=false` فقط مدل primary امتحان می‌شود. لایهٔ سوم برای ساعات اوج است که هر دو مدل Lite هم‌زمان `503` می‌دهند؛ بدون آن یک اجرا با خطا متوقف می‌شد. این مقادیر همان پیش‌فرض‌های `.github/workflows/cron.yml` هستند و با متغیرهای مخزن قابل بازنویسی‌اند (جدول کامل: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

**۲) پر شدن رویدادمحور اسلات‌ها (`465755c`):** در `_ConcurrentAnalysis` تلاش بعدی از callback پایان تلاش (`_on_attempt_done`) ارسال می‌شود، نه از یک حلقهٔ سریال؛ بنابراین یک مقالهٔ کند بقیهٔ اسلات‌ها را بیکار نمی‌گذارد. تحویل نتیجه به حلقهٔ مصرف‌کننده سریال و به ترتیب صف می‌ماند، چون تطبیق رویدادها به همین ترتیب وابسته است. سقف‌ها ثابت‌اند: `ai_processing_concurrency=4` و `max_concurrent=5` روتاتور برای محافظت از دیتابیس Aiven.

**۳) بدون اسکرپ لینک‌های Google News (`465755c`):** نشانی‌های `news.google.com/rss/articles/...` و `/articles/...` صفحهٔ واسط/رضایت‌نامه‌اند؛ `is_google_news_wrapper_url()` جلوی `fetch_article_full_text()` و حلقهٔ `fetch_feed` را می‌گیرد و این خبرها فقط از `title` و `summary` فید RSS ساخته می‌شوند — بدون درخواست HTTP و بدون `trafilatura`.

## Deployment

This project is designed to run on **zero-cost infrastructure with no credit card required** ($0/month):
- **Compute & Scheduler:** GitHub Actions Scheduled Cron Runner (Serverless batch processing)
- **Database:** Aiven PostgreSQL 16 Free (1 CPU / 1GB RAM)
- **Backup:** Backblaze B2 (10GB free tier, zero egress) — see `docs/BACKUP_RESTORE.md`
- **CI/CD:** GitHub Actions (public repo)

See the full deployment guide: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

For backup and restore: **[docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md)**

## License

MIT — see [LICENSE](LICENSE).

## Operations notes

- **Scheduling:** two twelve-hour bulletins per day (08:00 and 20:00 Tehran) with a three-hour
  catch-up window, collection every 6 hours. See `docs/DEPLOYMENT.md`.
- **Keep-alive:** `keepalive.yml` commits a monthly heartbeat into `docs/HEARTBEAT.md` so GitHub
  does not disable the scheduled workflows after 60 days of repository inactivity.
- **Backups:** daily dump to Backblaze B2 with 7-day retention. See `docs/BACKUP_RESTORE.md`.

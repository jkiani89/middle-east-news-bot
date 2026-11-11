# سند برنامه فنی و معماری فاز F (تحقیق و گذار از SQLite به PostgreSQL)

> **وضعیت سند:** تدوین‌شده در فاز F-0 (فاز تحقیق فقط‌خواندنی)  
> **مرجع انطباق:** بند ۳ سند راهنما و قوانین R-02 و R-05 پروتکل حاکمیتی AGENT_PROTOCOL  
> **هدف:** حذف دیتابیس دوم (`data/gemini_keys.db`) و یکپارچه‌سازی پایداری وضعیت چرخش کلیدهای Gemini در PostgreSQL بومی سامانه.

---

## ۱. پاسخ‌های تحلیلی به پرسش‌های کلیدی (Q1 تا Q5)

### Q1: رابط DBAdapter در کتابخانه `gemini-api-rotator`
- **ساختار اینترفیس:** طبق بررسی فایل‌های منبع کتابخانه در مسیر `.venv/Lib/site-packages/gemini_rotator/adapters/base.py`، کلاس `DBAdapter` یک پروتکل پایتونی (`@runtime_checkable Protocol`) است که ۷ متد همگام (Synchronous) را الزامی می‌کند: `get_suspended_keys`, `mark_key_suspended`, `unmark_key_suspended`, `record_request`, `get_stats`, `get_all_stats`, `reset_stats`.
- **فقدان PostgresAdapter آماده:** در نسخه 2.0.0 این کتابخانه، هیچ پیاده‌سازی رسمی برای PostgreSQL وجود ندارد (`adapters/` صرفاً شامل `base.py`، `memory.py`، `mongodb.py` و `sqlite.py` است).
- **الگوی MongoDB:** ماژول `mongodb.py` وجود دارد که از `pymongo` استفاده می‌کند، اما پیاده‌سازی آن کاملاً سنکرون بوده و برای محیط NoSQL طراحی شده است.

### Q2: وضعیت فعلی در کد پروژه
- **استفاده در `app/ai/provider.py`:** در خط ۱۶۹، کلاس `SQLiteAdapter(db_path_str)` نمونه‌سازی شده و مستقیماً به سازنده `GeminiAPIRotator(..., db=self._db_adapter)` پاس داده می‌شود.
- **ساختار اسکیما در `data/gemini_keys.db`:** این دیتابیس دارای ۳ جدول است:
  ۱. `suspended_keys`: شامل ستون‌های `key TEXT PRIMARY KEY` و `suspended_at REAL`.
  ۲. `key_stats`: شامل ستون‌های `key TEXT PRIMARY KEY`، ۶ شمارنده عددی درخواست‌ها (`total_requests`, `total_success`, `total_rate_limit`, `total_suspended`, `total_transient`, `total_error`)، ۴ فیلد تجمیع لتنسی (`latency_sum`, `latency_min`, `latency_max`, `latency_count`) و زمان آخرین استفاده `last_used_at REAL`.
  ۳. `request_log`: لاگ تفصیلی تک‌تک درخواست‌ها به همراه اندیس‌های `idx_log_key` و `idx_log_ts`.
- **متد `close()`:** متد `close()` در `GeminiProvider` (خط ~۲۰۰) تعریف شده و در چرخه حیات سامانه در `main.py` برای آزادسازی منابع فراخوانی می‌شود؛ در صورت داشتن متد `close` روی آداپتور، به درستی فراخوانی می‌گردد.

### Q3: گزینه‌های جایگزینی
- **گزینه A (آداپتور آماده Postgres):** منتفی است (در کتابخانه وجود ندارد).
- **گزینه B (آداپتور سفارشی PostgreSQL با SQLAlchemy):** به دلیل اینکه پروتکل `DBAdapter` از متدهای همگام (`def`) استفاده می‌کند ولی درایور پروژه ما `asyncpg` ناهمگام است، ساخت یک آداپتور هیبریدی (In-Memory Cache با همگام‌سازی ناهمگام با دیتابیس) بهترین و سازگارترین راه‌حل بدون نیاز به هیچ وابستگی جدید است.
- **گزینه C (استفاده از MongoDB):** تحمیل دیتابیس جدید و پکیج `pymongo` که کاملاً خلاف اصل سادگی و معماری پروژه است و رد می‌شود.

### Q4: طرح Migration پایگاه داده
- **جدول جدید در PostgreSQL:** افزودن جدول `gemini_key_states` به مدل‌های SQLAlchemy در `app/database/models.py`.
- **مهاجرت با Alembic:** ایجاد یک ریویژن جدید ادامه‌دهنده ریویژن فعلی (`e24bf6e4d3eb`) با قابلیت برگشت‌پذیری کامل (`upgrade` و `downgrade`).
- **عدم تداخل:** این جدول کاملاً ارتوگونال و مجزا از جداول کسب‌وکار (`articles`, `events`, `summaries`) بوده و هیچ اثر منفی بر مهاجرت‌های قبلی ندارد.

### Q5: تأثیر بر معماری سامانه (`ARCHITECTURE.md`)
- **حذف دیتابیس دوم:** این تغییر با حذف فایل SQLite، سامانه را به یک «تک‌دیتابیس متمرکز و ایزوله» (PostgreSQL Native) تبدیل می‌کند که تطابق کامل با بند ۳ سند راهنما دارد.
- **به‌روزرسانی سند:** در `docs/ARCHITECTURE.md` بخش ۳.۱، عبارت مربوط به ذخیره در SQLite با PostgreSQL جایگزین می‌گردد.

---

## ۲. جدول مقایسه گزینه‌های فنی

| مؤلفه ارزیابی | گزینه A (کتابخانه‌ای) | گزینه B (سفارشی PostgreSQL) | گزینه C (آداپتور MongoDB) |
|---|---|---|---|
| **وضعیت دسترسی** | ❌ در نسخه 2.0.0 وجود ندارد | 🟢 قابل پیاده‌سازی در `app/ai/` | 🟡 موجود در پکیج (`mongodb.py`) |
| **وابستگی پکیج جدید** | ندارد | 🟢 **صفر** (استفاده از SQLAlchemy + asyncpg) | 🔴 نیازمند نصب `pymongo` |
| **زیرساخت / سرویس جدید** | ندارد | 🟢 **صفر** (استفاده از PostgreSQL جاری) | 🔴 نیازمند راه‌اندازی دیتابیس MongoDB |
| **سازگاری با استک پروژه** | نامشخص | 🟢 **۱۰۰٪ همخوان** با ORM و مدل‌های سامانه | 🔴 ضعیف و ناهماهنگ با معماری سامانه |
| **پیچیدگی پیاده‌سازی** | صفر | 🟡 متوسط (مدیریت انطباق sync protocol با async DB) | 🔴 بالا (راه‌اندازی سرور، کانکشن و نگهداری) |
| **نتیجه ارزیابی** | **غیرقابل اجرا** | **گزینه منتخب و توصیه‌شده (RECOMMENDED)** | **مردود** |

---

## ۳. توصیه نهایی معماری

اجرای **گزینه B** با استفاده از الگوی **In-Memory Cache with PostgreSQL Async Persistence**:
1. چرخش کلیدها در کسری از میلی‌ثانیه انجام می‌شود؛ بنابراین فراخوانی I/O در هر درخواست جهت بررسی تعلیق کلید نادرست است. آداپتور جدید وضعیت کلیدهای معلق و آمارها را در حافظه نگهداری کرده و اینترفیس سنکرون `DBAdapter` را تأمین می‌کند.
2. با اجرای هر فراخوانی یا در نقاط عطف خط لوله (مانند ورود کلید به حالت Cooldown)، وضعیت به صورت ناهمگام در جدول `gemini_key_states` در دیتابیس PostgreSQL ذخیره و پایدار (Persist) می‌شود.
3. با ریستارت شدن سرویس، آداپتور در زمان راه‌اندازی وضعیت کلیدها را از PostgreSQL بارگذاری کرده و پیوستگی آمار و تعلیق‌ها حفظ می‌گردد.

---

## ۴. مشخصات فنی پیاده‌سازی (طرح مدل و امضای متدها)

### ۴.۱. مدل جدول PostgreSQL (`app/database/models.py`)
```python
class GeminiKeyState(Base):
    """Persistent storage for Gemini API key status and rotation metrics."""
    __tablename__ = "gemini_key_states"

    key = Column(String(256), primary_key=True)
    is_suspended = Column(Boolean, nullable=False, default=False)
    suspended_at = Column(DateTime(timezone=True), nullable=True)
    total_requests = Column(Integer, nullable=False, default=0)
    total_success = Column(Integer, nullable=False, default=0)
    total_rate_limit = Column(Integer, nullable=False, default=0)
    total_suspended = Column(Integer, nullable=False, default=0)
    total_transient = Column(Integer, nullable=False, default=0)
    total_error = Column(Integer, nullable=False, default=0)
    latency_sum = Column(Float, nullable=False, default=0.0)
    latency_min = Column(Float, nullable=True)
    latency_max = Column(Float, nullable=True)
    latency_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

### ۴.۲. امضای کلاس آداپتور (`app/ai/postgres_adapter.py`)
```python
class PostgresKeyAdapter:
    """PostgreSQL-backed adapter satisfying gemini_rotator.adapters.base.DBAdapter protocol."""

    def __init__(self, session_factory=None) -> None:
        ...

    # پیاده‌سازی متدهای پروتکل DBAdapter (همگام)
    def get_suspended_keys(self) -> set[str]: ...
    def mark_key_suspended(self, key: str) -> None: ...
    def unmark_key_suspended(self, key: str) -> None: ...
    def record_request(
        self,
        key: str,
        event: str,
        latency: float | None = None,
        model: str | None = None,
    ) -> None: ...
    def get_stats(self, key: str) -> KeyStats: ...
    def get_all_stats(self) -> list[KeyStats]: ...
    def reset_stats(self, key: str | None = None) -> None: ...

    # متدهای ناهمگام همگام‌سازی با PostgreSQL
    async def load_from_db(self, session: AsyncSession) -> None: ...
    async def persist_to_db(self, session: AsyncSession) -> None: ...
    def close(self) -> None: ...
```

---

## ۵. متن پیشنهادی برای به‌روزرسانی `docs/ARCHITECTURE.md`

در بخش ۳.۱ سند معماری (بند مربوط به `GeminiProvider`):

> **متن قبلی:**  
> «ارائه‌دهنده جمنای (`GeminiProvider`): پیاده‌سازی رسمی با استفاده از `gemini-api-rotator`، مدیریت استخر چندکلیدی در سطح چند پروژه Google Cloud، پایداری وضعیت کلیدهای تعلیق‌شده در SQLite (`data/gemini_keys.db`)، و مکانیزم فال‌بک خودکار...»

> **متن اصلاحی جدید:**  
> «ارائه‌دهنده جمنای (`GeminiProvider`): پیاده‌سازی رسمی با استفاده از `gemini-api-rotator`، مدیریت استخر چندکلیدی در سطح چند پروژه Google Cloud، پایداری متمرکز وضعیت کلیدهای تعلیق‌شده و شاخص‌های مصرف در جدول `gemini_key_states` در پایگاه داده اصلی PostgreSQL (حذف قطعی پایگاه داده دوم SQLite و تمرکز ذخیره‌سازی)، و مکانیزم فال‌بک خودکار...»

---

## ۶. پیش‌بینی فایل‌های تغییریافته در فاز F-1 (پیاده‌سازی)

۱. `app/database/models.py` (تعریف مدل `GeminiKeyState`)
۲. `alembic/versions/xxxx_add_gemini_key_states.py` (مایگریشن جدید ایجاد جدول)
۳. `app/ai/postgres_adapter.py` (پیاده‌سازی آداپتور جدید)
۴. `app/ai/provider.py` (تغییر آداپتور پیش‌فرض از SQLite به Postgres)
۵. `app/config.py` (حذف پارامتر دیتابیس فایل SQLite)
۶. `tests/test_postgres_adapter.py` (آزمون‌های واحد پوشش آداپتور جدید)
۷. `tests/test_gemini_rotator.py` و `tests/test_ai.py` (به‌روزرسانی آزمون‌ها)
۸. `docs/ARCHITECTURE.md` (به‌روزرسانی بند ۳.۱)
۹. `docs/DEVELOPMENT_LOG.md` (ثبت گزارش فاز F)
۱۰. حذف امن فایل محلی `data/gemini_keys.db`

---

## ۷. معیارهای پذیرش پیشنهادی برای فاز F-1

| معیار پذیرش | هدف |
|---|---|
| مایگریشن Alembic | ایجاد جدول `gemini_key_states` در PostgreSQL و قابلیت Downgrade کامل |
| انطباق پروتکل | اعتبارسنجی `isinstance(adapter, DBAdapter) == True` |
| پایداری تعلیق و آمار | بازیابی تعلیق و آمار پس از ریستارت نمونه Provider از PostgreSQL |
| عدم تداخل با آزمون‌ها | پاس شدن تمامی ۱۴۴ تست قبلی + تست‌های جدید آداپتور با Mock دیتابیس |
| حذف SQLite | عدم ایجاد یا وابستگی به هیچ فایلی در `data/gemini_keys.db` |
| پوشش کد | پوشش تست بالای ۹۰٪ برای فایل جدید `app/ai/postgres_adapter.py` |

---

## ۸. تصمیم نهایی معماری sync/async و ماتریس تصمیمات تصویب‌شده (Approved Decisions)

### ماتریس تصمیم‌گیری همگام/ناهمگام (Sync vs Async):
در پیاده‌سازی فاز F-1، دو رویکرد برای ارتباط با PostgreSQL بررسی و تحلیل شد:
- **رویکرد A (هیبریدی Memory Cache + ناهمگام asyncpg):** وضعیت در حافظه کش شده و با عملیات async در دیتابیس ثبت شود. (نقاط ضعف: ریسک Race Condition بین چند ورکر/پروسس، از دست رفتن داده‌های حافظه در کرش ناگهانی، پیچیدگی چرخه حیات و Flush دوره‌ای).
- **رویکرد B (آداپتور سنکرون با درایور استاندارد psycopg3):** پیاده‌سازی همگام پروتکل `DBAdapter` با استفاده از `psycopg[binary]>=3.2` که مستقیماً با پایگاه داده PostgreSQL ارتباط برقرار می‌کند. چون تراکنش‌های rotator بسیار سبک، سریع و در حد زیر میلی‌ثانیه (sub-millisecond) هستند (با توجه به ایندکس‌های روی کلیدها) و صرفاً هنگام چرخش یا ثبت نتیجه صدا زده می‌شوند، هیچ بلاک محسوسی در event loop ایجاد نمی‌کند و انطباق ۱۰۰٪ با پروتکل کتابخانه `gemini-api-rotator` بدون نیاز به کش پیچیده در حافظه فراهم می‌آورد. **(گزینه منتخب و تصویب‌شده)**.

### جدول ۵ تصمیم کلیدی تصویب‌شده در فاز F-1:

| ردیف | موضوع تصمیم | رویکرد مصوب | مبنا و دلیل تصمیم |
|---|---|---|---|
| ۱ | پکیج درایور دیتابیس | استفاده از `psycopg[binary]>=3.2,<4` | تطابق مستقیم با اینترفیس سنکرون `DBAdapter` بدون بلاک غیرضروری یا وابستگی‌های سنگین |
| ۲ | اسکیما و مایگریشن | نگاشت ۳ جدول بومی (`gemini_suspended_keys`, `gemini_key_stats`, `gemini_request_log`) | تطابق کامل با اسکیمای نیتیو `gemini-api-rotator` و تفکیک با فیلتر `include_object` در Alembic |
| ۳ | رفتار در محیط تست | استفاده از `MemoryAdapter` در محیط تست (`APP_ENV=test`) | تست‌های سریع، ایزوله و بدون نیاز به اتصالات خارجی برای آزمون‌های پیشین |
| ۴ | پاک‌سازی لاگ‌ها (Retention) | جاب روزانه ساعت ۰۳:۰۰ با متغیر `GEMINI_LOG_RETENTION_DAYS=30` | جلوگیری از رشد نامحدود جدول `gemini_request_log` بدون بار اضافه |
| ۵ | دیتابیس تست ایزوله | ایجاد دیتابیس مستقل `newsbot_test` با سوئیچ در `conftest.py` | اجرای تست‌های یکپارچگی روی دیتابیس واقعی PostgreSQL بدون تداخل با دیتابیس توسعه/عملیات |

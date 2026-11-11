# فاز F-1 — جایگزینی SQLite با PostgreSQL

## مقدمه
پس از F-0 مشخص شد `gemini-api-rotator` یک `PostgresAdapter` آماده ندارد و `DBAdapter` یک پروتکل همگام (sync) با ۷ متد است. این فاز آداپتور سفارشی میسازد و دیتابیس دوم (`data/gemini_keys.db`) را حذف میکند.

## تصمیمهای تأییدشده

| # | تصمیم | مقدار |
|---|---|---|
| ۱ | محل جدولها | همان دیتابیس `newsbot` |
| ۲ | Retention `gemini_request_log` | ۳۰ روز، پاکسازی روزانه |
| ۳ | داده SQLite موجود | Archive به `data/backup/`، شروع از صفر |
| ۴ | همگام/ناهمگام | گزینه B (Sync psycopg) |
| ۵ | کتابخانه جدید | `psycopg[binary]>=3.2,<4` |

**تصمیم ۵ نیازمند تأیید صریح (R-05).**

**درباره تصمیم ۴:** گزینه A (Hybrid Memory+Flush) ~۳۰۰ خط و پیچیدگی lifecycle دارد. گزینه B با `psycopg.connect()` sync، هر کوئری < ۱ms (چون ایندکس دارد)، عملاً event loop را بلاک نمیکند. برای ~۵۰ call/hour، انتخاب بهتر.

## دامنه تغییرات

**تغییر میکند:**
- `requirements.txt` (افزودن psycopg)
- `app/ai/postgres_adapter.py` (جدید ~۱۵۰ خط)
- `app/ai/provider.py` (SQLite → Postgres)
- `app/scheduler/jobs.py` (cleanup job جدید)
- `alembic/versions/xxxx_add_gemini_rotator_tables.py` (جدید)
- `app/main.py` (در صورت نیاز به lifecycle)
- `docs/ARCHITECTURE.md` بخش ۳.۱ (R-07 تأیید)
- `docs/PHASE_F_PLAN.md` (اصلاح ۳ خطا)
- `docs/KNOWN_ISSUES.md` (ISS-028 → 🟢)
- `docs/DEVELOPMENT_LOG.md` (ورودی فاز)
- `tests/test_postgres_adapter.py` (جدید ~۱۵ تست)
- `.env.example` (gemini_log_retention_days — R-05 تأیید)
- `data/gemini_keys.db` → `data/backup/gemini_keys.db.2026-11-11.bak`

**تغییر نمیکند:**
`app/processors/*`, `app/ai/summarizer.py`, `app/collectors/*`, `app/telegram/*`, `app/reports/*`, `app/database/models.py` (جدولها از Alembic نه ORM).

---

## تغییر ۱: Migration Alembic

فایل جدید `alembic/versions/xxxx_add_gemini_rotator_tables.py`

سه جدول (بدون ORM):

- `gemini_suspended_keys`: `key VARCHAR(200) PK`, `suspended_at DOUBLE PRECISION NOT NULL`
- `gemini_key_stats`: `key VARCHAR(200) PK` + ۱۰ ستون stats (total_requests، total_success، total_rate_limit، total_suspended، total_transient، total_error، latency_sum، latency_min، latency_max، latency_count، last_used_at)
- `gemini_request_log`: `id BIGSERIAL PK`, `key VARCHAR(200) NOT NULL`, `event VARCHAR(50) NOT NULL`, `latency_ms DOUBLE PRECISION`, `model VARCHAR(100)`, `ts DOUBLE PRECISION NOT NULL` + ایندکس روی `key` و `ts`

`down_revision = 'e24bf6e4d3eb'`. downgrade: DROP هر سه جدول.

---

## تغییر ۲: `app/ai/postgres_adapter.py` (جدید)

```python
"""PostgreSQL adapter for gemini-api-rotator (sync DBAdapter)."""
import logging
import time

import psycopg
from psycopg.rows import dict_row
from gemini_rotator.adapters import DBAdapter
from gemini_rotator.models import KeyStats

logger = logging.getLogger(__name__)


class PostgresAdapter(DBAdapter):
    """Sync DBAdapter backed by PostgreSQL via psycopg3."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None
        self._connect()

    def _connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._dsn, autocommit=True, row_factory=dict_row,
            )

    def _ensure(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._connect()
        return self._conn

    def get_suspended_keys(self) -> dict[str, float]:
        cur = self._ensure().execute(
            "SELECT key, suspended_at FROM gemini_suspended_keys",
        )
        return {r["key"]: r["suspended_at"] for r in cur.fetchall()}

    def mark_key_suspended(self, key: str, suspended_at: float) -> None:
        self._ensure().execute(
            "INSERT INTO gemini_suspended_keys(key, suspended_at) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET suspended_at = EXCLUDED.suspended_at",
            (key, suspended_at),
        )

    def unmark_key_suspended(self, key: str) -> None:
        self._ensure().execute(
            "DELETE FROM gemini_suspended_keys WHERE key = %s", (key,),
        )

    def record_request(self, key: str, event: str,
                       latency_ms: float | None = None,
                       model: str | None = None) -> None:
        self._ensure().execute(
            "INSERT INTO gemini_request_log(key, event, latency_ms, model, ts) "
            "VALUES (%s, %s, %s, %s, %s)",
            (key, event, latency_ms, model, time.time()),
        )
        self._update_stats(key, event, latency_ms)

    # get_stats, get_all_stats, reset_stats, _update_stats → پیادهسازی کامل
    # بر اساس KeyStats از gemini_rotator.models

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None
```

نکات:
- DSN از `settings.database_url` با حذف `+asyncpg`
- `autocommit=True`
- `close()` ایدمپوتنت
- `_ensure()` در صورت قطع، خودش reconnect میکند

---

## تغییر ۳: `app/ai/provider.py`

- ایمپورت: حذف `SQLiteAdapter`، افزودن `PostgresAdapter` از `app.ai.postgres_adapter`
- `self._db_adapter = PostgresAdapter(self._convert_dsn(settings.database_url))`
- متد جدید `_convert_dsn(dsn)` که `postgresql+asyncpg://` را به `postgresql://` تبدیل میکند
- `close()` حفظ میشود (خط ~۲۰۰ — قبلاً تعریف شده، تصحیح F-0)

---

## تغییر ۴: Cleanup job

در `app/scheduler/jobs.py`:

```python
async def cleanup_gemini_request_log():
    """Delete gemini_request_log rows older than retention days."""
    from app.ai.postgres_adapter import PostgresAdapter
    from app.config import settings
    adapter = PostgresAdapter(_convert_dsn(settings.database_url))
    try:
        cutoff = time.time() - settings.gemini_log_retention_days * 86400
        cur = adapter._ensure().execute(
            "DELETE FROM gemini_request_log WHERE ts < %s", (cutoff,),
        )
        logger.info("Cleaned %d rows from gemini_request_log", cur.rowcount)
    finally:
        adapter.close()
```

زمانبندی: `0 3 * * *` (روزانه ۳ بامداد Tehran).

`settings.gemini_log_retention_days = 30` در `.env.example` — **R-05 تأیید.**

---

## تغییر ۵: `ARCHITECTURE.md` بخش ۳.۱ (R-07 تأیید)

جایگزینی جمله:

> ... پایداری وضعیت کلیدهای تعلیقشده در SQLite (`data/gemini_keys.db`) ...

با:

> ... پایداری وضعیت کلیدهای تعلیقشده و آمار درخواستها در PostgreSQL (جداول `gemini_suspended_keys`، `gemini_key_stats`، `gemini_request_log`). Retention: حذف خودکار ردیفهای `gemini_request_log` قدیمیتر از ۳۰ روز با job روزانه. آداپتور `PostgresAdapter` (`app/ai/postgres_adapter.py`) پیادهسازی sync از پروتکل `DBAdapter` با `psycopg3` است که بدون بلاک محسوس event loop اجرا میشود.

---

## تغییر ۶: Archive SQLite

```powershell
New-Item data\backup -ItemType Directory -ErrorAction SilentlyContinue
Move-Item data\gemini_keys.db data\backup\gemini_keys.db.2026-11-11.bak
```

`data/` در `.gitignore` هست — backup هم شامل میشود.

---

## تغییر ۷: تستها

فایل جدید `tests/test_postgres_adapter.py` (~۱۵ تست):

1. `test_adapter_implements_db_adapter` (isinstance)
2. `test_mark_and_get_suspended_keys`
3. `test_unmark_key`
4. `test_record_request_writes_to_log`
5. `test_record_request_updates_stats`
6. `test_get_stats_returns_none_for_unknown`
7. `test_get_all_stats`
8. `test_reset_stats_clears`
9. `test_close_is_idempotent`
10. `test_dsn_conversion_from_asyncpg`
11-15. edge cases (concurrent marks، empty، large log، null model، closed reconnect)

تستها با **PostgreSQL واقعی** (`newsbot_test`)، نه mock.

`conftest.py` fixture جدید:
```python
@pytest.fixture
async def postgres_adapter(test_db_dsn):
    adapter = PostgresAdapter(test_db_dsn)
    adapter.reset_stats()
    yield adapter
    adapter.close()
```

**نیاز به `test_db_dsn` fixture** — اگر نیست، ساخته شود.

---

## اصلاح `PHASE_F_PLAN.md`

سه تغییر:
1. حذف ادعای غلط Q2 («`close()` تعریف نشده») → «`close()` در `GeminiProvider` خط ~۲۰۰ تعریف شده؛ بررسی فراخوانی در F-1»
2. افزودن بخش «تصمیم نهایی sync/async» با توضیح A vs B و انتخاب B
3. افزودن جدول ۵ تصمیم تأییدشده

---

## معیار پذیرش

| مورد | هدف |
|---|---|
| `alembic upgrade head` روی DB | ۳ جدول + ۲ ایندکس |
| `alembic revision --autogenerate` بعد | خالی |
| `isinstance(adapter, DBAdapter)` | True |
| کل تستها | ۱۴۴ + ~۱۵ = ۱۵۹ pass |
| پوشش `postgres_adapter.py` | ≥۹۰٪ |
| `grep -rn "SQLiteAdapter" app/` | خالی |
| `data/gemini_keys.db` | در `data/backup/` |
| `psycopg[binary]>=3.2,<4` | در `requirements.txt` |
| `ruff check` روی ۴ فایل | All checks passed |
| ARCHITECTURE بخش ۳.۱ | بهروز |
| ISS-028 | 🟢 |

---

## شواهد گزارش پایانی (۱۰ بند)

1. `alembic upgrade head` + `\dt gemini_*`
2. `alembic revision --autogenerate` (خالی)
3. `pytest tests/test_postgres_adapter.py -v`
4. `pytest -q --tb=no` (۱۵۹ pass)
5. `pytest --cov=app.ai.postgres_adapter --cov-report=term-missing`
6. `ruff check` روی ۴ فایل
7. `grep -rn "SQLiteAdapter" app/` (خالی)
8. `git diff` روی `ARCHITECTURE.md`، `PHASE_F_PLAN.md`، `KNOWN_ISSUES.md`، `DEVELOPMENT_LOG.md`
9. `ls data/backup/`
10. `git status`

---

## Commit structure (۶ کامیت اتمیک)

1. `feat(ai): add PostgresAdapter implementing DBAdapter via psycopg3` — adapter + requirements
2. `feat(migrations): add gemini rotator tables` — migration فایل
3. `refactor(ai): replace SQLiteAdapter with PostgresAdapter` — provider.py
4. `feat(scheduler): daily cleanup of gemini_request_log` — jobs.py + .env.example (R-05)
5. `test(ai): add PostgresAdapter integration tests` — tests + conftest
6. `docs(f1): update docs and archive SQLite` — ۴ سند + data/backup

---

## ریسکها

| ریسک | Mitigation |
|---|---|
| psycopg کند در event loop | ایندکسها + کوئریهای < ۱ms |
| psycopg با Python 3.15 سازگار نباشد | تست در قدم اول؛ fail → به گزینه A برگردیم |
| از دست رفتن داده SQLite | Archive قبل از حذف |
| Migration در prod | `downgrade` test در `newsbot_migration_test` |

---

## نکات R-10 (توقف و پرسش)

اگر به این موارد برخوردی، توقف کن:

1. `psycopg[binary]` روی Python 3.15 نصب نمیشود → گزارش بده، به A برمیگردیم
2. `app/main.py` پیدا نمیشود یا `close()` جای مناسبی ندارد
3. `test_db_dsn` fixture وجود ندارد و ساختنش پیچیده است
4. `ARCHITECTURE.md` بخش ۳.۱ ساختار متفاوت دارد
5. Migration `down_revision` با `e24bf6e4d3eb` همخوانی ندارد
6. تستهای موجود (۱۴۴) بعد از تغییر provider.py fail میشوند

---

**این پایان سند F-1 است.**

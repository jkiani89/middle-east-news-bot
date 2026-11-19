# ممیزی امنیتی G5 — سند مرجع

> **هدف این سند:** هیچ‌کس در آینده مجبور نشود ممیزی G5 را از ابتدا تکرار کند. هر تصمیم، هر خطا،
> علت ریشه‌ای، اصلاح، تغییر معیار، شاهد و هش در همین‌جا ثبت شده است.
>
> آخرین به‌روزرسانی: ۲۰۲۶-۱۱-۱۹ · نسخهٔ ممیزی: **fix5 (v4 surgical)** · وضعیت: **ابزار نهایی آماده؛ اصلاح production در انتظار تأیید و اجرا (بند ۹/۱۳)**

---

## ۱. دامنه و محیط

| مورد | مقدار |
|---|---|
| هدف | PostgreSQL 18.6 روی Aiven (DigitalOcean) |
| هاست / پورت | `pg-2f0e4f1c-newsbot-db.e.aivencloud.com` / `27362` |
| دیتابیس | `defaultdb` · IP: `167.71.69.231` |
| نقش ممیز | `audit_ro` (LOGIN، بدون SUPERUSER/CREATEDB/CREATEROLE/BYPASSRLS) |
| تراکنش | `BEGIN` + `SET TRANSACTION READ ONLY` + `ROLLBACK`؛ `xid_assigned = null` در همهٔ اجراها |
| مسیر اجرا | فقط لپ‌تاپ ویندوز کاربر (شبکهٔ sandbox به Aiven مسدود است) |
| بستهٔ ممیزی | `g5_handoff/g5_fix3.zip` (هش در بند ۷) |

ممیزی **فقط کاتالوگ** است: `audit_ro` هیچ مجوز SELECT روی جداول ندارد (`1D` نشان می‌دهد
`sel=false` برای هر ۱۳ جدول) و همین برای اثبات وضعیت دسترسی کافی است.

---

## ۲. تاریخ اجراها و علت ریشه‌ای خطاها

| دور | نسخه | نتیجه | خطا / یافته | علت ریشه‌ای | اصلاح |
|---|---|---|---|---|---|
| ۱ | `g5_probe.py` | ERROR | `column prs.prsowner does not exist` | `pg_ts_parser` هیچ ستون owner ندارد (parserها مالک per-object ندارند) | `fix1`: حذف شاخهٔ `pg_ts_parser` از کوئری `1E` |
| ۲ | `g5_probe_fix1.py` | ERROR | `column tmpl.tmplowner does not exist` | `pg_ts_template` هم ستون owner ندارد | `fix2`: حذف شاخهٔ `pg_ts_template` |
| ۳ | `fix2` (اجرای محلی PG 16.2) | ERROR | `column "usage" does not exist` در `ORDER BY (usage OR upd)` | PostgreSQL اجازهٔ استفادهٔ alias در expression داخل `ORDER BY` نمی‌دهد | `fix2`: تکرار دو فراخوانی `has_sequence_privilege` در `ORDER BY` |
| ۴ | `g5_probe_fix2.py` | **اجرای کامل، `verdict=FAIL`** | `3-4` و `3-7` FAIL | — (این‌ها خودِ یافته‌ها هستند) | `fix3` |

اجرای دور ۴ در `2026-11-19T16:25:40Z` انجام شد و **هیچ خطای ابزاری نداشت**:
هر ۱۵ بخش تولید شد، `error_message` خالی بود، `exit=1` فقط پیامد `verdict=FAIL` بود.
خروجی `psql` (`audit_out.txt`، ۵۸۷۲ بایت) هم مستقلاً همان شواهد را تأیید کرد.

---

## ۳. معیارها و منطق PASS/FAIL

همهٔ کوئری‌ها در `g5_probe_fix3.py` (دیکشنری `QUERIES`) و منطق در تابع `evaluate()` هستند.
هر کوئری قبل از اجرا از `assert_safe()` می‌گذرد: فقط `SELECT`/`WITH`، بدون `;`، بدون توکن نوشتنی.

| بخش | معیار | قاعدهٔ PASS | شاهد دور ۴ |
|---|---|---|---|
| `identity` | هویت و TLS | `current_user = session_user = audit_ro`، `ssl = true`، آدرس غیرلوکال | `18.6`، `ssl:true`، `167.71.69.231/32` |
| `read_only_txn` | تراکنش فقط‌خواندنی | `transaction_read_only = on` و `xid_assigned IS NULL` | `on` / `null` |
| `1A` | نقش‌های عضو با INHERIT خودکار | هیچ سطر | `[]` |
| `1B` | نقش‌های قابل SET ROLE | هیچ سطر | `[]` |
| `1C` | نقش‌های قابل سوءاستفاده (privileged) | هیچ سطر | `[]` |
| `1D` | نوشتن روی جداول (مستقیم/عضویت/SET ROLE) | هیچ `any_write=true` | ۱۳ جدول، همه `false` |
| `1E` | مالکیت اشیای غیرسیستمی توسط نقش‌های در دسترس | هیچ سطر | `[]` |
| `2` | Large Objects | اگر `total_large_objects = 0` → `NOT_APPLICABLE` | `NOT_APPLICABLE` |
| `3-4` | مجوزهای سطح دیتابیس | `CREATE` و `TEMPORARY` هر دو `false` | **FAIL** baseline (`can_temp=true`) → **PASS** بعد از fix production (`can_temp=false`) |
| `3-4b` | CREATE روی schemaها | هیچ `can_create=true` | PASS |
| `3-6` | سکوئنس‌ها | هیچ `usage=true` یا `upd=true` | PASS (۱۰ سکوئنس) |
| `3-7` | grantهای PUBLIC روی روابط **غیرسیستمی** | هیچ سطر | PASS (`[]`) |
| `3-7b` | grantهای PUBLIC روی schema/db/routine | هیچ سطر | PASS |
| `3-8` | توابع SECURITY DEFINER قابل اجرا بیرون از schemaهای سیستمی | شمارش `= 0` | PASS (`0`) |

`overall()`: هر `FAIL` ⇒ `FAIL`؛ هر `UNKNOWN` ⇒ `UNKNOWN`؛ در غیر این صورت `PASS`.
`NOT_APPLICABLE` عمداً حکم را FAIL نمی‌کند (دامنهٔ معیار خالی است، نه نقض).

---

## ۴. یافته‌ها

### ۴.۱ یافتهٔ واقعی — `3-4`: نقش ممیز `TEMPORARY` دارد

- **شاهد:** `can_connect=true, can_temp=true, can_create_schema=false`.
- **منبع، دقیق:** `datacl = {=Tc/avnadmin, avnadmin=CTc/avnadmin, _avnadmin_monitor=Tc/avnadmin}`.
  entry با grantee خالی یعنی `PUBLIC`. `audit_ro` هیچ entry مستقیمی در `datacl` ندارد و
  `role membership` آن صفر است، پس TEMP فقط از `PUBLIC` می‌آید. این همان ACL پیش‌فرض
  PostgreSQL برای دیتابیس است (هم‌شکل با `template1`)، نه اعطای عمدی پروژه.
- **چرا نقص است:** یک «ممیز فقط‌خواندنی» نباید بتواند شیء موقت بسازد (نوشتن در schema موقت،
  مصرف دیسک، سطح DoS). شدت: متوسط-پایین؛ نقض کمینه‌دسترسی، نه نشت داده.
- **چرا با تغییر کد رفع نمی‌شود:** کوئری `has_database_privilege` مجوز **مؤثر** را می‌سنجد و
  مجوز واقعاً وجود دارد. هر تغییری در قاعدهٔ PASS = پنهان‌کردن یافته (نقض R-04/R-09). پس معیار
  دست‌نخورده ماند و اصلاح واقعی به بند ۹ موکول شد.
- **همچنین:** `can_connect` ثبت می‌شود ولی در شرط PASS استفاده نمی‌شود (asymmetry بی‌اثر).

### ۴.۲ false positive — `3-7`: `pg_catalog.pg_settings | UPDATE`

- **شاهد دور ۴:** تنها یک سطر: `pg_catalog.pg_settings`, `relkind=b'v'`, `UPDATE`, `is_grantable=false`.
- **ACL واقعی است، ولی متعلق به نقش نیست:** `src/backend/catalog/system_views.sql` در تمام
  شاخه‌های `REL_14` تا `REL_18_6` و `master` این خط را دارد:
  `GRANT SELECT, UPDATE ON pg_settings TO PUBLIC;`
  پس این سطر روی **هر** خوشهٔ استاندارد PostgreSQL وجود دارد و برای **هر** نقشی یکسان است؛
  هیچ اطلاعاتی دربارهٔ `audit_ro` حمل نمی‌کند.
- **اثر امنیتی:** `UPDATE` روی این ویو با قاعدهٔ `pg_settings_u` به
  `set_config(old.name, new.setting,'f')` نگاشت می‌شود و `set_config` مجوز هر پارامتر را جدا
  بررسی می‌کند؛ پارامترهای سوپریوزری برای نقش غیرسوپریوزر همچنان قابل تنظیم نیستند.
  `is_grantable=false` هم یعنی قابل واگذاری نیست. ⇒ escalation صفر.
- **چرا رفع با دیتابیس ممکن نیست:** مالک کاتالوگ‌های سیستمی سوپریوزر bootstrap است و
  `avnadmin` و `audit_ro` هر دو `rolsuper=f` هستند؛ Aiven سوپریوزر نمی‌دهد.
- **نتیجه:** نقص کوئری (`3-7` هیچ فیلتر schema نداشت، برخلاف `3-8` و `audit_perms.sql:49`)
  ⇒ اصلاح معیار در `fix3` (بند ۵).

### ۴.۳ خارج از معیارهای G5 — `rolreplication = true`

- **شاهد:** `audit_ro: rolsuper=f, rolcreatedb=f, rolcreaterole=f, rolbypassrls=f, rolcanlogin=t,
  rolreplication=t, rolconnlimit=-1`؛ `avnadmin` هم `rolreplication=t`.
- **منشأ:** `grep -rn -E "CREATE ROLE|ALTER ROLE|NOSUPERUSER|rolreplication"` در کل مخزن فقط
  **یک** ضربه دارد: `g5_handoff/audit_perms.sql:24` که این ستون را فقط **می‌خواند**. یعنی نقش
  بیرون از مخزن (کنسول/CLI آوین) ساخته شده و الگوی `rolreplication=t` با قالب پیش‌فرض ارائه‌دهنده
  سازگار است — اثبات قطعی نیاز به کنسول آوین دارد.
- **اثر:** اجازهٔ باز کردن اتصال replication و ساخت slot — قابلیتی خارج از پروفایل ممیز.
  اینکه از این مسیر دادهٔ جداول بیرون بیاید به مسیر decoding و مجوزهای جدول بستگی دارد؛
  بنابراین «شکاف containment» ثبت می‌شود، نه «نشت اثبات‌شده».
- **وضعیت:** در معیارهای نوشته‌شدهٔ G5 نیست، پس در حکم اثری ندارد و عمداً از معیارها جدا نگه داشته شد.

---

## ۵. تغییر معیار `3-7` (before/after) و مجوز آن

**مجوز:** این تغییر به دستور صریح کارفرما در ۲۰۲۶-۱۱-۱۹ انجام شد («معیار را به شکلی اصلاح کن که
مجوزهای استاندارد سیستم به‌عنوان نقص امنیتی نقش audit_ro گزارش نشوند» + «تغییر معیار را با
before/after مستند کن»). این یک **تضعیف** معیار نیست: پوشش اشیای غیرسیستمی دست‌نخورده است.

**before (`g5_probe_fix2.py:323-330`):**
```sql
SELECT n.nspname || '.' || c.relname AS object, c.relkind,
       x.privilege_type, x.is_grantable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(c.relacl) AS x
WHERE x.grantee = 0 AND x.privilege_type <> 'SELECT'
ORDER BY 1, 2
```

**after (`g5_probe_fix3.py`، همان کوئری + دو شرط):**
```sql
WHERE x.grantee = 0 AND x.privilege_type <> 'SELECT'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_(toast|temp)'
```

**اثر:** فقط grantهای پیش‌فرض PostgreSQL حذف می‌شوند؛ هر جدول/ویو/سکوئنس در `public` یا هر
schema دیگر همچنان گزارش می‌شود، پس یک grant واقعی `PUBLIC` نمی‌تواند پنهان بماند.

**باگ جانبی که در همین نسخه رفع شد (`3-4b`):** فیلتر `NOT LIKE 'pg\\_toast%'` در fix1/fix2 داخل
رشتهٔ standard-conforming یک بک‌اسلش واقعی + wildcard `_` بود و عملاً **هیچ چیزی را حذف نمی‌کرد**.
با `!~ '^pg_(toast|temp)'` جایگزین شد. شاهد خنثی‌بودن: مجموعهٔ سطرهای `3-4b` در سه اجرای محلی
(A: fix2، B: fix3، C: fix3 پس از رفع) **یکسان** بود: `information_schema`, `pg_catalog`, `public`.

**متادیتای گزارش:** `fix3` دو کلید `probe_version` و `criteria_note` را داخل `g5_report.json`
می‌نویسد، تا هر گزارش خودش بگوید با کدام معیار تولید شده است.

---

## ۶. اعتبارسنجی محلی (شواهد خام)

محیط: PostgreSQL **16.2** (باینری `pgserver`، بدون OpenSSL)، `asyncpg==0.32.0`، نقش
غیرسوپر‌یوزر `audit_ro`، همهٔ کوئری‌ها داخل `BEGIN / SET TRANSACTION READ ONLY / ROLLBACK`.
اسکریپت‌ها: `g5_handoff/local_validation/` (خروجی‌ها در همان پوشه ذخیره شده‌اند).

| اجرا | نسخه | `3-4` | `3-7` | حکم معیارها (بدون identity) |
|---|---|---|---|---|
| A | fix2 | FAIL (`can_temp=true`) | **FAIL** (`pg_catalog.pg_settings`) | FAIL |
| B | fix3 | FAIL (`can_temp=true`) | **PASS** (`[]`) | FAIL |
| C | fix3 پس از `REVOKE TEMPORARY ... FROM PUBLIC` روی همان خوشهٔ موقت | **PASS** (`can_temp=false`) | PASS | **PASS** |

نتیجه‌گیری‌ها:
1. `fix3` فقط false positive را حذف کرد و **یافتهٔ واقعی `3-4` را پنهان نکرد** (اجرای B).
2. با اجرای اصلاح پیشنهادی، همهٔ معیارها PASS می‌شوند (اجرای C) — یعنی مانع، فقط خودِ ACL است.
3. `tro=on` و `xid_assigned=null` در هر سه اجرا؛ داده دست‌نخورده: سطر `baseline-row-must-survive`
   قبل و بعد یکسان.

**اعتبارسنجی بستهٔ SQL** روی خوشهٔ موقتِ هم‌نام با production (`defaultdb`، `avnadmin`،
`audit_ro`، `_avnadmin_monitor`):
- `PREFLIGHT_AUDIT_RO_v2.sql` (فقط SELECT) پیش از تغییر: `1` نقشِ LOGIN از دست می‌دهد
  (`audit_ro`)، `3` نقش LOGIN کل، `14` نقش از پیش تعریف‌شده (NOLOGIN) نادیده گرفته شد؛
  `public_temp=t`, `public_connect=t`, `public_create=f`.
- `FIX_AUDIT_RO_v2.sql`: `audit_ro_temp` از `t` به `f`؛ `avnadmin` و `_avnadmin_monitor` روی `t`
  ماندند؛ `datacl` از `{=Tc/avnadmin,...}` به `{=c/avnadmin,...}` رسید (فقط CONNECT برای PUBLIC).
- اجرای دوبارهٔ preflight: `login_roles_that_would_lose_temp = 0`.

**محدودیت صادقانه:** build محلی OpenSSL ندارد، پس مسیر TLS (`build_ssl_context` + دروازهٔ
`identity`) به‌صورت محلی اجرا نشد. این مسیر در `fix3` **تغییر نکرده** و در اجرای واقعی دور ۴ با
`ssl:true` و زنجیرهٔ CA + `check_hostname` عملاً کار کرده است.

---

## ۷. بستهٔ نهایی و هش‌ها

`g5_handoff/g5_fix3.zip` — ۶ عضو، ۱۶۷۰۰ بایت — SHA-256:
`b194b39fa3a4ef8d56c5569ec388381152c56b32447a79a316261f584aaeef4a`

| فایل | SHA-256 | نقش |
|---|---|---|
| `g5_probe_fix3.py` | `a3aeb1fe75a33a6622fa30a142e7752d073115f3e182dbcbb388ac561ad0643d` | ممیز (۱۶ کوئری) |
| `RUN_G5_fix3.ps1` | `5bbe0f7e5bc0af705d4bd22c7efea3635834a8a6d0735782517919491c87fd81` | runbook ویندوز |
| `audit_perms.sql` | `569373ad13196b053a6f45ba407477a12d826162652f225e7cbaf591845cfc8e` | snapshot فقط‌خواندنی psql (بدون تغییر) |
| `PREFLIGHT_AUDIT_RO_v2.sql` | `6d3f51bff392c1cf0552098534aec79b054a0ffe55ea17c288174b46de47a9f4` | inventory فقط‌خواندنی اثر 3-4 |
| `FIX_AUDIT_RO_v2.sql` | `074ee2d30da54a0908b211354cf7e1bbef99741730a08e0f690a5fe6601d0a43` | اصلاح gated برای 3-4 |
| `aiven-ca.pem` | `70e57f33b29936a458aad6e9cb99425f6d6d50ed1ab7770e853f590efefbcea4` | CA (بدون تغییر) |

نسخه‌های قبلی دست‌نخورده‌اند: `g5_probe.py` (`569eddc0…`), `g5_probe_fix1.py` (`6a4a3d1e…`),
`g5_probe_fix2.py` (`48af5bd3…`), `RUN_G5.ps1`, `RUN_G5_fix1.ps1`, `RUN_G5_fix2.ps1`,
`g5_final.zip`, `g5_fix1.zip`, `g5_fix2.zip`, `FIX_AUDIT_RO_IF_NEEDED.sql`.

---

## ۸. رویهٔ اجرا (ویندوز)

```powershell
# ۱) تأیید هش بسته
$zip = "$HOME\Downloads\g5_fix3.zip"
(Get-FileHash $zip -Algorithm SHA256).Hash   # b194b39fa3a4ef8d56c5569ec388381152c56b32447a79a316261f584aaeef4a

# ۲) استخراج غیرمخرب به پوشهٔ جدید
$dest = "C:\g5_fix3_" + (Get-Date -Format 'yyyyMMdd_HHmmss')
New-Item -ItemType Directory -Path $dest | Out-Null
Expand-Archive -Path $zip -DestinationPath $dest   # هرگز -Force

# ۳) اجرا (فقط audit_ro، TLS با تأیید CA + hostname در خود probe)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
& (Join-Path $dest 'RUN_G5_fix3.ps1')

# ۴) خلاصهٔ حکم در همان اجرا چاپ می‌شود؛ گزارش: g5_report.json
```

پس از آن، اگر تصمیم بند ۹ گرفته شد (نسخهٔ بازنگری‌شدهٔ verify-full در بند ۱۱):

```powershell
# preflight (فقط‌خواندنی، دو قفل: session read-only + BEGIN TRANSACTION READ ONLY)
& (Join-Path $dest 'RUN_G5_MAINT_fix4.ps1')                # فقط preflight
# و پس از تأیید انسانی:
& (Join-Path $dest 'RUN_G5_MAINT_fix4.ps1') -ApplyFix
```

> **بازنگری ۲۰۲۶-۱۱-۱۹:** تمام اتصال‌ها به `verify-full` ارتقا یافتند و `sslmode=require`
> از این سند حذف شد. جزئیات و دلایل در بند ۱۱.

---

## ۹. تصمیم‌های باقی‌مانده (نیازمند انسان — تنها مانع)

1. **`REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC`** (اصلاح `3-4`)
   - پیش‌شرط: اجرای `PREFLIGHT_AUDIT_RO_v3.sql` (بند ۱۱) و بررسی اینکه هیچ نقش LOGIN دیگری به TEMP وابسته نیست.
   - اثر مستند: `audit_ro` TEMP را از دست می‌دهد؛ `avnadmin` و `_avnadmin_monitor` صریحاً
     نگه می‌دارند؛ نقش‌های آیندهٔ این دیتابیس هم TEMP نخواهند داشت. برنامه از temp table
     استفاده نمی‌کند (جست‌وجوی مخزن: هیچ `CREATE TEMP`/`ON COMMIT DROP`/`pg_temp`).
   - پس از اجرا: `RUN_G5_fix3.ps1` مجدداً ⇒ انتظار `verdict=PASS` (اجرای C محلی همین را نشان داد).
2. **`ALTER ROLE audit_ro NOREPLICATION`** (اختیاری، خارج از معیار G5)
   - اثر: بستن قابلیت replication/slot؛ روی ممیزی SELECT اثری ندارد.

تا اجرای بند ۱، حکم معتبر G5 **`FAIL`** است و `3-4` تنها دلیل آن است (`3-7` در fix3 = PASS).

---

## ۱۰. بازتولید اعتبارسنجی محلی

```bash
python3 -m venv /tmp/g5v && /tmp/g5v/bin/pip install "asyncpg==0.32.0" pgserver
bash g5_handoff/local_validation/run_local_validation.sh   # سه اجرا A/B/C
bash g5_handoff/local_validation/run_sql_package_validation.sh  # preflight + fix + probe
```

خروجی‌های ذخیره‌شده: `local_validation_output.txt` و `sql_package_validation_output.txt`
در همان پوشه. این اسکریپت‌ها dev-only هستند و در ZIP ممیزی نیستند.

---

## ۱۱. بازبینی پیش از اقدام (بستهٔ fix4)

هدف این بخش: پیش از هر نوشتن روی production، سه چیز تثبیت شود — همهٔ اتصال‌ها `verify-full`،
فایل‌های SQL خط‌به‌خط بازبینی و اصلاح شده باشند، و preflight واقعاً نتواند چیزی را تغییر دهد.

### ۱۱.۱ الزام verify-full و نحوهٔ برآورده‌شدن

هر اتصال production باید زنجیرهٔ گواهی **و** hostname را بررسی کند؛ `sslmode=require` از این
بسته حذف شده است.

```powershell
psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=C:/g5_fix4/aiven-ca.pem" -v ON_ERROR_STOP=1 -f PREFLIGHT_AUDIT_RO_v3.sql
```

اگر libpq گواهی Aiven را رد کرد (نقص ثبت‌شدهٔ گواهی: IP-SAN بدشکل،
`certificate contains IP address with invalid length 16`)، مسیر دوم همان سطح امنیتی را می‌دهد:

```powershell
python g5_admin.py --mode preflight --sql PREFLIGHT_AUDIT_RO_v3.sql --ca aiven-ca.pem --user avnadmin
```

`g5_admin.py` فقط یک حالت TLS دارد: `CERT_REQUIRED` + `check_hostname=True` + CA آوین؛ اگر فایل CA
نبود یا فلگ‌ها درست نبود، به‌جای اتصال، برنامه خارج می‌شود. هیچ کد `CERT_NONE` یا
`sslmode=require` در آن وجود ندارد (تست خودکار: «no CERT_NONE / sslmode=require code path»).
این همان مسیری است که probe با آن روی همین کلاستر `ssl:true` گرفت.

### ۱۱.۲ پاسخ بازبینی دو فایل SQL

**(۱) دقیقاً کدام نقش‌ها از حذف TEMP از PUBLIC متأثر می‌شوند؟**
بخش `3c` فایل preflight یک جدول تصمیم per-role می‌دهد و بخش `4` شمارش می‌کند. قاعدهٔ `LOSES`:
`TEMP` مؤثر دارد، **grant صریح ندارد**، سوپرنیست نیست، owner دیتابیس نیست، و از طریق membership
به هیچ نقشی که TEMP صریح دارد هم نمی‌رسد. روی خوشهٔ آزمایشی (هم‌نام production) نتیجه:
`audit_ro = LOSES` و `avnadmin/_avnadmin_monitor = KEEPS (explicit grant)`، `postgres = KEEPS (superuser)`.
روی production انتظار همان است و preflight آن را قبل از تغییر اثبات می‌کند.

**(۲) آیا membership، SET ROLE و مجوزهای به‌ارث‌رسیده بررسی شده‌اند؟**
در v2 **نه** — این یک شکاف واقعی بود و در v3 بسته شد:
- بخش `3b`: تمام یال‌های `pg_auth_members` برای نقش‌های LOGIN، با `pg_has_role(..., 'USAGE')`
  (ارث‌بری مؤثر) و `pg_has_role(..., 'SET')` (قابلیت SET ROLE) و اینکه نقش اعطاشده TEMP صریح دارد یا نه؛
- بخش `3c`: ستون `keeps_via_set_role` با `pg_has_role` که **زنجیرهٔ تودرتوی membership** را
  بازگشتی حل می‌کند.

شاهد کارکرد: روی خوشهٔ آزمایشی، `3b` خالی بود (هیچ عضویتی) و `postgres` در `3c` به‌درستی
`_avnadmin_monitor, avnadmin` را به‌عنوان مسیر SET ROLE نشان داد.

**(۳) آیا نقش‌های برنامه، migration، backfill یا سرویس‌های دیگر به TEMP نیاز دارند؟**
نه. شواهد (جست‌وجوی ۲۰۲۶-۱۱-۱۹ روی کل مخزن):
- صفر مورد `CREATE TEMP TABLE` / `ON COMMIT DROP` / `pg_temp` در `app/`، `alembic/`، `scripts/`، `.github/`؛
- صفر `GRANT`/`REVOKE`/`ALTER ROLE` در مخزن؛
- تنها نقش دیتابیسی که در مخزن نام برده شده `avnadmin` است (`.env.example`، `docs/DEPLOYMENT.md`،
  `scripts/dev_only/run_replay_secure.ps1`) و همان چیزی است که `DATABASE_URL` اپ، migration،
  backfill و CI از آن استفاده می‌کنند؛ `avnadmin` با grant صریح TEMP را نگه می‌دارد؛
- migrationها فقط DDL/DML با SQLAlchemy/`op.execute` هستند (دو INSERT تاریخی)، بدون temp.

**(۴) آیا تغییر فقط مجوز TEMP را حذف می‌کند و هیچ ACL دیگری را تغییر نمی‌دهد؟**
بله، و اکنون دو اثبات دارد:
- **قبل/بعد واقعی:** `datacl` از `{=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}`
  به `{=c/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}` رسید؛ تنها بیت تغییرکرده
  `T` در entry مربوط به PUBLIC است. `CONNECT` برای PUBLIC حفظ شد، `CREATE` هرگز وجود نداشت.
- **assertهای درون‌تراکنشی:** فایل fix خودش بعد از REVOKE بررسی می‌کند که `audit_ro` TEMP نداشته باشد،
  `avnadmin` و `_avnadmin_monitor` داشته باشند، `PUBLIC` همچنان `CONNECT` داشته باشد و هیچ entry
  `TEMPORARY` برای PUBLIC نمانده باشد. نقض هر کدام ⇒ استثنا ⇒ rollback.

یک تغییر دیگر نسبت به v2: `GRANT TEMPORARY ... TO avnadmin` **شرطی** شد. در حالت عادی
(`avnadmin` قبلاً grant دارد) هیچ نوشتنی روی ACL انجام نمی‌شود؛ در تست محلی خروجی
`NOTICE: G5: avnadmin already holds TEMPORARY explicitly - no GRANT issued` ثبت شد.

| فایل | تغییر نسبت به v2 |
|---|---|
| `PREFLIGHT_AUDIT_RO_v3.sql` | `BEGIN TRANSACTION READ ONLY`/`ROLLBACK`؛ بخش `3b` membership؛ بخش `3c` جدول تصمیم؛ بخش `4` دو شمارندهٔ تازه؛ بخش `6` snapshot کامل ACL؛ ستون‌های `ssl`/`tx_read_only`/`in_recovery` |
| `FIX_AUDIT_RO_v3.sql` | guard هویت/دیتابیس/recovery؛ `GRANT` شرطی؛ `BEGIN … COMMIT`؛ چهار assert درون‌تراکنشی؛ snapshot قبل/بعد |

### ۱۱.۳ اثبات read-only بودن preflight

دو قفل مستقل:
1. فایل با `BEGIN TRANSACTION READ ONLY;` باز و با `ROLLBACK;` بسته می‌شود — سرور در تراکنش
   read-only هر `CREATE/ALTER/DROP/GRANT/REVOKE` و هر DML را رد می‌کند (SQLSTATE 25006)؛
2. runner در حالت preflight قبل از هر چیز `SET default_transaction_read_only = on` می‌گذارد و
   مقدار آن را چاپ می‌کند.

اثبات اجرایی روی خوشهٔ آزمایشی (`sql_package_v3_output.txt`، بخش ۲):
`ERROR: cannot execute CREATE TABLE in a read-only transaction` و `exit=3`.
`tx_read_only=on` هم در بخش ۰ همان خروجی دیده می‌شود.

### ۱۱.۴ اثبات rollback خودکار در FIX

سناریوی واقعی: grant صریح TEMP را از `_avnadmin_monitor` گرفتم تا فقط به PUBLIC وابسته شود،
سپس FIX اجرا شد. REVOKE انجام شد، اما assert مربوط به نقش provider شکست:
`ERROR: G5: _avnadmin_monitor lost TEMPORARY - provider role would break`.
نتیجهٔ بررسی بلافاصله بعد: `datacl = {=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=c/avnadmin}`
و `public_temp_still_true = t` — یعنی **REVOKE برگشت خورد و هیچ تغییری commit نشد**.
پس اگر شرایط روی production آن‌طور نباشد که پیش‌بینی کرده‌ایم، FIX خودش را متوقف و برمی‌گرداند.

بعد از بازگرداندن grant و اجرای واقعی FIX: `audit_ro=f, avnadmin=t, monitor=t, public_temp=f,
public_connect=t` و probe: `3-4=PASS`, حکم معیارها `PASS`، دادهٔ نمونه دست‌نخورده.

### ۱۱.۵ تست‌های آفلاین و محدودیت‌ها

- `local_validation/test_admin_runner.py` — ۲۲ بررسی، همه PASS: splitter (dollar-quote، quote،
  comment)، سیاست preflight/fix (فایل حاوی `DROP`، `GRANT ALL ... TO PUBLIC`، فایل بدون `REVOKE`،
  و نوشتن در preflight همه رد می‌شوند)، فلگ‌های TLS، اجباری‌بودن CA، و ترتیب اجرای ۱۱ statement.
- `local_validation/run_sql_package_validation_v3.sh` — کل چرخه روی خوشهٔ throwaway: preflight،
  اثبات read-only، probe قبل، rollback اجباری، FIX واقعی، preflight بعد، probe بعد، سلامت داده.
- **محدودیت:** `pwsh` در این محیط نصب نیست، بنابراین `RUN_G5_MAINT_fix4.ps1` فقط به‌صورت ساختاری
  بازبینی شده و اجرا نشده است؛ اولین اجرای واقعی آن روی ویندوز انجام می‌شود.

### ۱۱.۶ بستهٔ fix4 و هش‌ها

**بستهٔ تحویل نهایی:** `g5_handoff/g5_final_v3.zip` — ۹ عضو (همراه `README_FINAL.txt`)، ۳۰۰۷۲ بایت — SHA-256:
`b7d13a76dad1a316bb76426061c31bb77e4094cc3e2256e6f7d6ebf2c27749cf`

`g5_handoff/g5_fix4.zip` — همان ۸ فایل بدون README، ۲۶۶۴۸ بایت — SHA-256:
`8f0f4d468da7bbca0130abde7ae4c4e30b36bc619305c1b4bc7aef5d381913ae`

> همهٔ فایل‌های داخل این بسته فقط `verify-full` دارند. runbook قدیمی `RUN_G5_fix3.ps1`
> (که برای شاهد تکمیلی از `require` استفاده می‌کند) عمداً **در این بسته نیست** و فقط داخل
> بستهٔ تاریخی `g5_fix3.zip` دست‌نخورده باقی مانده است؛ جای آن، `RUN_G5_REAUDIT_fix4.ps1`
> فقط probe را با verify-full اجرا می‌کند.

| فایل | SHA-256 | وضعیت |
|---|---|---|
| `g5_probe_fix3.py` | `a3aeb1fe75a33a6622fa30a142e7752d073115f3e182dbcbb388ac561ad0643d` | بدون تغییر |
| `g5_admin.py` | `8163ecbfa5dcb94c4f2d2c7532e3877103a5cb619a0d1b0aa05ad5673b09dee1` | جدید (سخت‌شده: اسکن DO، پیام rollback) |
| `RUN_G5_REAUDIT_fix4.ps1` | `20cae82c6f6c5c142271ebeb47e1e045ff3cd2aecc7c60950108cc16ea2a40e7` | جدید |
| `RUN_G5_MAINT_fix4.ps1` | `f2bc3a7d3397776a88f9e2abcbc18155039f254432b492012b34e11799e3f98a` | جدید |
| `PREFLIGHT_AUDIT_RO_v3.sql` | `0c27e5e37ef781df3a80072199a7bed8db701d0b8705e47dd715f1791659ec20` | جدید |
| `FIX_AUDIT_RO_v3.sql` | `279657bc03b3b1ebbd2d9164e5537fa4a401ef6a01cc6ae87fb013defbbd30aa` | جدید |
| `README_FINAL.txt` | — (فقط در بستهٔ نهایی) | جدید |
| `audit_perms.sql` | `569373ad13196b053a6f45ba407477a12d826162652f225e7cbaf591845cfc8e` | بدون تغییر |
| `aiven-ca.pem` | `70e57f33b29936a458aad6e9cb99425f6d6d50ed1ab7770e853f590efefbcea4` | بدون تغییر |

`g5_fix3.zip` و همهٔ نسخه‌های قبلی دست‌نخورده‌اند (`b194b39f…`).

### ۱۱.۷ runbook نهایی و معیار توقف

```powershell
$dest = "C:\g5_final_v3_" + (Get-Date -Format 'yyyyMMdd_HHmmss')
New-Item -ItemType Directory -Path $dest | Out-Null
Expand-Archive -Path "$HOME\Downloads\g5_final_v3.zip" -DestinationPath $dest   # هرگز -Force
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

& (Join-Path $dest 'RUN_G5_MAINT_fix4.ps1')                  # فقط preflight، بدون تغییر
# پس از تأیید انسانی:
& (Join-Path $dest 'RUN_G5_MAINT_fix4.ps1') -ApplyFix
```

**خروجی مورد انتظار preflight:** `verify-full (CERT_REQUIRED + check_hostname)`،
`default_transaction_read_only=on`، `tx_read_only on`، `public_temp t`، `datacl` برابر
`{=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}`، بخش `3c` با `LOSES` فقط برای
`audit_ro`، و `login_roles_that_lose_temp = 1` با `total_login_roles = 3` (روی production؛ در خوشهٔ
آزمایشی ۴ بود چون `postgres` هم LOGIN است).

**معیار توقف (بدون استثنا):**
- هر نقش LOGIN دیگر جز `audit_ro` با برچسب `LOSES`؛
- هر عضویت/SET ROLE ناشناخته در `3b` که نقش سوم را به TEMP می‌رساند؛
- بیشتر از یک نقش در `login_roles_that_lose_temp`؛
- `temp_relations > 0` (کسی وسط کار temp است)؛
- انحراف `datacl_baseline` از مقدار انتظاری؛
- نبود `verify-full` یا `default_transaction_read_only=on` در خروجی؛
- هر خطا یا هر نقش/وابستگی که در این سند توضیح داده نشده است.

**دستور اصلاح (فقط بعد از تأیید):** همان `-ApplyFix` بالا. اسکریپت خودش پیش‌شرط‌ها را دوباره
بررسی می‌کند، عبارت `APPLY TEMPORARY REVOKE` را طلب می‌کند و در صورت شکست هر assert، rollback
می‌کند. معادل دستی (verify-full):

```powershell
python g5_admin.py --mode fix --apply-fix --sql FIX_AUDIT_RO_v3.sql --ca aiven-ca.pem --user avnadmin --verbose
```

**اجرای مجدد ممیزی (verify-full، بدون psql):**
`& (Join-Path $dest 'RUN_G5_REAUDIT_fix4.ps1')` ⇒ انتظار `3-4 = PASS` و `verdict = PASS`.

### ۱۱.۸ وضعیت پس از این بازبینی

- روی production **هیچ** اتصال نوشتنی، `GRANT`، `REVOKE` یا `ALTER` اجرا نشد؛ حکم G5 همچنان `FAIL`
  است و تنها دلیل آن `3-4` باقی می‌ماند.
- کارهای باقی‌مانده همان دو تصمیم بند ۹ است؛ مورد `NOREPLICATION` هنوز باز و اختیاری است.

---

## ۱۲. اعتبارسنجی نهایی بستهٔ تحویل (fix4 → final v3)

این بخش همان بررسی «خط‌به‌خط» و آزمون‌های تکراری است که پیش از تحویل اجرا شد؛ هیچ ممیزی جدیدی
شروع نشده است.

### ۱۲.۱ بازبینی خط‌به‌خط (نتیجه)

| بررسی | نتیجه | محل |
|---|---|---|
| `BEGIN TRANSACTION READ ONLY` نخستین استیتمنت preflight و `ROLLBACK` آخرین آن | ✅ | `PREFLIGHT_AUDIT_RO_v3.sql`؛ تست «preflight opens/ends …» |
| هیچ استیتمنت نوشتنی در preflight (فقط SELECT/CTE) | ✅ | policy mode=preflight |
| ترتیب FIX: guard → BEFORE → `BEGIN` → GRANT شرطی → `REVOKE` → assertها → `COMMIT` → AFTER | ✅ | ۱۰ استیتمنت؛ تست‌های ترتیب |
| شکست هر assert ⇒ استثنا داخل تراکنش ⇒ abort ⇒ عدم commit | ✅ | اثبات اجرایی ۱۲.۳ |
| TLS فقط `CERT_REQUIRED` + `check_hostname=True` + CA؛ بدون `CERT_NONE`/`require` | ✅ | `build_ssl_context` + تست‌ها |
| membership/inheritance/SET ROLE پوشش داده شده (بازگشتی) | ✅ | بخش‌های `3b` و `3c` |
| FIX فقط بیت TEMPORARY مربوط به PUBLIC را تغییر می‌دهد | ✅ | assertهای `CONNECT`/`CREATE`/datacl + snapshot قبل/بعد |

دو نقص در همین بازبینی پیدا و رفع شد (hashهای ۱۱.۶ به‌روزرسانی شد):
1. **سوراخ allow-list در runner:** پیش از این، یک `DO $$ … $$` می‌توانست DDL را پنهان کند.
   اکنون هر استیتمنت در حالت fix نخست از نظر کلمات ممنوعه اسکن می‌شود (literalها جدا می‌شوند تا
   متن پیام‌های `RAISE` مثلاً «has CREATE» اشتباه گرفته نشود؛ اما اگر استیتمنت `EXECUTE` داشته باشد،
   literalها هم اسکن می‌شوند چون همان‌جا payload است).
2. **پیام گمراه‌کنندهٔ rollback:** اگر خطا پس از `COMMIT` رخ می‌داد، runner می‌گفت «هیچ تغییری commit
   نشد». اکنون وضعیت commit ردیابی می‌شود و در آن حالت هشدار «تغییر commit شده است» می‌دهد.

### ۱۲.۲ تست‌های آفلاین

`local_validation/test_admin_runner.py` — ۲۳ بررسی، همه PASS، exit=0 (۲۲ بررسی پیشین + یک بررسی
جدید برای DDL پنهان‌شده در DO).

```
PASS  preflight parses to 11 statements  got 11
PASS  preflight opens with BEGIN TRANSACTION READ ONLY
PASS  preflight ends with ROLLBACK
PASS  fix has 3 DO blocks kept intact  3
PASS  fix file passes fix policy
PASS  fix policy rejects DROP
PASS  fix policy rejects broad GRANT
PASS  fix policy rejects DDL hidden inside a DO block
PASS  fix policy rejects a file without REVOKE
PASS  preflight policy rejects a write
PASS  TLS context is verify-full  check_hostname=True verify_mode=2
PASS  missing CA is refused
PASS  no CERT_NONE / sslmode=require code path in the runner
PASS  all 11 preflight statements executed in order  11
PASS  BEGIN/ROLLBACK go through execute, SELECT through fetch
ALL ADMIN-RUNNER TESTS PASSED
```

### ۱۲.۳ چرخهٔ کامل روی خوشهٔ موقت

`local_validation/run_sql_package_validation_v3.sh` → `sql_package_v3_output.txt`، exit=0.

| گام | شاهد خام |
|---|---|
| preflight | `server_version 16.2`, `tx_read_only on`, `public_temp t`, `datacl {=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}` |
| اثبات read-only | `ERROR: cannot execute CREATE TABLE in a read-only transaction` + `exit=3` |
| rollback اجباری | `ERROR: G5: _avnadmin_monitor lost TEMPORARY - provider role would break` و `datacl` بدون تغییر، `public_temp_still_true = t` |
| FIX واقعی | `NOTICE: avnadmin already holds TEMPORARY explicitly - no GRANT issued` ⇒ `audit_ro=f, avnadmin=t, monitor=t, public_temp=f, public_connect=t` |
| preflight دوم | `login_roles_that_lose_temp=0`، `datacl {=c/avnadmin,…}` |
| probe بعد | `3-4=PASS`، بقیه PASS جز `identity` (محیط محلی TLS ندارد) |
| داده | `1 | baseline-row-must-survive` سالم |

### ۱۲.۴ محدودیت نسخه (صریح)

خوشهٔ آزمایشی **PostgreSQL 16.2** است و production **18.6**. SQL فقط از کاتالوگ‌های پایدار استفاده
می‌کند (`pg_database`, `pg_roles`, `pg_auth_members`, `aclexplode`, `pg_stat_ssl`,
`pg_class.relpersistence`, `has_database_privilege`) و حداقل نسخهٔ لازم ۱۴ است، اما **هیچ PASS روی
۱۸.۶ ادعا نمی‌شود**؛ نخستین preflight واقعی روی production همان اثبات است. مسیر TLS هم محلی قابل
آزمون نبود (build محلی OpenSSL ندارد)، پس `ssl=f` در خروجی محلی طبیعی است.

### ۱۲.۵ وضعیت نهایی پروژه

**«ابزار نهایی آماده؛ اصلاح production در انتظار تأیید»**

- ابزار قابل تکرار: `g5_final_v3.zip` با hashهای ثبت‌شده.
- `3-7`: false positive اصلاح و مستند شد (بند ۵).
- `3-4`: همچنان نقص واقعی TEMP را تشخیص می‌دهد و تا اجرای FIX، حکم `FAIL` است.
- preflight و FIX محافظت‌شده و مستند؛ هیچ نوشتنی روی production انجام نشده.
- `audit_ro.rolreplication` جداگانه و **اختیاری** ثبت شد (بند ۹)؛ هیچ `ALTER ROLE` توصیه نمی‌شود.

---

## ۱۳. بازبینی تولید (production) — یافتهٔ دامنهٔ بزرگ‌تر و fix جراحی (v4)

### ۱۳.۱ preflight واقعی روی production (PG 18.6)

در ۲۰۲۶-۱۱-۱۹ preflight روی production اجرا شد (`RUN_G5_MAINT_fix5.ps1`، verify-full، TLSv1.3).
نتیجهٔ کلیدی: **دامنهٔ تأثیر بزرگ‌تر از آنچه تأیید شده بود.**

| نقش | temp_now | temp_explicit | keeps_via_set_role | impact_after_revoke |
|---|---|---|---|---|
| `_avnadmin_monitor` | t | t | — | KEEPS (explicit grant) |
| `avnadmin` | t | t | — | KEEPS (owner) |
| `_aiven` | t | f | avnadmin, _avnadmin_monitor | KEEPS (superuser) |
| `postgres` | t | f | avnadmin, _avnadmin_monitor | KEEPS (superuser) |
| `_aiven_repl` | t | f | — | **LOSES** |
| `_aiven_rewind` | t | f | — | **LOSES** |
| `audit_ro` | t | f | — | **LOSES** |

- `login_roles_that_lose_temp = 3` (نه ۱)، `total_login_roles = 7` (نه ۳/۴).
- `temp_relations = 0`.
- `datacl` baseline = `{=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}` (مطابق انتظار).
- گارد اسکریپت (stop criterion) فعال شد و **FIX اجرا نشد** (هیچ تغییری روی production).

**علت:** `REVOKE TEMPORARY FROM PUBLIC` همهٔ نقش‌هایی را که TEMP را فقط از PUBLIC می‌گیرند
قطع می‌کند؛ `_aiven_repl` و `_aiven_rewind` (نقش‌های داخلی Aiven) هم grant صریح ندارند و
سوپریوزر/owner نیستند، پس آن‌ها هم TEMP را از دست می‌دهند.

پیش‌شرط تأیید انسانی قبلی («فقط audit_ro متأثر؟ → بله») بر اساس فرض بود، نه دادهٔ واقعی.
دادهٔ production فرض را رد کرد.

### ۱۳.۲ تصمیم انسانی: Option C (fix جراحی)

کاربر گزینهٔ C را انتخاب کرد: **grant صریح TEMP به `_aiven_repl` و `_aiven_rewind` قبل از
revoke PUBLIC**، تا فقط `audit_ro` TEMP را از دست بدهد.

| گزینه | توضیح | وضعیت |
|---|---|---|
| A — توقف | بدون fix؛ 3-4 FAIL باقی می‌ماند | رد شد |
| B — revoke ساده | ۳ نقش TEMP را از دست می‌دهند | رد شد (blast radius) |
| C — جراحی (v4) | فقط audit_ro TEMP را از دست می‌دهد | **انتخاب شد** |

### ۱۳.۳ FIX_AUDIT_RO_v4.sql (ساخته‌شده، اعتبارسنجی‌شده)

فایل جدید: `g5_handoff/FIX_AUDIT_RO_v4.sql` (hash: `0c85a2e2…41ce8`).

تغییرات نسبت به v3:
- Guards اضافه‌شده: `_aiven_repl` و `_aiven_rewind` باید وجود داشته باشند و TEMP داشته باشند
  (در غیر این صورت abort — topology تغییر کرده).
- GRANT شرطی (idempotent، بر اساس وجود entry در datacl) به `_aiven_repl` و `_aiven_rewind`
  قبل از REVOKE.
- Assertهای جدید: `_aiven_repl` و `_aiven_rewind` باید بعد از fix هم TEMP داشته باشند
  (effective + explicit entry در datacl).
- BEFORE/AFTER snapshot شامل هر دو نقش جدید.
- `\echo` به `RUN_G5_REAUDIT_fix5.ps1` اشاره می‌کند.

### ۱۳.۴ اعتبارسنجی محلی (PG 16.2، با نقش‌های Aiven شبیه‌سازی‌شده)

اسکریپت: `g5_handoff/local_validation/run_sql_package_validation_v4.sh` → exit=0.

| گام | شاهد |
|---|---|
| preflight (با _aiven_repl/_aiven_rewind) | `3 LOSES: audit_ro, _aiven_repl, _aiven_rewind`، `login_roles_that_lose_temp=0` پس از fix |
| اثبات read-only | `ERROR: cannot execute CREATE TABLE in a read-only transaction` + exit=3 |
| rollback اجباری (monitor بدون TEMP) | `ERROR: G5: _avnadmin_monitor lost TEMPORARY`، `datacl` بدون تغییر، `public_temp_still_true=t` |
| FIX واقعی v4 | `avnadmin: no GRANT` (no-op)، `_aiven_repl: GRANT issued`، `_aiven_rewind: GRANT issued`، `all post-conditions hold; committing` |
| AFTER | `audit_ro=f, avnadmin=t, monitor=t, repl=t, rewind=t, public_temp=f, public_connect=t` |
| datacl_after | `{=c/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin,_aiven_repl=T/avnadmin,_aiven_rewind=T/avnadmin}` |
| probe بعد | `3-4=PASS` (`can_temp=false`)، بقیه PASS جز `identity` (محیط محلی TLS ندارد) |
| داده | `1 | baseline-row-must-survive` سالم |

### ۱۳.۵ بستهٔ نهایی (fix5)

- `g5_handoff/g5_final_v4.zip` — ۹ عضو، ۳۲۰۱۰ بایت — SHA-256:
  `d06b13c2a8eb0e2f7df430dd196e9fe7f08cb411e9756fd42f27be65cf08d769`
- `g5_handoff/g5_fix5.zip` — ۸ عضو، ۲۸۱۶۴ بایت — SHA-256:
  `709db7dd35e8a5833e87c329679a1e3bda16b66c773cbbf417fb489cc6a15c33`

| فایل | SHA-256 | وضعیت |
|---|---|---|
| `g5_probe_fix3.py` | `a3aeb1fe75a33a6622fa30a142e7752d073115f3e182dbcbb388ac561ad0643d` | بدون تغییر |
| `g5_admin.py` | `8163ecbfa5dcb94c4f2d2c7532e3877103a5cb619a0d1b0aa05ad5673b09dee1` | بدون تغییر |
| `RUN_G5_MAINT_fix5.ps1` | `98e47b5f4652f4f3ca2b025426d05454a94ecf2f628af09eb0157dd3fb357328` | جدید (stop criterion: LOSES ⊆ {audit_ro, _aiven_repl, _aiven_rewind}) |
| `RUN_G5_REAUDIT_fix5.ps1` | `fe80bc4914fad18e383c39749db8187cf8ca989b48fc3f6cdb31c788b6edf98f` | جدید |
| `PREFLIGHT_AUDIT_RO_v3.sql` | `0c27e5e37ef781df3a80072199a7bed8db701d0b8705e47dd715f1791659ec20` | بدون تغییر |
| `FIX_AUDIT_RO_v4.sql` | `0c85a2e2ed1d3daedb6733d67eead2ac7e5ed3a7bbd10aed7e8ab0fab9341ce8` | جدید (جراحی) |
| `audit_perms.sql` | `569373ad13196b053a6f45ba407477a12d826162652f225e7cbaf591845cfc8e` | بدون تغییر |
| `aiven-ca.pem` | `70e57f33b29936a458aad6e9cb99425f6d6d50ed1ab7770e853f590efefbcea4` | بدون تغییر |
| `README_FINAL.txt` | — (فقط در بستهٔ نهایی) | بازنویسی‌شده |

نسخه‌های قبلی (`g5_fix3.zip`, `g5_fix4.zip`, `g5_final.zip`, `g5_final_v3.zip`) دست‌نخورده‌اند.

### ۱۳.۶ دستور اجرا (verify-full)

```powershell
$dest = "C:\g5_final_v4_" + (Get-Date -Format 'yyyyMMdd_HHmmss')
New-Item -ItemType Directory -Path $dest | Out-Null
Expand-Archive -Path "$HOME\Downloads\g5_final_v4.zip" -DestinationPath $dest

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
& (Join-Path $dest 'RUN_G5_MAINT_fix5.ps1')              # فقط preflight
# پس از تأیید انسانی:
& (Join-Path $dest 'RUN_G5_MAINT_fix5.ps1') -ApplyFix    # fix جراحی
& (Join-Path $dest 'RUN_G5_REAUDIT_fix5.ps1')            # re-audit
```

معیار توقف preflight: LOSES ⊆ {audit_ro, _aiven_repl, _aiven_rewind}؛ هر نقش دیگر ⇒ abort.
انتظار re-audit: `3-4 = PASS` و `verdict = PASS`.

### ۱۳.۷ وضعیت نهایی

**«fix5/v4 surgical اجرا شد؛ re-audit production = PASS (verdict=PASS)»**

- `3-7`: false positive اصلاح و مستند شد.
- `3-4`: نقص واقعی بسته شد — fix جراحی v4 روی production commit شد؛
  `audit_ro_temp_before=True → audit_ro_temp_after=False`؛ `public_temp=True → False`؛
  `_aiven_repl` و `_aiven_rewind` هر دو TEMP را از طریق grant صریح حفظ کردند.
  re-audit probe: `3-4=PASS` (`can_temp=false`)، verdict=PASS.
- preflight (verify-full) و FIX (verify-full) محافظت‌شده و مستند؛ همهٔ اتصال‌ها verify-full.
- `audit_ro.rolreplication` جداگانه و اختیاری.
- نسخه‌های قبلی دست‌نخورده.

### ۱۳.۸ شواهد production (اجراشده 2026-11-19)

**Preflight (verify-full, `RUN_G5_MAINT_fix5.ps1`):**
- TLS: `TLSv1.3`، server `18.6`، `default_transaction_read_only=on` (guard active).
- baseline datacl: `{=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}`.
- `3c`: LOSES = `audit_ro`, `_aiven_repl`, `_aiven_rewind` (۳ نقش)؛ بقیه KEEPS.
- `3b`: تنها membership edges = `avnadmin→audit_ro`, `avnadmin→avnadmin_group`,
  `_avnadmin_monitor→pg_monitor`, `_avnadmin_monitor→pg_use_reserved_connections` — هیچ edge اضافی.
- `5`: `temp_relations=0` (هیچ session در حال کار موقت).
- `4`: `login_roles_that_lose_temp=3`, `total_login_roles=7`.
- stop criterion: LOSES ⊆ {audit_ro, _aiven_repl, _aiven_rewind} ⇒ OK.

**FIX (verify-full, `RUN_G5_MAINT_fix5.ps1 -ApplyFix`):**
- تأیید انسانی: تایپ `APPLY TEMPORARY REVOKE`.
- BEFORE: `audit_ro=t, avnadmin=t, monitor=t, repl=t, rewind=t, public_temp=t`.
- GRANT صریح: `_aiven_repl` و `_aiven_rewind` (هر دو `G5: issued GRANT TEMPORARY`).
- `REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC` اجرا شد.
- assertهای درون‌تراکنشی: `G5: all post-conditions hold; committing`.
- AFTER: `audit_ro=f, avnadmin=t, monitor=t, repl=t, rewind=t, public_temp=f, public_connect=t`.
- datacl_after: `{=c/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin,_aiven_repl=T/avnadmin,_aiven_rewind=T/avnadmin}`.
- commit موفق (`COMMIT`)، exit=0.

**Re-audit (verify-full, `RUN_G5_REAUDIT_fix5.ps1`):**
- probe_version = fix3، started_at = 2026-11-19T18:54:39Z.
- `3-4`: `can_connect=true, can_temp=false, can_create_schema=false` ⇒ **PASS**.
- verdict = **PASS** (exit=0).
- تمام بخش‌ها PASS: identity, read_only_txn, 1A-1E, 3-4, 3-4b, 3-6, 3-7, 3-7b, 3-8.
- `2`: NOT_APPLICABLE (large objects 0).
- report: `g5_report_20261119_222438.json` (روی ماشین کاربر).

**hash فایل‌های اجراشده (verify-full از خروجی اسکریپت):**
- `g5_admin.py`: `8163ecbfa5dcb94c4f2d2c7532e3877103a5cb619a0d1b0aa05ad5673b09dee1`
- `PREFLIGHT_AUDIT_RO_v3.sql`: `0c27e5e37ef781df3a80072199a7bed8db701d0b8705e47dd715f1791659ec20`
- `FIX_AUDIT_RO_v4.sql`: `0c85a2e2ed1d3daedb6733d67eead2ac7e5ed3a7bbd10aed7e8ab0fab9341ce8`
- `g5_probe_fix3.py`: `a3aeb1fe75a33a6622fa30a142e7752d073115f3e182dbcbb388ac561ad0643d`
- `aiven-ca.pem`: `70e57f33b29936a458aad6e9cb99425f6d6d50ed1ab7770e853f590efefbcea4`

**نتیجه:** finding `3-4` بسته شد. `audit_ro` دیگر `TEMPORARY` روی `defaultdb` ندارد.
`_aiven_repl` و `_aiven_rewind` TEMP را از طریق grant صریح حفظ کردند (blast radius صفر).
هیچ رمز/DSN در خروجی چاپ نشد (گذرواژه‌ها mask شده‌اند).

---

## ۱۴. بررسی مستقل: `rolreplication` و عضویت `avnadmin → audit_ro`

> وضعیت: **فقط بررسی (read-only) — هیچ تغییر production تا زمان اثبات ضرورت و تأیید اصلاح.**

### ۱۴.۱ یافتهٔ existing (از preflight 3b/3c)

از خروجی preflight production (۲۰۲۶-۱۱-۱۹):

```
=== 3b. MEMBERSHIP EDGES ===
member_role  | granted_role | admin_option | inherits_now | can_set_role | granted_role_has_temp
-------------+--------------+--------------+--------------+--------------+----------------------
avnadmin     | audit_ro     | True         | True         | True         | True
avnadmin     | avnadmin_group | True       | True         | True         | True
_avnadmin_monitor | pg_monitor | False      | True         | True         | True
_avnadmin_monitor | pg_use_reserved_connections | False | True | True | True
```

و `audit_ro: rolsuper=f, rolreplication=t, rolcreatedb=f, rolcreaterole=f, rolbypassrls=f`.

### ۱۴.۲ تحلیل codebase (مستقل، بدون دسترسی به production)

جست‌وجوی کل مخزن (`grep -rn` برای `replication`, `logical_decoding`, `slot`, `streaming`,
`SET ROLE`, `pg_replication`):

- **هیچ استفادهٔ replication در کد پروژه:** تمام اتصال‌ها از asyncpg/psycopg برای کوئری‌های
  عادی (SELECT/INSERT/UPDATE) استفاده می‌کنند. هیچ `pg_create_logical_replication_slot`،
  هیچ `pg_replication_slots` query، هیچ streaming replication. فایل‌های مرتبط:
  `app/database.py`, `alembic/`, `scripts/`, `g5_handoff/g5_probe*.py` — هیچ replication.
- **audit_ro فقط به‌عنوان probe read-only استفاده می‌شود:** `g5_probe_fix3.py` با `audit_ro`
  کوئری‌های فقط‌خواندنی روی کاتالوگ اجرا می‌کند. هیچ نیازی به replication.
- **avnadmin کاربر اصلی app است:** `DATABASE_URL` با `avnadmin` تنظیم شده (`.env.example`،
  `docs/DEPLOYMENT.md`). membership در `audit_ro` برای app ضروری نیست — app مستقیماً
  به جداول دسترسی دارد، نه از طریق audit_ro.
- **نتیجهٔ codebase:** نه `rolreplication` و نه membership `avnadmin→audit_ro` توسط
  هیچ کدی استفاده نمی‌شوند. هر دو «over-provisioned» هستند.

### ۱۴.۳ چرا avnadmin عضو audit_ro است؟

- `avnadmin` به‌عنوان owner دیتابیس ساخته شده (Aiven template). membership در `audit_ro`
  احتمالاً یک artifact از provisioning اولیهٔ Aiven است — یا اینکه Aiven به‌طور پیش‌فرض
  owner را در نقش‌های read-only membership می‌دهد.
- `admin_option=true` یعنی avnadmin می‌تواند دیگران را به audit_ro اضافه کند — قابلیتی
  که برای owner دیتابیس غیرضروری است.
- `inherits_now=true` یعنی avnadmin به‌طور خودکار privilegeهای audit_ro را به ارث می‌برد.
  قبل از fix 3-4، این شامل TEMPORARY (از طریق PUBLIC) بود — اما حالا audit_ro TEMP ندارد،
  پس این inheritance بی‌اثر شده است.
- `can_set_role=true` یعنی avnadmin می‌تواند `SET ROLE audit_ro` اجرا کند — capability
  که برای یک read-only probe role غیرضروری است.

### ۱۴.۴ ارزیابی ضرورت (بدون تغییر production)

| مورد | ضرورت | شواهد | تصمیم |
|---|---|---|---|
| `audit_ro.rolreplication` | **غیرضروری** | هیچ replication در codebase؛ audit_ro فقط probe read-only | candidate برای `ALTER ROLE audit_ro NOREPLICATION` (اختیاری) |
| `avnadmin → audit_ro` membership | **غیرضروری** | app مستقیماً به جداول دسترسی دارد؛ membership فقط privilegeهای audit_ro (که حالا TEMP ندارد) را به ارث می‌برد | candidate برای `REVOKE audit_ro FROM avnadmin` (اختیاری) |
| `avnadmin.admin_option on audit_ro` | **غیرضروری** | owner دیتابیس نیازی به اعطای audit_ro به دیگران ندارد | candidate برای `REVOKE ADMIN OPTION FOR audit_ro FROM avnadmin` |

### ۱۴.۵ ابزار بررسی (read-only)

فایل: `g5_handoff/INVESTIGATE_ROLREP_AND_MEMBERSHIP.sql` (read-only، verify-full)
Runner: `g5_handoff/RUN_G5_INVESTIGATE.ps1`

این ابزار بررسی می‌کند (بدون تغییر):
- A1: audit_ro role attributes (rolreplication, rolsuper, etc.)
- A2: replication slots (server-wide)
- A3: replication-related privileges برای audit_ro
- A4: replication slots summary
- A5: publications
- A6: subscriptions
- B1: membership edge avnadmin→audit_ro (admin_option, inherit, set)
- B2: تمام members of audit_ro
- B3: تمام roles که avnadmin عضو آنهاست
- B4: effective privileges avnadmin از audit_ro به ارث می‌برد
- B5: schema-level privileges inherited
- B6: table-level SELECT privileges inherited
- C1: roles که audit_ro عضو آنهاست (reverse)
- C2: objects owned by audit_ro
- C3: default privileges of audit_ro

### ۱۴.۶ وضعیت

- **هیچ تغییری روی production انجام نشده** (نه NOREPLICATION، نه REVOKE membership).
- هر دو candidate (NOREPLICATION و REVOKE membership) **اختیاری** و **خارج از معیارهای G5** هستند.
- قبل از هر اصلاح، باید:
  1. ابزار investigation روی production اجرا شود (verify-full) و خروجی بررسی شود.
  2. ضرورت اثبات شود (codebase analysis بالا: هیچ استفاده‌ای ندارند).
  3. تأیید صریح انسانی برای هر ALTER/REVOKE.
- پس از تأیید، fix SQL جداگانه (با guards و assertها) ساخته خواهد شد،
  الگوی FIX_AUDIT_RO_v4.sql رعایت می‌شود.

**«ابزار investigation آماده؛ هیچ تغییر production تا زمان تأیید.»**

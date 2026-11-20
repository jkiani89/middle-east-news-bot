# طراحی معماری v3.3 — اصلاحیهٔ سوم بازبینی خصمانه

> **وضعیت:** سند طراحی. هیچ SQL اجرا نشده، هیچ اتصالی برقرار نشده، هیچ DDL/DML انجام نشده است.
> `v3`، `v3.1` و `v3.2` **دست‌نخورده** حفظ می‌شوند. این سند هیچ فایل SQL تولید نمی‌کند.
> فقط ۷ ایراد `B-08..B-14` و ۶ مورد `M-01..M-06` اعمال شده است.

---

## ۰. تغییرنامه

| اصلاح | موضوع | تغییر | بخش |
|---|---|---|---|
| B-08 | سازندگان مؤثر در Default ACL | بستار `CREATE` (مستقیم/ارثی/`SET ROLE`/مالکیت ضمنی) | ۲ |
| B-09 | مدل `EXECUTE` ناقص | شش مسیر جداگانه + زمینهٔ اجرایی `SET ROLE` | ۳ |
| B-10 | `G-PARAM` فقط مستقیم | `has_parameter_privilege` برای مجوز مؤثر | ۴ |
| B-11 | «هفت کاتالوگ» کافی نیست | Inventory کامل مالکیت (۲۷ کاتالوگ) + `UNCONFIRMED`/`BLOCKED` | ۵ |
| B-12 | `ADMIN OPTION` به‌عنوان اختیار عمومی | ماتریس اختیار هر عملیات در فاز ۱ب | ۶ |
| B-13 | مرز مجوز ستونی | تفکیک `G-COL-SELECT` و `G-COL-WRITE` | ۷ |
| B-14 | `G-SECDEF` غیرقابل‌ممیزی | قواعد آزمون‌پذیر سه‌کلاسه + اعتبارسنجی شاهد | ۸ |
| M-01 | سیاست چنددیتابیسی | ثبت فهرست بررسی‌شده + سیاست کلاستر | ۹ |
| M-02 | sequenceهای آینده | Default ACL با `defaclobjtype='S'` | ۹ |
| M-03 | `G-TEMP` بدون تعریف عملیاتی | پیش‌شرط/عملیات/postcondition هر حالت | ۹ |
| M-04 | مالکیت بدون تفکیک مسیر | مسیر مستقیم/ارثی/`SET` جداگانه | ۹ |
| M-05 | هدف مبهم `G-RLS` | هدف = مشاهدهٔ کامل ردیف‌ها | ۹ |
| M-06 | هش ≠ صحت Manifest | تأییدیهٔ DBA + نسخهٔ extractor + شاهد تطبیق | ۹ |

---

## ۱. پایه: معنای `NULL` در ACL (مبنای B-09 و B-10)

در PostgreSQL، `acl IS NULL` یعنی **مجوزهای پیش‌فرض**، نه «هیچ مجوزی»:

| شیء | پیش‌فرض `NULL` |
|---|---|
| تابع/رویه | مالک: همه؛ `PUBLIC`: `EXECUTE` |
| schema | مالک: همه؛ `PUBLIC`: `USAGE` (از PG 15: بدون `CREATE`) |
| دیتابیس | مالک: همه؛ `PUBLIC`: `CONNECT` + `TEMPORARY` |
| جدول | مالک: همه؛ `PUBLIC`: هیچ |
| sequence | مالک: همه؛ `PUBLIC`: هیچ |

پیامد: «صفر سطر در `aclexplode`» هرگز به‌تنهایی یعنی «دسترسی ندارد». هر محاسبهٔ مؤثر باید `IS NULL` را جداگانه مدل کند. این قاعده در همهٔ دروازه‌ها اعمال می‌شود.

---

## ۲. B-08 — بستار سازندگان مؤثر

تعریف v3.2 («مالک schema + دارندگان مستقیم `CREATE`») ناقص بود. مجموعهٔ سازندگان مؤثر:

| لایه | محاسبه | گزارش |
|---|---|---|
| مستقیم | `has_schema_privilege(r, schema, 'CREATE')` برای هر نقش `r` | `via=direct` |
| ارثی | همان تابع، چون مجوزهای ارث‌برده را هم حساب می‌کند | `via=inherit` (تفکیک با `pg_auth_members.inherit_option`) |
| `SET ROLE` | نقش‌هایی که `pg_has_role(r, g, 'SET')` برای نقشی با `CREATE` دارند | `via=set_role:<g>` |
| مالکیت ضمنی | اعضای `pg_database_owner` | `via=db_owner` |

سپس برای هر سازنده، سطرهای `pg_default_acl` با `defaclrole = سازنده` و `(defaclnamespace = 0 OR = oid(schema))` خوانده می‌شود. اگر سازنده‌ای فقط از مسیر `SET ROLE` می‌تواند بسازد، Default ACL **نقش هدف** (`g`) اعمال می‌شود، نه `r`؛ اما `r` هم به‌صورت محافظه‌کارانه بررسی می‌شود تا اگر بعداً `CREATE` مستقیم گرفت، شکاف باز نشود.

اگر هیچ‌یک از این مسیرها قابل‌محاسبه نبود (مثلاً کاتالوگ ناخوانا) ⇒ `UNCONFIRMED` ⇒ `STOP`، نه PASS.

---

## ۳. B-09 — مدل شش‌مسیرهٔ `EXECUTE`

سه منبع کافی نبود. مدل کامل، با **تفکیک زمینهٔ اجرایی**:

| # | مسیر | محاسبه | زمینه |
|---|---|---|---|
| 1 | grant مستقیم | `aclexplode(proacl)` با `grantee = r` | نشست عادی |
| 2 | ارث‌بری عضویت | بستار با `inherit_option = true` | نشست عادی |
| 3 | `PUBLIC` | سطر `grantee = 0` **یا** `proacl IS NULL` (پیش‌فرض) | نشست عادی |
| 4 | مالکیت تابع | `pg_proc.proowner = r` | ذاتی مالک |
| 5 | superuser | `pg_roles.rolsuper` | سراسری |
| 6 | پس از `SET ROLE` | `pg_has_role(r, g, 'SET')` برای هر `g` با مسیر ۱–۴ | **زمینهٔ جدا** |

قواعد:
- مسیر ۶ هرگز با مسیرهای ۱–۳ **جمع** نمی‌شود؛ ستون جدا با برچسب `context=set_role:<g>` گزارش می‌شود.
- برای **نقش ممیزی**، مسیرهای ۴ و ۵ با `G-OWN` و `G-ROLE` اثباتاً بسته‌اند، مسیرهای ۱ و ۶ با `G-ACL` و `G-MEMBER-OUT` بسته‌اند ⇒ تنها مسیر ۳ باقی می‌ماند؛ همان که با `REVOKE ... FROM PUBLIC` بسته می‌شود. این استدلال فقط برای نقش ممیزی معتبر است.
- برای **سایر نقش‌ها** (تحلیل اثر revoke)، هر شش مسیر محاسبه می‌شود.
- اثر revoke بر `grantor`/`is_grantable`: سطرهای دارای `is_grantable = true` جداگانه چاپ می‌شوند؛ revoke از `PUBLIC` آن‌ها را تغییر نمی‌دهد و باید صریحاً گزارش شود که کدام نقش هنوز از مسیر مستقیم یا ارثی اجرا می‌کند.

---

## ۴. B-10 — `G-PARAM` مؤثر

`G-PARAM` علاوه بر سطرهای مستقیم، مجوز مؤثر را می‌سنجد:

| بررسی | شرط |
|---|---|
| مستقیم | صفر سطر در `pg_parameter_acl` با `grantee = audit_select_v3` |
| مؤثر | `has_parameter_privilege(role, parameter, 'SET') = false` و `... ('ALTER SYSTEM') = false` برای همهٔ پارامترهایی که سطر ACL دارند |
| `PUBLIC` | سطر با `grantee = 0` ⇒ `FAIL` |
| مسیر عضویت | با `G-MEMBER-OUT` پوشش داده می‌شود؛ در `G-PARAM` به‌صورت شاهد تکرار می‌شود |

دامنه: کاتالوگ shared، **کل کلاستر**، بدون محدودسازی schema (چون وجود ندارد).

---

## ۵. B-11 — Inventory مالکیت (جای فهرست هفت‌تایی)

فهرست ثابت هفت‌کاتالوگی نمی‌تواند ادعای «نقش هیچ مالکیتی ندارد» را پشتیبانی کند. Inventory کامل بر اساس کاتالوگ‌های واقعی PG 18:

| گروه | کاتالوگ‌ها (ستون مالک) |
|---|---|
| روابط | `pg_class.relowner` (همهٔ `relkind`ها: جدول، view، matview، sequence، index، composite، TOAST، foreign، partitioned) |
| فضا/دیتابیس | `pg_namespace.nspowner`، `pg_database.datdba`، `pg_tablespace.spcowner` (shared) |
| روتین/نوع | `pg_proc.proowner`، `pg_type.typowner` |
| Extension/LO | `pg_extension.extowner`، `pg_largeobject_metadata.lomowner` |
| عملگر | `pg_operator.oprowner`، `pg_opclass.opcowner`، `pg_opfamily.opfowner`، `pg_am.amowner` |
| تبدیل/تطبیق | `pg_cast.castowner`، `pg_collation.collowner`، `pg_conversion.conowner` |
| جست‌وجوی متنی | `pg_ts_config.cfgowner`، `pg_ts_dict.dictowner`، `pg_ts_parser.prsowner`، `pg_ts_template.tmplowner` |
| زبان | `pg_language.lanowner` |
| خارجی | `pg_foreign_data_wrapper.fdwowner`، `pg_foreign_server.srvowner` |
| تکثیر/رویداد | `pg_publication.pubowner`، `pg_subscription.subowner`، `pg_event_trigger.evtowner` |
| آمار/تبدیل نوع | `pg_statistic_ext.stxowner`، `pg_transform.trfowner` |

قواعد:
1. برای هر کاتالوگ، `owner = oid(audit_select_v3)` ⇒ `FAIL`.
2. مسیرهای مالکیت‌معادل **جدا** گزارش می‌شوند: مالکیت مستقیم (تساوی oid)، `SET ROLE` به نقش مالک، عضویت ضمنی `pg_database_owner`. `INHERIT` مالکیت نمی‌سازد، اما مجوزهای مالک را منتقل می‌کند و در `G-MEMBER-OUT` دیده می‌شود.
3. `pg_user_mapping` مالک ندارد (کلید آن کاربر نگاشت‌شده است) و در Inventory به‌عنوان «بدون مالک» ثبت می‌شود — نه نادیده.
4. هر کاتالوگی که در این نسخهٔ PG/Aiven قابل‌خواندن یا قابل‌ارزیابی نیست ⇒ `UNCONFIRMED` ⇒ `STOP`.

---

## ۶. B-12 — اختیار واقعی هر عملیات در فاز ۱ب

`ADMIN OPTION` فقط مدیریت عضویت نقش‌ها را مجاز می‌کند؛ اختیار عمومی remediation نیست. ماتریس درست:

| عملیات | اختیار لازم | اگر نبود |
|---|---|---|
| `GRANT`/`REVOKE` روی جدول/sequence | مالک شیء، یا دارندهٔ grant option روی آن | B |
| `GRANT`/`REVOKE` روی schema | مالک schema (`pg_database_owner` یا مالک واقعی) | B |
| `GRANT`/`REVOKE` روی دیتابیس | مالک دیتابیس | B |
| `REVOKE EXECUTE` روی تابع | مالک تابع | B |
| `GRANT`/`REVOKE` عضویت | `ADMIN OPTION` روی نقش grant‌شده | B |
| `ALTER ROLE` (صفات/تنظیمات) | `ADMIN OPTION` روی نقش + اختیار همان صفت | B |
| `ALTER DEFAULT PRIVILEGES` | بودن نقش سازنده (یا `SET ROLE` به آن) | B |
| `ALTER EXTENSION` | مالک Extension | B |

فاز ۱ب برای هر اقدام، ستون `required_authority` و `held_authority` را چاپ می‌کند؛ اختلاف ⇒ `STOP`. هیچ اقدام با «`ADMIN OPTION` داریم پس مجازیم» توجیه نمی‌شود.

---

## ۷. B-13 — تفکیک مجوز ستونی

`has_any_column_privilege` برای پاسخ به «آیا SELECT ستونی دارد؟» کافی نیست. دو دروازهٔ مستقل:

| دروازه | بررسی | شرط عبور |
|---|---|---|
| `G-COL-SELECT` | `has_column_privilege(role, attrelid, attnum, 'SELECT')` روی **همهٔ** ستون‌های همهٔ روابط | `true` فقط برای جداول Manifest (که از grant سطح جدول می‌آید)؛ `false` برای بقیه |
| `G-COL-WRITE` | همان تابع برای `INSERT`, `UPDATE`, `REFERENCES` | `false` برای همهٔ ستون‌ها، بدون استثنا |

`G-WRITE` سطح جدول باقی می‌ماند و `G-COL-WRITE` مکمل آن است، نه جایگزین. `attacl` همچنان در `G-ACL` بررسی می‌شود تا grant ستونی اضافی — حتی روی جدول Manifest — دیده شود.

---

## ۸. B-14 — `G-SECDEF` با قواعد آزمون‌پذیر

### ۸.۱ شاهد بازبینی (ثبت اجباری)

برای هر روتین `prosecdef = true` در schemaهای غیرسیستمی، یک رکورد شاهد:

`(routine_oid, prosrc_hash, owner, extension_name, extension_version, proconfig_snapshot, reviewer, review_date, decision, checklist)`

اعتبار شاهد فقط وقتی: `prosrc_hash` فعلی = ثبت‌شده، `owner` بدون تغییر، `extension_version` بدون تغییر. تغییر هر یک ⇒ شاهد **باطل** ⇒ بازبینی مجدد. («شاهد کد» بدون هش نسخهٔ دقیق کد، گیت نیست.)

### ۸.۲ چک‌لیست هر روتین (ثبت‌شده، نه خودکار)

1. تمام ارجاعات رابطه/تابع/نوع schema-qualified هستند.
2. `search_path` پین شده و `pg_temp` **آخر** است، یا `search_path = ''` با شرط ۱.
3. هیچ SQL پویا (`EXECUTE`, `format(`) وجود ندارد؛ در صورت وجود ⇒ بازبینی دستی اجباری.
4. هیچ ارجاعی به `pg_temp`/`pg_toast_temp*` نیست.
5. زنجیرهٔ فراخوانی: هر کالی شناخته‌شده خودش طبقه‌بندی شده است.
6. زبان تابع trusted است یا کدش بازبینی شده.

### ۸.۳ سه کلاس با شرط `PASS`/`FAIL`/`UNCONFIRMED`

| کلاس | `PASS` | `FAIL` | `UNCONFIRMED` |
|---|---|---|---|
| **A — غیرقابل‌اجرا** | نقش ممیزی هیچ‌یک از شش مسیر `EXECUTE` را ندارد | — | — |
| **B — قابل‌اجرا و تأییدشده** | شاهد معتبر + چک‌لیست کامل + `decision=accepted` + پین `pg_temp` آخر یا `''` | شاهد موجود اما چک‌لیست ناقص / `decision=rejected` | شاهد منقضی (هش/مالک/نسخه تغییر کرده) یا بند ۳/۵ قابل‌ارزیابی نیست |
| **C — قابل‌اجرا و تأییدنشده** | — | هر روتین قابل‌اجرا بدون شاهد، یا با پین ناامن | — |

`UNCONFIRMED` ⇒ `STOP`. هیچ روتینی با «به‌نظر امن است» طبقه‌بندی نمی‌شود. توابع مالک `postgres` که نتوان آن‌ها را REVOKE کرد، معمولاً کلاس C ⇒ `FAIL` ⇒ دستهٔ B.

---

## ۹. M-01..M-06

**M-01 `G-MULTIDB`:** Scope Descriptor فیلد `db_policy` می‌گیرد: `allowed_databases = [db_name]`. دروازه، فهرست **کامل** `pg_database` (با `datallowconn`، `datistemplate`، `has_database_privilege(role, d, 'CONNECT')`) را چاپ می‌کند و هر `CONNECT` بیرون از فهرست ⇒ `FAIL`. فهرست بررسی‌شده بخشی از شاهد است، نه یک ادعای کلی.

**M-02 `G-SEQ` آینده‌نگر:** علاوه بر sequenceهای موجود، سطرهای `pg_default_acl` با `defaclobjtype = 'S'` برای سازندگان مؤثر و `grantee IN (0, role)` بررسی می‌شوند. بدون این، یک grant پیش‌فرض آینده می‌تواند `G-SEQ` فعلی را دور بزند.

**M-03 `G-TEMP` عملیاتی:**

| حالت | پیش‌شرط | عملیات | Postcondition |
|---|---|---|---|
| ۱ (قبلاً حذف‌شده) | `datacl` بدون سطر `PUBLIC TEMPORARY` | هیچ تغییری | `has_database_privilege(role,'TEMPORARY')=false` + `has_database_privilege(_aiven_repl,'TEMPORARY')=true` (اگر نقش وجود دارد) |
| ۲ (فعال) | `datacl IS NULL` یا سطر صریح `PUBLIC TEMPORARY` | تحلیل وابستگی → `GRANT` جبرانی برای نقش‌های تأییدشده → `REVOKE` در فاز ۱ | همان postcondition + صفر نقش `LOGIN` ناشناخته |
| ۳ (نامشخص) | دسترسی ناکافی/تناقض | — | `STOP` |

**M-04 `G-OWN` مسیرها:** سه ستون جدا: `direct` (تساوی `owner` با oid نقش)، `inherit` (عضویت با `inherit_option` در نقش مالک — مالکیت نمی‌سازد اما مجوز مالک را می‌دهد)، `set_role` (قابلیت تبدیل به نقش مالک). هر سه در صورت وجود ⇒ `FAIL`، اما با برچسب متفاوت تا اقدام اصلاحی درست انتخاب شود.

**M-05 `G-RLS` هدف صریح:** هدف = **مشاهدهٔ کامل ردیف‌های Manifest**. `relrowsecurity = true` یا `relforcerowsecurity = true` ⇒ `FAIL`، چون نقش نه مالک است نه `BYPASSRLS` و هر policy می‌تواند بی‌صدا ردیف حذف کند. `pg_policies` فقط برای شاهد چاپ می‌شود؛ ارزیابی policy در دامنهٔ v3 نیست (نیازمند بازبینی معنایی است). `FORCE ROW LEVEL SECURITY` هم `FAIL`.

**M-06 Manifest:** علاوه بر هش‌ها، سه شاهد اجباری: `dba_attestation` (نام/تاریخ تأیید فهرست روابط)، `extractor_version`، و `relation_match_evidence` (تطبیق نام‌به‌نام Manifest با `to_regclass` و `relkind`). هش فقط تغییر را تشخیص می‌دهد، نه کامل بودن پوشش.

---

## ۱۰. فهرست واحد دروازه‌ها (v3.3)

| ID | شرط | تغییر |
|---|---|---|
| `G-ROLE` | صفات پاک؛ `rolinherit=false` | — |
| `G-MEMBER-OUT` | صفر یال خروجی؛ `pg_has_role(...,'SET')=false` | — |
| `G-MEMBER-IN` | صفر یال ورودی با چاپ سه گزینه | — |
| `G-OWN` | صفر مالکیت در Inventory کامل + سه مسیر جدا | بازنویسی (B-11/M-04) |
| `G-ACL` | تساوی با Allowlist در Scope Descriptor | — |
| `G-ACL-GRANTABLE` | هیچ `is_grantable=true` | — |
| `G-WRITE` | صفر نوشتن سطح جدول شامل `MAINTAIN` | — |
| `G-SELECT` | `SELECT` جدولی فقط Manifest | — |
| `G-COL-SELECT` | `SELECT` ستونی فقط Manifest | **جدید** (B-13) |
| `G-COL-WRITE` | صفر `INSERT/UPDATE/REFERENCES` ستونی | **جدید** (B-13) |
| `G-SEQ` | صفر privilege + صفر Default ACL `S` | گسترش (M-02) |
| `G-SCHEMA` | `USAGE` فقط schema دامنه؛ صفر `CREATE` | — |
| `G-DB` | `CONNECT` بله؛ `TEMPORARY`/`CREATE` خیر | — |
| `G-MULTIDB` | `CONNECT` فقط دیتابیس دامنه + فهرست شاهد | صریح‌سازی (M-01) |
| `G-TEMP` | حالت ۱/۲ با postcondition | عملیاتی (M-03) |
| `G-SECDEF` | سه‌کلاسهٔ آزمون‌پذیر + شاهد معتبر | بازنویسی (B-14) |
| `G-DEFACL` | سازندگان بستار + سراسری + schema + `PUBLIC` | بازنویسی (B-08) |
| `G-PARAM` | مستقیم + مؤثر | بازنویسی (B-10) |
| `G-LO` | صفر ACL large object | — |
| `G-RLS` | `relrowsecurity/relforcerowsecurity` = false | صریح (M-05) |
| `G-CFG` | `rolconfig` + `RESET ALL` | — |
| `G-LOGIN` | فاز ۲: `rolcanlogin=false` | — |

---

## ۱۱. فازها

| فاز | نوشتن | محتوا |
|---|---|---|
| ۰ Preflight | **خیر** | `P-*`، `plan[]`، `approval_id`، Inventory، شاهد Manifest |
| ۱ DDL + دروازه‌ها | یک تراکنش | ساخت نقش، Allowlist، همهٔ ACLهای تأییدشده |
| ۱ب Remediation | یک تراکنش | اصلاح انحراف با `required_authority` هر اقدام |
| ۲ Verify | خیر | همان دروازه‌ها + `G-LOGIN` |
| ۳ Credential | بیرون فایل | S0–S4 + مسیر R |
| ۴ Audit/Decommission | فقط SELECT | `NOLOGIN`, `PASSWORD NULL`, شمارش نشست |

---

## ۱۲. آزمون‌ها (افزوده‌ها)

| ID | نوع | آزمون | انتظار |
|---|---|---|---|
| T17 | ایستا | `G-OWN` همهٔ ۲۷ کاتالوگ Inventory را پوشش می‌دهد | کامل |
| T18 | ایستا | هیچ دروازه‌ای `ADMIN OPTION` را به‌عنوان اختیار عمومی ACL به کار نمی‌برد | صفر مورد |
| T19 | ایستا | `G-COL-SELECT` و `G-COL-WRITE` هر دو در فاز ۱ و VERIFY | ۱۰۰٪ |
| L29 | زنده | نقش با `CREATE` ارثی روی schema، دارای Default ACL سراسری | `G-DEFACL` آن سازنده را می‌بیند |
| L30 | زنده | مالک تابع بدون سطر `proacl` | مسیر ۴ در محاسبهٔ شش‌مسیره گزارش شود |
| L31 | زنده | `GRANT SET ON PARAMETER` به نقش سوم و دسترسی از طریق عضویت | `G-PARAM` مؤثر FAIL |
| L32 | زنده | `ALTER PUBLICATION ... OWNER TO audit_select_v3` | `G-OWN` FAIL |
| L33 | زنده | شاهد `G-SECDEF` با تغییر یک بایت در `prosrc` | شاهد باطل ⇒ `UNCONFIRMED` ⇒ STOP |
| L34 | زنده | `ALTER DEFAULT PRIVILEGES ... GRANT SELECT` روی `PUBLIC` برای sequence | `G-SEQ` FAIL |
| L35 | زنده | فاز ۱ب با اقدام نیازمند مالکیت شیء که اجراکننده ندارد | `STOP` پیش از اجرا |

---

## ۱۳. دسته‌بندی A/B

**A:** نقش تازه، Allowlist، Inventory مالکیت، ACK ستونی تفکیک‌شده، `G-PARAM` مؤثر، `G-DEFACL` با بستار سازندگان، Manifest با شاهد، فاز ۱ب با ماتریس اختیار، `G-TEMP` عملیاتی، `G-MULTIDB` (وقتی یک DB است).

**B:** `REVOKE EXECUTE FROM PUBLIC` روی توابع `postgres`-owned؛ به‌روزرسانی `aiven_extras`؛ حذف `CONNECT`/`TEMPORARY` از `PUBLIC` روی دیتابیس‌های دیگر؛ `pg_terminate_backend`؛ نقش قدیمی بدون `ADMIN`؛ شاهد HBA؛ هر کاتالوگ Inventory که در Aiven قابل‌ارزیابی نیست.

---

## ۱۴. `UNCONFIRMED` (افزوده‌ها)

| # | فرض |
|---|---|
| U28 | Default ACL سازندگان غیرمستقیم (`SET ROLE`) |
| U29 | مالکیت اشیای گروه عملگر/تبدیل/جست‌وجوی متنی در این کلاستر |
| U30 | سیاست دیتابیس‌های کلاستر و فهرست واقعی آن‌ها |
| U31 | آیا `pg_publication`/`pg_subscription`/`pg_event_trigger` در Aiven قابل‌خواندن‌اند |
| U32 | وجود SQL پویا در روتین‌های `SECURITY DEFINER` |
| U33 | کامل بودن Inventory در نسخهٔ دقیق PG 18.6 نصب‌شده |

---

## ۱۵. حکم

**`BLOCKED`** — تا بازبینی مستقل بعدی و تا بسته‌شدن دستهٔ B اجرایی.

- ۱۳ اصلاح (`B-08..B-14`, `M-01..M-06`) اعمال شد؛ هیچ اصلاح دیگری انجام نشد.
- `v3`، `v3.1`، `v3.2` دست‌نخورده‌اند و هیچ SQL تولید یا اجرا نشده است.
- محدودیت‌های پذیرفته‌شده و تغییرنکرده: نشت ذاتی کاتالوگ، عدم تضمین سرورسطحی read-only، اتمیک نبودن فاز ۳، و وابستگی شاهد HBA به کنسول Aiven.

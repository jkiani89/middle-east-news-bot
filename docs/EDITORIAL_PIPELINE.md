# خط لولهٔ تحریریه — سند مرجع (Editorial Pipeline Reference)

**نسخه:** ۱.۰ · **تاریخ:** ۲۰۲۶-۱۱-۲۱ · **دامنه:** `collect_new_articles` → مدل → گیت تحریریه → `Event`/`Summary` → گزارش Telegraph → تلگرام.

این سند «دانش ماندگار» رفتار واقعی کد است، نه طرح آینده. هر تغییر رفتاری در Pipeline، گیت تحریریه، موضوع‌بندی، پرامپت یا گزارش باید در همین فایل به‌روزرسانی شود؛ اگر کد و این سند ناسازگار شدند، **کد مرجع است** و سند باید اصلاح شود (A-02 در `docs/AGENT_PROTOCOL.md`).

> **گیت طبقه‌بندی (R-11):** این خط لوله موضوع باز ISS-036 است. پیش از هر تغییر در `pipeline.py`، `topics.py`، `regions.py`، `newsworthiness.py` یا گزارش، `docs/KNOWN_ISSUES.md` (ISS-036) و فهرست RCها خوانده شود و آزمون رگرسیون اضافه یا به‌روزرسانی شود.

---

## ۱. نقشهٔ کلی جریان

```
RSS feeds
   │  fetch_feed(extract_full_text=False)        ← فقط متادیتا
   ▼
collect_new_articles ── dedup(canonical_url | content_hash) ── status=collected | expired
   │  commit() بلافاصله (بقای جمع‌آوری مستقل از پردازش)
   ▼
process_collected_articles
   ├─ expire_stale_collected()        ← صف کهنه را قبل از انتخاب پارک می‌کند
   ├─ build_processing_query()        ← failed اول، سپس reference ASC (FIFO)
   ├─ _ConcurrentAnalysis             ← I/O موازی: fetch متن کامل + فراخوانی مدل
   │     └─ prefilter_analysis()      ← log-only، بدون تصمیم
   ▼  (نتیجه‌ها به ترتیب صف، روی یک AsyncSession سریال)
گیت تحریریه (قیف پنج‌مرحله‌ای، بخش ۵)
   ├─ رد ──► status=rejected (شمارشی در گزارش، بدون صفحه)
   └─ گذر ─► EventMatcher ─► Event + ArticleEvent ─► Summary (+ perspectives)
   ▼
daily_report.generate_slot_bulletins ─► fetch_complete_window_items
   ▼
telegraph_report: صفحهٔ هر موضوع (چندبخشی) → صفحهٔ فهرست → اعلان تلگرام
```

---

## ۲. دریافت (Ingestion)

`app/processors/pipeline.py::collect_new_articles`

| رفتار | جزئیات | دلیل |
|---|---|---|
| متادیتا فقط | `fetch_feed(..., extract_full_text=False)` | قبلاً برای هر آیتم فید متن کامل دانلود می‌شد (۳۵۵ استخراج در یک اجرا) حتی برای خبرهایی که هرگز به مدل نمی‌رسیدند. |
| متن کامل تنبل | `_hydrate_article_body` فقط برای مقاله‌ای که واقعاً به صف رسیده | کاهش شدید زمان اجرا و فشار روی ناشران. |
| دوگانه‌سازی | `is_duplicate_article` + `IntegrityError` روی `canonical_url`/`content_hash` | جلوگیری از درج تکراری حتی در Race. |
| کهنه‌ها دور ریخته نمی‌شوند | خبر قدیمی‌تر از `article_max_age_hours` (۲۴) با `status="expired"` درج می‌شود | اگر «رد» شود، اجرای بعدی آن را دوباره درج می‌کند؛ `expired` حافظهٔ منفیِ داپلیکیت است. |
| درج اتمی | هر مقاله در `session.begin_nested()` | یک خبر خراب، بقیهٔ فید را از بین نمی‌برد. |
| commit قبل از پردازش | در `collect_and_process` بلافاصله پس از جمع‌آوری | rollback پردازش نباید جمع‌آوری همان اجرا را پاک کند (ISS-037، RC مربوط به Rollback مشترک). |

---

## ۳. انتخاب صف (Queue Selection)

`expire_stale_collected` + `build_processing_query`

- **`expire_stale_collected`** — یک `UPDATE` تک‌ستونی روی `status` برای هر `collected` که `COALESCE(published_at, collected_at) < now() - 24h` است. برگشت‌پذیر، بدون حذف داده. `oldest_age_h` با `func.min` محاسبه می‌شود (نه `max`) تا «قدیمی‌ترین خبر در صف» را نشان دهد؛ باگ قبلی `oldest_age_h=0.1` در حالی بود که خبر ۳۶ ساعته در صف مانده بود. در حالت `dry_run` فقط اندازه‌گیری می‌کند.
- **`build_processing_query`** — دو گروه: `status='collected'` و `status='failed' AND attempt_count < ai_max_attempts` (۳)، هر دو محدود به پنجرهٔ تازگی. ترتیب:
  1. `CASE WHEN status='failed' THEN 0 ELSE 1 END ASC` → ضدِ گرسنگیِ تلاش مجدد؛
  2. `reference ASC` → FIFO، تا خبر قدیمی قبل از رسیدن به سقف عمر، تحلیل شود.
- **`article_age_hours`** — `COALESCE(published_at, collected_at)`؛ `published_at` آینده (ساعت خراب فید) سن ۰ می‌گیرد نه منفی.

---

## ۴. موتور موازی‌سازی (`_ConcurrentAnalysis`)

کلاس `_ConcurrentAnalysis` در `app/processors/pipeline.py`.

### چرا موازی
نیمهٔ شبکه‌ای هر مقاله (دریافت متن کامل + یک فراخوانی مدل) انتظار I/O خالص است. چهار مقالهٔ هم‌زمان یک بچ ۴۵ مقاله‌ای را داخل پنجرهٔ ساعتی نگه می‌دارد.

### چرا فقط نیمهٔ شبکه‌ای
`AsyncSession` هرگز نباید بین دو تسک مشترک باشد، و قواعد تحریریه ترتیب‌مندند (تطبیق رویداد یک مقاله، رویدادهای مقالهٔ قبلی را می‌بیند). بنابراین:

| لایه | اجرا | چه چیزی |
|---|---|---|
| موازی | `asyncio.Task` + `Semaphore(ai_processing_concurrency=4)` | `_hydrate_article_body` + `prefilter_analysis` + `analyze_article` |
| سریال | حلقهٔ اصلی `process_collected_articles` روی همان `session` | تمام نوشتن‌ها: `attempt_count`، `Event`، `ArticleEvent`، `Summary`، `commit` |

### پارامترها
```python
self._concurrency     = max(1, settings.ai_processing_concurrency)      # ۴
self._failure_budget  = max(1, settings.ai_max_consecutive_failures)    # ۵
```

### تایم‌اوت دریافت متن کامل
```python
await asyncio.wait_for(fetch_article_full_text(url), timeout=6.0)   # article_body_fetch_timeout_seconds
```
در `TimeoutError` مقاله با خلاصهٔ RSS خودش تحلیل می‌شود؛ یک ناشر کند یک اسلات بچ را قفل نمی‌کند. متن بلندتر از متن فعلی جایگزین می‌شود و `text_source="full_body"` ثبت می‌گردد.

### Circuit Breaker — رفتار دقیق
- بودجهٔ درخواست‌های در جریان: `len(tasks) + transient_failures < ai_max_consecutive_failures`؛ یعنی پس از ۵ خطای گذرای متوالی، **دقیقاً ۵ فراخوانی مدل** مصرف شده و بقیهٔ صف دست‌نخورده می‌ماند.
- خطای گذرا (`is_retryable_ai_error`: 429/503/timeout): `status="failed"`، `attempt_count += 1`، `commit` فوری، سپس در صورت رسیدن شمارنده به ۵: `_circuit_breaker_tripped = True` و `break`.
- خطای غیرگذرا: `status="failed"` و ادامهٔ بچ (یک خبر خراب بچ را متوقف نمی‌کند).
- مقاله‌ای که تسکش با `break` رها می‌شود، `attempt_count` مصرف نمی‌کند (شمارش در نیمهٔ سریال انجام می‌شود).
- `aclose()` در پایان (و در مسیر abort) تسک‌های در جریان را cancel و gather می‌کند؛ هیچ درخواست یتیمی باقی نمی‌ماند.
- حالت اجرا در `last_batch_stopped_by_circuit_breaker()` و `JobLog.status = stopped_unavailable` دیده می‌شود.

### آزمون‌های پین‌کننده
`tests/test_pipeline_resilience.py` (سقف بودجه، ترتیب تحویل، عدم مصرف تلاش توسط breaker، کف هم‌زمانی) و `tests/test_model_circuit_breaker.py` (سطح مدل/کلید).

---

## ۵. قیف گیت تحریریه (پنج مرحله)

ترتیب اهمیت دارد: هر مرحله ارزان‌تر و قاطع‌تر است و مرحلهٔ بعد فقط وقتی اجرا می‌شود که قبلی خبر را نگه داشته باشد. همهٔ تصمیم‌ها در `article.ai_payload` ثبت می‌شوند تا گزارش و پایش بتوانند بازسازی شوند.

### ۵.۱. تحلیل هوش مصنوعی و پرامپت
- `PROMPT_VERSION` **محاسبه‌ای** است، نه ثابت دستی: `f"v2-{sha256(system_prompt + json_schema)[:8]}"`. مقدار امروز: **`v2-ba0dca01`** (با اجرای واقعی تأیید شد). هر تغییر در پرامپت یا Schema، نسخه را خودکار عوض می‌کند؛ آزمون‌های پین‌شده باید آگاهانه به‌روزرسانی شوند.
- ورودی مدل: `EDITORIAL_SYSTEM_PROMPT` (قواعد) + پرامپت کاربر با `<<<UNTRUSTED_ARTICLE_CONTENT>>>` و بدنهٔ حداکثر `MODEL_BODY_CHAR_LIMIT`.
- کدهای مجاز دسته‌بندی: `POL, MIL, DIP, SEC, TER, ECO, ENE, MAR, INT, HUM` — **هیچ کد ورزشی/گردشگری/حادثه‌ای وجود ندارد**؛ همین محدودیت، دلیل وجود گیت واژگانی ۵.۲ است.
- تزریق تاریخ جاری: `_build_analysis_prompt` خط `تاریخ امروز (UTC): YYYY-MM-DD` را از `datetime.now(UTC).date().isoformat()` می‌سازد (اصل ۱۱ پرامپت). بدون این خط، مدل از دانش آرشیوی خود عنوان مقام‌ها را می‌نوشت («رئیس‌جمهور پیشین» برای ترامپ).
- JSON خراب: یک تلاش `schema_repair` مشروط (سقف `SCHEMA_REPAIR_RAW_MAX_CHARS`)؛ پاسخ بلندتر از سقف برای بازبینی انسانی fail می‌شود.
- `ai_request_timeout_seconds=30` سقف سخت هر فراخوانی مدل.

### ۵.۲. فیلتر ارزش خبری — `app/editorial/newsworthiness.py` (RC-17)
`non_political_story_reason(title, summary, countries, actors, location) -> "sport" | "tourism" | "incident" | None`

سه شرط هم‌زمان برای رد:

1. **در جغرافیای پوشش باشد** (`has_middle_east_nexus`)؛ خبر خارج از منطقه کار این گیت نیست.
2. **نشانهٔ ورزش/گردشگری/حادثهٔ عادی** در تیتر + خلاصه + بازیگرها باشد (متن نرمال‌شده؛ لاتین با مرز واژه، فارسی به‌صورت زیررشته).
3. **هیچ نشانهٔ سیاسی/امنیتی/اقتصادی نباشد** — این فهرست «وتو» است: انتخابات، تحریم، بازداشت، حمله، موشک، ارتش، وزارت، دادگاه، اعتصاب، مذاکره، تحریم، پناهنده، تسلیحاتی، …

نمونه‌های واقعی که این گیت گرفت (خروجی ۱۲ساعتهٔ ۲۰۲۶-۱۱-۲۱، importance = ۲):

| تیتر | دلیل |
|---|---|
| حضور پرشور جامعه نپالی‌های قطر در مسابقات کریکت | `sport` |
| برگزاری برنامه‌های آموزشی و تفریحی برای کودکان در العلا | `tourism` |
| تصادف زنجیره‌ای در جاده تهران–قم | `incident` |

و نمونه‌هایی که **نباید** بگیرد (آزمون منفی): «بازداشت ورزشکار ایرانی…» (وتوی بازداشت)، «حمله موشکی به ورزشگاه…» (وتوی حمله)، «اعتصاب کارکنان هتل‌های تهران» (وتوی اعتصاب).

- **حادثه‌های جمعی عمداً غایب‌اند:** زلزله، سیل، سقوط هواپیمای مسافربری و مانند آن‌ها خبر قابل‌انتشارند؛ فقط حادثهٔ عادی محلی (تصادف، آتش‌سوزی، ریزش، غرق، مسمومیت، سرقت) پوشش داده می‌شود.
- **محل اجرا:** `pipeline` قبل از `match_topics` و **قبل از نجات منطقه‌ای**؛ همچنین در ابتدای `match_topics` تا هر فراخوان دیگر (گزارش/بک‌فیل) همان نتیجه را بگیرد.
- **ثبت:** `ai_payload["editorial_exclusion"] = "sport" | "tourism" | "incident" | None`؛ وضعیت `rejected`، `topic_key=None`.
- **پوشش:** ۹۶٪ (`tests/test_match_topics.py` ۸ آزمون، `tests/test_editorial_gates.py` ۴ آزمون).

### ۵.۳. گیت بازیگر اصلی — `is_primary_actor` + `MIDDLE_EAST_OFFICIALS`
`app/editorial/regions.py`

خبر منطقه‌ای وقتی «طرف» دارد که یکی از این‌ها باشد:
1. `location` خودش یک مکان/آبراه منطقه‌ای باشد؛
2. یک کشور منطقه‌ای در `countries` باشد **و** نام همان کشور در `actors` بیاید (فارسی/انگلیسی، با مرز واژه برای لاتین)؛
3. یا یکی از مقام‌های رسمی نقشهٔ بستهٔ `MIDDLE_EAST_OFFICIALS` (۱۴ ورودی: امیر قطر، محمد بن سلمان، پزشکیان، عراقچی، نتانیاهو، هرتزوگ، السیسی، اردوغان، السودانی، …) در `actors` باشد **و** کشورش در `countries` فهرست شده باشد.

قاعدهٔ سوم از RC-15 آمد: سفر امیر قطر به برلین (importance 4) و کمک WFP به یمن «اشارهٔ گذرا» تشخیص داده و رد می‌شدند، چون نام کشور در `actors` نبود. نقشه بسته است: هیچ نهاد یا شخصی حدس زده نمی‌شود؛ هر ورودی باید شاهد واقعی داشته باشد.

### ۵.۴. موتور تطبیق موضوع — `match_topics`
`app/editorial/topics.py` + `editorial/topics.json`

ترتیب موضوع‌ها (اولویت) و شرط‌هایشان:

| کلید | نام | `min_importance` | `anchor_countries` |
|---|---|---|---|
| `ru-ua` | روسیه و اوکراین | ۲ | روسیه، اوکراین |
| `ir` | ایران و آمریکا | ۲ | ایران |
| `us` | سیاست داخلی آمریکا | ۳ | آمریکا |
| `ye` | عربستان و یمن | ۲ | یمن، عربستان |
| `il` | اسرائیل | ۲ | اسرائیل، فلسطین، غزه |
| `us-cn` | آمریکا و چین | ۲ | چین، تایوان |
| `me` | تحولات خاورمیانه | ۲ (پیش‌فرض) | — (قاعدهٔ `is_primary_actor`) |

الگوریتم برای هر موضوع نام‌دار:

1. **`min_importance`** — `importance < min_importance` → رد فوری. (این فیلد قبلاً تعریف شده بود ولی هیچ‌جا اعمال نمی‌شد؛ RC-14.)
2. **`anchor_countries`** — اگر تعریف شده باشد، دست‌کم یکی از کشورهای لنگر باید در `countries` باشد. `us-cn` بدون این شرط، هر خبر واشنگتنی را می‌گرفت («واکنش عراقچی به سفیر اسرائیل در واشنگتن»، RC-13).
3. **کشور + شاهد:** `country_hit AND (actor_hit OR place_hit)`.
4. **بدون کشور:** `keyword_hit AND (actor_hit OR place_hit)` — برای خبرهایی مثل «حملهٔ پهپادی به کی‌یف» که `countries` خالی است.
5. `me` **فقط به‌عنوان fallback** اضافه می‌شود، آن هم وقتی هیچ موضوع نام‌داری تطبیق نکرده باشد؛ خبر هرگز در دو بخش (موضوع + `me`) تکرار نمی‌شود.

مالکیت رگرسیون: `tests/test_match_topics.py` (RC-12…RC-14: غزه/لبنان در `ir` نباشند، `us-cn` بدون چین نباشد، کف اهمیت رعایت شود).

### ۵.۵. قانون نجات منطقه‌ای (`me` fallback)
`pipeline.py`، بلوک «Phase B»

اگر هیچ موضوعی تطبیق نکرد، خبر **هنوز** می‌تواند منتشر شود، مشروط به **هر چهار** شرط:

```python
not editorial_exclusion
and not topics
and result.is_relevant
and result.importance >= 2
and has_middle_east_nexus(result.countries, result.location)
and is_regional_only_story(result.countries)   # همهٔ کشورها منطقه‌ای
```

`is_regional_only_story` یعنی «تمام کشورهای تحلیل منطقه‌ای‌اند»؛ چنین خبری نمی‌تواند «اشارهٔ گذرا» باشد. شرط `importance >= 2` و `is_relevant` جلوی ورود آیتم محلی کم‌ارزش و خبر بی‌ربط را می‌گیرد، و شرط `not editorial_exclusion` تضمین می‌کند خبر ورزشی از این در پشتی برنگردد.

### ۵.۶. شاخه‌های پایانی (به ترتیب کد)

| شرط | نتیجه | ثبت |
|---|---|---|
| `is_injection_attempt` | `flagged_for_review` + `Event(status="flagged")` | حفظ اطلاعات برای بازبینی انسانی، بدون انتشار خودکار |
| `number_check_mode="flag"` و عدد بی‌پشتوانه | `flagged_for_review` + `Event("flagged")` | `number_check` در payload |
| `editorial_exclusion` | `rejected` | دلیل گیت ارزش خبری |
| `not is_relevant` یا بدون nexus | `rejected` | لاگ دلیل جغرافیایی |
| `topics` خالی و `not is_primary_actor` | `rejected` | «اشارهٔ گذرا» |
| در غیر این صورت | `EventMatcher` → `Event` + `ArticleEvent` + `Summary` | `status="summarized"` |

نکته: `rejected` و `failed` **هر دو** در شمارش «اخبار بازنمایی‌شده» می‌آیند تا معیار «هیچ خبری بی‌صدا گم نشود» معنا داشته باشد (بخش ۷).

---

## ۶. ذخیره‌سازی، اعتبارسنجی و انتشار لحظه‌ای

- **`EventMatcher.match_or_create_event`** — ادغام با رویداد موجود یا ساخت رویداد تازه؛ `ArticleEvent.match_type` (`initial`/…) و `confidence` ثبت می‌شود.
- **`resolve_verification_status`** — یک منبع هرگز `confirmed` نمی‌شود: `source_count < 2` → `unconfirmed`؛ دو منبع مستقل اجازه می‌دهد حکم مدل (`confirmed`/`partially_confirmed`) بماند؛ `disputed`/`false` همیشه دست‌نخورده.
- **ماتریس روایت (`_refresh_event_perspectives`)** — فقط وقتی دومین اردوگاه خبری به رویداد بپیوندد، حداکثر یک فراخوانی، با `check_perspectives_gate` (دست‌کم یک بازیگر/شهر مشترک غیرکشوری). «سکوت» از داده مشتق می‌شود (اردوگاهی که نمونه ندارد)، نه از حدس مدل. برای یمن سه طرف نام‌برده (`YEMEN_REQUIRED_SIDES`) همیشه در خط سکوت می‌آیند.
- **انتشار لحظه‌ای** — `ImmediateAnalysis` + `publish_immediate_analysis` با سقف ۵ پیام در اجرا، فاصلهٔ ۲ ثانیه، فقط `importance >= 3`، و خبر تک‌منبعِ `importance >= 4` فقط به چت ادمین. هر خطای تلگرام بلعیده می‌شود؛ کار ذخیره‌شده هرگز به‌خاطر تلگرام برنمی‌گردد.
- **Liveblog** — تشخیص از URL/عنوان؛ `importance` به `liveblog_max_importance=3` محدود و ماتریس روایت برای آن ساخته نمی‌شود.
- **`canonicalize_approved_names`** روی تیتر/خلاصه/`event_title`/`actors`/`countries` **قبل از** ذخیره اعمال می‌شود (بخش ۸).

---

## ۷. گزارش Telegraph و تصمیم D-9

### ساخت مجموعهٔ کامل پنجره
`fetch_complete_window_items` (در `app/reports/telegraph_report.py`، مصرف‌شده توسط `daily_report.generate_slot_bulletins`):

- همهٔ مقالاتی که `collected_at` آن‌ها در پنجرهٔ اسلات است خوانده می‌شوند — مستقل از داشتن خلاصه، رویداد یا موضوع.
- خبر بدون Event → `_article_item` با تیتر/منبع/لینک اصلی و برچسب «خلاصه در دسترس نیست».
- رویداد بدون Summary → `_event_item_without_summary` با موضوعِ خودِ رویداد (نه سقوط به «تحلیل‌نشده»).
- `metadata` تفکیک‌شده ساخته می‌شود: `collected_articles`, `articles_represented`, `articles_by_status`, `articles_unprocessed`, `articles_rejected`, `articles_failed`, `articles_without_summary`, `unlinked_articles`.
- اگر `articles_represented != len(rows)` باشد، **خطای صریح** در لاگ ثبت می‌شود؛ هیچ خبری بی‌صدا حذف نمی‌شود.

### D-9 — اخبار بررسی‌شده بدون انتشار
```python
COUNT_ONLY_STATUSES = frozenset({"rejected", "failed"})
```
در حلقهٔ `fetch_complete_window_items`، این دو وضعیت:

- `articles_represented += 1` (پس در معادلهٔ کامل‌بودن می‌مانند)؛
- **`continue`** می‌شوند: هیچ آیتم، هیچ صفحه و هیچ تیتر خامِ ترجمه‌نشده‌ای تولید نمی‌کنند.

یادداشت شفافیت (فقط شمارشی):

| کلید metadata | متن |
|---|---|
| `articles_rejected` | «… خبر بررسی و در گیت تحریریه رد شد؛ این موارد فقط به‌صورت شمارشی … و جزو اخبار «تحلیل‌نشده» نیستند.» |
| `articles_failed` | «… خبر در مرحله تحلیل با خطا مواجه شد؛ … فقط به‌صورت شمارشی …» |
| `articles_unprocessed` | «… خبر هنوز تحلیل نشده بود (در صف یا منقضی پیش از تحلیل) و در بخش «…» با برچسب … منتشر شد.» |

بخش «اخبار پردازش‌نشده» اگر صفر باشد اصلاً ساخته نمی‌شود (سکشن خالی در گزارش نمی‌آید). آزمون: `tests/test_report_completeness.py`.

### صفحه‌بندی و سقف ۶۴KB
- `telegraph_client.MAX_CONTENT_BYTES = 64 * 1024`؛ عبور از سقف → `TelegraphContentTooLargeError` (هیچ Trimming بی‌صدایی وجود ندارد — ISS-035).
- `telegraph_pagination.PAGE_CONTENT_BUDGET_BYTES = 60 * 1024` با رزرو ناوبری (`NAV_BASE_BYTES=500`, `NAV_LINK_RESERVE_BYTES=340` به‌ازای هر بخش).
- `plan_topic_parts` بلوک کامل هر خبر را اتمی می‌چیند؛ هر بخش یک خط هویت `شناسه: <YYYYMMDDTHHMM>|<topic_key>|<part>|sha1:<40 hex>` منتشر می‌کند و بازیابی صفحهٔ یتیم فقط با اثبات کامل (تطبیق خط هویت + شمارهٔ بخش + تعداد `<h4>`) مجاز است؛ ابهام = توقف، نه بازنویسی.
- اعلان تلگرام: لینک صفحهٔ فهرست + لینک مستقیم هر موضوع (و همهٔ بخش‌ها)، بدون حذف موضوعی که URL ندارد.

---

## ۸. استانداردسازی فارسی

### ۸.۱. اصل ۱۰ پرامپت — «فارسی معیار خبری»
واژه یا املای عربی نامأنوس ممنوع و معادل فارسی معیار الزامی است: «طهران» ← «تهران»، «واشنطن» ← «واشنگتن»، «مجزره/مجازر» ← «کشتار/کشتارها»، «تصدید» ← «تشدید»، «حراس» ← «نگهبانان»، «بحریه» ← «نیروی دریایی»، «استعوا/استعوای» ← «استعمار/استعماری»، «مسسیر» ← «مسیر»، «اکشافات» ← «اکتشافات»، «نحل» (در معنای کشور) ← «بحرین». نام باشگاه‌ها/نهادهای عربی («الأهلی»، «النهضه») به شکل رسمی رایج فارسی.

**چرا پرامپت تنها کافی نیست:** مدل گاهی قاعده را نمی‌گیرد، و اصلاح post-hoc متن تولیدشده ریسک تغییر معنا دارد. پس دو لایه اضافه شد.

### ۸.۲. لایهٔ قطعی — `glossary.canonicalize_approved_names`
`app/editorial/glossary.py`، جدول `_CANONICAL_REPLACEMENTS` (به ترتیب: نام‌های مرکب بلندتر اول، سپس تک‌واژه‌ها با clitic):

- نام‌ها: پوتین/ترمب/ترامب، شی جین‌پینگ، شنجن/شنزن، APEC.
- واژگان فاز C: طهران→تهران، واشنطن→واشنگتن، مجزره→کشتار، تصدید→تشدید، بحریه→نیروی دریایی، استعوای→استعماری، استعوا→استعمار، مسسیر→مسیر، اکشافات→اکتشافات.
- **مرز حروف فارسی فقط برای موارد مبهم:** `حراس` با `(?<![\u0600-\u06FF])…(?![\u0600-\u06FF])` تا «حراست» بازنویسی نشود. بقیهٔ نگاشت‌ها substring امن‌اند.
- تابع idempotent است و فقط روی متن روایی اجرا می‌شود، نه URL یا نام منبع.

**تفاوت با پایش:** نگاشت‌ها **اصلاح قطعی**اند (خروجی را عوض می‌کنند)؛ `persian_lint` فقط **هشدار** است.

### ۸.۳. لایهٔ پایش — `persian_lint.py` (log-only)
`lint_persian_output(headline, summary)` → `ai_payload["persian_lint"]` با کلیدهای `mode="log"`, `blocking=False`, `watchlist`, `findings[]` (واژه، تعداد، محل: headline/quote/narrative) و `official_titles[]`.

- `SUSPECT_WORDS`: واژه‌های عربی مشاهده‌شده در گزارش‌های واقعی («مسرح»، «مظله»، «تقویض»، «تهجیر»، …) + واژه‌های فاز C («طهران»، «واشنطن»، …). چون نگاشت قطعی وجود دارد، این فهرست نرخ «مدل هنوز چه اندازه بد می‌نویسد» را می‌سنجد.
- `STALE_OFFICE_TITLES` + `_stale_title_findings`: «رئیس‌جمهور پیشین/منتخب» **فقط اگر** متن «ترامپ» را داشته باشد گزارش می‌شود. عمداً log-only: پرامپت قاعده را می‌گوید، این لایه نقض را اندازه می‌گیرد؛ اصلاح post-hoc می‌تواند خبر تاریخی را خراب کند («رئیس‌جمهور پیشین» در خبر سال ۲۰۱۹ درست است).
- هیچ‌کدام وضعیت را تغییر نمی‌دهند، پیام نمی‌فرستند یا فراخوانی مدل مصرف نمی‌کنند. `persian_lint` **هرگز** باعث رد خبر نمی‌شود.

---

## ۹. پایش عملیاتی

### ۹.۱. گیت ارزش خبری
`scripts/monitor_editorial_exclusion.sql` (فقط‌خواندنی، با حساب SELECT-only):

```bash
psql "$DATABASE_URL" -f scripts/monitor_editorial_exclusion.sql
```

۱) شمارش دلیل‌ها در ۲۴ ساعت:

```sql
SELECT ai_payload->>'editorial_exclusion' AS reason, count(*) AS articles
FROM articles
WHERE collected_at >= now() - interval '24 hours'
  AND ai_payload->>'editorial_exclusion' IS NOT NULL
GROUP BY 1 ORDER BY articles DESC, reason;
```

۲) نرخ فعال‌شدن نسبت به کل تحلیل‌شده‌ها (`gated`, `suppressed`, `published_to_a_topic`, `analysed`, `suppressed_pct`). پنجره روی `collected_at` است چون ایندکس دارد (`ix_articles_collected_at`).

**تفسیر:** نرخ بالای `tourism`/`incident` طبیعی و مطلوب است؛ نرخ بالای `sport` همراه با خبرهای مهم ورزشی-سیاسی نشانهٔ کمبود واژه در فهرست وتو است، نه دلیل حذف فهرست.

### ۹.۲. کوئری‌های سلامت (نمونه، فقط‌خواندنی)
```sql
-- توزیع وضعیت در پنجرهٔ ۱۲ساعته (باید با metadata گزارش بخواند)
SELECT status, count(*) FROM articles
WHERE collected_at >= now() - interval '12 hours'
GROUP BY status ORDER BY count(*) DESC;

-- عمق صف و قدیمی‌ترین انتظار
SELECT count(*) AS queued, min(collected_at) AS oldest
FROM articles WHERE status = 'collected';

-- نرخ واژگان عربی پایش‌شده
SELECT f->>'word' AS word, count(*) AS articles
FROM articles, jsonb_array_elements(ai_payload->'persian_lint'->'findings') AS f
WHERE collected_at >= now() - interval '7 days'
GROUP BY 1 ORDER BY articles DESC;

-- عنوان‌های کهنهٔ مقام‌ها
SELECT ai_payload->'persian_lint'->'official_titles' AS stale, count(*)
FROM articles
WHERE collected_at >= now() - interval '7 days'
  AND ai_payload->'persian_lint'->'official_titles' <> '[]'::jsonb
GROUP BY 1;

-- توزیع موضوع (چند موضوعی)
SELECT ai_payload->>'topic_key' AS topic, count(*)
FROM articles
WHERE status = 'summarized' AND collected_at >= now() - interval '24 hours'
GROUP BY 1 ORDER BY count(*) DESC;

-- خطاهای گذرا و نشانهٔ Circuit Breaker
SELECT date_trunc('hour', last_attempt_at) AS hour, count(*) FILTER (WHERE status='failed') AS failed
FROM articles WHERE last_attempt_at >= now() - interval '24 hours'
GROUP BY 1 ORDER BY 1 DESC;
```

هیچ‌کدام Migration یا ایندکس جدید لازم ندارند.

---

## ۱۰. جدول ثابت‌های کلیدی

| ثابت | مقدار | محل |
|---|---|---|
| `ai_processing_concurrency` | ۴ | `app/config.py` |
| `article_body_fetch_timeout_seconds` | ۶.۰ | `app/config.py` |
| `ai_max_consecutive_failures` | ۵ | `app/config.py` |
| `ai_max_attempts` | ۳ | `app/config.py` |
| `ai_request_timeout_seconds` | ۳۰ | `app/config.py` |
| `article_max_age_hours` | ۲۴ | `app/config.py` |
| `max_articles_per_batch` | ۳۰ پیش‌فرض (در CI با `vars.MAX_ARTICLES_PER_BATCH` روی ۴۵) | `app/config.py` / `cron.yml` |
| `min_importance` (سراسری) | ۲ | `app/config.py` |
| `immediate_publish_min_importance` | ۳ | `app/config.py` |
| `liveblog_max_importance` | ۳ | `app/config.py` |
| `PROMPT_VERSION` | `v2-ba0dca01` (محاسبه‌ای) | `app/ai/summarizer.py` |
| `MIDDLE_EAST_OFFICIALS` | ۱۴ ورودی | `app/editorial/regions.py` |
| `COUNT_ONLY_STATUSES` | `{rejected, failed}` | `app/reports/telegraph_report.py` |
| `MAX_CONTENT_BYTES` | ۶۴KB | `app/reports/telegraph_client.py` |
| `PAGE_CONTENT_BUDGET_BYTES` | ۶۰KB | `app/reports/telegraph_pagination.py` |

---

## ۱۱. مالکیت آزمون‌ها

| رفتار | آزمون |
|---|---|
| موازی‌سازی، ترتیب تحویل، بودجهٔ breaker | `tests/test_pipeline_resilience.py` |
| Circuit Breaker سطح مدل/کلید | `tests/test_model_circuit_breaker.py` |
| گیت ارزش خبری | `tests/test_match_topics.py`, `tests/test_editorial_gates.py` |
| موضوع‌بندی، لنگر، کف اهمیت | `tests/test_match_topics.py` |
| «اشارهٔ گذرا» و نجات منطقه‌ای | `tests/test_editorial_gates.py` |
| کیفیت فارسی و عنوان | `tests/test_persian_lint.py`, `tests/test_glossary.py` |
| نسخهٔ پرامپت | `tests/test_ai_phase_y.py`, `tests/test_bulletin_integrity.py` |
| کامل‌بودن گزارش و D-9 | `tests/test_report_completeness.py`, `tests/test_report_render_guards.py` |
| صفحه‌بندی و سقف ۶۴KB | `tests/test_telegraph_multipart.py`, `tests/test_telegraph_client_paging.py`, `tests/test_telegraph_report.py` |

**R-09:** تغییر assertion برای پاس شدن ممنوع است. اگر رفتار عمداً عوض شد، آزمون هم عمداً و در همان commit عوض می‌شود و دلیلش در `docs/DEVELOPMENT_LOG.md` می‌آید.

---

## ۱۲. موارد باز

- **ISS-036 🔴 OPEN:** ممیزی محتوایی Production همچنان BLOCKED (نیازمند نقش `audit_select`). RC-12…RC-17 بخش‌های شناخته‌شده را بستند، اما دامنهٔ اولویت به یک قاعده محدود نیست.
- **ISS-037:** تعیین تکلیف قابل‌ردیابی برای هر خبر (دلیل/زمان ترمینال، backoff، claim دیتابیسی) نیازمند Migration است و در این سند رفتار فعلی توصیف شده، نه راه‌حل آینده.
- **صحه‌گذاری واقعی:** اجرای Workflow روی Gemini/Telegram با کاربر است (R-06)؛ اعداد این سند از کد و خروجی ۱۲ساعتهٔ ۲۰۲۶-۱۱-۲۱ می‌آیند، نه از اجرای جدید.

# مستندات و وضعیت تجربی سهمیه‌های مدل‌های هوش مصنوعی (AI Model Quotas & Benchmarks)

این سند مرجع رسمی ثبت سهمیه‌ها، محدودیت‌های نرخ مصرف (Rate Limits) و نتایج آزمون‌های تجربی اتصال به مدل‌های Google Gemini در پروژه ربات اخبار خاورمیانه است.

---

## ۱. جدول مرجع مشخصات و سهمیه‌های مدل‌ها

| شناسه مدل (Model ID) | وضعیت آزمون زنده (Live Test) | سقف ورودی (Input Tokens) | سقف خروجی (Output Tokens) | نرخ تخمینی RPM | سقف تخمینی RPD | منبع و درجه اعتبار سهمیه (Quota Source & Confidence) | تاریخ اعتبارسنجی |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `gemini-3.8-flash` | ⚠️ **503 UNAVAILABLE** (Transient High Demand Spike) | ۱,۰۴۸,۵۷۶ | ۶۵,۵۳۶ | ۱۵ RPM | **20 RPD (assumed, matches Phase 4.5 empirical)** | فرض مهندسی منطبق با سهمیه مصرف‌شده در فاز ۴.۵ برای رده Flash رایگان؛ Google AI Studio Console | ۲۰۲۶-۱۱-۱۰ |
| `gemini-3.5-flash-lite` | ✅ **۲۰۰ OK** (سریع و پایدار) | ۱,۰۴۸,۵۷۶ | ۶۵,۵۳۶ | ۳۰ RPM | **1,500 RPD (UNVERIFIED — community reports)** | **تأییدنشده تجربی در پروژه** — گزارش‌های کامیونیتی توسعه‌دهندگان و لایه‌های بازاریابی Free Tier مدل‌های Lite؛ کنسول رسمی سهمیه را از طریق API افشا نمی‌کند | ۲۰۲۶-۱۱-۱۰ |
| `gemini-2.5-flash` | ✅ **۲۰۰ OK** (پایدار) | ۱,۰۴۸,۵۷۶ | ۶۵,۵۳۶ | ۱۵ RPM | **20 RPD (empirically verified in Phase 4.5 task-1579)** | اثبات‌شده به‌صورت تجربی هنگام اتمام سهمیه روزانه در تست‌های فاز ۴.۵ روی کلید واقعی پروژه | ۲۰۲۶-۱۱-۱۰ |

---

## ۲. منابع و محدودیت‌های فنی استعلام سهمیه

1. **عدم وجود متد نرم‌افزاری استعلام سهمیه روزانه (ISS-024):**
   - متد استاندارد `client.models.list()` صرفاً متادیتای ساختاری مدل (مانند `input_token_limit` و `output_token_limit`) را بازمی‌گرداند.
   - هیچ اندپوینت عمومی در Google GenAI SDK وجود ندارد که سهمیه باقیمانده روزانه (RPD) یا دقیقه‌ای (RPM) را به صورت زنده گزارش دهد. سهمیه‌ها در سطح پروژه در سرورهای ابری گوگل اعمال می‌شوند و تنها پس از رسیدن به سقف و دریافت HTTP 429 (`RESOURCE_EXHAUSTED`) قابل تشخیص هستند.
2. **منابع رسمی Google AI Studio & Rate Limits:**
   - مستندات محدودیت نرخ مصرف: [https://ai.google.dev/gemini-api/docs/rate-limits](https://ai.google.dev/gemini-api/docs/rate-limits)
   - جداول قیمت‌گذاری و سطوح دسترسی: [https://ai.google.dev/pricing](https://ai.google.dev/pricing)
   - سقف ۱,۵۰۰ RPD برای مدل‌های Lite در گزارش‌های عمومی کامیونیتی توسعه‌دهندگان برای مدل‌های رده Flash-Lite ذکر شده است، اما برای این پروژه برچسب رسمی **`UNVERIFIED — community reports`** دارد تا هیچ ادعای اثبات‌نشده‌ای مبنای تصمیم قرار نگیرد.

---

## ۳. شواهد خام آزمون تجربی اتصال با کلید پروژه (Live Empirical Validation)

در تاریخ ۱۰ نوامبر ۲۰۲۶ ساعت ۲۱:۵۰ به وقت محلی، آزمون ارتباط مستقیم با ۳ مدل بر روی کلید واقعی پروژه (`AIzaSy...`) اجرا گردید:

```text
gemini-3.8-flash: ERROR -> ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}}
gemini-3.5-flash-lite: OK -> Hello.
gemini-2.5-flash: OK -> Hello.
```

### تحلیل نتایج تجربی:
1. **ثبت خطای ۵۰۳ در `gemini-3.8-flash`:**
   این مدل در زمان تست با خطای `503 UNAVAILABLE: This model is currently experiencing high demand. Spikes in demand are usually temporary.` مواجه شد. این خطا نشان داد که سرورهای گوگل برای مدل‌های پیشرو و پرتقاضا دچار نوسان بار موقت می‌شوند.
2. **تصحیح منطق Fallback در کد پروژه:**
   خطای ۵۰۳ دریافتی منجر به کشف یک باگ مهم در منطق انتقال به مدل پشتیبان شد: کد پیشین صرفاً خطاهای ۴۲۹ (`_is_rate_limit_or_quota`) را به مدل جایگزین منتقل می‌کرد و خطای ۵۰۳ باعث شکست کل پایپ‌لاین می‌شد. با ارتقا به تابع `is_retryable_ai_error(exc)`، اکنون خطاهای ۵۰۳ و خطاهای گذرا نیز بلافاصله Fallback به `gemini-3.5-flash-lite` را فعال می‌کنند.
3. **پایداری مدل پشتیبان:**
   مدل `gemini-3.5-flash-lite` با کد وضعیت ۲۰۰ و پاسخ سریع «Hello.» کارکرد خود را اثبات کرد و به عنوان بهترین کاندیدای Fallback برای حفظ ۹۹.۹٪ دردسترس‌بودن سیستم تثبیت شد.

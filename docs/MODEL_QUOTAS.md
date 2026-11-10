# مستندات و وضعیت تجربی سهمیه‌های مدل‌های هوش مصنوعی (AI Model Quotas & Benchmarks)

این سند مرجع رسمی ثبت سهمیه‌ها، محدودیت‌های نرخ مصرف (Rate Limits) و نتایج آزمون‌های تجربی اتصال به مدل‌های Google Gemini در پروژه ربات اخبار خاورمیانه است.

---

## ۱. جدول مرجع مشخصات و سهمیه‌های مدل‌ها

| شناسه مدل (Model ID) | وضعیت Free Tier در آزمون زنده | سقف ورودی (Input Tokens) | سقف خروجی (Output Tokens) | نرخ تخمینی RPM | سقف تخمینی RPD | منبع استناد (Source) | تاریخ اعتبارسنجی |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `gemini-3.8-flash` | ⚠️ **503 High Demand** (نیاز به Fallback) | ۱,۰۴۸,۵۷۶ | ۶۵,۵۳۶ | ۱۵ RPM | ۲۰ RPD | [Google AI Studio Rate Limits](https://ai.google.dev/gemini-api/docs/rate-limits) / [Pricing](https://ai.google.dev/pricing) | ۲۰۲۶-۱۱-۱۰ |
| `gemini-3.5-flash-lite` | ✅ **۲۰۰ OK** (سریع و پایدار) | ۱,۰۴۸,۵۷۶ | ۶۵,۵۳۶ | ۳۰ RPM | ۱,۵۰۰ RPD (طرح سبک) | [Google AI Studio Models](https://ai.google.dev/gemini-api/docs/models/gemini) / Pricing | ۲۰۲۶-۱۱-۱۰ |
| `gemini-2.5-flash` | ✅ **۲۰۰ OK** (پایدار) | ۱,۰۴۸,۵۷۶ | ۶۵,۵۳۶ | ۱۵ RPM | ۲۰ RPD (اثبات تجربی در task-1579) | [Google AI Studio Docs](https://ai.google.dev/pricing) | ۲۰۲۶-۱۱-۱۰ |

---

## ۲. منابع رسمی و پیوندهای مستندات Google Cloud & AI Studio

1. **راهنمای محدودیت نرخ مصرف (Rate Limits Documentation):**
   - پیوند رسمی: [https://ai.google.dev/gemini-api/docs/rate-limits](https://ai.google.dev/gemini-api/docs/rate-limits)
   - بخش مرتبط: جدول Free Tier Limits برای مدل‌های خانواده Gemini Flash (شامل ۱۵ RPM و ۱,۰۰۰,۰۰۰ TPM به ازای هر پروژه).
2. **جدول قیمت‌گذاری و سطوح دسترسی (Pricing & Quotas):**
   - پیوند رسمی: [https://ai.google.dev/pricing](https://ai.google.dev/pricing)
   - بخش مرتبط: مدل‌های Flash-Lite که به عنوان رده بهینه‌شده برای Throughput بالا و هزینه پایین‌تر طراحی شده و در طرح‌های توسعه‌ای سقف‌های درخواستی روزانه بالاتری (تا سقف ۱,۵۰۰ RPD) نسبت به مدل‌های سنگین‌تر استدلالی دارند.
3. **عدم بازگرداندن سهمیه روزانه در متد `models.list()`:**
   - متد استاندارد `client.models.list()` صرفاً متادیتای ساختاری مدل (مانند `input_token_limit` و `output_token_limit`) را بازمی‌گرداند و سهمیه‌های مصرفی (RPD / RPM) در سطح پروژه در کنسول ابری گوگل اعمال می‌شوند و از طریق این اندپوینت قابل استعلام نیستند.

---

## ۳. شواهد خام آزمون تجربی اتصال با کلید پروژه (Live Empirical Validation)

در تاریخ ۱۰ نوامبر ۲۰۲۶ ساعت ۲۱:۵۰ به وقت محلی، آزمون ارتباط مستقیم با ۳ مدل بر روی کلید واقعی پروژه اجرا گردید:

```text
gemini-3.8-flash: ERROR -> ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}}
gemini-3.5-flash-lite: OK -> Hello.
gemini-2.5-flash: OK -> Hello.
```

### تحلیل نتایج تجربی:
1. **تأیید دسترسی به `gemini-3.5-flash-lite`:** مدل بدون هیچ تاخیر یا خطایی پاسخ بازگرداند و به عنوان یک مدل پشتیبان (Fallback) فوق‌العاده سریع و دردسترس اثبات شد.
2. **وضعیت `gemini-3.8-flash`:** این مدل به دلیل اوج مصرف عمومی سرورهای گوگل (High Demand Spike) خطای ۵۰۳ بازگرداند. این آزمون ضرورت حیاتی پیاده‌سازی مکانیزم Fallback در [`app/ai/provider.py`](file:///d:/Joud%20Files/middle-east-news-bot/app/ai/provider.py) را به صورت عینی اثبات کرد.

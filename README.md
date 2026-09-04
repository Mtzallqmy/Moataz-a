# Moataz Media Bot v0.2.0

بوت Telegram عربي/إنجليزي لإدارة تنزيل ومعالجة الوسائط المسموح لك بتنزيلها من **YouTube** و**Facebook**. يعتمد على Python + aiogram + FastAPI + yt-dlp + FFmpeg، ويحتوي على نظام Jobs حقيقي، تقدم مباشر، إعادة محاولة ذكية، إلغاء المهام، سجل تنزيلات، وقاعدة بيانات PostgreSQL.

> استخدم المشروع فقط للمحتوى الذي تملكه أو لديك إذن بتنزيله أو تسمح المنصة/صاحب الحقوق بتنزيله. المشروع لا يهدف إلى تجاوز DRM أو الدفع أو الوصول غير المصرح به.

## ما الجديد في v0.2.0 — المرحلة الأولى الموحّدة

تم دمج محاور **الاستقرار + Job System + تجربة Telegram** في إصدار واحد:

- تشغيل Railway بسيط: المطلوب فقط `BOT_TOKEN` و`DATABASE_URL`.
- Polling افتراضي؛ لا تحتاج Domain أو Webhook أو Redis.
- Telegram API الرسمي مثبت صراحةً في جلسة aiogram.
- أخطاء Telegram المؤقتة لا تسقط عملية الويب؛ Polling يعيد المحاولة في الخلفية.
- دورة حياة Job واضحة:
  `PENDING → ANALYZING → READY → QUEUED → DOWNLOADING → CUTTING → UPLOADING → COMPLETED`.
- حالات إضافية: `RETRYING`, `FAILED`, `CANCELLED`، مع دعم الحالات القديمة في قاعدة البيانات.
- جدول `job_events` جديد يسجل تاريخ انتقالات المهمة والأخطاء بدون تعديل مدمر للجداول الموجودة.
- Retry انتقائي فقط للأخطاء المؤقتة مثل timeout / network / HTTP 429 و5xx.
- الفيديو الخاص، الرابط غير الصالح، الصيغة غير المتاحة، FFmpeg، والملف الكبير لا تدخل Retry بلا فائدة.
- Backoff تلقائي لإعادة المحاولة، افتراضيًا محاولتان إضافيتان.
- زر إلغاء أثناء المهمة، مع إيقاف التنزيل في الـInline worker عند أول progress callback ممكن.
- زر إعادة المحاولة يدويًا للمهام الفاشلة.
- قائمة رئيسية عربية/إنجليزية.
- `/jobs` و`📋 تحميلاتي` لعرض آخر المهام وحالتها.
- حد المهام النشطة لكل مستخدم مطبق قبل بدء Job جديد.
- Dashboard تحسب الحالات النشطة من نفس Job lifecycle الموحد.
- GitHub Actions: تثبيت + Ruff + Pytest + compile check.

## المزايا

- 🇸🇦 / 🇬🇧 عربية وإنجليزية.
- 🔎 تحليل الرابط قبل التنزيل.
- 📺 الجودات المتاحة: 360p / 480p / 720p / 1080p / 1440p / 2160p عند توفرها.
- ⭐ أفضل جودة متاحة.
- 🎵 استخراج MP3.
- ✂️ قص الفيديو بواسطة FFmpeg.
- 📊 نسبة التحميل والسرعة وETA.
- ❌ إلغاء المهمة.
- 🔄 Retry تلقائي للأخطاء المؤقتة + Retry يدوي.
- 📋 سجل آخر التنزيلات.
- 🗃 PostgreSQL للمستخدمين والمهام والأحداث.
- ⚡ Queue داخلية افتراضيًا؛ Redis اختياري فقط للتوزيع المتقدم.
- 🖥 Dashboard اختيارية.
- 🔒 Allowlist اختيارية.
- 🐳 Docker / Docker Compose.
- 🚂 Railway-ready.
- 🛡 URL validation وتقليل مخاطر SSRF.

---

# أسرع نشر على Railway

## المتغيرات المطلوبة فقط

```env
BOT_TOKEN=123456789:YOUR_TELEGRAM_BOT_TOKEN
DATABASE_URL=postgresql://...
```

كل ما عدا ذلك **اختياري**.

### 1) أنشئ PostgreSQL

داخل مشروع Railway:

```text
+ New → Database → PostgreSQL
```

ثم في Variables لخدمة البوت:

```env
BOT_TOKEN=توكن_البوت
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

التطبيق يقبل رابط Railway العادي `postgresql://...` ويحوّله تلقائيًا إلى AsyncPG.

### 2) Deploy

المستودع:

```text
https://github.com/Mtzallqmy/Moataz-a
```

المشروع يحتوي على `Dockerfile` و`railway.json`. الوضع الافتراضي:

```text
APP_MODE=polling
QUEUE_BACKEND=inline
```

لذلك لا تحتاج Redis أو Webhook URL أو Public URL أو Worker منفصل.

عند تشغيل الإصدار الصحيح سيظهر في logs:

```text
Starting Moataz Media Bot release 0.2.0-phase1
```

ثم افتح البوت وأرسل:

```text
/start
```

---

# تجربة Telegram

القائمة الرئيسية:

```text
🎬 إرسال رابط       📋 تحميلاتي
🌐 اللغة            ℹ️ مساعدة
```

بعد إرسال الرابط:

```text
🔎 جاري تحليل الرابط...
```

ثم يعرض العنوان والمنصة والمدة ورقم المهمة والجودات المتاحة.

أثناء التنزيل يتم تعديل نفس الرسالة تقريبًا بهذا الشكل:

```text
⬇️ التحميل 54.2%
██████████░░░░░░░░
⚡ 7.8 MB/s
⏳ 00:09

[ ❌ إلغاء المهمة ]
```

إذا حدث خطأ شبكة مؤقت:

```text
🔄 مشكلة مؤقتة في التنزيل. إعادة المحاولة 1/2...
```

وإذا انتهت المهمة بالفشل يظهر Error Code وزر:

```text
[ 🔄 إعادة المحاولة ]
```

---

# Job lifecycle

الحالات الأساسية:

```text
PENDING
ANALYZING
READY
QUEUED
RETRYING
DOWNLOADING
MERGING
CUTTING
UPLOADING
COMPLETED
FAILED
CANCELLED
```

الحالتان القديمتان `PROBING` و`PROCESSING` بقيتا مدعومتين لقراءة بيانات الإصدارات السابقة.

كل Job يسجل أحداثه في جدول مستقل:

```text
job_events
├── job_id
├── event_type
├── message
└── created_at
```

إضافة هذا الجدول لا تتطلب حذف قاعدة Railway الحالية أو إعادة إنشائها.

---

# Retry policy

الإعدادات الافتراضية:

```env
JOB_MAX_RETRIES=2
JOB_RETRY_BASE_SECONDS=5
```

وهما اختياريان بالكامل.

Retry تلقائي متوقع مع:

- Network / connection failures
- timeout
- HTTP 429
- HTTP 500 / 502 / 503 / 504
- أخطاء Telegram الشبكية المؤقتة

ولا يتم التكرار تلقائيًا عادةً مع:

- فيديو خاص أو يحتاج تسجيل دخول
- رابط غير مدعوم
- فيديو محذوف/غير متاح
- جودة غير متاحة
- FFmpeg processing error
- ملف أكبر من حد الإرسال
- خطأ مجهول غير مصنف كخطأ مؤقت

التأخير يستخدم exponential backoff: 5 ثوانٍ ثم 10 ثوانٍ افتراضيًا.

---

# قص الفيديو

1. أرسل رابطًا.
2. اختر `✂️ Cut / قص`.
3. أرسل المدى:

```text
00:00:10-00:00:45
```

تدخل المهمة `QUEUED` ثم `DOWNLOADING` ثم `CUTTING` ثم `UPLOADING`.

---

# حدود Telegram الحالية

الإصدار البسيط يستخدم Telegram Bot API الرسمي، ولذلك الحد المحافظ الافتراضي للمخرجات هو:

```env
MAX_FILE_SIZE_MB=49
```

إذا كانت الجودة تنتج ملفًا أكبر، اختر جودة أقل أو قص مقطعًا أقصر.

---

# الخصوصية — اختيارية

بدون IDs يكون البوت مفتوحًا لمن يعرف رابطه. لجعله خاصًا:

```env
ADMIN_TELEGRAM_IDS=123456789
ALLOWED_TELEGRAM_IDS=123456789,987654321
```

---

# Dashboard — اختيارية

اللوحة معطلة ما لم تضع كلمة مرور:

```env
DASHBOARD_PASSWORD=strong-password
```

اسم المستخدم الافتراضي:

```text
admin
```

يمكن تغييره عبر:

```env
DASHBOARD_USERNAME=myadmin
```

ثم افتح:

```text
https://YOUR-DOMAIN/dashboard
```

---

# Redis / Workers — اختياري ومتقدم

الوضع الافتراضي لا يحتاج Redis:

```env
QUEUE_BACKEND=inline
```

للتوزيع لاحقًا:

```env
QUEUE_BACKEND=redis
REDIS_URL=redis://...
```

وتشغيل Worker منفصل:

```bash
arq app.worker.WorkerSettings
```

الـWorker يحتاج نفس `BOT_TOKEN` و`DATABASE_URL` و`REDIS_URL`.

---

# Cookies — اختيارية

لبعض المحتوى المصرح لك بالوصول إليه قد تحتاج ملف Cookies:

```env
YTDLP_COOKIES_FILE=/run/secrets/cookies.txt
```

لا ترفع Cookies أو Tokens أو `.env` إلى GitHub.

---

# أهم متغيرات البيئة

| Variable | Default | Required |
|---|---:|---:|
| `BOT_TOKEN` | — | نعم |
| `DATABASE_URL` | SQLite محليًا | نعم على Railway |
| `APP_MODE` | `polling` | لا |
| `QUEUE_BACKEND` | `inline` | لا |
| `MAX_CONCURRENT_JOBS` | `2` | لا |
| `MAX_JOBS_PER_USER` | `1` | لا |
| `MAX_VIDEO_DURATION_SECONDS` | `14400` | لا |
| `MAX_FILE_SIZE_MB` | `49` | لا |
| `PROGRESS_UPDATE_SECONDS` | `2` | لا |
| `JOB_MAX_RETRIES` | `2` | لا |
| `JOB_RETRY_BASE_SECONDS` | `5` | لا |
| `DEFAULT_LANGUAGE` | `ar` | لا |
| `REDIS_URL` | فارغ | لا |
| `DASHBOARD_PASSWORD` | فارغ | لا |
| `YTDLP_COOKIES_FILE` | فارغ | لا |

راجع `.env.example` لبقية القيم الاختيارية.

---

# تشغيل Docker محليًا

```bash
cp .env.example .env
```

ثم ضع على الأقل:

```env
BOT_TOKEN=YOUR_TOKEN
DATABASE_URL=sqlite+aiosqlite:///./moataz.db
```

وشغّل:

```bash
docker build -t moataz-media-bot .
docker run --rm --env-file .env -p 8000:8000 moataz-media-bot
```

أو للبنية المتقدمة:

```bash
docker compose up --build
```

---

# هيكل المشروع

```text
app/
├── bot/
│   ├── access.py
│   ├── client.py
│   └── handlers.py
├── services/
│   ├── downloader.py
│   └── media.py
├── config.py
├── dashboard.py
├── db.py
├── i18n.py
├── jobs.py
├── main.py
├── queue.py
├── security.py
├── utils.py
└── worker.py
```

# الاختبارات

```bash
pip install -e '.[dev]'
ruff check app tests
pytest -q
python -m compileall -q app
```

GitHub Actions تنفذ هذه الفحوص تلقائيًا عند كل Push وPull Request.

## License

MIT

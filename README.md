# Moataz Media Bot v0.3.0

بوت Telegram عربي/إنجليزي لتنزيل ومعالجة الوسائط التي تملك حق تنزيلها من **YouTube** و**Facebook**. يعتمد على Python 3.12 وaiogram وFastAPI وyt-dlp وFFmpeg، ويحتوي على Job System، Retry، إلغاء، قص، MP3، مراقبة Workers، Dashboard اختيارية، وتحسين تلقائي للملفات قبل إرسالها إلى Telegram.

> استخدم المشروع فقط للمحتوى الذي تملكه أو لديك إذن بتنزيله أو تسمح المنصة/صاحب الحقوق بتنزيله. المشروع لا يهدف إلى تجاوز DRM أو أنظمة الدفع أو الوصول غير المصرح به.

## v0.3.0 — المراحل 4 + 5 + 6

تم تنفيذ المراحل الثلاث كحزمة واحدة بعد v0.2.0:

### المرحلة 4 — Media Source Engine

- طبقة مصادر مستقلة لـYouTube وFacebook بدل الاعتماد على أسماء extractors الخام.
- تطبيع اسم المنصة إلى `youtube` / `facebook`.
- استخراج الجودات بصورة مستقرة وإخفاء تنسيقات الصوت من قائمة الفيديو.
- اختيار Formats يفضّل MP4 + M4A المناسبة أكثر للإرسال والبث داخل Telegram، مع fallbacks لـyt-dlp.
- إعدادات اختيارية لـsocket timeout، retries، fragment retries، وعدد التحميلات المتوازية للأجزاء.
- لا تزال Cookies اختيارية ولا توجد أي أسرار إضافية مطلوبة للتشغيل العادي.

### المرحلة 5 — Adaptive Media Delivery

- فصل حد الملف الداخلي عن حد الإرسال إلى Telegram.
- الحد الداخلي الافتراضي: `MAX_FILE_SIZE_MB=512`.
- ميزانية الإرسال الافتراضية المحافظة: `TELEGRAM_UPLOAD_LIMIT_MB=49`.
- إذا تجاوز الفيديو/الصوت ميزانية الإرسال، يتم استخدام FFprobe لحساب المدة ثم FFmpeg لضبط bitrate والجودة تلقائيًا.
- الفيديو يتحول إلى MP4/H.264 + AAC مع `faststart`.
- الصوت الكبير يمكن ضغطه إلى MP3 بbitrate مناسب.
- إذا احتاج الملف أكثر من محاولة للوصول للحجم المطلوب، يتم تعديل bitrate تلقائيًا ضمن عدد محاولات محدود.
- يمكن تعطيل ذلك عبر `AUTO_COMPRESS_ENABLED=false`.

### المرحلة 6 — Production Operations & Recovery

- Endpoint جديد: `/readyz` يفحص PostgreSQL/SQLite + FFmpeg + FFprobe.
- `/healthz` يبقى Liveness سريعًا ولا يعتمد على خدمات خارجية.
- عند إعادة تشغيل خدمة تستخدم `QUEUE_BACKEND=inline`، لا تبقى المهام القديمة عالقة في حالة Running؛ يتم إنهاؤها كـ`FAILED / INTERRUPTED` لتكون قابلة لإعادة المحاولة بدل أن تبقى Stuck.
- Workers التي يتوقف Heartbeat الخاص بها تُعلّم `OFFLINE` تلقائيًا.
- Dashboard API تعرض إحصاءات completed/failed وWorker/file metadata إضافية.
- API محمية للوحة التحكم لعرض آخر 100 حدث لمهمة محددة: `/api/jobs/{job_id}/events`.
- سجل `job_events` يبقى Append-only ولا يتطلب Migration مدمرة لجدول المهام الحالي.

## أهم المزايا

- 🇸🇦 / 🇬🇧 واجهة Telegram بالعربية والإنجليزية.
- 🔎 تحليل الرابط قبل التنزيل.
- 📺 اختيار 360p / 480p / 720p / 1080p / 1440p / 2160p عند توفرها.
- ⭐ أفضل جودة متاحة.
- 🎵 استخراج MP3.
- ✂️ قص الفيديو عبر FFmpeg.
- 📊 تقدم مباشر: النسبة، السرعة وETA.
- ❌ إلغاء المهمة.
- 🔄 Retry تلقائي انتقائي + Retry يدوي.
- 📋 `/jobs` وسجل آخر التنزيلات.
- 🗃 PostgreSQL للمستخدمين والمهام والأحداث والWorkers.
- ⚡ Queue داخلية افتراضيًا؛ Redis + ARQ اختياريان للتوزيع المتقدم.
- 🖥 Dashboard اختيارية ومعطلة إذا لم تضبط كلمة مرور.
- 🛡 URL allowlist لـYouTube/Facebook وتقليل مخاطر SSRF.
- ✅ GitHub Actions: Ruff + Pytest + compile check.

---

# أسرع نشر على Railway

## المطلوب فقط

```env
BOT_TOKEN=123456789:YOUR_TELEGRAM_BOT_TOKEN
DATABASE_URL=postgresql://...
```

كل ما عدا ذلك اختياري. الوضع الافتراضي:

```text
APP_MODE=polling
QUEUE_BACKEND=inline
AUTO_COMPRESS_ENABLED=true
```

لذلك لا تحتاج Redis أو Webhook أو Public Domain أو Worker منفصل في النشر الأساسي.

### Railway

1. اربط المستودع بخدمة Railway.
2. أضف PostgreSQL.
3. أضف في Variables:

```env
BOT_TOKEN=توكن_البوت
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

4. Deploy.

عند تشغيل الإصدار الحالي يظهر في Logs:

```text
Starting Moataz Media Bot release 0.3.0-phase456
```

ثم افتح البوت وأرسل `/start`.

---

# دورة حياة المهمة

```text
ANALYZING
  ↓
READY
  ↓
QUEUED
  ↓
DOWNLOADING
  ↓
CUTTING / PROCESSING   (عند الحاجة)
  ↓
UPLOADING
  ↓
COMPLETED
```

مع الحالات:

```text
RETRYING / FAILED / CANCELLED
```

`PROCESSING` في v0.3.0 يشمل أيضًا مرحلة تهيئة ملف كبير لميزانية إرسال Telegram.

---

# إعدادات Media Engine الاختيارية

```env
YTDLP_SOCKET_TIMEOUT_SECONDS=30
YTDLP_RETRIES=2
YTDLP_FRAGMENT_RETRIES=3
YTDLP_CONCURRENT_FRAGMENTS=4
YTDLP_COOKIES_FILE=
```

لا تضف Cookies إلا للمحتوى الذي يحق لك الوصول إليه. لا ترفع Cookies أو Tokens أو `.env` إلى GitHub.

# إعدادات الملفات والإرسال الاختيارية

```env
MAX_FILE_SIZE_MB=512
TELEGRAM_UPLOAD_LIMIT_MB=49
AUTO_COMPRESS_ENABLED=true
MEDIA_COMPRESSION_ATTEMPTS=2
```

`MAX_FILE_SIZE_MB` هو حد العمل الداخلي قبل محاولة التحسين. `TELEGRAM_UPLOAD_LIMIT_MB` هو ميزانية الإرسال التي يحاول النظام تهيئة الملف لها.

---

# الخصوصية / Allowlist — اختياري

ترك القيم التالية فارغة يعني أن البوت متاح لمن يعرفه:

```env
ADMIN_TELEGRAM_IDS=
ALLOWED_TELEGRAM_IDS=
```

لجعله خاصًا:

```env
ADMIN_TELEGRAM_IDS=123456789
ALLOWED_TELEGRAM_IDS=123456789,987654321
```

---

# Dashboard — اختيارية

اللوحة معطلة افتراضيًا. لتفعيلها:

```env
DASHBOARD_PASSWORD=ضع_كلمة_مرور_قوية
```

واختياريًا:

```env
DASHBOARD_USERNAME=admin
DASHBOARD_WS_TOKEN=secret-for-websocket
```

المسارات:

```text
/dashboard
/api/dashboard
/api/jobs/{job_id}/events
```

# Health / Readiness

```text
GET /healthz
GET /readyz
```

`/healthz` يثبت أن عملية الويب تعمل. `/readyz` يتحقق من قاعدة البيانات ووجود FFmpeg وFFprobe داخل بيئة التشغيل.

---

# Redis وWorkers متعددة — اختياري

النشر العادي لا يحتاج Redis:

```env
QUEUE_BACKEND=inline
```

للتوزيع المتقدم:

```env
QUEUE_BACKEND=redis
REDIS_URL=redis://...
```

ثم شغّل Worker:

```bash
arq app.worker.WorkerSettings
```

يجب أن يشارك Worker نفس `BOT_TOKEN` و`DATABASE_URL` و`REDIS_URL`.

---

# Webhook — اختياري

Polling هو الوضع الافتراضي. لتفعيل Webhook:

```env
APP_MODE=webhook
WEBHOOK_BASE_URL=https://your-domain.example
WEBHOOK_SECRET=long-random-secret
```

المسار:

```text
/telegram/webhook
```

إذا فشل إعداد Webhook عند الإقلاع، يعود التطبيق إلى Polling بدل إسقاط خدمة Railway.

---

# تشغيل Docker محليًا

```bash
cp .env.example .env
```

أقل إعداد:

```env
BOT_TOKEN=123456:YOUR_TOKEN
DATABASE_URL=sqlite+aiosqlite:///./moataz.db
```

ثم:

```bash
docker build -t moataz-media-bot .
docker run --rm --env-file .env -p 8000:8000 moataz-media-bot
```

أو استخدم `docker-compose.yml` للبنية التي تتضمن PostgreSQL وRedis وWorker منفصل.

---

# الاختبارات

```bash
pip install -e '.[dev]'
ruff check app tests
pytest -q
python -m compileall -q app
```

GitHub Actions تنفذها تلقائيًا عند كل Push وPull Request.

## License

MIT.

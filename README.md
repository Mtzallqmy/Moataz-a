# Moataz Media Bot — بوت تحميل ومعالجة الوسائط

بوت Telegram عربي/إنجليزي مبني ببايثون لتنزيل ومعالجة الوسائط المسموح لك بتنزيلها من **YouTube** و**Facebook**، مع اختيار الجودة، استخراج MP3، قص الفيديو عبر FFmpeg، وعرض تقدم التحميل.

> **الاستخدام المسؤول:** استخدم المشروع فقط للمحتوى الذي تملكه، أو لديك إذن بتنزيله، أو تسمح المنصة/صاحب الحقوق بتنزيله. المشروع لا يهدف إلى تجاوز DRM أو أنظمة الدفع أو الوصول إلى محتوى غير مصرح به.

## المزايا

- 🇸🇦 / 🇬🇧 واجهة Telegram بالعربية والإنجليزية.
- 🎬 تحليل الرابط وعرض الجودات المتاحة قبل التنزيل.
- 📺 دعم 360p / 480p / 720p / 1080p / 1440p / 2160p عندما تكون متاحة.
- 🎵 استخراج MP3.
- ✂️ قص الفيديو بواسطة FFmpeg.
- 📊 نسبة التحميل والسرعة وETA.
- ⚡ Queue داخلية افتراضيًا: **لا تحتاج Redis أو Worker منفصل**.
- 🗃 PostgreSQL لحفظ المستخدمين والمهام.
- 🖥 Dashboard اختيارية بتصميم Glassmorphism.
- 🔒 Allowlist اختيارية للمستخدمين.
- 🌐 Polling افتراضيًا: **لا تحتاج Domain أو Webhook**.
- 🐳 Docker + Docker Compose.
- 🚂 إعداد جاهز لـRailway.
- 🧰 Redis + ARQ متاحان اختياريًا للتوزيع على Workers متعددة.
- 🛡 فحص الروابط وتقليل مخاطر SSRF.
- ✅ GitHub Actions للاختبارات وRuff وcompile check.

---

# أسرع تشغيل على Railway

## المطلوب فقط

في النشر العادي تحتاج **متغيرين فقط**:

```env
BOT_TOKEN=123456789:YOUR_TELEGRAM_BOT_TOKEN
DATABASE_URL=postgresql://...
```

جميع المتغيرات الأخرى اختيارية.

## 1. أنشئ البوت

من Telegram افتح `@BotFather` وأنشئ Bot ثم انسخ الـToken.

## 2. اربط المستودع بـRailway

المستودع:

```text
https://github.com/Mtzallqmy/Moataz-a
```

المشروع يحتوي على `railway.json` و`Dockerfile`، وسيستخدم Railway صورة Python 3.12 مع FFmpeg تلقائيًا.

## 3. أضف PostgreSQL في Railway

من مشروع Railway:

```text
+ New → Database → PostgreSQL
```

ثم في خدمة البوت افتح **Variables** وضع:

```env
BOT_TOKEN=توكن_البوت
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

إذا كان اسم خدمة قاعدة البيانات مختلفًا عن `Postgres` استخدم اسمها الفعلي في Railway، أو انسخ `DATABASE_URL` مباشرة.

> التطبيق يقبل رابط Railway العادي `postgresql://...` ويحوّله تلقائيًا إلى صيغة AsyncPG المطلوبة؛ لا تحتاج تعديل الرابط يدويًا.

## 4. Redeploy

هذا كل شيء. الوضع الافتراضي هو:

```text
APP_MODE=polling
QUEUE_BACKEND=inline
```

لذلك لا تحتاج:

- Redis
- Webhook URL
- Webhook Secret
- Public URL
- Worker Service
- Admin Telegram ID
- Dashboard Password

Railway يرسل متغير `PORT` تلقائيًا والتطبيق يستخدمه مباشرة.

بعد نجاح النشر افتح Bot في Telegram وأرسل `/start`.

---

# الخصوصية / Allowlist — اختياري

إذا لم تضبط أي IDs فالبوت يعمل لكل من يعرف رابطه.

لجعله خاصًا ضع مثلًا:

```env
ADMIN_TELEGRAM_IDS=123456789
ALLOWED_TELEGRAM_IDS=123456789,987654321
```

يمكن وضع أكثر من ID مفصولًا بفاصلة.

---

# لوحة التحكم — اختيارية

الـDashboard **معطلة افتراضيًا** حتى لا يكون هناك Password افتراضي مكشوف.

لتفعيلها يكفي متغير واحد:

```env
DASHBOARD_PASSWORD=ضع_كلمة_مرور_قوية
```

اسم المستخدم الافتراضي:

```text
admin
```

ويمكن تغييره اختياريًا:

```env
DASHBOARD_USERNAME=myadmin
```

ثم افتح:

```text
https://YOUR-RAILWAY-DOMAIN/dashboard
```

`DASHBOARD_WS_TOKEN` اختياري بالكامل؛ إذا لم تضبطه يستخدم التطبيق كلمة مرور الداشبورد لحماية WebSocket.

---

# Webhook — اختياري

Polling هو الوضع الافتراضي والأبسط على Railway.

إذا أردت Webhook لاحقًا:

```env
APP_MODE=webhook
WEBHOOK_BASE_URL=https://your-domain.example
WEBHOOK_SECRET=long-random-secret
```

المسار:

```text
/telegram/webhook
```

`WEBHOOK_SECRET` مستحسن أمنيًا، لكنه ليس مطلوبًا في Polling.

---

# Redis وWorkers متعددة — اختياري ومتقدم

لا تحتاج Redis في النشر العادي.

المشروع يشغل المهام داخل نفس خدمة التطبيق افتراضيًا:

```env
QUEUE_BACKEND=inline
```

إذا أردت لاحقًا عدة Workers على أكثر من سيرفر، أضف Redis واضبط:

```env
QUEUE_BACKEND=redis
REDIS_URL=redis://...
```

ثم شغّل Worker منفصلًا:

```bash
arq app.worker.WorkerSettings
```

ويجب أن يستخدم الـWorker نفس:

```text
BOT_TOKEN
DATABASE_URL
REDIS_URL
```

---

# تشغيل Docker محليًا

انسخ ملف البيئة:

```bash
cp .env.example .env
```

أقل إعداد ممكن:

```env
BOT_TOKEN=123456:YOUR_TOKEN
DATABASE_URL=sqlite+aiosqlite:///./moataz.db
```

ثم:

```bash
docker build -t moataz-media-bot .
docker run --rm --env-file .env -p 8000:8000 moataz-media-bot
```

أو استخدم `docker-compose.yml` للبنية المتقدمة التي تتضمن PostgreSQL وRedis وWorker منفصل.

---

# قص الفيديو

1. أرسل رابط YouTube أو Facebook.
2. اضغط `✂️ Cut / قص`.
3. أرسل المدى مثل:

```text
00:00:10-00:00:45
```

سيتم تنزيل الفيديو ومعالجة المقطع عبر FFmpeg ثم إرساله إلى Telegram.

---

# الملفات الكبيرة

بدون `TELEGRAM_LOCAL_API_URL` يطبق التطبيق حدًا محافظًا على الملفات المرفوعة عبر Telegram Bot API.

يمكن لاحقًا توجيه التطبيق إلى Local Telegram Bot API Server:

```env
TELEGRAM_LOCAL_API_URL=http://telegram-bot-api:8081
```

هذا الإعداد اختياري ولا يؤثر على تشغيل البوت الأساسي.

---

# yt-dlp Cookies — اختياري

بعض المحتوى المصرح لك بالوصول إليه قد يحتاج جلسة متصفح. يمكن Mount ملف cookies ثم ضبط:

```env
YTDLP_COOKIES_FILE=/run/secrets/cookies.txt
```

لا ترفع Cookies أو Tokens أو `.env` إلى GitHub.

---

# متغيرات البيئة

## المطلوبة للإنتاج

| Variable | الاستخدام |
|---|---|
| `BOT_TOKEN` | Telegram Bot Token |
| `DATABASE_URL` | رابط PostgreSQL؛ Railway URLs مدعومة مباشرة |

## الاختيارية

| Variable | Default | الاستخدام |
|---|---|---|
| `APP_MODE` | `polling` | `polling` أو `webhook` |
| `QUEUE_BACKEND` | `inline` | `inline` أو `redis` |
| `REDIS_URL` | فارغ | فقط عند استخدام Redis Queue |
| `ADMIN_TELEGRAM_IDS` | فارغ | IDs للمشرفين / تفعيل الوضع الخاص |
| `ALLOWED_TELEGRAM_IDS` | فارغ | Allowlist |
| `DASHBOARD_USERNAME` | `admin` | مستخدم لوحة التحكم |
| `DASHBOARD_PASSWORD` | فارغ | تفعيل وحماية Dashboard |
| `DASHBOARD_WS_TOKEN` | فارغ | Secret منفصل اختياري للـWebSocket |
| `WEBHOOK_BASE_URL` | فارغ | HTTPS URL في webhook mode |
| `WEBHOOK_SECRET` | فارغ | حماية Webhook |
| `TELEGRAM_LOCAL_API_URL` | فارغ | Telegram Bot API محلي |
| `MAX_CONCURRENT_JOBS` | `2` | عدد التنزيلات المتزامنة |
| `MAX_JOBS_PER_USER` | `1` | عدد المهام النشطة لكل مستخدم |
| `MAX_VIDEO_DURATION_SECONDS` | `14400` | أقصى مدة فيديو |
| `MAX_FILE_SIZE_MB` | `1900` | الحد الأعلى الداخلي للملف |
| `PROGRESS_UPDATE_SECONDS` | `2` | معدل تحديث رسالة التقدم |
| `DEFAULT_LANGUAGE` | `ar` | `ar` أو `en` |
| `YTDLP_COOKIES_FILE` | فارغ | مسار Cookies اختياري |

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
├── static/
│   └── dashboard.css
├── templates/
│   └── dashboard.html
├── config.py
├── dashboard.py
├── db.py
├── i18n.py
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

GitHub Actions تنفذ الاختبارات تلقائيًا عند كل Push وPull Request.

## License

MIT.

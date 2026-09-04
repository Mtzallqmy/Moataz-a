# Moataz Media Bot — بوت تحميل ومعالجة وسائط خاص

بوت Telegram خاص ومفتوح المصدر مبني ببايثون لتحليل وتنزيل الوسائط المسموح لك بتنزيلها من **YouTube** و**Facebook**، مع اختيار الجودة، MP3، قص الفيديو عبر FFmpeg، نسبة تقدم مباشرة، Queue منفصلة، ولوحة تحكم Glassmorphism عربية/إنجليزية.

> **الاستخدام المسؤول:** استخدم المشروع فقط لتنزيل المحتوى الذي تملكه، أو لديك إذن بتنزيله، أو تسمح المنصة/صاحب الحقوق بتنزيله. لا يهدف المشروع لتجاوز DRM أو أنظمة الدفع أو الوصول إلى محتوى غير مصرح به.

## المزايا

- 🇸🇦 / 🇬🇧 واجهة Telegram عربية وإنجليزية.
- 🔒 Allowlist خاصة لمجموعة صغيرة من المستخدمين.
- 🎬 تحليل الرابط قبل التنزيل وعرض الجودات المتاحة.
- 📺 اختيار 360p / 480p / 720p / 1080p / 1440p / 2160p عندما تكون متاحة.
- 🎵 استخراج MP3.
- ✂️ قص دقيق للفيديو بواسطة FFmpeg.
- 📊 تقدم التحميل: النسبة، السرعة وETA.
- 🧵 Redis + ARQ لفصل معالجة الفيديو عن Webhook/API.
- 🗃 PostgreSQL لحفظ المستخدمين والمهام والحالة.
- 🖥 Dashboard زجاجي متجاوب مع تحديث حي عبر WebSocket.
- 🟢 Worker heartbeat مع عرض العقد المتصلة والمهام النشطة.
- 🐳 Docker + Docker Compose.
- 🌐 Polling للتجربة السريعة أو Webhook للإنتاج.
- 🧰 دعم تشغيل أكثر من Worker على خوادم مختلفة باستخدام Redis/PostgreSQL مشتركين.
- 🛡 Allowlist للدومينات وتقليل مخاطر SSRF.
- ✅ GitHub Actions للاختبارات وRuff وcompile check.

## المعمارية

```text
Telegram
   │
   ├── Polling (development)
   │
   └── HTTPS Webhook (production)
            │
            ▼
      FastAPI + aiogram
            │
            ├──────────────► Dashboard / WebSocket
            │
            ▼
          Redis
            │
            ▼
       ARQ Worker(s)
            │
      ┌─────┴─────┐
      ▼           ▼
   yt-dlp       FFmpeg
      │           │
      └─────┬─────┘
            ▼
        Telegram

FastAPI / Worker ────────── PostgreSQL
```

## تشغيل سريع باستخدام Docker

### 1) إنشاء البوت

من Telegram افتح `@BotFather`، أنشئ بوتًا واحصل على `BOT_TOKEN`.

للحصول على Telegram numeric user ID استخدم أي طريقة موثوقة لديك ثم ضعه في `ADMIN_TELEGRAM_IDS` و`ALLOWED_TELEGRAM_IDS`.

### 2) إعداد البيئة

```bash
cp .env.example .env
```

عدّل أهم القيم:

```env
BOT_TOKEN=123456:YOUR_TOKEN
APP_MODE=polling
ADMIN_TELEGRAM_IDS=123456789
ALLOWED_TELEGRAM_IDS=123456789,987654321
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=use-a-long-random-password
DASHBOARD_WS_TOKEN=use-another-long-random-value
```

### 3) التشغيل

```bash
docker compose up -d --build
```

الحالة:

```bash
docker compose ps
```

السجلات:

```bash
docker compose logs -f api worker
```

لوحة التحكم:

```text
http://SERVER_IP:8000/dashboard
```

ستظهر نافذة HTTP Basic Auth؛ استخدم `DASHBOARD_USERNAME` و`DASHBOARD_PASSWORD`.

## وضع Webhook للإنتاج

تحتاج إلى Domain وHTTPS. يمكن استخدام Caddy أو Nginx أو Cloudflare Tunnel.

```env
APP_MODE=webhook
WEBHOOK_BASE_URL=https://bot.example.com
WEBHOOK_SECRET=VERY_LONG_RANDOM_SECRET
PUBLIC_BASE_URL=https://bot.example.com
```

المسار الذي يسجله التطبيق تلقائيًا عند البدء:

```text
https://bot.example.com/telegram/webhook
```

ويتحقق التطبيق من هيدر Telegram السري قبل معالجة Update.

يوجد `Caddyfile` بسيط في المستودع كنقطة بداية.

## تشغيل بدون Docker للتطوير

يلزم Python 3.12+ وFFmpeg وRedis وPostgreSQL، أو يمكن استخدام SQLite محليًا.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

للتجربة المحلية السريعة غيّر:

```env
DATABASE_URL=sqlite+aiosqlite:///./moataz.db
REDIS_URL=redis://localhost:6379/0
APP_MODE=polling
```

ثم شغّل الطرفين:

```bash
python -m app.main
```

وفي Terminal آخر:

```bash
arq app.worker.WorkerSettings
```

## قص الفيديو

1. أرسل الرابط.
2. اضغط `✂️ Cut / قص`.
3. أرسل المدى:

```text
00:00:10-00:00:45
```

يقوم Worker بتنزيل نسخة مناسبة ثم يعيد ترميز المقطع إلى MP4/H.264 + AAC لضمان دقة نقطة البداية والنهاية وتوافق جيد.

## أكثر من Worker / أكثر من سيرفر

المشروع جاهز أفقيًا. أبقِ API/PostgreSQL/Redis في الخادم الرئيسي، وعلى أي VPS أو جهاز ثانٍ شغّل Worker مع **نفس** `DATABASE_URL` و`REDIS_URL` و`BOT_TOKEN`.

```bash
arq app.worker.WorkerSettings
```

ARQ سيقوم بتوزيع Jobs المتاحة بين Workers. لا تجعل مجلد التنزيل Shared؛ كل Worker يستخدم مجلده المؤقت المحلي ثم يرفع الناتج إلى Telegram.

> في Docker Compose الحالي `media_data` مشترك فقط بين `api` و`worker` على نفس الجهاز، مع أن API لا يعتمد عليه فعليًا.

## الملفات الكبيرة وLocal Telegram Bot API

يمكنك توجيه aiogram إلى Telegram Bot API Server محلي عبر:

```env
TELEGRAM_LOCAL_API_URL=http://telegram-bot-api:8081
```

عند عدم ضبطه، يطبق Worker حدًا محافظًا للرفع عبر Bot API الرسمي ويطلب منك اختيار جودة أصغر إذا كان الملف كبيرًا. تشغيل Telegram Bot API Server نفسه يعتمد على بيئة استضافتك ويتطلب إعدادات Telegram المناسبة؛ لذلك لم يتم فرض Image معينة داخل `docker-compose.yml` حتى لا نربط المشروع بصورة Docker غير موثوقة أو إعداد واحد فقط.

## Cookies لـ yt-dlp

بعض المحتوى المصرح لك بالوصول إليه قد يحتاج جلسة متصفح. يمكن Mount ملف cookies إلى الحاوية ثم ضبط:

```env
YTDLP_COOKIES_FILE=/run/secrets/cookies.txt
```

لا ترفع cookies أو Tokens أو `.env` إلى GitHub. `.gitignore` يمنع `.env`، لكن مسؤولية الأسرار تبقى عليك.

## أهم متغيرات البيئة

| Variable | الاستخدام |
|---|---|
| `BOT_TOKEN` | Telegram bot token |
| `WORKER_NAME` | اسم اختياري ثابت للـWorker في الداشبورد |
| `APP_MODE` | `polling` أو `webhook` |
| `WEBHOOK_BASE_URL` | عنوان HTTPS العام في webhook mode |
| `WEBHOOK_SECRET` | Secret للتحقق من Webhook |
| `ADMIN_TELEGRAM_IDS` | IDs للمشرفين |
| `ALLOWED_TELEGRAM_IDS` | Allowlist أولية |
| `DATABASE_URL` | PostgreSQL/SQLite async URL |
| `REDIS_URL` | Redis DSN |
| `DOWNLOAD_DIR` | الملفات المؤقتة |
| `MAX_CONCURRENT_JOBS` | عدد Jobs المتزامنة لكل Worker |
| `MAX_JOBS_PER_USER` | الحد الأقصى للمهام النشطة لكل مستخدم |
| `MAX_VIDEO_DURATION_SECONDS` | الحد الأقصى لمدة الفيديو |
| `MAX_FILE_SIZE_MB` | حد الملف النهائي |
| `PROGRESS_UPDATE_SECONDS` | معدل تعديل رسالة Telegram |
| `TELEGRAM_LOCAL_API_URL` | Bot API Server محلي اختياري |
| `YTDLP_COOKIES_FILE` | Cookies اختياري |

## الأمان

- الروابط مقيدة حاليًا إلى YouTube/Facebook/fb.watch بدل قبول أي URL عشوائي.
- IP addresses و`file://` غير مقبولة.
- Webhook يتحقق من `X-Telegram-Bot-Api-Secret-Token`.
- Dashboard محمي بـHTTP Basic Auth.
- لا يتم تمرير URL للمستخدم عبر `shell=True`.
- FFmpeg يستقبل Arguments مباشرة عبر subprocess.
- الأسرار محصورة في `.env`.

يفضل في الإنتاج أيضًا:

- Firewall يسمح فقط بـ80/443 وSSH من عنوانك عند الإمكان.
- HTTPS إجباري.
- كلمات مرور عشوائية طويلة.
- تحديث Docker images والحزم دوريًا.
- Backup لقاعدة PostgreSQL.

## هيكل المشروع

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

## الاختبارات

```bash
pip install -e '.[dev]'
ruff check app tests
pytest -q
python -m compileall -q app
```

GitHub Actions ينفذها تلقائيًا عند Push وPull Request.

## English quick start

```bash
cp .env.example .env
# set BOT_TOKEN, ADMIN_TELEGRAM_IDS, ALLOWED_TELEGRAM_IDS and dashboard passwords
docker compose up -d --build
```

Use `APP_MODE=polling` first. For production, put the app behind HTTPS and switch to `APP_MODE=webhook` with `WEBHOOK_BASE_URL=https://your-domain`.

## License

MIT.

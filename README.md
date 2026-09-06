# Moataz Media Bot

Media Download Manager يعمل من Telegram ومن Dashboard اختيارية، مبني على Python 3.12 وFastAPI وaiogram وPostgreSQL وyt-dlp وFFmpeg.

## التشغيل الأساسي

التشغيل الطبيعي على Railway يحتاج متغيرين فقط:

```env
BOT_TOKEN=
DATABASE_URL=
```

يعمل البوت افتراضيًا عبر **Telegram Polling**، وتعمل المهام عبر **Inline Queue** داخل الخدمة نفسها. لا يحتاج Redis أو ARQ أو Webhook أو Worker منفصل أو Cookies أو API keys إضافية.

```bash
python -m app.main
```

Railway يمرر `PORT` تلقائيًا، والتطبيق يستخدمه مباشرة.

## القدرات

- DownloaderService مستقل عن Telegram: `probe()`, `get_formats()`, `download()`, `download_audio()`, `expand_playlist()`, `cancel()`, `classify_error()`.
- YouTube وFacebook مساران أساسيان ومختبران، مع Generic yt-dlp لأي URL عام يستطيع yt-dlp التعرف عليه فعليًا.
- MP4 وMP3 وBest Quality واختيار Resolution من الجودات الموجودة فعلًا فقط.
- لا يحدث downgrade صامت: اختيار 1080p مثلًا يستخدم تطابق ارتفاع exact، وإذا لم يعد متاحًا تفشل المهمة بـ `FORMAT_UNAVAILABLE`.
- Metadata: العنوان، الصورة المصغرة، المدة، الناشر، المنصة والجودات.
- Bulk URLs مع normalization وdeduplication، وكل URL يصبح Job مستقلًا.
- Playlist confirmation/expansion إلى Jobs مستقلة بحد آمن افتراضي 10 عناصر.
- Progress على نفس رسالة Telegram: Job ID، الحالة، الجودة، Downloaded/Total، النسبة، الشريط، السرعة وETA.
- FAST cutting عبر stream copy، وPRECISE عبر H.264/AAC re-encode.
- cancellation فعلية للـyt-dlp وFFmpeg، retry محدود مع exponential backoff + jitter، startup reconciliation وcleanup.
- Dashboard: Overview / Downloads / Jobs / Users / Workers / Errors / System، مع Analyze وDownload وDownload All وCancel.

## الحماية

كل URL يمر عبر طبقة حماية قبل yt-dlp: HTTP/HTTPS فقط، حظر localhost وprivate/link-local/reserved addresses وcloud metadata، والتحقق من DNS. `SafeYoutubeDL` يعيد فحص الطلبات التي ينفذها yt-dlp للمساعدة في حماية redirects. لا توجد آليات لتجاوز DRM أو paywalls أو private/authenticated media.

العمليات الخارجية تستخدم argument arrays فقط ولا تستخدم `shell=True`. كل Job يكتب داخل مجلد معزول، مع حدود للمدة والحجم والمهلة والتزامن والتنظيف التلقائي وSecret Redaction للـlogs.

## Dashboard

اللوحة **معطلة** افتراضيًا. إذا لم يكن `DASHBOARD_PASSWORD` مضبوطًا فإن `/dashboard` يعيد 404 ولا توجد لوحة غير محمية. هذا متغير اختياري وليس مطلوبًا لتشغيل البوت.

## Health

- `GET /healthz`: liveness بدون اتصال Telegram أو yt-dlp.
- `GET /readyz`: PostgreSQL + وجود FFmpeg/FFprobe.
- `GET /version`: إصدار التطبيق.

لا يتم تشغيل yt-dlp أو FFmpeg أثناء startup، وفشل Job أو خطأ Telegram مؤقت لا يسقط FastAPI container.

## الاختبارات

```bash
ruff check .
python -m compileall -q app tests
pytest -q
```

الاختبارات تستخدم mocks للـTelegram وyt-dlp وFFmpeg والمواقع الخارجية، وتغطي URL/SSRF، probing/formats، generic extractors، bulk/dedup، playlists، MP3، progress، retries، cancellation، FAST/PRECISE cuts، redaction، settings/DB URL، stale recovery، transitions، وregressions الخاصة بالـRouter/import/Telegram API/Redis dependencies/background tasks.

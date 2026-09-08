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
- أزرار Telegram الرئيسية أصبحت مسارات فعلية: **تحميل فيديو** يعرض الجودات، **MP3** يحلل ثم يضع تنزيل الصوت مباشرة في الطابور، **قص** يفتح خيارات القص بعد التحليل، و**تحميل عدة روابط** ينشئ Job مستقل لكل رابط.
- لا يحدث downgrade صامت: اختيار 1080p مثلًا يستخدم تطابق ارتفاع exact، وإذا لم يعد متاحًا تفشل المهمة بـ `FORMAT_UNAVAILABLE`.
- Metadata: العنوان، الصورة المصغرة، المدة، الناشر، المنصة والجودات.
- Bulk URLs مع normalization وdeduplication، وكل URL يصبح Job مستقلًا.
- Playlist confirmation/expansion إلى Jobs مستقلة بحد آمن افتراضي 10 عناصر.
- Progress على نفس رسالة Telegram: Job ID، الحالة، الجودة، Downloaded/Total، النسبة، الشريط، السرعة وETA.
- القص يدعم:
  - **قص حر**: بداية ونهاية يحددها المستخدم.
  - **30 ثانية للقصص**: يحدد المستخدم وقت البداية ويُنتج مقطعًا مدته 30 ثانية بالضبط.
  - **60 ثانية**: يحدد المستخدم وقت البداية ويُنتج مقطعًا مدته 60 ثانية بالضبط.
  - **FAST** عبر stream copy و**PRECISE** عبر H.264/AAC re-encode.
- cancellation فعلية للـyt-dlp وFFmpeg، retry محدود مع exponential backoff + jitter، startup reconciliation وcleanup.
- Dashboard: Overview / Downloads / Jobs / Users / Workers / Errors / System، مع Analyze وDownload وDownload All وCancel، إضافة إلى قص حر/30ث/60ث مع FAST/PRECISE.
- تبويب System يعرض `RAILWAY_GIT_COMMIT_SHA` وbranch وdeployment ID عندما يكون التشغيل من Railway، لتعرف فورًا أي Commit يعمل فعليًا.

## الحماية

كل URL يمر عبر طبقة حماية قبل yt-dlp: HTTP/HTTPS فقط، حظر localhost وprivate/link-local/reserved addresses وcloud metadata، والتحقق من DNS. `SafeYoutubeDL` يعيد فحص الطلبات التي ينفذها yt-dlp للمساعدة في حماية redirects. لا توجد آليات لتجاوز DRM أو paywalls أو private/authenticated media.

العمليات الخارجية تستخدم argument arrays فقط ولا تستخدم `shell=True`. كل Job يكتب داخل مجلد معزول، مع حدود للمدة والحجم والمهلة والتزامن والتنظيف التلقائي وSecret Redaction للـlogs.

## Dashboard

اللوحة **معطلة** افتراضيًا. إذا لم يكن `DASHBOARD_PASSWORD` مضبوطًا فإن `/dashboard` يعيد 404 ولا توجد لوحة غير محمية. هذا متغير اختياري وليس مطلوبًا لتشغيل البوت.

## Railway Auto Deploy

المستودع مهيأ لـRailway عبر `railway.json` وDockerfile، وGitHub Actions يعمل على `push` إلى `main`. النشر التلقائي نفسه هو إعداد Native داخل Railway وليس حقلًا في `railway.json`.

في خدمة Railway المرتبطة بهذا المستودع اضبط:

1. **Source → GitHub Repo:** `Mtzallqmy/Moataz-a`.
2. **Trigger Branch:** `main`.
3. **Autodeploy:** Enabled.
4. **Wait for CI:** Enabled، حتى لا تنشر Railway أي Commit إلا بعد نجاح GitHub Actions.

عند هذا الإعداد، أي Merge/Push ناجح إلى `main` سيبدأ Deployment تلقائيًا، وإذا فشل CI يتم تخطي النشر. لا يحتاج ذلك Railway token داخل المستودع ولا Secret إضافيًا.

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

الاختبارات تستخدم mocks للـTelegram وyt-dlp وFFmpeg والمواقع الخارجية، وتغطي URL/SSRF، probing/formats، generic extractors، bulk/dedup، playlists، MP3، progress، retries، cancellation، FAST/PRECISE cuts، القص الحر و30ث و60ث، redaction، settings/DB URL، stale recovery، transitions، وregressions الخاصة بالـRouter/import/Telegram API/Redis dependencies/background tasks.

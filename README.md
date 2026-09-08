# Moataz Media Bot

Media Download Manager يعمل من Telegram ومن Dashboard اختيارية، مبني على Python 3.12 وFastAPI وaiogram وPostgreSQL وyt-dlp وFFmpeg.

## التشغيل الأساسي

تشغيل ميزات الوسائط على Railway يحتاج متغيرين أساسيين فقط:

```env
BOT_TOKEN=
DATABASE_URL=
```

ولتفعيل دردشة AI عبر أي مزود **OpenAI-compatible** أضف اختياريًا:

```env
OPENAI_BASE_URL=
OPENAI_API_TOKEN=
```

`OPENAI_BASE_URL` يجب أن يكون جذر API المتوافق، مثل `https://provider.example/v1`. لا تضع API Token في رسائل Telegram أو داخل المستودع؛ مكانه الصحيح هو Railway Variables.

يعمل البوت افتراضيًا عبر **Telegram Polling**، وتعمل المهام عبر **Inline Queue** داخل الخدمة نفسها. لا يحتاج Redis أو ARQ أو Webhook أو Worker منفصل أو Cookies.

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
- القص يدعم **قص حر**، **30 ثانية للقصص**، **60 ثانية**، مع FAST stream-copy أو PRECISE H.264/AAC.
- cancellation فعلية للـyt-dlp وFFmpeg، retry محدود مع exponential backoff + jitter، startup reconciliation وcleanup.
- Dashboard: Overview / Downloads / Jobs / Users / Workers / Errors / System، مع Analyze وDownload وDownload All وCancel، إضافة إلى قص حر/30ث/60ث مع FAST/PRECISE.
- تبويب System يعرض `RAILWAY_GIT_COMMIT_SHA` وbranch وdeployment ID عندما يكون التشغيل من Railway، لتعرف أي Commit يعمل فعليًا.

## OpenAI-compatible AI Chat

عند ضبط `OPENAI_BASE_URL` و`OPENAI_API_TOKEN` يظهر مسار **🤖 دردشة AI** في Telegram. البوت يتصل فعليًا بـ:

- `GET {OPENAI_BASE_URL}/models` لقراءة النماذج الحقيقية.
- `POST {OPENAI_BASE_URL}/chat/completions` للمحادثة.

تختار النموذج من Telegram ثم ترسل الرسائل بشكل طبيعي، ويحتفظ البوت بسياق قصير للمحادثة داخل جلسة Telegram. يمكن بدء محادثة جديدة أو تغيير النموذج من الأزرار. إذا كان المزود لا يدعم `/models` أو `/chat/completions` بالشكل المتوافق مع OpenAI فلن يتم الادعاء بأنه مدعوم.

### أين أضع Base URL وAPI Token؟

**Railway → Service `Moataz-a` → Variables** هو المكان الموصى به:

```env
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_API_TOKEN=your-secret-token
```

لا توجد نافذة في البوت لإدخال الـToken عمدًا، حتى لا يمر السر عبر Telegram أو يدخل في history/logs. البوت يعرض فقط اختيار النموذج والاستخدام، وليس إدارة الأسرار.

## الحماية

كل URL وسائط يمر عبر طبقة حماية قبل yt-dlp: HTTP/HTTPS فقط، حظر localhost وprivate/link-local/reserved addresses وcloud metadata، والتحقق من DNS. `SafeYoutubeDL` يعيد فحص الطلبات التي ينفذها yt-dlp للمساعدة في حماية redirects. لا توجد آليات لتجاوز DRM أو paywalls أو private/authenticated media.

العمليات الخارجية تستخدم argument arrays فقط ولا تستخدم `shell=True`. كل Job يكتب داخل مجلد معزول، مع حدود للمدة والحجم والمهلة والتزامن والتنظيف التلقائي وSecret Redaction للـlogs.

## Dashboard

اللوحة **معطلة** افتراضيًا. إذا لم يكن `DASHBOARD_PASSWORD` مضبوطًا فإن `/dashboard` يعيد 404 ولا توجد لوحة غير محمية. هذا متغير اختياري وليس مطلوبًا لتشغيل البوت.

## Railway Auto Deploy

المستودع مهيأ لـRailway عبر `railway.json` وDockerfile، وGitHub Actions يعمل على `push` إلى `main`. النشر التلقائي نفسه إعداد Native داخل Railway.

في خدمة Railway المرتبطة بهذا المستودع اضبط:

1. **Source → GitHub Repo:** `Mtzallqmy/Moataz-a`.
2. **Trigger Branch:** `main`.
3. **Autodeploy:** Enabled.
4. **Wait for CI:** Enabled إن كان متاحًا في إعداد المصدر.

بهذا يصبح أي Merge/Push ناجح إلى `main` مرشحًا للنشر تلقائيًا. لا يحتاج ذلك Railway token داخل المستودع.

## Health

- `GET /healthz`: liveness بدون اتصال Telegram أو yt-dlp أو مزود AI.
- `GET /readyz`: PostgreSQL + وجود FFmpeg/FFprobe.
- `GET /version`: إصدار التطبيق.

لا يتم تشغيل yt-dlp أو FFmpeg أو AI أثناء startup، وفشل Job أو خطأ Telegram/AI مؤقت لا يسقط FastAPI container.

## الاختبارات

```bash
ruff check .
python -m compileall -q app tests
pytest -q
```

الاختبارات تستخدم mocks للـTelegram وyt-dlp وFFmpeg والمواقع الخارجية ومزود AI، وتغطي URL/SSRF، probing/formats، generic extractors، bulk/dedup، playlists، MP3، progress، retries، cancellation، FAST/PRECISE cuts، القص الحر و30ث و60ث، OpenAI-compatible models/chat، redaction، settings/DB URL، stale recovery، transitions، وregressions الخاصة بالـRouter/import/Telegram API/Redis dependencies/background tasks.

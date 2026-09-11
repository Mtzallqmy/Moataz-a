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

## Media Studio MVP

زر **🎞 مشروع مونتاج** في Telegram يفتح workflow مستقلًا عن Download Jobs:

1. أنشئ مشروعًا جديدًا.
2. أرسل فيديوهات أو صورًا أو ملفات صوت/voice أو documents وسائط، وأضف روابط URL مفردة أو متعددة ضمن حدود المشروع.
3. راجع **📦 المواد**، غيّر الترتيب، احذف الرابط من المشروع، أو عيّن أدوار `main / intro / outro / music / voice / logo / background` المتوافقة مع نوع الملف.
4. اختر Canvas: `9:16` أو `16:9` أو `1:1`، وFit: `fit / fill / blur-background`، وانتقال `fade / dissolve / slide / wipe / zoom / blur / push / dip-to-black`، ووضع الصوت `replace / mix / background music`، ومكان الشعار.
5. اختر القالب المقترح، تابع تقدم FFmpeg الحقيقي، ألغِ عند الحاجة، ثم استلم MP4 صالحًا. إذا تجاوز الناتج ميزانية Telegram يُضغط تكيفيًا قبل الإرسال.

القوالب المنفذة فعليًا: **Audio + Image، Slideshow بصوت اختياري، Merge Videos، Video + Audio، Intro/Main/Outro، Logo Overlay**. يتم توحيد المقاس وFPS وSAR وpixel format والصوت قبل الدمج، وتضاف silent audio للفيديو الصامت. الملفات الأصلية للأصول لا تُعدّل أثناء الرندر.

الرابط داخل المشروع يستخدم `DownloaderService` والحماية نفسها ضد SSRF والـprivate networks؛ Playlists لا تُضاف ككيان واحد في MVP، ويجب إرسال روابط العناصر المفردة. الملفات المرفوعة تمر بفحص الامتداد/MIME وFFprobe ولا يُستخدم اسم Telegram كمسار تخزين.

### Timeline V2 والمونتاج بالمحادثة

زر **🤖 مونتاج بالذكاء الاصطناعي** داخل المشروع يربط المشروع بأي نموذج نصي مفعّل في `AIProviderRegistry`. يمكن متابعة رفع الملفات والصور والصوت والـvoice والروابط في الوضع نفسه، ثم كتابة تعليمات طبيعية لتعديل المشروع الحالي بدل إنشاء نتيجة جديدة.

مصدر الحقيقة هو Timeline JSON مستقل عن FFmpeg وTelegram. يدعم مسارات visual/audio/overlay/text/subtitle، وعمليات trim/split/move/reorder، السرعة والصوت وfade، crop/scale/position، canvas وfit modes، الانتقالات، النصوص والترجمة، Intro/Main/Outro، keyframes قابلة للتوسعة، ونسخ revisions كاملة مع undo/redo. يمكن إزالة الصوت الأصلي من فيديو كامل، أو خفضه/كتمه ضمن نطاق زمني، واستبداله بملف Audio/Voice مع خيار المزج، وخفض الموسيقى تلقائيًا أثناء Voice متداخل. كل مجموعة تعديلات من Agent تحفظ كعملية ذرية قابلة للمراجعة والتراجع.

الـAI لا يحصل على shell ولا يبني FFmpeg command. النموذج يستدعي catalog أدوات محددة، ثم يتحقق `TimelineService` من الأداة والملكية والأنواع والحدود قبل تعديل Timeline. النماذج التي تدعم native tool calling تستخدمه، والبقية تستخدم Structured JSON مع تحقق وإعادة محاولة محدودة. مفاتيح المزود تبقى في متغيرات البيئة ولا تُكتب في Timeline أو سجل المحادثة.

يمكن طلب **معاينة** قصيرة منخفضة الدقة ثم متابعة المحادثة والتعديل، أو طلب **تصدير نهائي**. ومن **مشاريعي** يمكن إعادة فتح مشروع مكتمل، إضافة مواد جديدة وإعادة ترتيبها ثم رندره مجددًا؛ يظل ملف الرندر السابق مستقلًا ولا تُعدّل الأصول الأصلية. FFmpegRenderer يترجم Timeline إلى filter graph آمن ويدعم compositing للنصوص والشعارات والترجمة والمزج متعدد المسارات. Remotion ليس dependency؛ واجهة `BaseRenderer` تبقي إضافة renderer اختياري لاحقًا ممكنة.

اعتمد التصميم على فصل Timeline/commands الموجود في OpenChatCut وفكرة composition/op-log في MakeMyClip كمرجع معماري فقط. لم يُنسخ كود AGPL من OpenChatCut.

### متغيرات Media Studio

جميعها اختيارية ولها defaults آمنة، وأسماؤها موثقة أيضًا في `.env.example`:

```env
PROJECT_DIR=/data/projects
RENDER_TEMP_DIR=/data/tmp
MAX_PROJECT_ASSETS=20
MAX_PROJECT_DURATION_SECONDS=1800
MAX_RENDER_DURATION_SECONDS=1800
MAX_CONCURRENT_RENDERS=1
MAX_RENDERS_PER_USER=1
RENDER_TIMEOUT_SECONDS=1800
MAX_RENDER_RETRIES=1
DEFAULT_RENDER_FPS=30
```

تُطبّق كذلك الحدود العامة `MAX_FILE_SIZE_MB` و`MAX_VIDEO_DURATION_SECONDS` و`TELEGRAM_UPLOAD_LIMIT_MB` على ingestion والتسليم. تحديث رسالة Telegram مضبوط بواسطة `PROGRESS_UPDATE_SECONDS` ولا يستخدم timer وهميًا.

## OpenAI-compatible AI Chat

عند ضبط `OPENAI_BASE_URL` و`OPENAI_API_TOKEN` يظهر مسار **🤖 دردشة AI** في Telegram. البوت يتصل فعليًا بـ:

- `GET {OPENAI_BASE_URL}/models` لقراءة النماذج الحقيقية.
- `POST {OPENAI_BASE_URL}/chat/completions` للمحادثة.

تختار النموذج من Telegram ثم ترسل الرسائل بشكل طبيعي، ويحتفظ البوت بسياق قصير للمحادثة داخل جلسة Telegram. يمكن بدء محادثة جديدة أو تغيير النموذج من الأزرار. إذا كان المزود لا يدعم `/models` أو `/chat/completions` بالشكل المتوافق مع OpenAI فلن يتم الادعاء بأنه مدعوم.

يدعم registry إعدادات OpenAI وOpenRouter وDeepSeek وواجهة Gemini المتوافقة مع OpenAI، إضافة إلى Runware وNVIDIA وAgentRouter وxAI وGroq وأي عدد من المزودين المخصصين عبر `AI_PROVIDER_<ID>_*`. لكل مزود Base URL وAPI key وأولوية، ولكل نموذج capabilities مكتشفة من catalog المزود.

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

الاختبارات لا تعتمد على Telegram أو YouTube أو AI provider حي. وهي تغطي أيضًا fixtures مولدة بـFFmpeg لكل قوالب الاستوديو، Timeline متعدد المسارات، الانتقالات الثمانية، النصوص والترجمة والـoverlays، كتم واستبدال الصوت والنطاقات الزمنية، revisions وundo/redo، إعادة فتح المشروع، Agent tool validation وStructured JSON/native tools، preview، نسب العرض وfit modes، الفيديو الصامت واختلاف FPS/المقاسات، تقدم FFmpeg، الإلغاء والتنظيف، recovery بعد restart، ownership، Telegram uploads، URL ingestion، وفشل التسليم دون تحويل render ناجح إلى FAILED.

# Moataz Media Bot

مدير تنزيل ومعالجة وسائط يعمل من Telegram وDashboard وMedia Studio، مبني على Python 3.12 وFastAPI وaiogram وSQLAlchemy وyt-dlp وFFmpeg.

## التشغيل الأساسي

الحد الأدنى للتشغيل:

```env
BOT_TOKEN=
DATABASE_URL=
```

ثم:

```bash
python -m app.main
```

على Railway يمرر `PORT` تلقائيًا. التطبيق يشغّل Telegram Polling وInline Queue داخل الخدمة نفسها، ولا يحتاج Redis أو Worker منفصل للمسار الحالي.

## مسار التنزيل في Production

المسار الفعلي موحد الآن:

```text
Telegram / Dashboard / Media Studio
        ↓
DownloaderService compatibility facade
        ↓
DownloadManager
        ↓
PlatformDetector
        ↓
ProviderRouter
        ↓
DownloadBackend adapters
        ↓
NormalizedMediaResult
        ↓
FFmpeg / MP3 / cut / 30-60 split / AssetService
```

`DownloaderService` يحافظ على API القديم المستخدم في handlers والworker:

- `probe()`
- `get_formats()`
- `download()`
- `download_audio()`
- `expand_playlist()`
- `cancel()`
- `classify_error()`

ولا يوجد Cobalt fallback منفصل خارج الـRouter. إذا فشل backend أول ثم نجح التالي، لا يظهر فشل الأول للمستخدم.

## ترتيب Backends

الترتيب الحالي حسب المنصة ونوع المحتوى:

| المنصة | الترتيب |
| --- | --- |
| YouTube | `yt-dlp → Cobalt → pytubefix` |
| Instagram Story | `gallery-dl → Instaloader → yt-dlp → Cobalt` |
| Instagram Highlight | `gallery-dl → Instaloader → yt-dlp → Cobalt` |
| Instagram Post/Reel | `yt-dlp → Cobalt → gallery-dl → Instaloader` |
| TikTok/Douyin Story | `gallery-dl → TikTok sidecar → yt-dlp → Cobalt` |
| TikTok/Douyin | `yt-dlp → Cobalt → TikTok sidecar → gallery-dl` |
| Facebook | `yt-dlp → Cobalt → gallery-dl` |
| Generic URLs | `yt-dlp → Cobalt → gallery-dl` |

داخل `yt-dlp` نفسه يوجد ترتيب attempts مستقل: direct ثم browser impersonation عبر `curl_cffi` عند توفره، ثم الـproxies المضبوطة.

لا يحدث fallback عند `AUTH_REQUIRED` أو `PRIVATE_MEDIA`. هذه الحالات لا تُحسب أيضًا كعطل عالمي في Circuit Breaker. أما `HTTP_403`, `HTTP_429`, anti-bot, timeouts, upstream 5xx والأعطال المؤقتة فتسمح بالانتقال إلى backend التالي.

## إعدادات yt-dlp وCobalt

```env
YTDLP_COOKIES_FILE=
YTDLP_COOKIES_B64=
YTDLP_PROXY_URLS=
YTDLP_IMPERSONATE=true

COBALT_API_URLS=
COBALT_API_TOKEN=
COBALT_AUTH_SCHEME=Api-Key
COBALT_TIMEOUT_SECONDS=45

DOWNLOAD_BACKEND_FAILURE_THRESHOLD=3
DOWNLOAD_BACKEND_COOLDOWN_SECONDS=120
```

- `YTDLP_COOKIES_FILE`: ملف Netscape `cookies.txt` mounted.
- `YTDLP_COOKIES_B64`: بديل مناسب للـRailway Variables. لا تضبط الملف وBase64 معًا إلا إذا كنت تقصد أن يكون الملف هو المصدر الأول.
- `YTDLP_PROXY_URLS`: حتى أربعة مخارج مرتبة، مفصولة بفاصلة أو سطر جديد.
- `COBALT_API_URLS`: حتى أربعة API roots ذاتية الاستضافة أو مصرح باستخدامها.
- `COBALT_API_TOKEN`: سر، ويُرسل فقط إلى Cobalt endpoint الموافق.

لا يضيف المشروع `api.cobalt.tools` تلقائيًا ولا يجب استخدام الخادم العام دون إذن من مشغله.

### تشغيل Cobalt ذاتيًا

المشروع لا يضم كود Cobalt. شغّل instance منفصلًا وفق توثيق Cobalt الرسمي. مثال Docker أساسي:

```yaml
services:
  cobalt:
    image: ghcr.io/imputnet/cobalt:11
    restart: unless-stopped
    ports:
      - "9000:9000"
    environment:
      API_URL: "https://cobalt.example.com/"
```

إذا كان الـinstance مكشوفًا للإنترنت، فعّل API-key protection في Cobalt بدل تركه مفتوحًا. Cobalt يدعم `API_KEY_URL` و`API_AUTH_REQUIRED=1`، والعميل هنا يرسل المفتاح بهذا الشكل:

```text
Authorization: Api-Key <uuid-key>
```

ثم اضبط في التطبيق:

```env
COBALT_API_URLS=https://cobalt.example.com
COBALT_API_TOKEN=<uuid-key>
COBALT_AUTH_SCHEME=Api-Key
```

توثيق Cobalt الرسمي:

- https://github.com/imputnet/cobalt/blob/main/docs/run-an-instance.md
- https://github.com/imputnet/cobalt/blob/main/docs/protect-an-instance.md
- https://github.com/imputnet/cobalt/blob/main/docs/api.md

## gallery-dl

`gallery-dl` مدمج كـCLI adapter معزول؛ لا يتم استيراد أو نسخ كوده داخل المشروع. صورة Docker الكاملة تثبت executable خارجيًا، لكن backend يبقى معطلًا حتى تفعيله:

```env
GALLERYDL_ENABLED=true
GALLERYDL_COOKIE_FILE=
GALLERYDL_TIMEOUT_SECONDS=180
```

التنفيذ يستخدم `asyncio.create_subprocess_exec()` مع argument array فقط، بدون `shell=True`. الملفات تبقى داخل Job directory، مع timeout وcancellation وterminate/kill وتنظيف partial outputs. الناتج يُفحص كمحتوى وسائط ولا يعتمد على الامتداد وحده.

يستخدم خصوصًا لصور/Carousels Instagram، Stories/Highlights عندما يستطيع extractor الوصول إليها، وصور TikTok عند دعم extractor الحالي.

## Instaloader

Instaloader optional dependency وlazy import. صورة Railway الكاملة تثبته، لكن تفعيله صريح:

```env
INSTALOADER_ENABLED=true
INSTAGRAM_SESSION_FILE=
```

Posts/Reels يمكن تجربتها بدون session. Story مفردة تستخدم media id مباشرة ولا تقوم بتنزيل Profile كامل. Stories تتطلب `INSTAGRAM_SESSION_FILE` مصرحًا به.

Highlight URLs تُمرر أولًا إلى gallery-dl. Instaloader لا يقوم بعمل profile crawl لمجرد Highlight URL لا يمكن حله بأمان إلى عنصر مفرد؛ في هذه الحالة يرفض adapter المسار ويستمر Router في fallback المسموح.

اسم المستخدم أو محتوى session لا يُخزن في Job metadata.

## pytubefix

pytubefix fallback اختياري ليوتيوب بعد yt-dlp وCobalt:

```env
PYTUBEFIX_ENABLED=true
```

يدعم metadata للفيديو وplaylist، تنزيل video/audio، اختيار streams بشكل deterministic، ودمج adaptive video+audio عبر FFmpeg عند الحاجة. الاستيراد lazy، وغياب الحزمة لا يسقط Startup أو Edge Runtime.

pytubefix ليس ضمانًا لتجاوز YouTube IP/anti-bot challenge؛ قد يتأثر بالحظر نفسه.

## TikTok / Douyin sidecar

لا يحتوي المستودع TikTokDownloader أو أي كود GPL-3.0 منه. التكامل عبارة عن adapter إلى sidecar/API يشغله مالك النظام:

```env
TIKTOK_BACKEND_ENABLED=false
TIKTOK_BACKEND_URL=
TIKTOK_BACKEND_TOKEN=
TIKTOK_COOKIE_FILE=
TIKTOK_BACKEND_TIMEOUT_SECONDS=60
```

يبقى backend معطلًا إذا لم يكن `TIKTOK_BACKEND_ENABLED=true` أو لم يوجد URL. الواجهة تدعم تدريجيًا `video`, `images/slideshow`, `audio` و`story` عندما يوفرها الـsidecar. HTTP 401/403 من الـsidecar يصنف كـ`AUTH_REQUIRED` ولا يفتح fallback غير مصرح.

لا تستخدم sidecar عامًا مجهول المصدر، ولا ترسل cookies إليه إلا إذا كنت أنت مشغل الخدمة أو تثق بها صراحة.

## Anti-bot على Railway

وجود Multi-Backend routing لا يعني أن حظر YouTube أصبح محلولًا. إذا لم تتوفر Cookies أو Proxy أو Cobalt self-hosted/authorized، فقد يستمر الخطأ:

```text
Sign in to confirm you’re not a bot
```

المسار يحافظ على browser impersonation، ويجرب proxies/Cobalt/pytubefix عند السماح، لكنه لا يتجاوز DRM ولا تسجيل الدخول ولا صلاحيات المحتوى الخاص.

إذا كانت كل المخارج من عناوين datacenter محظورة، فقد تفشل جميع backends. في هذه الحالة الحل التشغيلي هو توفير credentials صالحة أو egress مصرح أو Cobalt instance على شبكة مناسبة، وليس إضافة public API عشوائي.

## Observability وHealth

كل محاولة backend تسجل حقولًا منظمة بدون أسرار:

```text
job=<id> platform=<platform> backend=<backend> result=<SUCCESS|ERROR_CODE> fallback_count=<n>
```

ويُحفظ في schema الحالي عبر `JobEvent`:

- `attempted_backends`
- `successful_backend`
- `normalized_error`
- `fallback_count`

لم تتم إعادة تسمية أي جدول ولم تُفرض migration لكسر `create_all` deployments الحالية.

Dashboard يعرض health snapshot لكل platform/backend (`HEALTHY`, `DEGRADED`, `OPEN`, `UNAVAILABLE`) ولا يعرض cookies أو proxy credentials أو tokens أو signed URLs.

## الأسرار والحماية

الحقول الحساسة تستخدم `SecretStr` حيث يلزم. الـlogging يزيل أو يخفي:

- Cookies
- `Authorization`
- API keys/tokens
- Proxy credentials
- signed URL query parameters
- Telegram bot token وDatabase credentials

روابط المستخدم تمر بحماية SSRF، وتُرفض private/special IPs وcredential-bearing URLs والمنافذ غير القياسية.

## Media Studio

Media Studio يدعم مشاريع تحتوي فيديو/صور/صوت/voice/subtitles وروابط URL. URL ingestion يمر بنفس `DownloaderService → DownloadManager` ثم يتحول الناتج إلى Asset بعد فحصه.

القص والتقسيم 30/60 ثانية يعيدان Job نفسه إلى Queue؛ التنزيل الأصلي يتم أولًا عبر DownloadManager ثم يطبق FFmpeg العملية على ملف backend الناجح. MP3 يستخدم المسار نفسه.

Timeline/AI layer منفصل عن shell: النموذج لا ينشئ أوامر FFmpeg مباشرة، وإنما يطلب أدوات محددة وتتحقق الخدمات من الملكية والأنواع والحدود قبل تعديل Timeline أو بدء Render.

## Dependencies الاختيارية

التثبيت الأساسي يبقى بدون specialist Python packages:

```bash
pip install .
```

لتثبيت Instaloader وpytubefix:

```bash
pip install ".[download-specialists]"
```

وصورة Docker الإنتاجية تثبت أيضًا `gallery-dl` كـCLI منفصل. Edge Runtime workflow يتعمد الاستيراد بدون هذه optional dependencies للتأكد أن غيابها لا يمنع startup.

## الاختبارات

لا تتصل اختبارات CI بمنصات أو APIs حقيقية؛ تستخدم mocks وfixtures محلية.

شغّل قبل الدمج أو النشر:

```bash
ruff check .
python -m compileall -q app tests
pytest -q
```

CI يغطي، من ضمن ما يغطيه:

- yt-dlp success وfallback إلى Cobalt ثم pytubefix.
- 403/429/timeouts/anti-bot وكل-backends-fail.
- عدم fallback لـAUTH_REQUIRED/private media.
- Circuit Breaker per platform وcooldown recovery.
- gallery-dl CLI argument isolation وcancellation cleanup.
- Instaloader carousel وStory session behavior.
- pytubefix video/audio/playlist deterministic selection.
- TikTok sidecar video/audio/slideshow وAUTH behavior.
- MP3/القص/التقسيم/Media Studio عبر الـfacade الموحد.
- Secret redaction وEdge Runtime imports بدون optional dependencies.

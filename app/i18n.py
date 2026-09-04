MESSAGES = {
    "ar": {
        "welcome": (
            "👋 أهلاً بك في {name}\n\n"
            "مدير وسائط شخصي لتحليل وتنزيل ومعالجة روابط YouTube وFacebook.\n"
            "أرسل الرابط مباشرة أو استخدم الأزرار أدناه."
        ),
        "private": "⛔ هذا البوت خاص وغير متاح لهذا الحساب.",
        "send_link_prompt": "🔗 أرسل رابط YouTube أو Facebook وسأعرض لك الجودات المتاحة.",
        "help": (
            "ℹ️ طريقة الاستخدام\n\n"
            "1) أرسل رابط YouTube أو Facebook.\n"
            "2) انتظر تحليل الفيديو.\n"
            "3) اختر الجودة أو MP3 أو القص.\n"
            "4) تابع النسبة والسرعة والوقت المتبقي من نفس الرسالة.\n\n"
            "يمكنك إلغاء المهمة أثناء التنفيذ أو إعادة محاولة المهمة الفاشلة."
        ),
        "send_link_button": "🎬 إرسال رابط",
        "jobs_button": "📋 تحميلاتي",
        "language_button": "🌐 اللغة",
        "help_button": "ℹ️ مساعدة",
        "cancel_button": "❌ إلغاء المهمة",
        "retry_button": "🔄 إعادة المحاولة",
        "back_button": "↩️ رجوع",
        "analyzing": "🔎 جاري تحليل الرابط...",
        "invalid_url": "⚠️ أرسل رابطًا صالحًا من YouTube أو Facebook.",
        "unsupported": "⚠️ الرابط غير مدعوم أو تعذر تحليله.",
        "too_long": "⚠️ مدة الفيديو تتجاوز الحد المسموح.",
        "choose_quality": (
            "🎬 {title}\n"
            "🌐 {platform}\n"
            "⏱ {duration}\n"
            "🆔 #{job_id}\n\n"
            "اختر الجودة:"
        ),
        "queued": "🕓 المهمة #{job_id} في قائمة الانتظار.",
        "download_start": "⬇️ بدء تنزيل الملف...",
        "download": "⬇️ التحميل {progress:.1f}%\n{bar}\n⚡ {speed}\n⏳ {eta}",
        "retrying": "🔄 مشكلة مؤقتة في التنزيل. إعادة المحاولة {attempt}/{max_attempts}...",
        "retrying_upload": "🔄 مشكلة مؤقتة في الرفع. إعادة المحاولة {attempt}/{max_attempts}...",
        "processing": "🎞 جاري معالجة الفيديو...",
        "cutting": "✂️ جاري قص الفيديو...",
        "uploading": "☁️ جاري رفع الملف إلى Telegram...",
        "done": "✅ اكتمل التحميل والإرسال بنجاح.",
        "cancelled": "🚫 تم إلغاء المهمة.",
        "failed": "❌ فشلت المهمة:\n{error}",
        "failed_code": "❌ تعذر إكمال المهمة.\nرمز الخطأ: {code}",
        "cut_prompt": "✂️ أرسل وقت البداية والنهاية بهذا الشكل:\n00:00:10-00:00:45",
        "cut_invalid": "⚠️ صيغة الوقت غير صحيحة. مثال: 00:00:10-00:00:45",
        "cut_queued": "✂️ تم حفظ المقطع وإضافة المهمة #{job_id} إلى قائمة الانتظار.",
        "language_changed": "✅ تم تغيير اللغة إلى العربية.",
        "jobs_empty": "📋 لا توجد لديك مهام حتى الآن.",
        "jobs_header": "📋 آخر مهامك:\n\n{jobs}",
        "job_not_found": "المهمة غير موجودة.",
        "already_queued": "المهمة قيد التنفيذ بالفعل.",
        "active_limit": "لديك الحد الأقصى من المهام النشطة: {limit}",
        "retry_queued": "🔄 تمت إعادة المهمة #{job_id} إلى قائمة الانتظار.",
        "cannot_retry": "لا يمكن إعادة هذه المهمة في حالتها الحالية.",
        "cancel_requested": "🚫 تم طلب إلغاء المهمة #{job_id}.",
        "cannot_cancel": "لا يمكن إلغاء هذه المهمة في حالتها الحالية.",
    },
    "en": {
        "welcome": (
            "👋 Welcome to {name}\n\n"
            "A personal media manager for analyzing, downloading and processing YouTube and Facebook links.\n"
            "Send a link directly or use the buttons below."
        ),
        "private": "⛔ This bot is private and your account is not allowed.",
        "send_link_prompt": "🔗 Send a YouTube or Facebook link and I will show the available qualities.",
        "help": (
            "ℹ️ How to use\n\n"
            "1) Send a YouTube or Facebook link.\n"
            "2) Wait for analysis.\n"
            "3) Choose a quality, MP3, or clip.\n"
            "4) Follow progress, speed and ETA in the same message.\n\n"
            "You can cancel a running job or retry a failed one."
        ),
        "send_link_button": "🎬 Send link",
        "jobs_button": "📋 My downloads",
        "language_button": "🌐 Language",
        "help_button": "ℹ️ Help",
        "cancel_button": "❌ Cancel job",
        "retry_button": "🔄 Retry",
        "back_button": "↩️ Back",
        "analyzing": "🔎 Analyzing the link...",
        "invalid_url": "⚠️ Send a valid YouTube or Facebook URL.",
        "unsupported": "⚠️ Unsupported URL or the media could not be analyzed.",
        "too_long": "⚠️ The video duration exceeds the configured limit.",
        "choose_quality": (
            "🎬 {title}\n"
            "🌐 {platform}\n"
            "⏱ {duration}\n"
            "🆔 #{job_id}\n\n"
            "Choose quality:"
        ),
        "queued": "🕓 Job #{job_id} is queued.",
        "download_start": "⬇️ Starting download...",
        "download": "⬇️ Download {progress:.1f}%\n{bar}\n⚡ {speed}\n⏳ {eta}",
        "retrying": "🔄 Temporary download problem. Retry {attempt}/{max_attempts}...",
        "retrying_upload": "🔄 Temporary upload problem. Retry {attempt}/{max_attempts}...",
        "processing": "🎞 Processing video...",
        "cutting": "✂️ Cutting video...",
        "uploading": "☁️ Uploading the file to Telegram...",
        "done": "✅ Download and delivery completed successfully.",
        "cancelled": "🚫 Job cancelled.",
        "failed": "❌ Job failed:\n{error}",
        "failed_code": "❌ The job could not be completed.\nError code: {code}",
        "cut_prompt": "✂️ Send start and end time in this format:\n00:00:10-00:00:45",
        "cut_invalid": "⚠️ Invalid time format. Example: 00:00:10-00:00:45",
        "cut_queued": "✂️ Clip saved and job #{job_id} was added to the queue.",
        "language_changed": "✅ Language changed to English.",
        "jobs_empty": "📋 You do not have any jobs yet.",
        "jobs_header": "📋 Your latest jobs:\n\n{jobs}",
        "job_not_found": "Job not found.",
        "already_queued": "This job is already running.",
        "active_limit": "You reached the active job limit: {limit}",
        "retry_queued": "🔄 Job #{job_id} was queued again.",
        "cannot_retry": "This job cannot be retried in its current state.",
        "cancel_requested": "🚫 Cancellation requested for job #{job_id}.",
        "cannot_cancel": "This job cannot be cancelled in its current state.",
    },
}

STATUS_LABELS = {
    "ar": {
        "PENDING": "⏳ جديد",
        "ANALYZING": "🔎 تحليل",
        "PROBING": "🔎 تحليل",
        "READY": "✅ جاهز",
        "QUEUED": "🕓 انتظار",
        "RETRYING": "🔄 إعادة محاولة",
        "DOWNLOADING": "⬇️ تنزيل",
        "MERGING": "🔗 دمج",
        "PROCESSING": "🎞 معالجة",
        "CUTTING": "✂️ قص",
        "UPLOADING": "☁️ رفع",
        "COMPLETED": "✅ مكتمل",
        "FAILED": "❌ فشل",
        "CANCELLED": "🚫 ملغي",
    },
    "en": {
        "PENDING": "⏳ New",
        "ANALYZING": "🔎 Analyzing",
        "PROBING": "🔎 Analyzing",
        "READY": "✅ Ready",
        "QUEUED": "🕓 Queued",
        "RETRYING": "🔄 Retrying",
        "DOWNLOADING": "⬇️ Downloading",
        "MERGING": "🔗 Merging",
        "PROCESSING": "🎞 Processing",
        "CUTTING": "✂️ Cutting",
        "UPLOADING": "☁️ Uploading",
        "COMPLETED": "✅ Completed",
        "FAILED": "❌ Failed",
        "CANCELLED": "🚫 Cancelled",
    },
}


def tr(language: str, key: str, **kwargs) -> str:
    catalog = MESSAGES.get(language, MESSAGES["ar"])
    return catalog.get(key, key).format(**kwargs)


def status_label(language: str, status: str) -> str:
    catalog = STATUS_LABELS.get(language, STATUS_LABELS["ar"])
    return catalog.get(status, status)

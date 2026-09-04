MESSAGES = {
    "ar": {
        "welcome": "👋 أهلاً بك في {name}\n\nأرسل رابط YouTube أو Facebook وسأحلله لك.",
        "private": "⛔ هذا البوت خاص وغير متاح لهذا الحساب.",
        "analyzing": "🔎 جاري تحليل الرابط...",
        "invalid_url": "⚠️ أرسل رابطًا صالحًا من YouTube أو Facebook.",
        "unsupported": "⚠️ الرابط غير مدعوم أو تعذر تحليله.",
        "too_long": "⚠️ مدة الفيديو تتجاوز الحد المسموح.",
        "choose_quality": "🎬 {title}\n⏱ {duration}\n\nاختر الجودة:",
        "queued": "🕓 تمت إضافة المهمة إلى قائمة الانتظار.",
        "download": "⬇️ التحميل {progress:.1f}%\n{bar}\n⚡ {speed}\n⏳ {eta}",
        "processing": "🎞 جاري معالجة الفيديو...",
        "uploading": "☁️ جاري رفع الملف إلى Telegram...",
        "done": "✅ اكتمل التحميل والإرسال.",
        "failed": "❌ فشلت المهمة:\n{error}",
        "cut_prompt": "✂️ أرسل وقت البداية والنهاية بهذا الشكل:\n00:00:10-00:00:45",
        "cut_invalid": "⚠️ صيغة الوقت غير صحيحة. مثال: 00:00:10-00:00:45",
        "cut_queued": "✂️ تم حفظ المقطع وإضافته إلى قائمة الانتظار.",
        "language_changed": "✅ تم تغيير اللغة إلى العربية.",
    },
    "en": {
        "welcome": "👋 Welcome to {name}\n\nSend a YouTube or Facebook link and I will analyze it.",
        "private": "⛔ This bot is private and your account is not allowed.",
        "analyzing": "🔎 Analyzing the link...",
        "invalid_url": "⚠️ Send a valid YouTube or Facebook URL.",
        "unsupported": "⚠️ Unsupported URL or the media could not be analyzed.",
        "too_long": "⚠️ The video duration exceeds the configured limit.",
        "choose_quality": "🎬 {title}\n⏱ {duration}\n\nChoose quality:",
        "queued": "🕓 The job was added to the queue.",
        "download": "⬇️ Download {progress:.1f}%\n{bar}\n⚡ {speed}\n⏳ {eta}",
        "processing": "🎞 Processing video...",
        "uploading": "☁️ Uploading the file to Telegram...",
        "done": "✅ Download and delivery completed.",
        "failed": "❌ Job failed:\n{error}",
        "cut_prompt": "✂️ Send start and end time in this format:\n00:00:10-00:00:45",
        "cut_invalid": "⚠️ Invalid time format. Example: 00:00:10-00:00:45",
        "cut_queued": "✂️ Clip saved and added to the queue.",
        "language_changed": "✅ Language changed to English.",
    },
}


def tr(language: str, key: str, **kwargs) -> str:
    catalog = MESSAGES.get(language, MESSAGES["ar"])
    return catalog.get(key, key).format(**kwargs)

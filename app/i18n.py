from __future__ import annotations

STRINGS = {
    "ar": {
        "welcome": "مرحبًا بك في {name}. اختر ما تريد ثم أرسل رابطًا عامًا للوسائط.",
        "help": "أرسل رابطًا واحدًا أو عدة روابط. سأحلل الوسائط أولًا ثم أعرض الجودات المتاحة فعليًا وMP3 والقص.",
        "private": "هذا المستخدم غير مسموح له حاليًا.",
        "send_prompt": "أرسل رابط HTTP/HTTPS عام. سيتم تحليله قبل التنزيل.",
        "bulk_prompt": "أرسل عدة روابط مفصولة بمسافة أو سطر جديد. الروابط المكررة تُحذف تلقائيًا.",
        "analyzing": "🔎 ANALYZING…",
        "invalid_url": "الرابط غير صالح أو حظرته حماية SSRF.",
        "failed": "تعذر تحليل الوسائط: {code}",
        "jobs_empty": "لا توجد تنزيلات بعد.",
        "cut_prompt": "أرسل المدى بهذا الشكل: 00:10 - 00:45",
        "cut_invalid": "مدى القص غير صالح أو يتجاوز مدة الوسائط.",
        "queued": "تمت إضافة Job #{job_id} إلى الطابور.",
    },
    "en": {
        "welcome": "Welcome to {name}. Choose an action and send a public media URL.",
        "help": "Send one URL or multiple URLs. Media is analyzed first; only real resolutions, MP3 and cutting options are shown.",
        "private": "This user is currently not allowed.",
        "send_prompt": "Send a public HTTP/HTTPS media URL. It will be analyzed before downloading.",
        "bulk_prompt": "Send multiple URLs separated by spaces or new lines. Duplicates are removed automatically.",
        "analyzing": "🔎 ANALYZING…",
        "invalid_url": "The URL is invalid or was blocked by SSRF protection.",
        "failed": "Media analysis failed: {code}",
        "jobs_empty": "No downloads yet.",
        "cut_prompt": "Send the range like: 00:10 - 00:45",
        "cut_invalid": "Invalid cut range or it exceeds the media duration.",
        "queued": "Job #{job_id} was queued.",
    },
}


def tr(language: str, key: str, **kwargs) -> str:
    table = STRINGS.get(language, STRINGS["ar"])
    template = table.get(key) or STRINGS["en"].get(key) or key
    return template.format(**kwargs)


def status_label(language: str, status: str) -> str:  # noqa: ARG001
    return status

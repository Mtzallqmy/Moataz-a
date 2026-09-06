from __future__ import annotations

from dataclasses import dataclass


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def format_speed(value: int | float | None) -> str:
    return "—" if value is None else f"{format_bytes(value)}/s"


def format_eta(value: int | float | None) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def progress_bar(percent: float, width: int = 12) -> str:
    bounded = max(0.0, min(100.0, percent))
    filled = round((bounded / 100) * width)
    return "█" * filled + "░" * (width - filled)


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    downloaded: int
    total: int
    percent: float
    speed: float | None
    eta: float | None

    @classmethod
    def from_ytdlp(cls, payload: dict) -> "ProgressSnapshot":
        downloaded = int(payload.get("downloaded_bytes") or 0)
        total = int(payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0)
        percent = (downloaded / total * 100.0) if total > 0 else 0.0
        return cls(downloaded, total, max(0.0, min(100.0, percent)), payload.get("speed"), payload.get("eta"))

    def render(self, *, job_id: int, status: str, quality: str) -> str:
        total = format_bytes(self.total) if self.total else "—"
        return (
            f"Job #{job_id}\n"
            f"Status: {status}\n"
            f"Quality: {quality}\n"
            f"Downloaded: {format_bytes(self.downloaded)} / {total}\n"
            f"Progress: {self.percent:.1f}%\n"
            f"{progress_bar(self.percent)}\n"
            f"Speed: {format_speed(self.speed)}\n"
            f"ETA: {format_eta(self.eta)}"
        )

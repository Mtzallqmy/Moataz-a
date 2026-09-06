from __future__ import annotations

from datetime import timedelta

from app import progress as _progress


def progress_bar(percent: float, width: int = 12) -> str:
    return _progress.progress_bar(percent, width)


def parse_time(value: str) -> float:
    text = value.strip()
    if not text or text.startswith("-"):
        raise ValueError("Invalid time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError("Invalid time")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError("Invalid time") from exc
    if any(number < 0 for number in numbers):
        raise ValueError("Invalid time")
    if len(numbers) >= 2 and numbers[-1] >= 60:
        raise ValueError("Invalid seconds")
    if len(numbers) == 3 and numbers[-2] >= 60:
        raise ValueError("Invalid minutes")
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def seconds_to_hms(value: int | float | None) -> str:
    if value is None:
        return "—"
    return str(timedelta(seconds=int(value)))


__all__ = ["parse_time", "progress_bar", "seconds_to_hms"]

from datetime import timedelta


def seconds_to_hms(seconds: int | float | None) -> str:
    if not seconds:
        return "00:00"
    return str(timedelta(seconds=int(seconds)))


def parse_time(value: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("invalid time")
    nums = [float(part) for part in parts]
    if any(num < 0 for num in nums):
        raise ValueError("invalid time")
    if len(nums) == 3:
        hours, minutes, seconds = nums
    elif len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    else:
        hours, minutes, seconds = 0, 0, nums[0]
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid time")
    return hours * 3600 + minutes * 60 + seconds


def progress_bar(progress: float, width: int = 12) -> str:
    progress = max(0.0, min(100.0, progress))
    filled = round(width * progress / 100)
    return "█" * filled + "░" * (width - filled)

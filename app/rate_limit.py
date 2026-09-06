from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Small process-local rate limiter for a single-service deployment."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            if not bucket:
                self._hits.pop(key, None)
            return True


telegram_analyze_limiter = SlidingWindowLimiter(limit=12, window_seconds=60)
dashboard_write_limiter = SlidingWindowLimiter(limit=30, window_seconds=60)

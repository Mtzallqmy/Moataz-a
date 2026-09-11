from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from app.errors import ErrorCode, ErrorInfo


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OPEN = "OPEN"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(slots=True)
class BackendHealth:
    failures: int = 0
    successes: int = 0
    open_until: float = 0.0
    last_error: ErrorCode | None = None


_CIRCUIT_ERRORS = {
    ErrorCode.ANTI_BOT,
    ErrorCode.HTTP_403,
    ErrorCode.HTTP_429,
    ErrorCode.NETWORK_TIMEOUT,
    ErrorCode.UPSTREAM_5XX,
    ErrorCode.EXTRACTOR_ERROR,
    ErrorCode.BACKEND_UNAVAILABLE,
}


class BackendHealthRegistry:
    """Small in-memory circuit breaker scoped by backend and platform."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 120.0,
        clock=time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(1.0, cooldown_seconds)
        self.clock = clock
        self._records: dict[tuple[str, str], BackendHealth] = {}

    def _record(self, backend: str, platform: str) -> BackendHealth:
        return self._records.setdefault((backend, platform), BackendHealth())

    def allows(self, backend: str, platform: str) -> bool:
        record = self._record(backend, platform)
        if record.open_until and self.clock() >= record.open_until:
            record.open_until = 0.0
            record.failures = max(0, self.failure_threshold - 1)
        return record.open_until <= 0.0

    def success(self, backend: str, platform: str) -> None:
        record = self._record(backend, platform)
        record.successes += 1
        record.failures = 0
        record.open_until = 0.0
        record.last_error = None

    def failure(self, backend: str, platform: str, error: ErrorInfo) -> None:
        if error.code not in _CIRCUIT_ERRORS:
            return
        record = self._record(backend, platform)
        record.failures += 1
        record.last_error = error.code
        if record.failures >= self.failure_threshold:
            record.open_until = self.clock() + self.cooldown_seconds

    def status(self, backend: str, platform: str, *, available: bool = True) -> HealthStatus:
        if not available:
            return HealthStatus.UNAVAILABLE
        record = self._record(backend, platform)
        if not self.allows(backend, platform):
            return HealthStatus.OPEN
        if record.failures:
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def snapshot(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for backend, platform in sorted(self._records):
            result.setdefault(platform, {})[backend] = self.status(backend, platform).value
        return result

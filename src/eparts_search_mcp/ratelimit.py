"""Per-provider request budgeting.

Short windows (per second, per minute) are enforced with token buckets and a
caller that waits. The daily window is a persisted counter instead: a caller
cannot usefully wait out a quota that resets at midnight, so exceeding it
raises immediately and the tool reports it as a partial failure.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

from .config import RateLimitConfig


class RateLimitExceeded(RuntimeError):
    """Raised when a request cannot be admitted within the configured wait."""


class UsageStore(Protocol):
    """Persistence for the daily counter, so restarts do not reset the quota."""

    def get_daily_usage(self, provider: str, day: str) -> int: ...

    def increment_daily_usage(self, provider: str, day: str) -> int: ...


class _TokenBucket:
    """Classic leaky bucket: `capacity` tokens refilling at `rate` per second."""

    def __init__(self, rate_per_second: float, capacity: float) -> None:
        self.rate = rate_per_second
        self.capacity = max(1.0, capacity)
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now

    def wait_time(self, now: float) -> float:
        self._refill(now)
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate

    def consume(self) -> None:
        self.tokens -= 1.0


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class RateLimiter:
    """Admission control for a single provider.

    Concurrent callers serialize on one lock, which makes the wait FIFO and
    keeps two tasks from both seeing the last available token.
    """

    def __init__(
        self,
        provider: str,
        config: RateLimitConfig,
        usage_store: UsageStore | None = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self._usage_store = usage_store
        self._lock = asyncio.Lock()
        self._buckets: list[_TokenBucket] = []
        if config.per_second:
            self._buckets.append(_TokenBucket(config.per_second, max(config.burst, 1)))
        if config.per_minute:
            self._buckets.append(_TokenBucket(config.per_minute / 60.0, max(config.burst, 1)))
        # Counts only when no store is supplied, so limiting still works in-memory.
        self._local_day = _utc_day()
        self._local_count = 0

    def _daily_used(self) -> int:
        day = _utc_day()
        if self._usage_store is not None:
            return self._usage_store.get_daily_usage(self.provider, day)
        if day != self._local_day:
            self._local_day = day
            self._local_count = 0
        return self._local_count

    def _record_use(self) -> None:
        day = _utc_day()
        if self._usage_store is not None:
            self._usage_store.increment_daily_usage(self.provider, day)
            return
        if day != self._local_day:
            self._local_day = day
            self._local_count = 0
        self._local_count += 1

    def remaining_today(self) -> int | None:
        if self.config.per_day is None:
            return None
        return max(0, self.config.per_day - self._daily_used())

    async def acquire(self) -> None:
        """Block until a request may proceed, or raise RateLimitExceeded."""
        async with self._lock:
            deadline = time.monotonic() + max(0.0, self.config.max_wait_seconds)
            while True:
                if self.config.per_day is not None:
                    used = self._daily_used()
                    if used >= self.config.per_day:
                        raise RateLimitExceeded(
                            f"{self.provider}: daily limit of {self.config.per_day} "
                            f"requests reached (resets at 00:00 UTC)"
                        )

                now = time.monotonic()
                wait = max((bucket.wait_time(now) for bucket in self._buckets), default=0.0)
                if wait <= 0.0:
                    for bucket in self._buckets:
                        bucket.consume()
                    self._record_use()
                    return

                if now + wait > deadline:
                    raise RateLimitExceeded(
                        f"{self.provider}: request would wait {wait:.1f}s, over the "
                        f"{self.config.max_wait_seconds:.1f}s limit"
                    )
                await asyncio.sleep(wait)

    def describe(self) -> dict[str, object]:
        return {
            "limits": self.config.describe(),
            "used_today": self._daily_used(),
            "remaining_today": self.remaining_today(),
        }

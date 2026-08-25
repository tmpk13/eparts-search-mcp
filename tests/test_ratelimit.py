from __future__ import annotations

import asyncio
import time

import pytest

from digi_mouse_search.cache import Cache
from digi_mouse_search.config import RateLimitConfig
from digi_mouse_search.ratelimit import RateLimiter, RateLimitExceeded


async def test_daily_limit_raises_rather_than_waiting():
    limiter = RateLimiter("digikey", RateLimitConfig(per_day=2))
    await limiter.acquire()
    await limiter.acquire()
    with pytest.raises(RateLimitExceeded, match="daily limit"):
        await limiter.acquire()


async def test_remaining_today_tracks_use():
    limiter = RateLimiter("mouser", RateLimitConfig(per_day=5))
    assert limiter.remaining_today() == 5
    await limiter.acquire()
    assert limiter.remaining_today() == 4


async def test_unlimited_config_never_blocks():
    limiter = RateLimiter("mouser", RateLimitConfig())
    assert limiter.remaining_today() is None
    for _ in range(50):
        await limiter.acquire()


async def test_per_second_limit_paces_requests():
    limiter = RateLimiter("digikey", RateLimitConfig(per_second=20.0, burst=1))
    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    # First call consumes the single burst token; the next two wait ~50ms each.
    assert time.monotonic() - start >= 0.08


async def test_wait_beyond_max_wait_raises():
    limiter = RateLimiter("digikey", RateLimitConfig(per_second=0.5, burst=1, max_wait_seconds=0.1))
    await limiter.acquire()
    with pytest.raises(RateLimitExceeded, match="over the"):
        await limiter.acquire()


async def test_concurrent_callers_do_not_exceed_daily_limit():
    limiter = RateLimiter("mouser", RateLimitConfig(per_day=3))
    results = await asyncio.gather(*(limiter.acquire() for _ in range(6)), return_exceptions=True)
    granted = [r for r in results if not isinstance(r, BaseException)]
    assert len(granted) == 3


async def test_daily_usage_survives_restart(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3", default_ttl=60)
    try:
        first = RateLimiter("digikey", RateLimitConfig(per_day=2), cache)
        await first.acquire()

        # A fresh limiter stands in for a restarted server process.
        second = RateLimiter("digikey", RateLimitConfig(per_day=2), cache)
        assert second.remaining_today() == 1
        await second.acquire()
        with pytest.raises(RateLimitExceeded):
            await second.acquire()
    finally:
        cache.close()


async def test_limits_are_independent_per_provider(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3", default_ttl=60)
    try:
        digikey = RateLimiter("digikey", RateLimitConfig(per_day=1), cache)
        mouser = RateLimiter("mouser", RateLimitConfig(per_day=1), cache)
        await digikey.acquire()
        await mouser.acquire()
        assert digikey.remaining_today() == 0
        assert mouser.remaining_today() == 0
    finally:
        cache.close()

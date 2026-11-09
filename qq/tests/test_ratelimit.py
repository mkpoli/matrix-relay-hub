import asyncio

from qq_bridge.ratelimit import RateLimiter


def test_daily_cap_stops_immediately():
    limiter = RateLimiter(per_minute=10, per_day=2)
    assert asyncio.run(limiter.acquire()) is True
    assert asyncio.run(limiter.acquire()) is True
    assert asyncio.run(limiter.acquire()) is False

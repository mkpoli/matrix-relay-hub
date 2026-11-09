"""Sliding-window rate limiting for the QQ send quota."""

from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(self, per_minute: int, per_day: int | None = None) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._minute: deque[float] = deque()
        self._day: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Wait for a slot. False means the daily quota is used up."""
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._minute and now - self._minute[0] >= 60:
                    self._minute.popleft()
                while self._day and now - self._day[0] >= 86400:
                    self._day.popleft()
                if self.per_day is not None and len(self._day) >= self.per_day:
                    return False
                if len(self._minute) < self.per_minute:
                    self._minute.append(now)
                    self._day.append(now)
                    return True
                wait = 60 - (now - self._minute[0]) + 0.05
            await asyncio.sleep(max(0.05, min(wait, 5.0)))

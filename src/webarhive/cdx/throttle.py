"""Shared throttle for Internet Archive requests (spec §2.1, §14).

The bottleneck is IA, not CPU — so all parallel domains share one
common gate. Implemented as an async token-bucket-ish semaphore that
enforces a minimum spacing between requests, plus exponential backoff
on 429/5xx via tenacity in the client.
"""

from __future__ import annotations

import asyncio
import time


class IAThrottle:
    """Async rate limiter: at most `rate` requests/sec across all callers."""

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._min_interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval

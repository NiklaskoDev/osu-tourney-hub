"""Async token bucket shared by every osu! API call, with priorities.

When the bucket is empty, waiting callers are served in priority order (lowest value first), so a
user clicking "add guest" is never stuck behind the background scanner.
"""

import asyncio
import heapq
import itertools
import time
from collections.abc import Callable
from enum import IntEnum

DEFAULT_BURST = 5


class Priority(IntEnum):
    LIVE = 0
    INTERACTIVE = 1
    CAPTURE = 2
    SCAN = 3


class RateLimiter:
    def __init__(
        self,
        rate_per_min: int,
        burst: int = DEFAULT_BURST,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_min < 1 or burst < 1:
            raise ValueError("rate_per_min and burst must be at least 1")
        self._rate_per_second = rate_per_min / 60
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._monotonic = monotonic
        self._last_refill = monotonic()
        self._waiting: list[tuple[int, int, asyncio.Future[None]]] = []
        self._order = itertools.count()
        self._timer: asyncio.TimerHandle | None = None

    async def acquire(self, priority: Priority = Priority.INTERACTIVE) -> None:
        """Wait until a request may be sent."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiting, (int(priority), next(self._order), future))
        self._dispatch()
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self._tokens = min(self._capacity, self._tokens + 1)  # granted but never used
            raise

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)

    def _dispatch(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._refill()
        while self._waiting and self._tokens >= 1:
            _, _, future = heapq.heappop(self._waiting)
            if future.cancelled():
                continue
            self._tokens -= 1
            future.set_result(None)
        while self._waiting and self._waiting[0][2].cancelled():
            heapq.heappop(self._waiting)
        if self._waiting:
            delay = (1 - self._tokens) / self._rate_per_second
            self._timer = asyncio.get_running_loop().call_later(delay, self._dispatch)

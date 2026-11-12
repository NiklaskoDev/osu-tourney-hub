import asyncio
import time

import pytest

from app.clients.ratelimit import Priority, RateLimiter

pytestmark = pytest.mark.anyio

FAST = 60_000  # 1000 tokens a second: waits last a millisecond, order stays observable


async def test_the_burst_is_served_immediately():
    limiter = RateLimiter(rate_per_min=60, burst=3)

    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire(Priority.INTERACTIVE)

    assert time.monotonic() - started < 0.2


async def test_calls_beyond_the_burst_wait_for_the_refill():
    limiter = RateLimiter(rate_per_min=6000, burst=1)  # one token every 10 ms

    started = time.monotonic()
    for _ in range(4):
        await limiter.acquire(Priority.INTERACTIVE)

    assert time.monotonic() - started >= 0.025


async def test_waiting_callers_are_served_by_priority_then_arrival():
    limiter = RateLimiter(rate_per_min=FAST, burst=1)
    await limiter.acquire(Priority.INTERACTIVE)  # empties the bucket
    served: list[str] = []

    async def call(name: str, priority: Priority) -> None:
        await limiter.acquire(priority)
        served.append(name)

    order = [
        ("scan-1", Priority.SCAN),
        ("capture", Priority.CAPTURE),
        ("scan-2", Priority.SCAN),
        ("interactive", Priority.INTERACTIVE),
        ("live", Priority.LIVE),
    ]
    await asyncio.gather(*(call(name, priority) for name, priority in order))

    assert served == ["live", "interactive", "capture", "scan-1", "scan-2"]


async def test_a_cancelled_waiter_does_not_block_the_others():
    limiter = RateLimiter(rate_per_min=FAST, burst=1)
    await limiter.acquire(Priority.INTERACTIVE)
    doomed = asyncio.create_task(limiter.acquire(Priority.LIVE))
    await asyncio.sleep(0)
    doomed.cancel()

    await asyncio.wait_for(limiter.acquire(Priority.SCAN), timeout=1)

    assert doomed.cancelled()


@pytest.mark.parametrize(("rate", "burst"), [(0, 1), (-5, 1), (60, 0)])
def test_nonsensical_settings_are_refused(rate, burst):
    with pytest.raises(ValueError, match="at least 1"):
        RateLimiter(rate_per_min=rate, burst=burst)


def test_priorities_rank_live_above_interactive_above_capture_above_scan():
    assert Priority.LIVE < Priority.INTERACTIVE < Priority.CAPTURE < Priority.SCAN

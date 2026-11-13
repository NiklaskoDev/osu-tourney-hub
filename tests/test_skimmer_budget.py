"""The scanner's request budget, in virtual time: ten minutes pass in a blink, so the per-minute
limits can really be counted."""

import asyncio
from collections import Counter

import httpx
import pytest

from app.clients.http import make_http_client
from app.clients.osu import OsuClient
from app.clients.ratelimit import RateLimiter
from app.repos import jobs as jobs_repo
from app.services.skimmer import Skimmer
from tests.support import add_member, add_pool_map, add_stage, add_tournament

SIMULATED_SECONDS = 600
OPEN_LOBBIES = (3001, 3002, 3003)


class VirtualLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock only moves when everything is waiting: it jumps to the next
    timer instead of sleeping, so rate limits and sleeps run in virtual time."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0
        real_select = self._selector.select

        def select(timeout=None):
            if timeout:
                self.now += timeout
            return real_select(0)

        self._selector.select = select

    def time(self) -> float:
        return self.now


@pytest.fixture
def busy_osu(osu, db):
    """3000 closed lobbies the scanner has to get through, three open ones above them, and a pool
    map so a few lobbies are worth keeping."""
    tid = add_tournament(db)
    add_member(db, tid, 2001, "captain")
    add_pool_map(db, add_stage(db, tid), "NM1", beatmap_id=1001)
    for match_id in range(1, 3001):
        osu.add_match(match_id)
    osu.add_game(2500, 1001, [(2001, 900_000)])
    for match_id in OPEN_LOBBIES:
        osu.add_match(match_id, end_time=None)
    jobs_repo.set_cursor(db, "skimmer", "100")
    return osu


def simulate(settings, clock, osu, db, scan_budget, global_rate=60):
    """Run the scanner (workers, head every minute, open check every five) and an interactive
    caller for ten virtual minutes. Returns the request times by kind and the interactive waits."""
    loop = VirtualLoop()
    seen: list[tuple[float, str]] = []
    waits: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append((loop.now, request.url.path))
        return osu._handle(request)

    transport = httpx.MockTransport(record)
    client = OsuClient(
        settings,
        make_http_client(transport),
        RateLimiter(global_rate, burst=1, monotonic=loop.time),
        clock,
        transport=transport,
        scan_limiter=(
            RateLimiter(scan_budget, burst=1, monotonic=loop.time) if scan_budget else None
        ),
    )
    osu.add_user(7, "Seven")
    skimmer = Skimmer(settings, clock, client)

    async def every(seconds, action):
        while True:
            await action()
            await asyncio.sleep(seconds)

    async def head():
        await skimmer.check_head(db)

    async def open_check():
        await skimmer.check_open(db)

    async def interactive():
        started = loop.now
        await client.get_user(7)
        waits.append(loop.now - started)

    async def main():
        tasks = [
            asyncio.create_task(skimmer.run_workers()),
            asyncio.create_task(every(60, head)),
            asyncio.create_task(every(300, open_check)),
            asyncio.create_task(every(20, interactive)),
        ]
        await asyncio.sleep(SIMULATED_SECONDS)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    loop.run_until_complete(main())
    loop.close()
    return seen, waits


def per_minute(seen, kind):
    return Counter(int(at // 60) for at, path in seen if kind(path))


def is_scan(path):
    return path.startswith("/api/v2/matches")


def test_the_scanner_never_exceeds_its_budget_and_all_requests_stay_inside_the_cap(
    settings, clock, busy_osu, db
):
    seen, waits = simulate(settings, clock, busy_osu, db, scan_budget=30)

    scan = per_minute(seen, is_scan)
    everything = per_minute(seen, lambda path: True)
    assert max(scan.values()) <= 30
    assert sum(scan.values()) >= 250  # it really uses what it may: about 30 a minute for 10
    assert max(everything.values()) <= 60
    assert max(waits) <= 2  # the person who is waiting hardly notices the scanner


def test_a_low_budget_slows_the_scanner_down_in_proportion(settings, clock, busy_osu, db):
    seen, _ = simulate(settings, clock, busy_osu, db, scan_budget=10)

    scan = per_minute(seen, is_scan)
    assert max(scan.values()) <= 10
    assert 80 <= sum(scan.values()) <= 105


def test_an_interactive_request_is_served_ahead_of_scanner_requests_that_are_waiting(
    settings, clock, busy_osu, db
):
    # No scanner budget of its own: only the shared limiter holds the four workers back, so there
    # are always scan requests queued when the person's request arrives.
    seen, waits = simulate(settings, clock, busy_osu, db, scan_budget=0)

    everything = per_minute(seen, lambda path: True)
    assert max(everything.values()) <= 60  # the overall cap holds
    assert sum(per_minute(seen, is_scan).values()) >= 500  # the scanner really was busy
    assert max(waits) <= 2

"""The osu! client's lobby calls: the listing, event paging backwards and forwards, and the
scanner's request counter."""

import pytest

from app.clients.osu import OsuNotFound, OsuUnavailable
from app.clients.ratelimit import Priority

pytestmark = pytest.mark.anyio

MATCH = 111_000


def lobby_with_games(osu, games, match_id=MATCH, **kwargs):
    osu.add_match(match_id, **kwargs)
    return [osu.add_game(match_id, 5000 + number, [(7, 800_000)]) for number in range(games)]


# --- the listing ---


async def test_the_listing_gives_newest_first_with_name_and_times(scan_client, osu):
    osu.add_match(5, "Older", end_time=None)
    osu.add_match(
        9, "Newer", start_time="2026-11-12T10:00:00+02:00", end_time="2026-11-12T09:00:00Z"
    )

    matches, more = await scan_client.list_matches()

    assert [m.id for m in matches] == [9, 5]
    newer, older = matches
    assert (newer.name, newer.start_time, newer.end_time) == (
        "Newer",
        "2026-11-12T08:00:00Z",
        "2026-11-12T09:00:00Z",
    )
    assert older.end_time is None
    assert more is None


async def test_the_listing_continues_with_the_cursor_and_can_ask_for_open_lobbies(scan_client, osu):
    for number in range(1, 121):
        osu.add_match(number, end_time=None if number % 2 else "2026-11-12T11:00:00+00:00")

    first, cursor = await scan_client.list_matches()
    second, cursor_2 = await scan_client.list_matches(cursor)
    third, cursor_3 = await scan_client.list_matches(cursor_2)
    open_only, _ = await scan_client.list_matches(active=True)

    assert [len(first), len(second), len(third)] == [50, 50, 20]
    assert cursor_3 is None
    assert [m.id for m in first + second + third] == list(range(120, 0, -1))
    assert all(m.end_time is None for m in open_only)
    request = [r for r in osu.requests if r.url.params.get("active")][0]
    assert request.url.params["active"] == "true"
    assert request.url.params["sort"] == "id_desc"
    assert request.url.params["limit"] == "50"


async def test_a_listing_in_an_unexpected_shape_is_unavailable(scan_client, osu):
    osu.garbage_paths.add("/api/v2/matches")

    with pytest.raises(OsuUnavailable):
        await scan_client.list_matches()


# --- events, backwards ---


async def test_a_short_lobby_is_read_in_one_request_with_its_events_in_order(scan_client, osu):
    lobby_with_games(osu, 3)

    fetched = await scan_client.fetch_match(MATCH)

    assert [e["game"]["beatmap_id"] for e in fetched.events] == [5000, 5001, 5002]
    assert fetched.match.name == "Practice room"
    assert fetched.truncated is False
    assert len(osu.match_requests(MATCH)) == 1


async def test_a_long_lobby_is_paged_backwards_with_before_until_the_first_event(scan_client, osu):
    lobby_with_games(osu, 250)

    fetched = await scan_client.fetch_match(MATCH)

    assert len(fetched.events) == 250
    assert [e["id"] for e in fetched.events] == sorted(e["id"] for e in fetched.events)
    assert fetched.truncated is False
    requests = osu.match_requests(MATCH)
    assert len(requests) == 3
    assert "before" not in requests[0].url.params
    assert requests[1].url.params["before"] == str(fetched.events[150]["id"])


async def test_the_page_limit_stops_the_read_and_reports_truncated(scan_client, osu):
    lobby_with_games(osu, 250)

    fetched = await scan_client.fetch_match(MATCH, max_pages=2)

    assert fetched.truncated is True
    assert len(fetched.events) == 200  # the newest ones
    assert len(osu.match_requests(MATCH)) == 2


async def test_a_lobby_without_events_comes_back_empty(scan_client, osu):
    osu.add_match(MATCH)

    fetched = await scan_client.fetch_match(MATCH)

    assert fetched.events == [] and fetched.truncated is False


@pytest.mark.parametrize("status", [403, 404])
async def test_a_deleted_or_private_lobby_is_not_found(scan_client, osu, status):
    osu.match_statuses[MATCH] = status

    with pytest.raises(OsuNotFound):
        await scan_client.fetch_match(MATCH)
    assert len(osu.match_requests(MATCH)) == 1


async def test_server_errors_are_retried_and_then_unavailable(scan_client, osu):
    osu.match_statuses[MATCH] = 500

    with pytest.raises(OsuUnavailable):
        await scan_client.fetch_match(MATCH)
    assert len(osu.match_requests(MATCH)) == 5


async def test_an_event_in_an_unexpected_shape_is_unavailable(scan_client, osu):
    osu.add_match(MATCH)
    osu.matches[MATCH]["events"].append({"id": "not a number", "detail": {}})

    with pytest.raises(OsuUnavailable):
        await scan_client.fetch_match(MATCH)


# --- events, forwards ---


async def test_reading_after_an_event_gives_only_newer_events(scan_client, osu):
    events = lobby_with_games(osu, 4)

    fetched = await scan_client.fetch_match_after(MATCH, events[1]["id"])

    assert [e["id"] for e in fetched.events] == [events[2]["id"], events[3]["id"]]
    assert fetched.truncated is False
    assert osu.match_requests(MATCH)[0].url.params["after"] == str(events[1]["id"])


async def test_reading_after_pages_forward_until_the_latest_event(scan_client, osu):
    events = lobby_with_games(osu, 230)

    fetched = await scan_client.fetch_match_after(MATCH, events[9]["id"])

    assert len(fetched.events) == 220
    assert fetched.truncated is False
    assert len(osu.match_requests(MATCH)) == 3


async def test_reading_after_the_latest_event_gives_nothing_in_one_request(scan_client, osu):
    events = lobby_with_games(osu, 2)

    fetched = await scan_client.fetch_match_after(MATCH, events[-1]["id"])

    assert fetched.events == []
    assert len(osu.match_requests(MATCH)) == 1


async def test_reading_after_stops_at_the_page_limit(scan_client, osu):
    events = lobby_with_games(osu, 250)

    fetched = await scan_client.fetch_match_after(MATCH, events[0]["id"], max_pages=1)

    assert fetched.truncated is True and len(fetched.events) == 100


# --- the scanner's request counter ---


async def test_only_scan_priority_requests_are_counted(scan_client, osu):
    lobby_with_games(osu, 1)
    osu.add_user(7, "Seven")

    await scan_client.fetch_match(MATCH, priority=Priority.INTERACTIVE)
    await scan_client.get_user(7)
    assert scan_client.scan_requests == 0

    await scan_client.fetch_match(MATCH)
    await scan_client.list_matches()
    assert scan_client.scan_requests == 2


async def test_a_retried_scan_request_counts_every_try(scan_client, osu):
    osu.match_statuses[MATCH] = 500

    with pytest.raises(OsuUnavailable):
        await scan_client.fetch_match(MATCH)

    assert scan_client.scan_requests == 5


async def test_a_scan_request_is_counted_when_it_is_sent_not_while_it_waits(settings, clock, osu):
    import asyncio

    from app.clients.http import make_http_client
    from app.clients.osu import OsuClient

    class Gate:
        def __init__(self):
            self.open = asyncio.Event()

        async def acquire(self, priority):
            await self.open.wait()

    gate = Gate()
    client = OsuClient(
        settings, make_http_client(osu.transport), gate, clock, transport=osu.transport
    )
    osu.add_match(MATCH)
    gate.open.set()
    await client.client_token()  # the token request is not a scan request
    gate.open.clear()

    task = asyncio.create_task(client.fetch_match(MATCH))
    await asyncio.sleep(0.01)
    assert client.scan_requests == 0  # still waiting for the shared limiter

    gate.open.set()
    await task
    assert client.scan_requests == 1


async def test_a_task_can_tally_its_own_scan_requests(scan_client, osu):
    from app.clients.osu import tally_scan_requests

    osu.add_match(MATCH)
    await scan_client.fetch_match(MATCH)

    with tally_scan_requests() as tally:
        await scan_client.fetch_match(MATCH)
        await scan_client.list_matches()
    await scan_client.fetch_match(MATCH)

    assert tally[0] == 2
    assert scan_client.scan_requests == 4

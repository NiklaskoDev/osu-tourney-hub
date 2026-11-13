"""The pure availability rules: ranges, the weekly grid, who is free, and the best-slot finder."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.clock import to_utc_iso
from app.domain.slots import MATCH_DURATION, SLOT, zone_from
from app.services import availability
from app.services.availability import (
    Column,
    MatchBand,
    OpponentPlayer,
    RangeError,
    best_slots,
)

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # a Monday


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def free(osu_id: int, status: str, start: datetime = START, slots: int = 3) -> dict:
    """{osu_id: {slot text: status}} for `slots` consecutive slots from `start`."""
    return {
        osu_id: {to_utc_iso(start + SLOT * index): status for index in range(slots)},
    }


def merged(*maps: dict) -> dict:
    result: dict = {}
    for one in maps:
        for osu_id, slots in one.items():
            result.setdefault(osu_id, {}).update(slots)
    return result


# --- ranges and weeks --------------------------------------------------------------------------


def test_a_range_lists_every_slot_from_start_to_end_inclusive():
    slots = availability.parse_range("2026-11-16T18:00:00Z", "2026-11-16T19:00:00Z")

    assert slots == [utc(2026, 11, 16, 18, 0), utc(2026, 11, 16, 18, 30), utc(2026, 11, 16, 19, 0)]


def test_a_single_slot_range_is_one_slot():
    assert availability.parse_range("2026-11-16T18:00:00Z", "2026-11-16T18:00:00Z") == [
        utc(2026, 11, 16, 18, 0)
    ]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-11-16T19:00:00Z", "2026-11-16T18:00:00Z"),  # backwards
        ("2026-11-16T18:15:00Z", "2026-11-16T19:00:00Z"),  # not on the half hour
        ("2026-11-16T18:00:30Z", "2026-11-16T19:00:00Z"),  # seconds
        ("2026-11-16 18:00", "2026-11-16T19:00:00Z"),  # not the stored format
        ("", ""),
        ("2026-11-16T18:00:00Z", "nonsense"),
        ("2026-11-16T18:00:00+01:00", "2026-11-16T19:00:00Z"),  # an offset instead of Z
    ],
)
def test_a_bad_range_is_refused(start, end):
    with pytest.raises(RangeError):
        availability.parse_range(start, end)


def test_a_range_may_cover_at_most_the_limit():
    first = utc(2026, 11, 16, 0, 0)
    last_allowed = first + SLOT * (availability.MAX_RANGE_SLOTS - 1)

    assert len(availability.parse_range(to_utc_iso(first), to_utc_iso(last_allowed))) == 400
    with pytest.raises(RangeError, match="week"):
        availability.parse_range(to_utc_iso(first), to_utc_iso(last_allowed + SLOT))


def test_only_the_three_states_are_accepted():
    assert [availability.parse_state(state) for state in ("yes", "maybe", "no")] == [
        "yes",
        "maybe",
        "no",
    ]
    for bad in ("", "YES", "true", "yes "):
        with pytest.raises(RangeError):
            availability.parse_state(bad)


@pytest.mark.parametrize(
    ("text", "monday"),
    [
        ("2026-11-12", date(2026, 11, 9)),  # any day names its week
        ("2026-11-09", date(2026, 11, 9)),
        ("2026-11-15", date(2026, 11, 9)),
        (None, date(2026, 11, 2)),  # falls back to today (a Wednesday)
        ("", date(2026, 11, 2)),
        ("11/12/2026", date(2026, 11, 2)),
        ("2026-13-45", date(2026, 11, 2)),
        ("1999-01-01", date(2026, 11, 2)),
        ("2101-01-01", date(2026, 11, 2)),
    ],
)
def test_parse_week(text, monday):
    assert availability.parse_week(text, date(2026, 11, 4)) == monday


def test_week_navigation_moves_by_seven_days():
    monday = date(2026, 11, 9)

    assert availability.previous_week(monday) == date(2026, 11, 2)
    assert availability.next_week(monday) == date(2026, 11, 16)


# --- the grid ----------------------------------------------------------------------------------

COLUMNS = [Column(1, "Ann"), Column(2, "Ben")]


def test_the_grid_has_a_row_per_slot_and_a_state_per_column():
    found = {1: {"2026-11-16T18:00:00Z": "yes"}, 2: {"2026-11-16T18:00:00Z": "maybe"}}

    grid = availability.build_grid(date(2026, 11, 16), UTC, COLUMNS, found)

    assert len(grid.rows) == 336
    row = next(row for row in grid.rows if row.slot_text == "2026-11-16T18:00:00Z")
    assert row.states == ["yes", "maybe"]
    assert (row.yes, row.maybe) == (1, 1)
    quiet = grid.rows[0]
    assert quiet.states == ["no", "no"]
    assert (quiet.yes, quiet.maybe) == (0, 0)


def test_the_first_row_of_each_local_day_carries_the_day_label():
    grid = availability.build_grid(date(2026, 11, 16), UTC, COLUMNS, {})

    labelled = [row for row in grid.rows if row.day_label]

    assert len(labelled) == 7
    assert labelled[0].day_label == "Monday 16 November"
    assert labelled[0].slot_text == "2026-11-16T00:00:00Z"
    assert [label for _, label in grid.days][-1] == "Sunday 22 November"


def test_rows_show_the_viewers_clock_while_the_slot_stays_utc():
    found = {1: {"2026-11-16T19:00:00Z": "yes"}}

    grid = availability.build_grid(date(2026, 11, 16), BERLIN, COLUMNS, found)

    row = next(row for row in grid.rows if row.slot_text == "2026-11-16T19:00:00Z")
    assert row.time_label == "20:00"  # Berlin is UTC+1 in November
    assert row.spoken_label == "Mon 16 Nov 20:00"
    assert row.states[0] == "yes"


def test_a_local_week_starts_at_local_midnight():
    grid = availability.build_grid(date(2026, 11, 16), BERLIN, COLUMNS, {})

    assert grid.rows[0].slot_text == "2026-11-15T23:00:00Z"
    assert grid.rows[0].time_label == "00:00"
    assert grid.rows[0].day_label == "Monday 16 November"


def test_dst_change_day_shows_the_repeated_hour_once_per_instant():
    grid = availability.build_grid(date(2026, 10, 19), BERLIN, COLUMNS, {})
    sunday = [row for row in grid.rows if row.day_anchor == "day-2026-10-25"]

    assert len(sunday) == 50  # a 25-hour day
    labels = [row.time_label for row in sunday]
    assert labels.count("02:00") == 2 and labels.count("02:30") == 2
    assert len({row.slot_text for row in sunday}) == 50


def test_a_match_shades_the_slots_it_covers_and_names_the_first():
    bands = [MatchBand(utc(2026, 11, 16, 19, 30), "vs Rival Crew")]

    grid = availability.build_grid(date(2026, 11, 16), UTC, COLUMNS, {}, bands)
    shaded = [row for row in grid.rows if row.in_match]

    assert [row.slot_text for row in shaded] == [
        "2026-11-16T19:30:00Z",
        "2026-11-16T20:00:00Z",
        "2026-11-16T20:30:00Z",
    ]
    assert [row.band_label for row in shaded] == ["vs Rival Crew", None, None]


def test_a_match_starting_between_slots_covers_the_slots_it_touches():
    bands = [MatchBand(utc(2026, 11, 16, 19, 15), "vs Rival Crew")]

    covered = availability.band_covers(bands)

    assert sorted(covered) == [
        utc(2026, 11, 16, 19, 0),
        utc(2026, 11, 16, 19, 30),
        utc(2026, 11, 16, 20, 0),
        utc(2026, 11, 16, 20, 30),
    ]
    assert covered[utc(2026, 11, 16, 19, 0)] == "vs Rival Crew"


def test_the_overlay_lands_on_the_right_local_rows():
    bands = [MatchBand(utc(2026, 11, 16, 19, 0), "vs Rival Crew")]  # 20:00 in Berlin

    rows = availability.build_rows([utc(2026, 11, 16, 19, 0)], BERLIN, COLUMNS, {}, bands)

    assert rows[0].time_label == "20:00" and rows[0].band_label == "vs Rival Crew"


def test_week_bounds_cover_exactly_the_week_in_that_zone():
    first, end = availability.week_bounds(date(2026, 11, 16), BERLIN)

    assert first == utc(2026, 11, 15, 23, 0)
    assert end == utc(2026, 11, 22, 23, 0)


# --- who is free for a match -------------------------------------------------------------------


def test_someone_is_available_only_when_every_slot_of_the_match_says_yes():
    found = merged(
        free(1, "yes"),  # 18:00 to 19:30, the whole match
        free(2, "yes", slots=2),  # only until 19:00
        free(3, "maybe"),
    )

    assert availability.available_players(found, [1, 2, 3, 4], START) == {1}
    assert availability.maybe_players(found, [1, 2, 3, 4], START) == {3}


def test_one_maybe_slot_makes_the_whole_match_a_maybe():
    found = {
        1: {
            to_utc_iso(START): "yes",
            to_utc_iso(START + SLOT): "maybe",
            to_utc_iso(START + 2 * SLOT): "yes",
        }
    }

    assert availability.available_players(found, [1], START) == set()
    assert availability.maybe_players(found, [1], START) == {1}


def test_a_match_off_the_slot_grid_needs_the_touched_slots():
    found = free(1, "yes", START, slots=4)  # 18:00 to 20:00

    assert availability.available_players(found, [1], START + timedelta(minutes=15)) == {1}
    assert (
        availability.available_players(
            free(1, "yes", START, slots=3), [1], START + timedelta(minutes=15)
        )
        == set()
    )


# --- best slots --------------------------------------------------------------------------------

WINDOW_END = START + timedelta(hours=6)


def opponent(name: str, zone_name: str | None, offset: int | None = None) -> OpponentPlayer:
    return OpponentPlayer(name, zone_from(zone_name, offset), zone_name or "?")


def test_a_start_scores_two_per_yes_and_one_per_maybe_over_the_best_lineup_size_players():
    found = merged(free(1, "yes"), free(2, "yes"), free(3, "maybe"), free(4, "yes"))

    slots = best_slots(found, [1, 2, 3, 4], 2, [], START, START + SLOT)

    assert len(slots) == 1
    assert slots[0].start == START
    assert slots[0].score == 4  # the two best of four players, both yes
    assert (slots[0].yes, slots[0].maybe) == (2, 0)


def test_maybes_fill_the_lineup_when_there_are_not_enough_yes():
    found = merged(free(1, "yes"), free(2, "maybe"), free(3, "maybe"))

    slot = best_slots(found, [1, 2, 3], 2, [], START, START + SLOT)[0]

    assert slot.score == 3  # 2 + 1
    assert (slot.yes, slot.maybe) == (1, 1)


def test_a_start_where_nobody_is_free_is_left_out():
    found = free(1, "yes")  # free 18:00 to 19:30 only

    starts = [s.start for s in best_slots(found, [1], 1, [], START, WINDOW_END)]

    assert starts == [START]  # 18:30 would need 20:00-20:30 too


def test_only_a_start_with_the_whole_match_free_counts():
    found = free(1, "yes", START, slots=4)  # 18:00 to 20:00

    starts = [s.start for s in best_slots(found, [1], 1, [], START, WINDOW_END)]

    assert starts == [START, START + SLOT]


def test_the_window_start_is_rounded_up_to_a_slot():
    found = free(1, "yes", START, slots=6)

    first = best_slots(found, [1], 1, [], START + timedelta(minutes=10), WINDOW_END)[0]

    assert first.start == START + SLOT


def test_the_window_end_is_exclusive():
    found = free(1, "yes", START, slots=6)

    starts = [s.start for s in best_slots(found, [1], 1, [], START, START + SLOT)]

    assert starts == [START]


def test_an_opponent_outside_ten_to_twenty_three_local_costs_a_point():
    found = free(1, "yes", START, slots=8)
    # 18:00Z is 19:00 in Berlin (fine) and 10:00 in Los Angeles (fine, exactly at the edge) but
    # 23:30 for UTC+5:30.
    night = opponent("Ria", None, 330)
    day = opponent("Rex", "Europe/Berlin")

    slot = best_slots(found, [1], 1, [night, day], START, START + SLOT)[0]

    assert slot.outside == 1
    assert slot.score == 2 - availability.OPPONENT_OUTSIDE_PENALTY
    by_name = {time.name: time for time in slot.opponents}
    assert by_name["Ria"].outside and by_name["Ria"].local_time == "Mon 16 Nov 23:30"
    assert not by_name["Rex"].outside and by_name["Rex"].local_time == "Mon 16 Nov 19:00"


def test_a_match_that_runs_past_twenty_three_local_is_outside_even_if_it_starts_before():
    rex = opponent("Rex", "Europe/Berlin")
    fits = utc(2026, 11, 16, 20, 0)  # 21:00 to 22:30 in Berlin
    runs_over = utc(2026, 11, 16, 21, 30)  # 22:30 to 00:00 in Berlin
    found = merged(free(1, "yes", fits, slots=3), free(1, "yes", runs_over, slots=3))

    by_start = {s.start: s for s in best_slots(found, [1], 1, [rex], fits, runs_over + SLOT)}

    assert by_start[fits].outside == 0
    assert by_start[runs_over].outside == 1


def test_an_opponent_with_no_known_timezone_is_not_penalised_and_says_so():
    found = free(1, "yes")
    unknown = opponent("Ghost", None)

    slot = best_slots(found, [1], 1, [unknown], START, START + SLOT)[0]

    assert slot.outside == 0 and slot.score == 2
    assert slot.opponents[0].local_time is None


def test_the_penalty_reorders_equal_starts_and_the_order_is_deterministic():
    found = free(1, "yes", START, slots=8)
    ria = opponent("Ria", None, 330)  # 23:30 local at 18:00Z, 00:00 at 18:30Z, 22:30 at 17:00Z
    window_start = START - timedelta(hours=1)

    first = best_slots(found, [1], 1, [ria], window_start, START + SLOT)
    second = best_slots(found, [1], 1, [ria], window_start, START + SLOT)

    assert first == second
    # 17:00Z is 22:30 for Ria: the match runs to 00:00, so it is outside too. All starts are
    # penalised equally, so the earliest comes first.
    assert [s.start for s in first] == sorted(s.start for s in first)


def test_a_better_start_beats_an_earlier_one():
    found = merged(
        free(1, "yes", START, slots=4),  # free for a match at 18:00 or 18:30
        free(2, "yes", START + SLOT, slots=3),  # free for a match at 18:30 only
    )

    slots = best_slots(found, [1, 2], 2, [], START, START + 2 * SLOT)

    assert [s.start for s in slots] == [START + SLOT, START]
    assert [s.score for s in slots] == [4, 2]


def test_an_equal_score_prefers_fewer_inconvenienced_opponents_then_the_earlier_start():
    ria = opponent("Ria", None, 330)
    # 17:00Z: Ria 22:30 -> match to 00:00, outside. 12:00Z: Ria 17:30 -> fine.
    early = utc(2026, 11, 16, 12, 0)
    late = utc(2026, 11, 16, 17, 0)
    found = merged(free(1, "yes", early, slots=3), free(1, "yes", late, slots=3))

    slots = best_slots(found, [1], 1, [ria], early, late + SLOT)

    assert [s.start for s in slots] == [early, late]
    assert [s.outside for s in slots] == [0, 1]


def test_only_the_top_ten_are_returned():
    found = free(1, "yes", START, slots=60)

    slots = best_slots(found, [1], 1, [], START, START + 30 * SLOT)

    assert len(slots) == availability.TOP_SLOTS == 10
    assert slots[0].start == START


def test_people_outside_the_given_ids_do_not_count():
    found = merged(free(1, "yes"), free(2, "yes"))

    slot = best_slots(found, [1], 2, [], START, START + SLOT)[0]

    assert slot.score == 2  # only player 1 is counted, whatever the lineup size


def test_the_match_duration_is_the_default_length():
    assert MATCH_DURATION == timedelta(minutes=90)


# --- the zone the grid is shown in -------------------------------------------------------------


def test_utc_is_the_default_view():
    view = availability.view_zone("utc", "Europe/Berlin", "Asia/Tokyo")

    assert (view.name, view.is_local, view.needs_detection) == ("UTC", False, False)
    assert availability.view_zone("anything else", "Europe/Berlin", None).name == "UTC"


def test_local_uses_the_saved_timezone_first():
    view = availability.view_zone("local", "Europe/Berlin", "Asia/Tokyo")

    assert (view.name, view.is_local, view.needs_detection) == ("Europe/Berlin", True, False)


def test_local_falls_back_to_the_browsers_zone():
    view = availability.view_zone("local", None, "Asia/Tokyo")

    assert (view.name, view.needs_detection) == ("Asia/Tokyo", False)


@pytest.mark.parametrize("browser", [None, "", "Mars/Olympus", "US/Eastern", "../../etc/passwd"])
def test_local_with_no_usable_zone_shows_utc_and_asks_for_detection(browser):
    view = availability.view_zone("local", None, browser)

    assert (view.name, view.is_local, view.needs_detection) == ("UTC", True, True)


def test_an_invalid_saved_timezone_is_skipped():
    view = availability.view_zone("local", "Mars/Olympus", "Asia/Tokyo")

    assert view.name == "Asia/Tokyo"

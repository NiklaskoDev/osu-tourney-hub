from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.slots import (
    MATCH_DURATION,
    SLOT,
    ceil_slot,
    covered_slots,
    floor_slot,
    is_reasonable_hour,
    is_slot_aligned,
    slots_between,
    week_monday,
    week_slots,
    zone_from,
)

BERLIN = ZoneInfo("Europe/Berlin")


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_a_slot_is_thirty_minutes_and_a_match_ninety():
    assert SLOT == timedelta(minutes=30)
    assert MATCH_DURATION == timedelta(minutes=90)


@pytest.mark.parametrize(
    ("moment", "floor", "ceil"),
    [
        (utc(2026, 11, 12, 18, 0), utc(2026, 11, 12, 18, 0), utc(2026, 11, 12, 18, 0)),
        (utc(2026, 11, 12, 18, 1), utc(2026, 11, 12, 18, 0), utc(2026, 11, 12, 18, 30)),
        (utc(2026, 11, 12, 18, 29, 59), utc(2026, 11, 12, 18, 0), utc(2026, 11, 12, 18, 30)),
        (utc(2026, 11, 12, 18, 30), utc(2026, 11, 12, 18, 30), utc(2026, 11, 12, 18, 30)),
        (utc(2026, 11, 12, 23, 45), utc(2026, 11, 12, 23, 30), utc(2026, 11, 13, 0, 0)),
    ],
)
def test_floor_and_ceil_snap_to_the_utc_half_hour(moment, floor, ceil):
    assert floor_slot(moment) == floor
    assert ceil_slot(moment) == ceil


def test_floor_works_from_any_zone():
    assert floor_slot(datetime(2026, 11, 12, 20, 10, tzinfo=BERLIN)) == utc(2026, 11, 12, 19, 0)


@pytest.mark.parametrize(
    ("moment", "aligned"),
    [
        (utc(2026, 11, 12, 18, 0), True),
        (utc(2026, 11, 12, 18, 30), True),
        (utc(2026, 11, 12, 18, 15), False),
        (utc(2026, 11, 12, 18, 0, 1), False),
    ],
)
def test_slot_alignment(moment, aligned):
    assert is_slot_aligned(moment) is aligned


def test_slots_between_is_half_open():
    slots = slots_between(utc(2026, 11, 12, 18, 0), utc(2026, 11, 12, 19, 0))

    assert slots == [utc(2026, 11, 12, 18, 0), utc(2026, 11, 12, 18, 30)]
    assert slots_between(utc(2026, 11, 12, 18, 0), utc(2026, 11, 12, 18, 0)) == []


def test_a_match_on_the_hour_covers_three_slots():
    assert covered_slots(utc(2026, 11, 12, 19, 0)) == [
        utc(2026, 11, 12, 19, 0),
        utc(2026, 11, 12, 19, 30),
        utc(2026, 11, 12, 20, 0),
    ]


def test_a_match_off_the_slot_grid_covers_every_slot_it_touches():
    # 19:15 to 20:45 touches 19:00, 19:30, 20:00 and 20:30.
    assert covered_slots(utc(2026, 11, 12, 19, 15)) == [
        utc(2026, 11, 12, 19, 0),
        utc(2026, 11, 12, 19, 30),
        utc(2026, 11, 12, 20, 0),
        utc(2026, 11, 12, 20, 30),
    ]


def test_week_monday():
    assert week_monday(date(2026, 11, 12)) == date(2026, 11, 9)  # a Thursday
    assert week_monday(date(2026, 11, 9)) == date(2026, 11, 9)
    assert week_monday(date(2026, 11, 15)) == date(2026, 11, 9)  # the Sunday


def test_a_plain_week_has_336_slots_from_local_midnight():
    slots = week_slots(date(2026, 11, 9), BERLIN)

    assert len(slots) == 336
    assert slots[0] == utc(2026, 11, 8, 23, 0)  # Berlin is UTC+1 in November
    assert slots[-1] == utc(2026, 11, 15, 22, 30)
    assert all(b - a == SLOT for a, b in zip(slots, slots[1:], strict=False))


def test_the_week_the_clocks_go_back_has_two_extra_slots():
    slots = week_slots(date(2026, 10, 19), BERLIN)  # 25 October 2026: 03:00 becomes 02:00

    assert len(slots) == 338
    assert slots[0] == utc(2026, 10, 18, 22, 0)  # still summer time on Monday
    assert slots[-1] == utc(2026, 10, 25, 22, 30)
    # Slots are instants: none repeats, none is skipped.
    assert all(b - a == SLOT for a, b in zip(slots, slots[1:], strict=False))


def test_the_week_the_clocks_go_forward_has_two_fewer_slots():
    slots = week_slots(date(2026, 3, 23), BERLIN)  # 29 March 2026: 02:00 becomes 03:00

    assert len(slots) == 334
    assert slots[0] == utc(2026, 3, 22, 23, 0)


def test_utc_weeks_are_always_336_slots():
    assert len(week_slots(date(2026, 10, 19), UTC)) == 336


def test_a_zone_off_the_half_hour_shows_slots_on_the_quarter():
    nepal = zone_from(None, 345)  # UTC+5:45
    slots = week_slots(date(2026, 11, 9), nepal)

    assert slots[0] == utc(2026, 11, 8, 18, 30)  # local midnight is 18:15 UTC: no slot starts there
    assert slots[0].astimezone(nepal).strftime("%H:%M") == "00:15"
    assert len(slots) == 336


def test_zone_from_prefers_a_valid_iana_name():
    zone = zone_from("Europe/Berlin", 0)

    assert zone is not None
    assert utc(2026, 7, 1, 12, 0).astimezone(zone).hour == 14  # summer time, not the offset


def test_zone_from_falls_back_to_the_offset_wybin_gives():
    zone = zone_from(None, 330)  # "UTC+5:30"

    assert zone is not None
    assert utc(2026, 11, 12, 12, 0).astimezone(zone).strftime("%H:%M") == "17:30"


def test_zone_from_ignores_an_unknown_iana_name_and_uses_the_offset():
    zone = zone_from("Mars/Olympus", -360)

    assert zone is not None
    assert utc(2026, 11, 12, 12, 0).astimezone(zone).hour == 6


def test_zone_from_is_none_when_nothing_is_known():
    assert zone_from(None, None) is None
    assert zone_from("", None) is None
    assert zone_from("Mars/Olympus", None) is None


def test_zero_offset_is_utc():
    zone = zone_from(None, 0)

    assert zone is not None
    assert utc(2026, 11, 12, 12, 0).astimezone(zone).hour == 12


@pytest.mark.parametrize(
    ("hour", "minute", "reasonable"),
    [(9, 59, False), (10, 0, True), (22, 59, True), (23, 0, False), (3, 0, False)],
)
def test_reasonable_hours_are_ten_to_twenty_three_local(hour, minute, reasonable):
    moment = datetime(2026, 11, 12, hour, minute, tzinfo=BERLIN)

    assert is_reasonable_hour(moment.astimezone(UTC), BERLIN) is reasonable

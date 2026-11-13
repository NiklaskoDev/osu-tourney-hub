"""Availability slots and match times: 30-minute slots in UTC, and showing them in a zone.

Slots are aligned to :00 and :30 in UTC. A zone whose offset is not a multiple of 30 minutes
(Nepal, UTC+5:45) therefore shows slots at :15 and :45 on the local clock; nothing is rounded to
the local clock because a slot is one instant that must mean the same to everyone. No I/O.
"""

import zoneinfo
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo

SLOT = timedelta(minutes=30)
# How long a match is assumed to take: the calendar entry's length, and the time players must be
# free for in the best-slot finder and the lineup filter.
MATCH_DURATION = timedelta(minutes=90)
WEEK_DAYS = 7
OPPONENT_DAY_START = time(10, 0)
OPPONENT_DAY_END = time(23, 0)


def is_slot_aligned(moment: datetime) -> bool:
    return moment.minute % 30 == 0 and moment.second == 0 and moment.microsecond == 0


def floor_slot(moment: datetime) -> datetime:
    """The start of the slot that contains `moment`, in UTC."""
    utc = moment.astimezone(UTC)
    return utc.replace(minute=utc.minute - utc.minute % 30, second=0, microsecond=0)


def ceil_slot(moment: datetime) -> datetime:
    """The first slot start at or after `moment`, in UTC."""
    start = floor_slot(moment)
    return start if start == moment.astimezone(UTC) else start + SLOT


def slots_between(start: datetime, end: datetime) -> list[datetime]:
    """Every slot start in [start, end), both aligned."""
    slots = []
    current = start
    while current < end:
        slots.append(current)
        current += SLOT
    return slots


def covered_slots(start: datetime, duration: timedelta = MATCH_DURATION) -> list[datetime]:
    """The slots a match starting at `start` (any minute) occupies."""
    first = floor_slot(start)
    return slots_between(first, ceil_slot(start + duration))


def zone_from(iana: str | None, offset_minutes: int | None) -> tzinfo | None:
    """The zone for a person: their IANA name when it is valid, else a fixed offset (wybin often
    only knows "UTC+5:30"), else None."""
    if iana:
        try:
            return zoneinfo.ZoneInfo(iana)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            pass
    if offset_minutes is not None:
        return timezone(timedelta(minutes=offset_minutes))
    return None


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_slots(monday: date, zone: tzinfo) -> list[datetime]:
    """The slots from the local midnight that starts `monday` up to the next Monday's: 336 in a
    plain week, 2 fewer or more when the zone changes its clock inside it."""
    start = datetime.combine(monday, time(), tzinfo=zone)
    end = datetime.combine(monday + timedelta(days=WEEK_DAYS), time(), tzinfo=zone)
    return slots_between(ceil_slot(start), ceil_slot(end))


def is_reasonable_hour(moment: datetime, zone: tzinfo) -> bool:
    """Whether `moment` falls between 10:00 and 23:00 on the clock of `zone`."""
    local = moment.astimezone(zone).time()
    return OPPONENT_DAY_START <= local < OPPONENT_DAY_END

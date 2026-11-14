"""Availability: the weekly grid, who is free when, and the best-slot finder.

Everything here is pure: the caller loads availability and passes it in as
`{osu_id: {slot start (UTC text): "yes" | "maybe"}}`. A slot with no entry means "no". A match
needs players free for its whole length, so a person counts as free only when every slot the
match covers says so.
"""

import zoneinfo
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

from app.clock import to_utc_iso
from app.domain.slots import (
    MATCH_DURATION,
    SLOT,
    WEEK_DAYS,
    ceil_slot,
    covered_slots,
    is_reasonable_hour,
    is_slot_aligned,
    slots_between,
    week_monday,
    week_slots,
)
from app.domain.timezones import is_valid_timezone

YES, MAYBE, NO = "yes", "maybe", "no"
STATES = (YES, MAYBE, NO)
STATE_LABELS = {YES: "Yes", MAYBE: "Maybe", NO: "No"}
_VALUE = {YES: 2, MAYBE: 1}

TOP_SLOTS = 10
SEARCH_DAYS = 14
# Taken off a slot's score for every opponent for whom it is outside 10:00-23:00 local time. Our
# players score 2 for "yes" and 1 for "maybe", so one inconvenient opponent costs half a "yes".
OPPONENT_OUTSIDE_PENALTY = 1
# A person sets at most this many slots in one request (a week plus the two extra slots of a
# 25-hour day, rounded up).
MAX_RANGE_SLOTS = 400

AvailabilityMap = Mapping[int, Mapping[str, str]]


class RangeError(ValueError):
    """A slot range that cannot be saved; the message is safe to show."""


@dataclass(frozen=True)
class Column:
    osu_id: int
    name: str
    avatar_url: str | None = None


@dataclass(frozen=True)
class MatchBand:
    """A match drawn across the grid: the label shows on the first row it covers."""

    start: datetime
    label: str


@dataclass(frozen=True)
class GridRow:
    slot: datetime  # UTC start
    slot_text: str  # as stored, e.g. 2026-11-16T17:00:00Z
    row_id: str  # the row's element id, from the epoch second so it needs no escaping
    time_label: str  # 18:00 on the viewer's clock
    spoken_label: str  # Mon 16 Nov 18:00, for screen readers
    day_label: str | None  # set on the first row of each local day
    day_anchor: str
    states: list[str]  # one per column, in column order
    yes: int
    maybe: int
    band_label: str | None  # set on the first row of a match
    in_match: bool


@dataclass(frozen=True)
class Grid:
    monday: date
    rows: list[GridRow]
    days: list[tuple[str, str]]  # (anchor, label) per local day, for the jump links


@dataclass(frozen=True)
class HourHead:
    label: str  # "18:00"
    span: int  # slots in this local hour, normally 2


@dataclass(frozen=True)
class TimelineRun:
    """Consecutive slots with the same answer, drawn as one bar."""

    state: str
    span: int
    start_label: str
    end_label: str  # "24:00" when the run reaches the end of the day


@dataclass(frozen=True)
class TimelineRow:
    column: Column
    filled: bool  # saved at least one Yes or Maybe slot in the shown week
    runs: list[TimelineRun]
    summary: str  # spoken text: "Yes 18:00-23:00, Maybe 23:00-24:00, No otherwise"


@dataclass(frozen=True)
class BandRun:
    """A stretch of the match row. `label` is set on the first run of a match only; `in_match`
    marks the runs a match covers (a match that began on the previous day has none)."""

    label: str | None
    span: int
    in_match: bool = False


@dataclass(frozen=True)
class FreeRun:
    yes: int
    maybe: int
    span: int


@dataclass(frozen=True)
class DayTimeline:
    day_label: str  # "Monday 16 Nov"
    anchor: str
    hours: list[HourHead]
    rows: list[TimelineRow]
    bands: list[BandRun]
    free: list[FreeRun]
    slot_count: int

    @property
    def has_match(self) -> bool:
        return any(run.label for run in self.bands)


def parse_state(text: str) -> str:
    if text not in STATES:
        raise RangeError("Choose Yes, Maybe or No.")
    return text


def parse_range(start: str, end: str) -> list[datetime]:
    """The slots from `start` to `end` inclusive. Both must be slot starts in UTC text; the range
    is at most `MAX_RANGE_SLOTS` long."""
    first, last = _parse_slot(start), _parse_slot(end)
    if last < first:
        raise RangeError("The range ends before it starts.")
    count = int((last - first) / SLOT) + 1
    if count > MAX_RANGE_SLOTS:
        raise RangeError("Choose at most one week at a time.")
    return slots_between(first, last + SLOT)


def _parse_slot(text: str) -> datetime:
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise RangeError("That time slot is not valid.") from error
    if not is_slot_aligned(moment):
        raise RangeError("Time slots start on the hour or half hour (UTC).")
    return moment


def parse_week(text: str | None, today: date) -> date:
    """The Monday of the week named by `text` (any day in it, `YYYY-MM-DD`), else of `today`."""
    if text and len(text) == 10 and text.isascii():
        try:
            day = date.fromisoformat(text)
        except ValueError:
            day = today
        else:
            if not 2000 <= day.year <= 2100:
                day = today
    else:
        day = today
    return week_monday(day)


def band_covers(bands: Iterable[MatchBand]) -> dict[datetime, str | None]:
    """Every slot a match covers -> the label on its first slot (None on the others)."""
    covered: dict[datetime, str | None] = {}
    for band in bands:
        for index, slot in enumerate(covered_slots(band.start)):
            covered.setdefault(slot, band.label if index == 0 else None)
    return covered


def build_rows(
    slots: Iterable[datetime],
    zone: tzinfo,
    columns: list[Column],
    availability: AvailabilityMap,
    bands: Iterable[MatchBand] = (),
) -> list[GridRow]:
    covered = band_covers(bands)
    rows: list[GridRow] = []
    previous_day: date | None = None
    for slot in slots:
        local = slot.astimezone(zone)
        text = to_utc_iso(slot)
        states = [availability.get(column.osu_id, {}).get(text, NO) for column in columns]
        new_day = local.date() != previous_day
        previous_day = local.date()
        rows.append(
            GridRow(
                slot=slot,
                slot_text=text,
                row_id=f"slot-{int(slot.timestamp())}",
                time_label=local.strftime("%H:%M"),
                spoken_label=local.strftime("%a %d %b %H:%M"),
                day_label=local.strftime("%A %d %B") if new_day else None,
                day_anchor=f"day-{local.date().isoformat()}",
                states=states,
                yes=states.count(YES),
                maybe=states.count(MAYBE),
                band_label=covered.get(slot) if slot in covered else None,
                in_match=slot in covered,
            )
        )
    return rows


def build_grid(
    monday: date,
    zone: tzinfo,
    columns: list[Column],
    availability: AvailabilityMap,
    bands: Iterable[MatchBand] = (),
) -> Grid:
    rows = build_rows(week_slots(monday, zone), zone, columns, availability, bands)
    days = [(row.day_anchor, row.day_label) for row in rows if row.day_label]
    return Grid(monday, rows, days)


def build_timelines(
    monday: date,
    zone: tzinfo,
    columns: list[Column],
    availability: AvailabilityMap,
    bands: Iterable[MatchBand],
    filled_ids: set[int],
) -> list[DayTimeline]:
    """One timeline per local day of the week: a row of bars per player, the matches and how many
    are free. A day that the zone shortens or lengthens has 46 or 50 slots."""
    covered = band_covers(bands)
    days: dict[date, list[datetime]] = {}
    for slot in week_slots(monday, zone):
        days.setdefault(slot.astimezone(zone).date(), []).append(slot)
    return [
        _day_timeline(day, slots, zone, columns, availability, covered, filled_ids)
        for day, slots in days.items()
    ]


def _day_timeline(
    day: date,
    slots: list[datetime],
    zone: tzinfo,
    columns: list[Column],
    availability: AvailabilityMap,
    covered: Mapping[datetime, str | None],
    filled_ids: set[int],
) -> DayTimeline:
    states = {
        column.osu_id: [
            availability.get(column.osu_id, {}).get(to_utc_iso(slot), NO) for slot in slots
        ]
        for column in columns
    }
    rows = []
    for column in columns:
        runs = _state_runs(slots, states[column.osu_id], zone, day)
        rows.append(TimelineRow(column, column.osu_id in filled_ids, runs, _summary(runs)))
    free = _free_runs(
        [
            (
                sum(states[c.osu_id][i] == YES for c in columns),
                sum(states[c.osu_id][i] == MAYBE for c in columns),
            )
            for i in range(len(slots))
        ]
    )
    return DayTimeline(
        day_label=datetime.combine(day, datetime.min.time()).strftime("%A %d %b"),
        anchor=f"day-{day.isoformat()}",
        hours=_hour_heads(slots, zone),
        rows=rows,
        bands=_band_runs(slots, covered),
        free=free,
        slot_count=len(slots),
    )


def _hour_heads(slots: list[datetime], zone: tzinfo) -> list[HourHead]:
    """One head per local hour; built from the slots so a clock change keeps the spans right."""
    heads: list[tuple[str, int]] = []
    previous: tuple[date, int] | None = None
    for slot in slots:
        local = slot.astimezone(zone)
        key = (local.date(), local.hour)
        if key == previous:
            heads[-1] = (heads[-1][0], heads[-1][1] + 1)
        else:
            heads.append((f"{local.hour:02d}:00", 1))
            previous = key
    return [HourHead(label, span) for label, span in heads]


def _clock_label(moment: datetime, zone: tzinfo, day: date) -> str:
    local = moment.astimezone(zone)
    return "24:00" if local.date() != day else local.strftime("%H:%M")


def _state_runs(
    slots: list[datetime], states: list[str], zone: tzinfo, day: date
) -> list[TimelineRun]:
    runs: list[TimelineRun] = []
    first = 0
    for index in range(1, len(slots) + 1):
        if index < len(slots) and states[index] == states[first]:
            continue
        runs.append(
            TimelineRun(
                state=states[first],
                span=index - first,
                start_label=_clock_label(slots[first], zone, day),
                end_label=_clock_label(slots[index - 1] + SLOT, zone, day),
            )
        )
        first = index
    return runs


def _summary(runs: list[TimelineRun]) -> str:
    parts = [
        f"{STATE_LABELS[run.state]} {run.start_label}-{run.end_label}"
        for run in runs
        if run.state != NO
    ]
    if not parts:
        return "No all day"
    if any(run.state == NO for run in runs):
        parts.append("No otherwise")
    return ", ".join(parts)


def _band_runs(slots: list[datetime], covered: Mapping[datetime, str | None]) -> list[BandRun]:
    runs: list[BandRun] = []
    for slot in slots:
        inside = slot in covered
        label = covered.get(slot)
        extends = (
            runs and label is None and runs[-1].in_match == inside
        )  # an unlabelled slot continues the run before it
        if extends:
            runs[-1] = BandRun(runs[-1].label, runs[-1].span + 1, inside)
        else:
            runs.append(BandRun(label, 1, inside))
    return runs


def _free_runs(counts: list[tuple[int, int]]) -> list[FreeRun]:
    runs: list[FreeRun] = []
    for yes, maybe in counts:
        if runs and (runs[-1].yes, runs[-1].maybe) == (yes, maybe):
            runs[-1] = FreeRun(yes, maybe, runs[-1].span + 1)
        else:
            runs.append(FreeRun(yes, maybe, 1))
    return runs


def week_bounds(monday: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """The first slot of the week and the instant it ends, both UTC."""
    slots = week_slots(monday, zone)
    return slots[0], slots[-1] + SLOT


def previous_week(monday: date) -> date:
    return monday - timedelta(days=WEEK_DAYS)


def next_week(monday: date) -> date:
    return monday + timedelta(days=WEEK_DAYS)


# --- who is free for a match -----------------------------------------------------------------


def _worst(slots: Mapping[str, str], covered: list[str]) -> int:
    """0 (no), 1 (maybe) or 2 (yes): the weakest answer over every covered slot."""
    return min(_VALUE.get(slots.get(slot, NO), 0) for slot in covered)


def _covered_texts(start: datetime, duration: timedelta) -> list[str]:
    return [to_utc_iso(slot) for slot in covered_slots(start, duration)]


def available_players(
    availability: AvailabilityMap,
    osu_ids: Collection[int],
    start: datetime,
    duration: timedelta = MATCH_DURATION,
) -> set[int]:
    """The people who said "yes" for every slot of a match starting at `start`."""
    covered = _covered_texts(start, duration)
    return {osu_id for osu_id in osu_ids if _worst(availability.get(osu_id, {}), covered) == 2}


def maybe_players(
    availability: AvailabilityMap,
    osu_ids: Collection[int],
    start: datetime,
    duration: timedelta = MATCH_DURATION,
) -> set[int]:
    """The people whose weakest answer for the match is "maybe"."""
    covered = _covered_texts(start, duration)
    return {osu_id for osu_id in osu_ids if _worst(availability.get(osu_id, {}), covered) == 1}


# --- best slots ------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpponentPlayer:
    name: str
    zone: tzinfo | None  # None = wybin knows no timezone for them
    zone_label: str


@dataclass(frozen=True)
class OpponentTime:
    name: str
    zone_label: str
    local_time: str | None  # "Mon 16 Nov 20:00"; None when their zone is unknown
    outside: bool  # the match would not fit between 10:00 and 23:00 for them


@dataclass(frozen=True)
class SlotScore:
    start: datetime
    score: int
    yes: int  # of the players that would play, how many said yes the whole time
    maybe: int
    lineup_size: int
    opponents: list[OpponentTime]
    outside: int


def best_slots(
    availability: AvailabilityMap,
    osu_ids: Collection[int],
    lineup_size: int,
    opponents: list[OpponentPlayer],
    window_start: datetime,
    window_end: datetime,
    limit: int = TOP_SLOTS,
    duration: timedelta = MATCH_DURATION,
) -> list[SlotScore]:
    """The best start times in [window_start, window_end), best first.

    Our score for a start is "yes" = 2 and "maybe" = 1 summed over the `lineup_size` people with
    the highest answers, each judged over the whole match. Every opponent for whom the match
    falls outside 10:00-23:00 on their clock costs `OPPONENT_OUTSIDE_PENALTY`. Starts where none
    of ours is free are left out. Ties go to fewer inconvenienced opponents, then the earlier
    start, so the order is the same on every run.
    """
    scored: list[SlotScore] = []
    for start in slots_between(ceil_slot(window_start), window_end):
        covered = _covered_texts(start, duration)
        values = sorted(
            (_worst(availability.get(osu_id, {}), covered) for osu_id in osu_ids), reverse=True
        )[:lineup_size]
        ours = sum(values)
        if ours == 0:
            continue
        times = [_opponent_time(opponent, start, duration) for opponent in opponents]
        outside = sum(time.outside for time in times)
        scored.append(
            SlotScore(
                start=start,
                score=ours - OPPONENT_OUTSIDE_PENALTY * outside,
                yes=values.count(2),
                maybe=values.count(1),
                lineup_size=lineup_size,
                opponents=times,
                outside=outside,
            )
        )
    scored.sort(key=lambda slot: (-slot.score, slot.outside, slot.start))
    return scored[:limit]


def _opponent_time(opponent: OpponentPlayer, start: datetime, duration: timedelta) -> OpponentTime:
    if opponent.zone is None:
        return OpponentTime(opponent.name, opponent.zone_label, None, False)
    fits = all(is_reasonable_hour(slot, opponent.zone) for slot in covered_slots(start, duration))
    return OpponentTime(
        opponent.name,
        opponent.zone_label,
        start.astimezone(opponent.zone).strftime("%a %d %b %H:%M"),
        not fits,
    )


# --- the zone the grid is shown in -------------------------------------------------------------


@dataclass(frozen=True)
class ViewZone:
    name: str  # "UTC" or an IANA name
    zone: tzinfo
    is_local: bool
    needs_detection: bool  # local was asked for but no timezone is known yet


def view_zone(mode: str, saved: str | None, browser: str | None) -> ViewZone:
    """The zone for `?tz=`: UTC unless "local". Local uses the timezone saved on the profile,
    else the one the browser reported, else falls back to UTC and asks the browser for it."""
    if mode != "local":
        return ViewZone("UTC", UTC, False, False)
    for name in (saved, browser):
        if name and is_valid_timezone(name):
            return ViewZone(name, zoneinfo.ZoneInfo(name), True, False)
    return ViewZone("UTC", UTC, True, True)

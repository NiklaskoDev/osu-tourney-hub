"""What the schedule, availability and lineup pages show about matches: our upcoming and past
matches with their opponents, the slot finder, and the "available at match time" lineup filter.
Loads from the repos and uses the pure rules in `availability`."""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.clock import from_utc_iso, to_utc_iso
from app.domain.models import Match, Member, Stage, Team, TeamPlayer, Tournament
from app.domain.parsing import parse_uint
from app.domain.slots import MATCH_DURATION, ceil_slot, floor_slot, zone_from
from app.repos import availability as availability_repo
from app.repos import matches as matches_repo
from app.repos import members as members_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos import tournaments as tournaments_repo
from app.services import availability, lineup
from app.services.availability import Column, MatchBand, OpponentPlayer, SlotScore
from app.services.ical import CalendarEvent
from app.services.score_capture import capture_stages
from app.services.tournament_settings import effective_lineup_size

WON, LOST = "W", "L"
UNKNOWN_TEAM = "To be decided"


@dataclass(frozen=True)
class MatchView:
    match: Match
    title: str  # "Alpha Squad vs Rival Crew"
    opponent: Team | None  # None when we do not play in it, or the opponent is not decided
    opponent_name: str
    stage_name: str | None
    result: str | None  # "5 - 3" from our side, when we play and it has a result
    outcome: str | None  # "W" or "L"
    can_find_slot: bool

    @property
    def start_text(self) -> str:
        """The start as plain UTC text for pickers, e.g. "2026-12-06 12:00 UTC"."""
        stamp = self.match.scheduled_at
        return f"{stamp[:16].replace('T', ' ')} UTC" if stamp else "no time yet"


@dataclass(frozen=True)
class ScheduleView:
    upcoming: list[MatchView]
    past: list[MatchView]
    only_ours: bool  # False when no team is marked as ours, so every match is listed


@dataclass(frozen=True)
class SlotSearch:
    view: MatchView
    slots: list[SlotScore]
    window_start: datetime
    window_end: datetime
    lineup_size: int
    players: int  # how many of ours were considered


@dataclass(frozen=True)
class LineupFilter:
    """The "only players available at match time" choice on the lineup page."""

    matches: list[MatchView]  # our upcoming matches of the stage that have a time
    selected: MatchView | None
    enabled: bool
    available_ids: set[int] | None  # None = not filtering
    maybe_count: int  # people left out who answered "maybe"


def teams_by_id(conn: sqlite3.Connection, tournament_id: int) -> dict[int, Team]:
    return {team.id: team for team in teams_repo.list_for_tournament(conn, tournament_id)}


def load_schedule(conn: sqlite3.Connection, tournament: Tournament, now: datetime) -> ScheduleView:
    teams = teams_by_id(conn, tournament.id)
    stages = {stage.id: stage for stage in stages_repo.list_for_tournament(conn, tournament.id)}
    ours = tournament.our_team_id
    matches = matches_repo.list_for_tournament(conn, tournament.id)
    if ours is not None:
        matches = [match for match in matches if match.involves(ours)]
    stamp = to_utc_iso(now)
    upcoming, past = [], []
    for match in matches:
        view = _view(match, teams, stages, ours)
        is_past = match.status == "done" or (
            match.scheduled_at is not None and match.scheduled_at <= stamp
        )
        (past if is_past else upcoming).append(view)
    past.reverse()  # most recent first
    return ScheduleView(upcoming, past, ours is not None)


def _view(
    match: Match, teams: dict[int, Team], stages: dict[int, Stage], ours: int | None
) -> MatchView:
    opponent_id = match.opponent_of(ours)
    opponent = teams.get(opponent_id) if opponent_id is not None else None
    team1 = teams.get(match.team1_id) if match.team1_id else None
    team2 = teams.get(match.team2_id) if match.team2_id else None
    title = f"{team1.name if team1 else UNKNOWN_TEAM} vs {team2.name if team2 else UNKNOWN_TEAM}"
    result = outcome = None
    if match.status == "done" and match.score1 is not None and match.score2 is not None:
        if ours is not None and match.team2_id == ours:
            mine, theirs = match.score2, match.score1
        else:
            mine, theirs = match.score1, match.score2
        result = f"{mine} - {theirs}"
        if match.involves(ours):
            outcome = WON if mine > theirs else LOST
    stage = stages.get(match.stage_id) if match.stage_id else None
    return MatchView(
        match=match,
        title=title,
        opponent=opponent,
        opponent_name=opponent.name if opponent else UNKNOWN_TEAM,
        stage_name=stage.name if stage else None,
        result=result,
        outcome=outcome,
        can_find_slot=match.status != "done" and opponent is not None,
    )


def match_view(conn: sqlite3.Connection, tournament: Tournament, match: Match) -> MatchView:
    stages = {stage.id: stage for stage in stages_repo.list_for_tournament(conn, tournament.id)}
    return _view(match, teams_by_id(conn, tournament.id), stages, tournament.our_team_id)


# --- lineup size, columns and bands ------------------------------------------------------------


def current_lineup_size(
    conn: sqlite3.Connection, tournament: Tournament, now: datetime, stage_id: int | None = None
) -> int:
    """Players per map: for the given stage, else for the stage being played now, else the
    smallest team size wybin allows."""
    stages = stages_repo.list_for_tournament(conn, tournament.id)
    chosen = next((stage for stage in stages if stage.id == stage_id), None)
    if chosen is None:
        current = capture_stages(stages, now)
        chosen = current[0] if current else None
    if chosen is None:
        return 1 if tournament.is_solo else tournament.team_size_min or 1
    return effective_lineup_size(chosen, tournament)


def roster_columns(members: list[Member]) -> list[Column]:
    """Everyone on the roster but guests, who have no availability row."""
    return [
        Column(
            member.osu_id,
            member.username or f"osu! user {member.osu_id}",
            member.avatar_url,
        )
        for member in members
        if member.role != "guest"
    ]


def bands_between(
    conn: sqlite3.Connection, tournament: Tournament, start: datetime, end: datetime
) -> list[MatchBand]:
    """Our matches that start inside [start, end), for the overlay on the grid."""
    ours = tournament.our_team_id
    if ours is None:
        return []
    teams = teams_by_id(conn, tournament.id)
    bands = []
    for match in matches_repo.list_for_tournament(conn, tournament.id):
        if not match.involves(ours) or match.scheduled_at is None:
            continue
        moment = from_utc_iso(match.scheduled_at)
        if start <= moment < end:
            opponent = teams.get(match.opponent_of(ours) or 0)
            bands.append(MatchBand(moment, f"vs {opponent.name if opponent else UNKNOWN_TEAM}"))
    return bands


# --- slot finder --------------------------------------------------------------------------------


def search_window(match: Match, stage: Stage | None, now: datetime) -> tuple[datetime, datetime]:
    """From the next free slot for two weeks, but not past the end of the match's stage."""
    start = ceil_slot(now)
    end = start + timedelta(days=availability.SEARCH_DAYS)
    if stage is not None and stage.end_at:
        stage_end = from_utc_iso(stage.end_at)
        if stage_end > start:
            end = min(end, stage_end)
    return start, end


def find_slots(
    conn: sqlite3.Connection, tournament: Tournament, match: Match, now: datetime
) -> SlotSearch:
    view = match_view(conn, tournament, match)
    stage = stages_repo.get(conn, match.stage_id) if match.stage_id else None
    start, end = search_window(match, stage, now)
    members = members_repo.list_for_tournament(conn, tournament.id)
    ids = lineup.eligible_players(members)
    size = current_lineup_size(conn, tournament, now, match.stage_id)
    found = availability_repo.list_range(
        conn, tournament.id, ids, to_utc_iso(start), to_utc_iso(end + MATCH_DURATION)
    )
    opponents = _opponent_players(conn, view.opponent)
    slots = availability.best_slots(found, ids, size, opponents, start, end)
    return SlotSearch(view, slots, start, end, size, len(ids))


def _opponent_players(conn: sqlite3.Connection, team: Team | None) -> list[OpponentPlayer]:
    if team is None:
        return []
    players = []
    for player in teams_repo.list_players(conn, team.id):
        zone = zone_from(player.timezone_iana, player.timezone_offset_min)
        players.append(OpponentPlayer(player.username, zone, _zone_label(player)))
    return players


def _zone_label(player: TeamPlayer) -> str:
    if player.timezone_iana and zone_from(player.timezone_iana, None) is not None:
        return str(player.timezone_iana)
    offset = player.timezone_offset_min
    if offset is None:
        return "unknown timezone"
    sign = "-" if offset < 0 else "+"
    hours, minutes = divmod(abs(offset), 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


# --- lineup filter -----------------------------------------------------------------------------


def lineup_filter(
    conn: sqlite3.Connection,
    tournament: Tournament,
    stage: Stage,
    members: Collection[Member],
    now: datetime,
    match_ref: str | None,
    enabled: bool,
) -> LineupFilter:
    """The upcoming matches of this stage (ours, with a time) to choose from, and, when the
    filter is on, who said "yes" for the whole of the chosen one (default: the next)."""
    schedule = load_schedule(conn, tournament, now)
    choices = [
        view
        for view in schedule.upcoming
        if view.match.stage_id == stage.id
        and view.match.scheduled_at is not None
        and tournament.our_team_id is not None
    ]
    wanted = parse_uint(match_ref or "")
    selected = next((view for view in choices if view.match.id == wanted), None)
    if selected is None and choices:
        selected = choices[0]
    if not (enabled and selected is not None):
        return LineupFilter(choices, selected, False, None, 0)
    start = from_utc_iso(selected.match.scheduled_at or "")
    ids = lineup.eligible_players(members, include_substitutes=True)
    found = availability_repo.list_range(
        conn,
        tournament.id,
        ids,
        to_utc_iso(floor_slot(start)),
        to_utc_iso(start + MATCH_DURATION + availability.SLOT),
    )
    free = availability.available_players(found, ids, start)
    maybes = availability.maybe_players(found, ids, start)
    return LineupFilter(choices, selected, True, free, len(maybes))


# --- calendar feed and homepage ----------------------------------------------------------------


@dataclass(frozen=True)
class UpcomingMatch:
    tournament: Tournament
    view: MatchView


def _calendar_views(
    conn: sqlite3.Connection, matches: list[Match]
) -> list[tuple[Tournament, MatchView]]:
    """Each timed match with its tournament and view. Tournament, teams and stages are loaded
    once per tournament."""
    tournaments: dict[int, Tournament | None] = {}
    teams: dict[int, dict[int, Team]] = {}
    stages: dict[int, dict[int, Stage]] = {}
    pairs = []
    for match in matches:
        tid = match.tournament_id
        if tid not in tournaments:
            tournaments[tid] = tournaments_repo.get(conn, tid)
            teams[tid] = teams_by_id(conn, tid)
            stages[tid] = {s.id: s for s in stages_repo.list_for_tournament(conn, tid)}
        tournament = tournaments[tid]
        if tournament is None or match.scheduled_at is None:
            continue
        pairs.append((tournament, _view(match, teams[tid], stages[tid], tournament.our_team_id)))
    return pairs


def feed_events(conn: sqlite3.Connection, osu_id: int) -> list[CalendarEvent]:
    """Our team's matches with a time in every active tournament the person belongs to."""
    events = []
    for tournament, view in _calendar_views(conn, matches_repo.list_for_calendar(conn, osu_id)):
        match = view.match
        prefix = tournament.acronym or tournament.name
        label = f"{match.label}: " if match.label else ""
        events.append(
            CalendarEvent(
                match_id=match.id,
                start=from_utc_iso(match.scheduled_at or ""),
                summary=f"[{prefix}] {label}{view.title}",
                modified=from_utc_iso(match.updated_at),
            )
        )
    return events


def upcoming_for_user(
    conn: sqlite3.Connection, osu_id: int, now: datetime, limit: int = 5
) -> list[UpcomingMatch]:
    """The next matches of our teams across the person's active tournaments, soonest first. A match
    that began less than `MATCH_DURATION` ago still counts, so one being played shows."""
    earliest = to_utc_iso(now - MATCH_DURATION)
    matches = [
        match
        for match in matches_repo.list_for_calendar(conn, osu_id)
        if match.status != "done" and (match.scheduled_at or "") >= earliest
    ]
    return [
        UpcomingMatch(tournament, view)
        for tournament, view in _calendar_views(conn, matches[:limit])
    ]

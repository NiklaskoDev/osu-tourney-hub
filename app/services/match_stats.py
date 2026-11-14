"""What was played in a match: the winner and totals of every map, and what each rostered player
scored. Built from `match_events` (the picks) and the `scores` the lobby read stored; stats are a
different concern from `history`, which compares the picks with the prediction.

Only players on either team's roster have scores here. A stand-in counts in a map's team total (via
the team colour) but has no player line.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from app.domain.models import Match, PoolMap, Score, Stage, Team, Tournament
from app.repos import match_events as events_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.services import schedule
from app.services.schedule import MatchView

MAP_KINDS = ("pick", "tiebreaker")


@dataclass(frozen=True)
class PlayerScore:
    osu_id: int
    name: str
    side: int | None  # 1 or 2 as in the matches table
    score: int
    accuracy: float | None
    mods: str
    misses: int | None
    max_combo: int | None
    passed: bool


@dataclass(frozen=True)
class MapStat:
    seq: int
    kind: str  # pick or tiebreaker
    pool_map: PoolMap | None
    beatmap_id: int | None
    winner_side: int | None
    winner_name: str | None
    team1_total: int | None
    team2_total: int | None
    scores: list[PlayerScore]


@dataclass(frozen=True)
class PlayerLine:
    osu_id: int
    name: str
    side: int | None  # None in the tournament totals, where a player's side changes per match
    maps_played: int
    mean_score: float
    mean_accuracy: float | None
    maps_won: int


@dataclass(frozen=True)
class MatchStats:
    match: Match
    stage: Stage | None
    team1: Team | None
    team2: Team | None
    maps: list[MapStat]
    players: list[PlayerLine]
    has_lobby: bool
    lobby_read: bool  # the lobby's games were read at least once


@dataclass(frozen=True)
class ResultsView:
    matches: list[MatchView]
    players: list[PlayerLine]


def build_match_stats(conn: sqlite3.Connection, match: Match) -> MatchStats:
    stage = stages_repo.get(conn, match.stage_id) if match.stage_id is not None else None
    team1 = teams_repo.get(conn, match.team1_id) if match.team1_id is not None else None
    team2 = teams_repo.get(conn, match.team2_id) if match.team2_id is not None else None
    events = events_repo.list_for_match(conn, match.id)
    lobby_read = any(event.source == "mp" for event in events)
    has_lobby = match.mp_match_id is not None
    roster = _roster(conn, match)
    by_beatmap = _last_game_scores(conn, match) if has_lobby else {}
    maps = []
    for event in events:
        if event.kind not in MAP_KINDS:
            continue
        pool_map = stages_repo.get_map(conn, event.pool_map_id) if event.pool_map_id else None
        # Only the two rosters get player rows; a stand-in still counts in the map totals.
        scores = [
            _player_score(score, roster)
            for score in by_beatmap.get(event.beatmap_id or 0, [])
            if score.osu_id in roster
        ]
        maps.append(
            MapStat(
                seq=event.seq,
                kind=event.kind,
                pool_map=pool_map,
                beatmap_id=event.beatmap_id,
                winner_side=event.winner_side,
                winner_name=_team_name(event.winner_side, team1, team2),
                team1_total=event.team1_score,
                team2_total=event.team2_score,
                scores=scores,
            )
        )
    return MatchStats(
        match, stage, team1, team2, maps, _lines(_entries(maps, None)), has_lobby, lobby_read
    )


def tournament_results(
    conn: sqlite3.Connection, tournament: Tournament, now: datetime
) -> ResultsView:
    """The played matches, and the totals of our team's players over every played match whose lobby
    was read. No players when no team is marked as ours."""
    played = schedule.load_schedule(conn, tournament, now).past
    ours = tournament.our_team_id
    # One stats build per played match: fine for a tournament's few matches, so no batching.
    if ours is None:
        return ResultsView(played, [])
    entries: list[tuple[PlayerScore, bool]] = []
    for view in played:
        if view.match.mp_match_id is None:
            continue
        stats = build_match_stats(conn, view.match)
        our_side = 1 if view.match.team1_id == ours else 2
        entries.extend(_entries(stats.maps, our_side))
    return ResultsView(played, _lines(entries, with_side=False))


def _roster(conn: sqlite3.Connection, match: Match) -> dict[int, tuple[str, int]]:
    """osu! id -> (name, side) of every player on either team."""
    roster: dict[int, tuple[str, int]] = {}
    for side, team_id in ((1, match.team1_id), (2, match.team2_id)):
        if team_id is not None:
            for player in teams_repo.list_players(conn, team_id):
                roster[player.osu_id] = (player.username or f"osu! user {player.osu_id}", side)
    return roster


def _last_game_scores(conn: sqlite3.Connection, match: Match) -> dict[int, list[Score]]:
    """beatmap id -> the scores of its last game. A map played again counts once, with the result
    of the last game (the rule the lobby events follow)."""
    games: dict[int, dict[int, list[Score]]] = defaultdict(lambda: defaultdict(list))
    for score in scores_repo.list_mp_for_lobby(conn, match.mp_match_id or 0):
        game_id = int(score.source_ref.split(":", 1)[1])
        games[score.beatmap_id][game_id].append(score)
    return {beatmap_id: per_game[max(per_game)] for beatmap_id, per_game in games.items()}


def _player_score(score: Score, roster: dict[int, tuple[str, int]]) -> PlayerScore:
    """A rostered player's score; the caller has checked the roster."""
    name, side = roster[score.osu_id]
    return PlayerScore(
        osu_id=score.osu_id,
        name=name,
        side=side,
        score=score.score,
        accuracy=score.accuracy,
        mods=score.mods,
        misses=score.misses,
        max_combo=score.max_combo,
        passed=score.passed,
    )


def _team_name(side: int | None, team1: Team | None, team2: Team | None) -> str | None:
    team = {1: team1, 2: team2}.get(side or 0)
    return team.name if team else None


def _entries(maps: list[MapStat], only_side: int | None) -> list[tuple[PlayerScore, bool]]:
    """(score, whether that player's team won the map) for every player score, optionally only
    the players of one side."""
    return [
        (score, score.side is not None and score.side == stat.winner_side)
        for stat in maps
        for score in stat.scores
        if only_side is None or score.side == only_side
    ]


def _lines(entries: list[tuple[PlayerScore, bool]], with_side: bool = True) -> list[PlayerLine]:
    per_player: dict[int, list[tuple[PlayerScore, bool]]] = defaultdict(list)
    for entry in entries:
        per_player[entry[0].osu_id].append(entry)
    lines = []
    for osu_id, played in per_player.items():
        accuracies = [score.accuracy for score, _ in played if score.accuracy is not None]
        lines.append(
            PlayerLine(
                osu_id=osu_id,
                name=played[0][0].name,
                side=played[0][0].side if with_side else None,
                maps_played=len(played),
                mean_score=sum(score.score for score, _ in played) / len(played),
                mean_accuracy=sum(accuracies) / len(accuracies) if accuracies else None,
                maps_won=sum(won for _, won in played),
            )
        )
    return sorted(lines, key=lambda line: (line.side or 0, -line.mean_score, line.name.lower()))

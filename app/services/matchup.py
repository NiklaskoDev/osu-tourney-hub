"""What the match page, the pickems page and the qualifier seed line show. Loads rosters, scores and
the captain's plan from the repos and runs the pure model in `prediction`.

The "left" team of a match is ours when we play in it, else team 1, so the page reads from our side.
Prediction snapshots are stored from team 1's side (the `matches` table's own order).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import Match, Member, PoolMap, Stage, Team, Tournament
from app.repos import matches as matches_repo
from app.repos import members as members_repo
from app.repos import predictions as predictions_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos.predictions import PlanItem
from app.services import history, lineup, prediction, sheet
from app.services.prediction import Expectation, MatchPrediction, SeedEstimate, SeriesResult
from app.services.schedule import UNKNOWN_TEAM, teams_by_id
from app.services.sheet import BestKey, BestScore
from app.services.tournament_settings import effective_lineup_size

NO_TEAMS = "One or both teams of this match are not decided yet, so there is nothing to compare."
NO_STAGE = "This match has no stage yet, so there is no map pool to compare."
NO_POOL = "The pool for this stage is not published yet. The comparison appears here automatically."
NO_FORMAT = "This stage has no best-of set, so no series prediction is shown."
NO_DATA = "No scores yet for both teams on any map."


@dataclass(frozen=True)
class StageModel:
    """The scores of some teams on one stage's pool, and what each team is expected to score."""

    stage: Stage
    pool: list[PoolMap]
    expectations: dict[int, dict[int, Expectation | None]]  # team id -> pool map id -> expectation
    rosters: dict[int, list[int]]  # team id -> the osu! ids that may play
    best: dict[BestKey, BestScore]  # (osu id, pool map id) -> that player's best score


@dataclass(frozen=True)
class MatchupView:
    match: Match
    stage: Stage | None
    left: Team | None  # ours when we play in the match, else team 1
    right: Team | None
    we_play: bool
    pool: list[PoolMap]
    prediction: MatchPrediction | None
    plan: list[PlanItem]
    notice: str | None  # why there is no comparison
    format_notice: str | None  # why there is no series prediction

    @property
    def title(self) -> str:
        left = self.left.name if self.left else UNKNOWN_TEAM
        right = self.right.name if self.right else UNKNOWN_TEAM
        return f"{left} vs {right}"


@dataclass(frozen=True)
class PickemRow:
    match: Match
    title: str
    winner: str | None
    win_chance: float | None  # of the winner
    score: str | None  # winner's maps first, "5-3"
    with_data: int
    total: int
    result: str | None  # the real score once played
    reason: str | None  # why there is no prediction


@dataclass(frozen=True)
class PickemsView:
    stage: Stage
    rows: list[PickemRow]

    @property
    def copy_text(self) -> str:
        lines = [f"Pickems: {self.stage.name}"]
        for row in self.rows:
            if row.winner is not None and row.win_chance is not None:
                lines.append(f"{row.title}: {row.winner} {row.win_chance:.0%}, {row.score}")
        return "\n".join(lines)


@dataclass(frozen=True)
class SeedLine:
    stage_name: str
    estimate: SeedEstimate

    @property
    def text(self) -> str:
        ranked = self.estimate.ranked
        if self.estimate.seed is None:
            return f"Estimated seed: not enough scores yet ({ranked} teams with data)"
        total = ranked + self.estimate.left_out
        return (
            f"Estimated seed: {self.estimate.seed} of {total} (based on {ranked} teams with data)"
        )


# --- loading ----------------------------------------------------------------------------------


def load_model(
    conn: sqlite3.Connection, tournament: Tournament, stage: Stage, team_ids: list[int]
) -> StageModel:
    pool = stages_repo.list_maps(conn, stage.id)
    members = members_repo.list_for_tournament(conn, tournament.id)
    rosters = {team_id: _roster(conn, tournament, team_id, members) for team_id in team_ids}
    beatmaps = {m.beatmap_id for m in pool if m.beatmap_id is not None}
    everyone = {osu_id for roster in rosters.values() for osu_id in roster}
    scores = scores_repo.list_for_beatmaps(conn, beatmaps, everyone, tournament.gamemode)
    best = sheet.best_scores(pool, scores, tournament.scoring)
    size = effective_lineup_size(stage, tournament)
    expectations = {
        team_id: prediction.team_expectations(pool, best, roster, size)
        for team_id, roster in rosters.items()
    }
    return StageModel(stage, pool, expectations, rosters, best)


def _roster(
    conn: sqlite3.Connection, tournament: Tournament, team_id: int, members: list[Member]
) -> list[int]:
    """Who may play for the team. For our team that leaves out guests and substitutes, the same
    people the lineup suggestion uses."""
    players = [player.osu_id for player in teams_repo.list_players(conn, team_id)]
    if team_id != tournament.our_team_id:
        return players
    if not players:
        return lineup.eligible_players(members)
    benched = {m.osu_id for m in members if m.role == "guest" or m.is_substitute}
    return [osu_id for osu_id in players if osu_id not in benched]


def orient(match: Match, our_team_id: int | None) -> tuple[int | None, int | None]:
    """(left, right) team ids: ours on the left when we play in the match."""
    if our_team_id is not None and match.team2_id == our_team_id:
        return match.team2_id, match.team1_id
    return match.team1_id, match.team2_id


def build_matchup(conn: sqlite3.Connection, tournament: Tournament, match: Match) -> MatchupView:
    teams = teams_by_id(conn, tournament.id)
    left_id, right_id = orient(match, tournament.our_team_id)
    left = teams.get(left_id) if left_id is not None else None
    right = teams.get(right_id) if right_id is not None else None
    stage = stages_repo.get(conn, match.stage_id) if match.stage_id is not None else None
    we_play = match.involves(tournament.our_team_id)
    plan = predictions_repo.list_plan(conn, match.id) if we_play else []
    notice = _notice(left, right, stage)
    if notice is not None or left is None or right is None or stage is None:
        return MatchupView(match, stage, left, right, we_play, [], None, plan, notice, None)
    model = load_model(conn, tournament, stage, [left.id, right.id])
    result = prediction.predict_match(
        model.pool,
        stage,
        model.expectations[left.id],
        model.expectations[right.id],
        _habits(conn, tournament, match, left.id, right.id),
    )
    format_notice = None
    if result.with_data == 0:
        format_notice = NO_DATA
    elif result.series is None:
        format_notice = NO_FORMAT
    return MatchupView(
        match, stage, left, right, we_play, model.pool, result, plan, None, format_notice
    )


def _habits(
    conn: sqlite3.Connection, tournament: Tournament, match: Match, left_id: int, right_id: int
) -> tuple[prediction.Habits | None, prediction.Habits | None]:
    """How both teams picked and banned in the matches played before this one."""
    return (
        history.habits(conn, tournament.id, left_id, match.id, match.scheduled_at),
        history.habits(conn, tournament.id, right_id, match.id, match.scheduled_at),
    )


def _notice(left: Team | None, right: Team | None, stage: Stage | None) -> str | None:
    if left is None or right is None:
        return NO_TEAMS
    if stage is None:
        return NO_STAGE
    if not stage.pool_released:
        return NO_POOL
    return None


# --- snapshots --------------------------------------------------------------------------------


def snapshot_payload(
    left: Team, right: Team, result: MatchPrediction, series: SeriesResult, plan: list[PlanItem]
) -> dict:
    """What a snapshot keeps: the map table, the simulated protects, bans and picks per first-pick
    order, and the captain's plan. Side 1 is the match's left team."""
    return {
        "side1_team_id": left.id,
        "side2_team_id": right.id,
        "maps": [
            {
                "pool_map_id": c.pool_map.id,
                "slot": c.pool_map.slot,
                "side1": c.side1.score if c.side1 else None,
                "side2": c.side2.score if c.side2 else None,
                "prob_side1": c.prob,
                "quality": c.quality,
            }
            for c in result.comparisons
        ],
        "orders": [
            {
                "first_pick": order.first_pick,
                "win_prob_side1": order.win_prob,
                "protects": order.protects,
                "bans": order.bans,
                "picks": order.picks,
            }
            for order in series.orders
        ],
        "plan": [
            {"kind": item.kind, "pool_map_id": item.pool_map_id, "position": item.position}
            for item in plan
        ],
    }


def save_snapshot(conn: sqlite3.Connection, view: MatchupView, now: datetime) -> bool:
    """Store the prediction as it stands. Skipped (False) when there is none to keep. The page
    calls `save_daily_snapshot`; a plan change saves one directly."""
    result, left, right = view.prediction, view.left, view.right
    if result is None or result.series is None or result.with_data == 0:
        return False
    if left is None or right is None:
        return False
    series = result.series
    left_is_team1 = left.id == view.match.team1_id
    one, two = series.expected_score
    win_prob = min(1.0, max(0.0, series.win_prob))  # rounding must not leave the 0 to 1 range
    predictions_repo.insert_snapshot(
        conn,
        view.match.id,
        "stat",
        to_utc_iso(now),
        win_prob if left_is_team1 else 1 - win_prob,
        f"{one}-{two}" if left_is_team1 else f"{two}-{one}",
        json.dumps(snapshot_payload(left, right, result, series, view.plan), separators=(",", ":")),
        prediction.MODEL_VERSION,
    )
    return True


def save_daily_snapshot(conn: sqlite3.Connection, view: MatchupView, now: datetime) -> bool:
    """The first view of a match per day keeps a snapshot; a match that is over keeps none (a
    prediction made after the fact says nothing)."""
    if view.match.status == "done":
        return False
    day = to_utc_iso(now)[:10]
    if predictions_repo.has_snapshot_on_day(conn, view.match.id, "stat", day):
        return False
    with transaction(conn):
        return save_snapshot(conn, view, now)


# --- pickems ----------------------------------------------------------------------------------


def build_pickems(conn: sqlite3.Connection, tournament: Tournament, stage: Stage) -> PickemsView:
    """A prediction for every match of the stage, in time order."""
    matches = [
        m for m in matches_repo.list_for_tournament(conn, tournament.id) if m.stage_id == stage.id
    ]
    teams = teams_by_id(conn, tournament.id)
    ready = [m for m in matches if m.team1_id in teams and m.team2_id in teams]
    team_ids = sorted({team_id for m in ready for team_id in (m.team1_id, m.team2_id) if team_id})
    model = load_model(conn, tournament, stage, team_ids) if ready and stage.pool_released else None
    rows = [_pickem_row(conn, tournament, match, teams, stage, model) for match in matches]
    return PickemsView(stage, rows)


def _pickem_row(
    conn: sqlite3.Connection,
    tournament: Tournament,
    match: Match,
    teams: dict[int, Team],
    stage: Stage,
    model: StageModel | None,
) -> PickemRow:
    team1 = teams.get(match.team1_id) if match.team1_id else None
    team2 = teams.get(match.team2_id) if match.team2_id else None
    title = f"{team1.name if team1 else UNKNOWN_TEAM} vs {team2.name if team2 else UNKNOWN_TEAM}"
    played = (
        f"{match.score1} - {match.score2}"
        if match.status == "done" and match.score1 is not None and match.score2 is not None
        else None
    )
    if team1 is None or team2 is None:
        return PickemRow(match, title, None, None, None, 0, 0, played, "Teams not decided yet")
    if model is None:
        return PickemRow(match, title, None, None, None, 0, 0, played, "Pool not published yet")
    forecast = prediction.predict_match(
        model.pool,
        stage,
        model.expectations[team1.id],
        model.expectations[team2.id],
        _habits(conn, tournament, match, team1.id, team2.id),
    )
    reason = None
    if forecast.with_data == 0:
        reason = "No scores yet for both teams"
    elif forecast.series is None:
        reason = "No best-of set for this stage"
    if reason is not None or forecast.series is None:
        return PickemRow(match, title, None, None, None, 0, forecast.total, played, reason)
    one, two = forecast.series.expected_score
    first_wins = forecast.series.win_prob >= 0.5
    chance = forecast.series.win_prob if first_wins else 1 - forecast.series.win_prob
    return PickemRow(
        match,
        title,
        (team1 if first_wins else team2).name,
        chance,
        f"{one}-{two}" if first_wins else f"{two}-{one}",
        forecast.with_data,
        forecast.total,
        played,
        None,
    )


# --- qualifier seed ---------------------------------------------------------------------------


def seed_line(conn: sqlite3.Connection, tournament: Tournament, now: datetime) -> SeedLine | None:
    """Our estimated qualifier seed, while a qualifier stage with a published pool is still
    running. None when there is no such stage or no team of ours."""
    ours = tournament.our_team_id
    if ours is None:
        return None
    stamp = to_utc_iso(now)
    stage = next(
        (
            s
            for s in stages_repo.list_for_tournament(conn, tournament.id)
            if s.is_qualifier and s.pool_released and (s.end_at is None or s.end_at > stamp)
        ),
        None,
    )
    if stage is None:
        return None
    team_ids = [team.id for team in teams_repo.list_for_tournament(conn, tournament.id)]
    model = load_model(conn, tournament, stage, team_ids)
    map_ids = [m.id for m in model.pool if m.beatmap_id is not None]
    if not map_ids:
        return None
    return SeedLine(stage.name, prediction.qualifier_seed(ours, model.expectations, map_ids))

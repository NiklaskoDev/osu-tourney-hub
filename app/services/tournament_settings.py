"""Per-tournament settings: lineup size per stage, freemod rule, scoring, our team, placing.

Captains change the rules and lineup sizes. Only an admin changes which team is ours (it decides
who the roster is) and the final placing.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import FREEMOD_RULES, SCORING_MODES, Stage, Tournament
from app.domain.parsing import parse_uint
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos import tournaments as tournaments_repo
from app.services.archive import ArchiveError, clean_placing
from app.services.wybin_sync import add_roster_members

MAX_LINEUP_SIZE = 16
SOLO_LINEUP_SIZE = 1
FREEMOD_RULE_LABELS = {
    "none": "No rule",
    "each_player_mod": "Every player on a freemod map must use a mod",
    "two_distinct_mods": "A freemod map needs at least two different mods in the lineup",
}
SCORING_LABELS = {"v2": "ScoreV2", "v1": "ScoreV1", "lazer": "Lazer standardised"}


class SettingsError(Exception):
    """A refused form. `field` names the input to mark (None = a message above the form)."""

    def __init__(self, message: str, status: int = 422, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


@dataclass(frozen=True)
class SettingsForm:
    freemod_rule: str
    scoring: str
    lineup_sizes: dict[int, str] = field(default_factory=dict)  # by stage id
    our_team: str | None = None  # None = the form did not carry the field
    final_placing: str | None = None


def effective_lineup_size(stage: Stage, tournament: Tournament) -> int:
    """Players per map: always 1 in a solo tournament, else the stage's own setting, else the
    smallest team size wybin allows, else 1."""
    if tournament.is_solo:
        return SOLO_LINEUP_SIZE
    return stage.lineup_size or tournament.team_size_min or 1


def update_settings(
    conn: sqlite3.Connection,
    tournament: Tournament,
    form: SettingsForm,
    is_admin: bool,
    now: datetime,
) -> None:
    """Validate everything first, then apply it in one transaction."""
    if form.freemod_rule not in FREEMOD_RULES:
        raise SettingsError("Choose one of the listed rules.", field="freemod_rule")
    if form.scoring not in SCORING_MODES:
        raise SettingsError("Choose one of the listed scoring types.", field="scoring")
    stages = stages_repo.list_for_tournament(conn, tournament.id)
    sizes = {} if tournament.is_solo else _lineup_sizes(form, stages, tournament)
    placing = _placing(form) if is_admin else None
    with transaction(conn):
        tournaments_repo.set_rules(conn, tournament.id, form.freemod_rule, form.scoring)
        for stage_id, size in sizes.items():
            stages_repo.set_lineup_size(conn, stage_id, size)
        if is_admin and form.final_placing is not None:
            tournaments_repo.set_final_placing(conn, tournament.id, placing)
        if is_admin and form.our_team:
            _change_our_team(conn, tournament, form.our_team, now)


def _lineup_sizes(
    form: SettingsForm, stages: list[Stage], tournament: Tournament
) -> dict[int, int | None]:
    limit = min(tournament.team_size_max or MAX_LINEUP_SIZE, MAX_LINEUP_SIZE)
    sizes: dict[int, int | None] = {}
    for stage in stages:
        text = form.lineup_sizes.get(stage.id)
        if text is None:
            continue
        value = text.strip()
        if not value:
            sizes[stage.id] = None
            continue
        size = parse_uint(value)
        if size is None or not 1 <= size <= limit:
            raise SettingsError(
                f"Lineup size must be a whole number from 1 to {limit}.",
                field=f"lineup_size_{stage.id}",
            )
        sizes[stage.id] = size
    return sizes


def _placing(form: SettingsForm) -> str | None:
    try:
        return clean_placing(form.final_placing or "")
    except ArchiveError as error:
        raise SettingsError(error.message, field="final_placing") from error


def _change_our_team(
    conn: sqlite3.Connection, tournament: Tournament, team_text: str, now: datetime
) -> None:
    value = team_text.strip()
    team_id = parse_uint(value)
    team = teams_repo.get(conn, team_id) if team_id is not None else None
    if team is None or team.tournament_id != tournament.id:
        raise SettingsError("Choose one of the teams in this tournament.", field="our_team")
    if team.id == tournament.our_team_id:
        return
    first_assignment = tournament.our_team_id is None
    tournaments_repo.set_our_team(conn, tournament.id, team.id)
    add_roster_members(
        conn, tournament.id, team.id, tournament.is_solo, first_assignment, {}, to_utc_iso(now)
    )

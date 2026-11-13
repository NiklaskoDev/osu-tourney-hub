"""The captain's plan for a match: which maps our team protects, bans and picks, in order.

The form has one slot per protect, ban and pick the stage allows; an empty slot is skipped and the
rest keep their order. Raises `PlanError` for a refused form; `field` names the input to mark.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.db.connection import transaction
from app.domain.models import Match, PoolMap, Stage, Tournament
from app.domain.parsing import parse_uint
from app.repos import predictions as predictions_repo
from app.repos import stages as stages_repo
from app.repos.predictions import PLAN_KINDS, PlanItem
from app.services import matchup
from app.services.prediction import series_format

MAX_NOTE_LENGTH = 300
KIND_LABELS = {"protect": "Protect", "ban": "Ban", "pick": "Pick"}


class PlanError(Exception):
    def __init__(self, message: str, status: int = 422, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


@dataclass(frozen=True)
class PlanSlots:
    """How many protects, bans and picks one team has in a stage (picks: its share of the games
    before the tiebreaker, zero when the stage has no best-of)."""

    protects: int
    bans: int
    picks: int

    def count(self, kind: str) -> int:
        return {"protect": self.protects, "ban": self.bans, "pick": self.picks}[kind]


def plan_slots(stage: Stage) -> PlanSlots:
    stage_format = series_format(stage)
    picks = (stage_format.best_of - 1) // 2 if stage_format else 0
    return PlanSlots(stage.protects or 0, stage.bans or 0, picks)


def map_field(kind: str, slot: int) -> str:
    return f"{kind}_{slot}"


def note_field(kind: str, slot: int) -> str:
    return f"{kind}_note_{slot}"


def form_values(plan: list[PlanItem]) -> dict[str, str]:
    """The form's fields as a saved plan fills them in."""
    values = {}
    for item in plan:
        values[map_field(item.kind, item.position)] = str(item.pool_map_id)
        values[note_field(item.kind, item.position)] = item.note or ""
    return values


def parse_plan(
    values: Mapping[str, str], pool: list[PoolMap], slots: PlanSlots, set_by: int
) -> list[PlanItem]:
    """The plan a form describes, or a `PlanError`. Tiebreakers cannot be protected, banned or
    picked; a protected map cannot be banned; a banned map cannot be picked."""
    allowed = {m.id for m in pool if not m.is_tiebreaker}
    items: list[PlanItem] = []
    chosen: dict[str, set[int]] = {kind: set() for kind in PLAN_KINDS}
    for kind in PLAN_KINDS:
        position = 0
        for slot in range(1, slots.count(kind) + 1):
            raw = values.get(map_field(kind, slot), "").strip()
            if not raw:
                continue
            name = map_field(kind, slot)
            pool_map_id = parse_uint(raw)
            if pool_map_id not in allowed:
                raise PlanError("Choose a map from this stage's pool.", 422, name)
            if pool_map_id in chosen[kind]:
                raise PlanError(f"The same map is chosen twice for {kind}s.", 422, name)
            if kind == "ban" and pool_map_id in chosen["protect"]:
                raise PlanError("A protected map cannot be banned.", 422, name)
            if kind == "pick" and pool_map_id in chosen["ban"]:
                raise PlanError("A banned map cannot be picked.", 422, name)
            note = values.get(note_field(kind, slot), "").strip() or None
            if note is not None and len(note) > MAX_NOTE_LENGTH:
                raise PlanError(
                    f"A note can have at most {MAX_NOTE_LENGTH} characters.",
                    422,
                    note_field(kind, slot),
                )
            chosen[kind].add(pool_map_id)
            position += 1
            items.append(PlanItem(kind, pool_map_id, position, note, set_by))
    return items


def save_plan(
    conn: sqlite3.Connection,
    tournament: Tournament,
    match: Match,
    values: Mapping[str, str],
    set_by: int,
    now: datetime,
) -> bool:
    """Replace the match's plan with the form's. A change also saves a prediction snapshot (the
    snapshot keeps the plan beside the prediction). Returns whether the plan changed."""
    if not match.involves(tournament.our_team_id):
        raise PlanError("A plan can only be made for a match our team plays in.", 409)
    stage = stages_repo.get(conn, match.stage_id) if match.stage_id is not None else None
    if stage is None:
        raise PlanError("This match has no stage yet, so there is no pool to plan from.", 409)
    items = parse_plan(values, stages_repo.list_maps(conn, stage.id), plan_slots(stage), set_by)
    with transaction(conn):
        before = predictions_repo.list_plan(conn, match.id)
        if _same(before, items):
            return False
        predictions_repo.replace_plan(conn, match.id, items)
    view = matchup.build_matchup(conn, tournament, match)
    if match.status != "done":
        with transaction(conn):
            matchup.save_snapshot(conn, view, now)
    return True


def _same(before: list[PlanItem], after: list[PlanItem]) -> bool:
    """Whether two plans hold the same choices (who saved them does not matter)."""
    return [(i.kind, i.pool_map_id, i.position, i.note) for i in before] == [
        (i.kind, i.pool_map_id, i.position, i.note) for i in after
    ]

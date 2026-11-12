"""The lineup page: the suggestion for a stage, and the saved lineup captains edit."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clock import Clock
from app.domain.models import Role, Stage
from app.domain.parsing import parse_uint
from app.repos import lineups as lineups_repo
from app.repos import stages as stages_repo
from app.services import lineup_edit, pool_sheet
from app.services.lineup_edit import LineupError, LineupForm
from app.services.tournament_settings import SCORING_LABELS, effective_lineup_size
from app.web.deps import (
    TournamentAccess,
    captain_access,
    get_clock,
    get_db,
    member_access,
    require_writable,
)
from app.web.error_pages import error_page
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/lineup")


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    stage_ref: str | None,
    include_substitutes: bool,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    tournament = access.tournament
    stages = stages_repo.list_for_tournament(db, tournament.id)
    stage = pool_sheet.pick_stage(stages, stage_ref)
    suffix = "&subs=1" if include_substitutes else ""
    context: dict[str, Any] = {
        "access": access,
        "tournament": tournament,
        "nav_current": "lineup",
        "archived": tournament.status == "archived",
        "stage": stage,
        "stage_tabs": [
            (
                s.name,
                f"/t/{tournament.id}/lineup?stage={s.id}{suffix}",
                stage is not None and s.id == stage.id,
            )
            for s in stages
        ],
        "scoring_label": SCORING_LABELS[tournament.scoring],
        "include_substitutes": include_substitutes,
        "can_manage": access.role >= Role.CAPTAIN,
        **extra,
    }
    if stage is not None:
        context.update(_stage_context(db, access, stage, include_substitutes, context))
    return render(request, "pages/tournament/lineup.html", context, status_code=status_code)


def _stage_context(
    db: sqlite3.Connection,
    access: TournamentAccess,
    stage: Stage,
    include_substitutes: bool,
    context: dict[str, Any],
) -> dict[str, Any]:
    tournament = access.tournament
    data = pool_sheet.load_stage(db, tournament, stage)
    names = {p.osu_id: p.username or f"osu! user {p.osu_id}" for p in data.players}
    size = effective_lineup_size(stage, tournament)
    saved = lineups_repo.get_for_stage(db, stage.id)
    entries = lineups_repo.list_entries(db, saved.id) if saved else []
    slots: dict[int, list[Any]] = {pool_map.id: [] for pool_map in data.pool}
    for entry in entries:
        slots.setdefault(entry.pool_map_id, []).append(entry)
    values = context.get("form_values")
    if values is None:
        values = {}
        for pool_map_id, row in slots.items():
            for slot, entry in enumerate(row[:size]):
                values[lineup_edit.pick_field(pool_map_id, slot)] = str(entry.osu_id)
                values[lineup_edit.mods_field(pool_map_id, slot)] = entry.mods
    return {
        "pool": data.pool,
        "lineup_size": size,
        "suggestions": pool_sheet.suggest_for_stage(data, tournament, include_substitutes),
        "saved": saved,
        "slots": slots,
        "names": names,
        "player_options": [
            ("", "Nobody"),
            *((str(osu_id), name) for osu_id, name in names.items()),
        ],
        "form_values": values,
        "pick_field": lineup_edit.pick_field,
        "mods_field": lineup_edit.mods_field,
    }


def _stage_or_none(db: sqlite3.Connection, tournament_id: int, raw: str) -> Stage | None:
    stage_id = parse_uint(raw)
    stage = stages_repo.get(db, stage_id) if stage_id is not None else None
    return stage if stage and stage.tournament_id == tournament_id else None


@router.get("", response_class=HTMLResponse)
async def lineup_page(
    request: Request,
    stage: str | None = None,
    subs: str = "",
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _page(request, access, db, stage, bool(subs))


@router.post("/copy-suggestion")
async def copy_suggestion(
    request: Request,
    stage: str = Form(""),
    include_substitutes: str = Form(""),
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    chosen = _stage_or_none(db, access.tournament.id, stage)
    if chosen is None:
        return error_page(request, 404, "This stage is not in this tournament.")
    data = pool_sheet.load_stage(db, access.tournament, chosen)
    suggestions = pool_sheet.suggest_for_stage(data, access.tournament, bool(include_substitutes))
    lineup_edit.copy_suggestion(
        db, access.tournament, chosen, suggestions, access.user.osu_id, clock.now()
    )
    flash(request, "success", "Suggested lineup copied. It is saved as a draft you can edit.")
    return _back(access.tournament.id, chosen.id, bool(include_substitutes))


@router.post("")
async def save_lineup(
    request: Request,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    fields = await request.form()
    values = {key: value for key, value in fields.items() if isinstance(value, str)}
    chosen = _stage_or_none(db, access.tournament.id, values.get("stage", ""))
    if chosen is None:
        return error_page(request, 404, "This stage is not in this tournament.")
    pool = stages_repo.list_maps(db, chosen.id)
    form = LineupForm(values.get("status", ""), values)
    try:
        lineup_edit.save_lineup(
            db, access.tournament, chosen, pool, form, access.user.osu_id, clock.now()
        )
    except LineupError as refused:
        return _page(
            request,
            access,
            db,
            str(chosen.id),
            False,
            refused.status,
            form_values=values,
            form_error=refused.message,
            form_error_field=refused.field,
        )
    flash(request, "success", "Lineup saved.")
    return _back(access.tournament.id, chosen.id, False)


def _back(tid: int, stage_id: int, include_substitutes: bool) -> RedirectResponse:
    suffix = "&subs=1" if include_substitutes else ""
    return RedirectResponse(f"/t/{tid}/lineup?stage={stage_id}{suffix}", status_code=303)

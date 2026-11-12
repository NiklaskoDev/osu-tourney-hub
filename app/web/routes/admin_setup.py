"""Admin page for one tournament: its stages and pool, with forms for tournaments entered by
hand. A tournament imported from wybin shows the same page read-only."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clients.osu import OsuClient
from app.domain.models import Tournament, User
from app.domain.modes import mode_name
from app.domain.parsing import parse_uint
from app.repos import stages as stages_repo
from app.repos import tournaments as tournaments_repo
from app.services.tournament_setup import (
    SetupError,
    StageForm,
    add_pool_map,
    add_stage,
    remove_pool_map,
    remove_stage,
)
from app.web.deps import get_db, get_osu, require_admin
from app.web.errors import UserFacingError
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/admin/tournaments/{tid}")


def _tournament(db: sqlite3.Connection, tid: int) -> Tournament:
    tournament = tournaments_repo.get(db, tid)
    if tournament is None:
        raise UserFacingError(404, "This tournament does not exist.")
    return tournament


def _page(
    request: Request,
    tournament: Tournament,
    db: sqlite3.Connection,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    stages = [
        {"stage": stage, "maps": stages_repo.list_maps(db, stage.id)}
        for stage in stages_repo.list_for_tournament(db, tournament.id)
    ]
    editable = tournament.source == "manual" and tournament.status != "archived"
    context = {
        "nav_current": "admin",
        "tournament": tournament,
        "mode": mode_name(tournament.gamemode),
        "stages": stages,
        "editable": editable,
        "stage_options": [(str(item["stage"].id), item["stage"].name) for item in stages],
        **extra,
    }
    return render(request, "pages/admin/tournament.html", context, status_code=status_code)


def _refused(
    request: Request,
    tournament: Tournament,
    db: sqlite3.Connection,
    refused: SetupError,
    form: str,
    values: dict[str, Any],
) -> Response:
    """Re-show the page with the message next to the field and the typed values kept."""
    if refused.status == 404:
        raise UserFacingError(404, refused.message)
    return _page(
        request,
        tournament,
        db,
        refused.status,
        form_error=refused.message,
        form_error_field=refused.field,
        form_name=form,
        form_values=values,
    )


@router.get("", response_class=HTMLResponse)
async def tournament_admin_page(
    request: Request,
    tid: int,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _page(request, _tournament(db, tid), db)


@router.post("/stages")
async def add_stage_route(
    request: Request,
    tid: int,
    name: str = Form(""),
    start_at: str = Form(""),
    end_at: str = Form(""),
    best_of: str = Form(""),
    bans: str = Form(""),
    protects: str = Form(""),
    is_qualifier: str = Form(""),
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    tournament = _tournament(db, tid)
    form = StageForm(name, start_at, end_at, best_of, bans, protects, bool(is_qualifier))
    try:
        add_stage(db, tournament, form)
    except SetupError as refused:
        values = {**form.__dict__}
        return _refused(request, tournament, db, refused, "stage", values)
    flash(request, "success", f"Stage {name.strip()} added.")
    return RedirectResponse(f"/admin/tournaments/{tid}", status_code=303)


@router.post("/stages/{stage_id}/remove")
async def remove_stage_route(
    request: Request,
    tid: int,
    stage_id: int,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    tournament = _tournament(db, tid)
    try:
        remove_stage(db, tournament, stage_id)
    except SetupError as refused:
        return _refused(request, tournament, db, refused, "stage-remove", {})
    flash(request, "success", "Stage removed together with its maps.")
    return RedirectResponse(f"/admin/tournaments/{tid}", status_code=303)


@router.post("/maps")
async def add_map_route(
    request: Request,
    tid: int,
    stage_id: str = Form(""),
    slot: str = Form(""),
    beatmap: str = Form(""),
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    osu: OsuClient = Depends(get_osu),
) -> Response:
    tournament = _tournament(db, tid)
    values = {"stage_id": stage_id, "slot": slot, "beatmap": beatmap}
    stage = parse_uint(stage_id)
    if stage is None:
        refused = SetupError("Choose the stage this map belongs to.", field="stage_id")
        return _refused(request, tournament, db, refused, "map", values)
    try:
        await add_pool_map(db, osu, tournament, stage, slot, beatmap)
    except SetupError as refused:
        return _refused(request, tournament, db, refused, "map", values)
    flash(request, "success", f"Map {slot.strip().upper()} added.")
    return RedirectResponse(f"/admin/tournaments/{tid}", status_code=303)


@router.post("/maps/{map_id}/remove")
async def remove_map_route(
    request: Request,
    tid: int,
    map_id: int,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    tournament = _tournament(db, tid)
    try:
        remove_pool_map(db, tournament, map_id)
    except SetupError as refused:
        return _refused(request, tournament, db, refused, "map-remove", {})
    flash(request, "success", "Map removed from the pool.")
    return RedirectResponse(f"/admin/tournaments/{tid}", status_code=303)

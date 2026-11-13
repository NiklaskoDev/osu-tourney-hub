"""The mappool sheet: best scores per player and map, averages, notes. Players add their own
scores and notes; captains add scores for any player."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clock import Clock
from app.domain.models import Role
from app.domain.modes import mode_name, selectable_mods
from app.domain.parsing import parse_uint
from app.repos import stages as stages_repo
from app.services import pool_sheet
from app.services import score_entry as entry
from app.services.score_entry import EntryError, ScoreForm
from app.services.tournament_settings import SCORING_LABELS
from app.web.deps import (
    TournamentAccess,
    get_clock,
    get_db,
    member_access,
    player_access,
    require_writable,
)
from app.web.error_pages import error_page
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}")

OLD_SCORES_HINT = "Scores played more than 6 months ago are marked old and still count."


def _actor(access: TournamentAccess) -> entry.Actor:
    return entry.Actor(access.user.osu_id, access.role >= Role.CAPTAIN)


def _redirect(tid: int, stage_id: int | None) -> RedirectResponse:
    suffix = f"?stage={stage_id}" if stage_id is not None else ""
    return RedirectResponse(f"/t/{tid}/pool{suffix}", status_code=303)


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    clock: Clock,
    stage_ref: str | int | None,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    tournament = access.tournament
    stages = stages_repo.list_for_tournament(db, tournament.id)
    stage = pool_sheet.pick_stage(stages, None if stage_ref is None else str(stage_ref))
    context: dict[str, Any] = {
        "access": access,
        "tournament": tournament,
        "nav_current": "pool",
        "mode": mode_name(tournament.gamemode),
        "archived": tournament.status == "archived",
        "stages": stages,
        "stage": stage,
        "stage_tabs": [
            (
                s.name,
                f"/t/{tournament.id}/pool?stage={s.id}",
                stage is not None and s.id == stage.id,
            )
            for s in stages
        ],
        "scoring_label": SCORING_LABELS[tournament.scoring],
        "old_scores_hint": OLD_SCORES_HINT,
        "can_write": access.role >= Role.PLAYER,
        "can_manage": access.role >= Role.CAPTAIN,
        "can_refresh": access.member is not None and access.member.role != "guest",
        "mod_choices": selectable_mods(tournament.gamemode),
        **extra,
    }
    if stage is not None:
        data = pool_sheet.load_stage(db, tournament, stage)
        viewer = pool_sheet.Viewer(
            access.user.osu_id,
            access.role >= Role.CAPTAIN,
            pool_sheet.visible_tournament_names(db, access.user.osu_id, access.is_admin),
        )
        context["view"] = pool_sheet.build_pool_view(db, tournament, data, viewer, clock.now())
        context["player_options"] = [
            (str(p.osu_id), p.username or f"osu! user {p.osu_id}") for p in data.players
        ]
    return render(request, "pages/tournament/pool.html", context, status_code=status_code)


def _stage_of_map(db: sqlite3.Connection, raw_map_id: str) -> int | None:
    map_id = parse_uint(raw_map_id)
    pool_map = stages_repo.get_map(db, map_id) if map_id is not None else None
    return pool_map.stage_id if pool_map else None


def _refused(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    clock: Clock,
    refused: EntryError,
    stage_id: int | None,
    **extra: Any,
) -> Response:
    if refused.status in (403, 404):
        return error_page(request, refused.status, refused.message)
    return _page(request, access, db, clock, stage_id, refused.status, **extra)


@router.get("/pool", response_class=HTMLResponse)
async def pool_page(
    request: Request,
    stage: str | None = None,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    return _page(request, access, db, clock, stage)


@router.post("/scores")
async def add_score(
    request: Request,
    access: TournamentAccess = Depends(player_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    fields = await request.form()
    values = {key: value for key, value in fields.items() if isinstance(value, str)}
    mods = [value for value in fields.getlist("mods") if isinstance(value, str)]
    form = ScoreForm(
        pool_map_id=values.get("pool_map_id", ""),
        osu_id=values.get("osu_id", ""),
        score=values.get("score", ""),
        accuracy=values.get("accuracy", ""),
        misses=values.get("misses", ""),
        max_combo=values.get("max_combo", ""),
        mods=mods,
        link=values.get("link", ""),
    )
    stage_id = _stage_of_map(db, form.pool_map_id)
    try:
        entry.record_score(db, access.tournament, _actor(access), form, clock.now())
    except EntryError as refused:
        return _refused(
            request,
            access,
            db,
            clock,
            refused,
            stage_id,
            entry_map_id=form.pool_map_id,
            entry_values={**values, "mods": mods},
            entry_error=refused.message,
            entry_error_field=refused.field,
            entry_kind="score",
        )
    flash(request, "success", "Score saved.")
    return _redirect(access.tournament.id, stage_id)


@router.post("/scores/{score_id}/delete")
async def delete_score(
    request: Request,
    score_id: int,
    stage: str = Form(""),
    access: TournamentAccess = Depends(player_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        entry.delete_score(db, access.tournament, _actor(access), score_id)
    except EntryError as refused:
        return error_page(request, refused.status, refused.message)
    flash(request, "success", "Score removed.")
    return _redirect(access.tournament.id, parse_uint(stage))


@router.post("/notes")
async def add_note(
    request: Request,
    pool_map_id: str = Form(""),
    visibility: str = Form(""),
    body: str = Form(""),
    access: TournamentAccess = Depends(player_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    stage_id = _stage_of_map(db, pool_map_id)
    try:
        entry.add_note(
            db, access.tournament, _actor(access), pool_map_id, visibility, body, clock.now()
        )
    except EntryError as refused:
        return _refused(
            request,
            access,
            db,
            clock,
            refused,
            stage_id,
            entry_map_id=pool_map_id,
            entry_values={"body": body, "visibility": visibility},
            entry_error=refused.message,
            entry_error_field=refused.field,
            entry_kind="note",
        )
    flash(request, "success", "Note saved.")
    return _redirect(access.tournament.id, stage_id)


@router.post("/notes/{note_id}/delete")
async def delete_note(
    request: Request,
    note_id: int,
    stage: str = Form(""),
    access: TournamentAccess = Depends(player_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        entry.delete_note(db, access.tournament, _actor(access), note_id)
    except EntryError as refused:
        return error_page(request, refused.status, refused.message)
    flash(request, "success", "Note removed.")
    return _redirect(access.tournament.id, parse_uint(stage))

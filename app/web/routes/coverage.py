"""Practice coverage: who has a score on which map, and the targets captains set."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clock import Clock
from app.domain.models import Role
from app.repos import practice as practice_repo
from app.repos import stages as stages_repo
from app.services import coverage, pool_sheet
from app.services.coverage import TargetForm
from app.services.score_entry import EntryError
from app.services.tournament_settings import SCORING_LABELS
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

router = APIRouter(prefix="/t/{tid}")


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    stage_ref: str | None,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    tournament = access.tournament
    stages = stages_repo.list_for_tournament(db, tournament.id)
    stage = pool_sheet.pick_stage(stages, stage_ref)
    context: dict[str, Any] = {
        "access": access,
        "tournament": tournament,
        "nav_current": "coverage",
        "archived": tournament.status == "archived",
        "stage": stage,
        "stage_tabs": [
            (
                s.name,
                f"/t/{tournament.id}/coverage?stage={s.id}",
                stage is not None and s.id == stage.id,
            )
            for s in stages
        ],
        "scoring_label": SCORING_LABELS[tournament.scoring],
        "can_manage": access.role >= Role.CAPTAIN,
        **extra,
    }
    if stage is not None:
        data = pool_sheet.load_stage(db, tournament, stage)
        targets = practice_repo.list_for_maps(db, [pool_map.id for pool_map in data.pool])
        view = coverage.build_coverage(data, tournament, targets)
        context.update(
            view=view,
            pool=data.pool,
            targets=targets,
            names={p.osu_id: p.username or f"osu! user {p.osu_id}" for p in data.players},
            map_options=[
                (str(m.id), f"{m.slot} {m.title or 'Map not announced'}") for m in data.pool
            ],
            player_options=[
                ("", "Whole team"),
                *((str(p.osu_id), p.username or f"osu! user {p.osu_id}") for p in data.players),
            ],
        )
    return render(request, "pages/tournament/coverage.html", context, status_code=status_code)


@router.get("/coverage", response_class=HTMLResponse)
async def coverage_page(
    request: Request,
    stage: str | None = None,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _page(request, access, db, stage)


@router.post("/practice-targets")
async def set_target(
    request: Request,
    stage: str = Form(""),
    pool_map_id: str = Form(""),
    osu_id: str = Form(""),
    target_score: str = Form(""),
    target_acc: str = Form(""),
    note: str = Form(""),
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Set or replace a target; with no score and no accuracy the target is removed."""
    form = TargetForm(pool_map_id, osu_id, target_score, target_acc, note)
    try:
        outcome = coverage.save_target(db, access.tournament, form, access.user.osu_id, clock.now())
    except EntryError as refused:
        if refused.status == 404:
            return error_page(request, 404, refused.message)
        return _page(
            request,
            access,
            db,
            stage,
            refused.status,
            form_values={
                "pool_map_id": pool_map_id,
                "osu_id": osu_id,
                "target_score": target_score,
                "target_acc": target_acc,
                "note": note,
            },
            form_error=refused.message,
            form_error_field=refused.field,
        )
    flash(request, "success", "Target saved." if outcome == "saved" else "Target removed.")
    suffix = f"?stage={stage}" if stage.isdecimal() else ""
    return RedirectResponse(f"/t/{access.tournament.id}/coverage{suffix}", status_code=303)

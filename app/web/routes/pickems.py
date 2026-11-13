"""Pickems: a predicted winner, chance and score for every match of a stage."""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.repos import stages as stages_repo
from app.services import matchup, pool_sheet
from app.web.deps import TournamentAccess, get_db, member_access
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/pickems")


@router.get("", response_class=HTMLResponse)
async def pickems_page(
    request: Request,
    stage: str | None = None,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    tournament = access.tournament
    stages = stages_repo.list_for_tournament(db, tournament.id)
    chosen = pool_sheet.pick_stage(stages, stage)
    context = {
        "access": access,
        "tournament": tournament,
        "nav_current": "pickems",
        "stage": chosen,
        "stage_tabs": [
            (
                s.name,
                f"/t/{tournament.id}/pickems?stage={s.id}",
                chosen is not None and s.id == chosen.id,
            )
            for s in stages
        ],
        "pickems": matchup.build_pickems(db, tournament, chosen) if chosen else None,
    }
    return render(request, "pages/tournament/pickems.html", context)

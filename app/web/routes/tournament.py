"""Tournament overview. Later phases add the match countdown, current stage and lobbies here."""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.clock import Clock
from app.domain.modes import mode_name
from app.repos import members as members_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.services import matchup
from app.services.access import role_label
from app.web.deps import TournamentAccess, get_clock, get_db, member_access
from app.web.templating import render

router = APIRouter()


@router.get("/t/{tid}", response_class=HTMLResponse)
async def overview(
    request: Request,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    context = {
        "access": access,
        "tournament": access.tournament,
        "nav_current": "overview",
        "role_name": role_label(access.member),
        "member_count": len(members_repo.list_for_tournament(db, access.tournament.id)),
        "mode": mode_name(access.tournament.gamemode),
        "our_team": teams_repo.get(db, access.tournament.our_team_id)
        if access.tournament.our_team_id
        else None,
        "stages": stages_repo.list_for_tournament(db, access.tournament.id),
        "seed": matchup.seed_line(db, access.tournament, clock.now()),
    }
    return render(request, "pages/tournament/overview.html", context)

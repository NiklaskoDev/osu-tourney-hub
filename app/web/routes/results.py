"""Results: the matches that were played and what our players scored, and one match's stats page.
Both are read-only for everyone on the tournament."""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.clock import Clock
from app.services import match_stats
from app.web.deps import TournamentAccess, get_clock, get_db, member_access
from app.web.routes.history import may_correct
from app.web.routes.matches import find_match
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}")


@router.get("/results", response_class=HTMLResponse)
async def results_page(
    request: Request,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    tournament = access.tournament
    context = {
        "access": access,
        "tournament": tournament,
        "nav_current": "results",
        "results": match_stats.tournament_results(db, tournament, clock.now()),
    }
    return render(request, "pages/tournament/results.html", context)


@router.get("/matches/{mid}/stats", response_class=HTMLResponse)
async def match_stats_page(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    tournament = access.tournament
    match = find_match(db, access, mid)
    context = {
        "access": access,
        "tournament": tournament,
        "nav_current": "results",
        "stats": match_stats.build_match_stats(db, match),
        "can_refresh": access.can_manage
        and tournament.status != "archived"
        and may_correct(access, match),
    }
    return render(request, "pages/tournament/match_stats.html", context)

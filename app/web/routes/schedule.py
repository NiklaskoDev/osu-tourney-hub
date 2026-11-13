"""The schedule: our upcoming matches with a countdown, past results, and the slot finder."""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.clock import Clock
from app.domain.models import Role
from app.domain.parsing import parse_uint
from app.repos import matches as matches_repo
from app.services import schedule
from app.web.deps import TournamentAccess, get_clock, get_db, member_access
from app.web.errors import UserFacingError
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/schedule")

NO_SUCH_MATCH = "This match is not in this tournament."
NO_SLOTS_FOR_MATCH = (
    "Slots can only be suggested for a match that has not been played and has an opponent."
)


@router.get("", response_class=HTMLResponse)
async def schedule_page(
    request: Request,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    tournament = access.tournament
    context = {
        "access": access,
        "tournament": tournament,
        "nav_current": "schedule",
        "archived": tournament.status == "archived",
        "can_manage": access.role >= Role.CAPTAIN,
        "schedule": schedule.load_schedule(db, tournament, clock.now()),
    }
    return render(request, "pages/tournament/schedule.html", context)


@router.get("/slots", response_class=HTMLResponse)
async def find_slots(
    request: Request,
    match: str = "",
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    """The best start times for one match. htmx asks for the fragment; a plain request gets the
    whole page."""
    tournament = access.tournament
    match_id = parse_uint(match)
    found = matches_repo.get(db, match_id) if match_id is not None else None
    if found is None or found.tournament_id != tournament.id:
        raise UserFacingError(404, NO_SUCH_MATCH)
    search = schedule.find_slots(db, tournament, found, clock.now())
    if not search.view.can_find_slot:
        raise UserFacingError(409, NO_SLOTS_FOR_MATCH)
    context = {
        "access": access,
        "tournament": tournament,
        "nav_current": "schedule",
        "search": search,
    }
    template = (
        "partials/slots.html"
        if request.headers.get("HX-Request") == "true"
        else "pages/tournament/slots.html"
    )
    return render(request, template, context)

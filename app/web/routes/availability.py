"""The availability grid: everyone's answers for a week, and each member's own row to edit.

Slots are stored in UTC. The grid is shown in UTC or in the viewer's own timezone; the viewer picks
a range of slots and the browser posts it as one request.
"""

import sqlite3
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.clock import Clock, to_utc_iso
from app.domain.models import Role
from app.domain.parsing import parse_uint
from app.domain.slots import MATCH_DURATION
from app.repos import availability as availability_repo
from app.repos import members as members_repo
from app.services import availability, schedule
from app.web.deps import (
    TournamentAccess,
    get_clock,
    get_db,
    member_access,
    player_access,
    require_writable,
)
from app.web.errors import UserFacingError
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/availability")

NOT_ON_ROSTER = "Only people on the roster can set availability."
NOT_A_PLAYER = "This person is not a player in this tournament."
ONLY_CAPTAINS = "Only captains can change someone else's availability."
STATE_WORDS = {availability.YES: "Yes", availability.MAYBE: "Maybe", availability.NO: "No"}


def _url(tid: int, **params: str) -> str:
    query = urlencode({key: value for key, value in params.items() if value})
    return f"/t/{tid}/availability" + (f"?{query}" if query else "")


def _editable_column(
    access: TournamentAccess, columns: list[availability.Column], wanted: str
) -> int | None:
    """Whose column has buttons: a captain's pick, else the viewer's own."""
    ids = {column.osu_id for column in columns}
    chosen = parse_uint(wanted)
    if access.role >= Role.CAPTAIN and chosen in ids:
        return chosen
    own = access.user.osu_id
    return own if own in ids and access.role >= Role.PLAYER else None


@router.get("", response_class=HTMLResponse)
async def availability_page(
    request: Request,
    tz: str = "utc",
    zone: str = "",
    week: str = "",
    member: str = "",
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    tournament = access.tournament
    shown = availability.view_zone(tz, access.user.timezone, zone)
    now = clock.now()
    monday = availability.parse_week(week, now.astimezone(shown.zone).date())
    members = members_repo.list_for_tournament(db, tournament.id)
    columns = schedule.roster_columns(members)
    start, end = availability.week_bounds(monday, shown.zone)
    found = availability_repo.list_range(
        db, tournament.id, [column.osu_id for column in columns], to_utc_iso(start), to_utc_iso(end)
    )
    bands = schedule.bands_between(db, tournament, start - MATCH_DURATION, end)
    grid = availability.build_grid(monday, shown.zone, columns, found, bands)
    editable = _editable_column(access, columns, member)

    def link(week_value: str) -> str:
        """The same view for another week."""
        return _url(
            tournament.id,
            tz="local" if shown.is_local else "",
            zone=shown.name if shown.is_local and not shown.needs_detection else "",
            week=week_value,
            member=member,
        )

    context: dict[str, Any] = {
        "access": access,
        "tournament": tournament,
        "nav_current": "availability",
        "archived": tournament.status == "archived",
        "grid": grid,
        "columns": columns,
        "editable_id": editable,
        "own_id": access.user.osu_id,
        "lineup_size": schedule.current_lineup_size(db, tournament, now),
        "view": shown,
        "tz_mode": "local" if shown.is_local else "utc",
        "monday": monday.isoformat(),
        "state_words": STATE_WORDS,
        "can_pick_member": access.role >= Role.CAPTAIN,
        "member_options": [(str(column.osu_id), column.name) for column in columns],
        "member_value": str(editable) if editable is not None else "",
        "tz_tabs": _tz_tabs(tournament.id, shown, monday.isoformat(), access.user.timezone, zone),
        "previous_url": link(availability.previous_week(monday).isoformat()),
        "next_url": link(availability.next_week(monday).isoformat()),
        "today_url": link(""),
        "week_label": _week_label(monday),
    }
    return render(request, "pages/tournament/availability.html", context)


def _tz_tabs(
    tid: int, shown: availability.ViewZone, week: str, saved: str | None, browser_zone: str
) -> list[tuple[str, str, bool]]:
    local_name = shown.name if shown.is_local and not shown.needs_detection else (saved or "")
    local_label = f"Local ({local_name})" if local_name else "Local"
    return [
        ("UTC", _url(tid, week=week), not shown.is_local),
        (local_label, _url(tid, tz="local", zone=browser_zone, week=week), shown.is_local),
    ]


def _week_label(monday) -> str:
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%d %b')} - {sunday.strftime('%d %b %Y')}"


@router.post("", response_class=HTMLResponse)
async def save_availability(
    request: Request,
    start: str = Form(""),
    end: str = Form(""),
    state: str = Form(""),
    osu_id: str = Form(""),
    tz: str = Form("utc"),
    zone: str = Form(""),
    access: TournamentAccess = Depends(player_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Set a range of slots for one person. Players change their own row; captains anyone's.
    The answer is the new state of the changed rows (swapped in by htmx) and a status line."""
    try:
        chosen_state = availability.parse_state(state)
        slots = availability.parse_range(start, end)
    except availability.RangeError as error:
        raise UserFacingError(422, str(error)) from error
    tournament = access.tournament
    members = members_repo.list_for_tournament(db, tournament.id)
    columns = schedule.roster_columns(members)
    target = _target(access, columns, osu_id)
    slot_texts = [to_utc_iso(slot) for slot in slots]
    availability_repo.set_slots(db, tournament.id, target, slot_texts, chosen_state)
    shown = availability.view_zone(tz, access.user.timezone, zone)
    found = availability_repo.list_range(
        db,
        tournament.id,
        [column.osu_id for column in columns],
        slot_texts[0],
        to_utc_iso(slots[-1] + availability.SLOT),
    )
    bands = schedule.bands_between(db, tournament, slots[0] - MATCH_DURATION, slots[-1])
    rows = availability.build_rows(slots, shown.zone, columns, found, bands)
    count = len(slots)
    noun = "slot" if count == 1 else "slots"
    context = {
        "rows": rows,
        "columns": columns,
        "editable_id": target,
        "lineup_size": schedule.current_lineup_size(db, tournament, clock.now()),
        "archived": False,
        "state_words": STATE_WORDS,
        "message": f"Saved {count} {noun} as {STATE_WORDS[chosen_state]}.",
    }
    return render(request, "partials/availability_rows.html", context)


def _target(access: TournamentAccess, columns: list[availability.Column], wanted: str) -> int:
    """Whose row is changed, after checking the viewer may."""
    ids = {column.osu_id for column in columns}
    own = access.user.osu_id
    chosen = parse_uint(wanted) if wanted.strip() else own
    if chosen is None:
        raise UserFacingError(422, NOT_A_PLAYER)
    if chosen != own and access.role < Role.CAPTAIN:
        raise UserFacingError(403, ONLY_CAPTAINS)
    if chosen not in ids:
        if chosen == own:
            raise UserFacingError(409, NOT_ON_ROSTER)
        raise UserFacingError(422, NOT_A_PLAYER)
    return chosen

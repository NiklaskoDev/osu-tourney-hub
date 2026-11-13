"""Live match page: the running score and the next pick while a match is played, pushed to the
browser as server-sent events. A captain starts or stops following the match's lobby; it also
starts by itself shortly before a match of our team."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from app.domain.models import Role
from app.services.live import LiveFollower, LiveHub
from app.services.live_state import LiveState
from app.web.deps import TournamentAccess, captain_access, get_db, member_access, require_writable
from app.web.errors import UserFacingError
from app.web.routes.matches import find_match
from app.web.session import flash
from app.web.templating import render, templates

router = APIRouter(prefix="/t/{tid}/live/{mid}")

HEARTBEAT_SECONDS = 15  # keeps proxies from closing a quiet connection
STATE_EVENT = "state"
END_EVENT = "end"  # the page closes its connection on this event instead of reconnecting
NO_LOBBY = "This match has no multiplayer lobby yet. Live updates start once the lobby is linked."
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse_message(event: str, data: str) -> str:
    """One server-sent event; every line of the data needs its own `data:` field."""
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n"


def render_state(state: LiveState) -> str:
    return templates.get_template("partials/live_state.html").render(state=state).strip()


async def stream_frames(
    hub: LiveHub,
    match_id: int,
    first: LiveState,
    heartbeat: float,
    render_fn: Callable[[LiveState], str] = render_state,
) -> AsyncIterator[str]:
    """The current state, then every state the hub publishes, with a comment line whenever
    nothing happened for `heartbeat` seconds. Ends after a final state."""
    queue = hub.subscribe(match_id)
    try:
        state = first
        while True:
            yield sse_message(STATE_EVENT, render_fn(state))
            if state.is_final:
                yield sse_message(END_EVENT, "closed")
                return
            while True:
                try:
                    state = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    break
                except TimeoutError:
                    yield ": heartbeat\n\n"
    finally:
        hub.unsubscribe(match_id, queue)


@router.get("", response_class=HTMLResponse)
async def live_page(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    match = find_match(db, access, mid)
    follower: LiveFollower = request.app.state.live
    archived = access.tournament.status == "archived"
    context = {
        "access": access,
        "tournament": access.tournament,
        "nav_current": "schedule",
        "match": match,
        "state": follower.current_state(db, access.tournament, match),
        "following": follower.is_following(match.id),
        "can_control": access.role >= Role.CAPTAIN and not archived,
        "archived": archived,
        "has_lobby": match.mp_match_id is not None,
    }
    return render(request, "pages/tournament/live.html", context)


@router.get("/stream")
async def live_stream(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    match = find_match(db, access, mid)
    follower: LiveFollower = request.app.state.live
    first = follower.current_state(db, access.tournament, match)
    # The database connection is closed when this handler returns, so the stream only uses what
    # was read here and the hub.
    frames = stream_frames(follower.hub, match.id, first, HEARTBEAT_SECONDS)
    return StreamingResponse(frames, media_type="text/event-stream", headers=STREAM_HEADERS)


@router.post("/start")
async def start_following(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    match = find_match(db, access, mid)
    if match.mp_match_id is None:
        raise UserFacingError(409, NO_LOBBY)
    started = request.app.state.live.start(match.id)
    flash(
        request,
        "success" if started else "info",
        "Live updates started." if started else "Live updates are already running.",
    )
    return RedirectResponse(f"/t/{access.tournament.id}/live/{match.id}", status_code=303)


@router.post("/stop")
async def stop_following(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    match = find_match(db, access, mid)
    stopped = await request.app.state.live.stop(match.id)
    flash(
        request,
        "success" if stopped else "info",
        "Live updates stopped." if stopped else "Live updates were not running.",
    )
    return RedirectResponse(f"/t/{access.tournament.id}/live/{match.id}", status_code=303)

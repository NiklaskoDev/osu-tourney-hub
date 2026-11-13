"""Asking for score pulls from osu!: "Refresh my scores" and, for captains, a whole team."""

import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.clock import Clock
from app.domain.parsing import parse_uint
from app.repos import teams as teams_repo
from app.services import score_capture
from app.web.deps import (
    TournamentAccess,
    captain_access,
    get_clock,
    get_db,
    player_access,
    require_writable,
)
from app.web.errors import UserFacingError
from app.web.security import SlidingWindowLimiter
from app.web.session import flash
from app.workers.jobs import CAPTURE_DRAIN
from app.workers.scheduler import Scheduler

router = APIRouter(prefix="/t/{tid}")

NOT_ON_ROSTER = "Only players on the roster have scores to refresh."
PULL_STARTED = "Pulling scores from osu!. They appear on the pool sheet within a few minutes."


def _limit(request: Request, limiter_name: str, key: str, what: str) -> None:
    limiter: SlidingWindowLimiter = getattr(request.app.state, limiter_name)
    if not limiter.allow(key):
        wait = limiter.retry_after_seconds(key)
        minutes = max(1, round(wait / 60))
        raise UserFacingError(
            429,
            f"{what} was started a moment ago. Try again in about {minutes} minutes.",
            headers={"Retry-After": str(wait)},
        )


def _start_pull(request: Request, background: BackgroundTasks) -> None:
    """Run the queue right away instead of waiting for the next timer tick."""
    scheduler: Scheduler = request.app.state.scheduler
    background.add_task(scheduler.run_once, scheduler.job(CAPTURE_DRAIN))


@router.post("/capture/refresh")
async def refresh_my_scores(
    request: Request,
    background: BackgroundTasks,
    stage: str = Form(""),
    access: TournamentAccess = Depends(player_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Queue the person's own pulls for the current and next stage, whatever was pulled lately."""
    if access.member is None or access.member.role == "guest":
        raise UserFacingError(409, NOT_ON_ROSTER)
    _limit(request, "refresh_limiter", f"{access.user.osu_id}", "A refresh")
    queued = score_capture.enqueue_players(
        db,
        access.tournament,
        [access.user.osu_id],
        score_capture.PRIORITY_REQUESTED,
        clock.now(),
        skip_within=None,
    )
    if queued:
        flash(request, "success", PULL_STARTED)
        _start_pull(request, background)
    else:
        flash(request, "info", "There are no maps with an osu! beatmap to pull scores for yet.")
    stage_id = parse_uint(stage)
    suffix = f"?stage={stage_id}" if stage_id is not None else ""
    return RedirectResponse(
        f"/t/{access.tournament.id}/pool{suffix}", status_code=303, background=background
    )


@router.post("/opponents/{team_id}/capture")
async def capture_team_scores(
    request: Request,
    team_id: int,
    background: BackgroundTasks,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Queue every player of one team (an opponent, usually) for the current and next stage."""
    team = teams_repo.get(db, team_id)
    if team is None or team.tournament_id != access.tournament.id:
        raise UserFacingError(404, "This team is not in this tournament.")
    _limit(request, "opponent_capture_limiter", f"{access.tournament.id}:{team.id}", "A pull")
    queued = score_capture.enqueue_for_team(
        db, access.tournament, team.id, score_capture.PRIORITY_REQUESTED, clock.now()
    )
    if queued:
        flash(request, "success", f"{PULL_STARTED} Team: {team.name}.")
        _start_pull(request, background)
    else:
        flash(request, "info", f"{team.name} has no players or there are no maps to pull for yet.")
    return RedirectResponse(f"/t/{access.tournament.id}", status_code=303, background=background)

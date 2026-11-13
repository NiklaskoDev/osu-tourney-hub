"""One match: both teams compared map by map, the predicted protects, bans and picks, the series
chance, and the captain's plan."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clients.ai import AiError, AiProvider
from app.clock import Clock
from app.config import Settings
from app.domain.models import Match, Role
from app.domain.parsing import parse_uint
from app.repos import matches as matches_repo
from app.services import ai_prediction, match_plan, matchup
from app.services.ai_prediction import CannotPredict
from app.services.match_plan import PlanError
from app.web.deps import (
    TournamentAccess,
    captain_access,
    get_ai,
    get_clock,
    get_db,
    get_settings,
    member_access,
    require_writable,
)
from app.web.errors import UserFacingError
from app.web.security import SlidingWindowLimiter
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/matches/{mid}")

NO_SUCH_MATCH = "This match is not in this tournament."
AI_FAILED = "The AI service could not give a usable estimate. Nothing was saved. Try again later."


def find_match(db: sqlite3.Connection, access: TournamentAccess, mid: str) -> Match:
    match_id = parse_uint(mid)
    found = matches_repo.get(db, match_id) if match_id is not None else None
    if found is None or found.tournament_id != access.tournament.id:
        raise UserFacingError(404, NO_SUCH_MATCH)
    return found


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    match: Match,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    tournament = access.tournament
    clock: Clock = request.app.state.clock
    archived = tournament.status == "archived"
    view = matchup.build_matchup(db, tournament, match)
    if not archived:
        matchup.save_daily_snapshot(db, view, clock.now())
    slots = match_plan.plan_slots(view.stage) if view.stage else None
    can_plan = access.role >= Role.CAPTAIN and view.we_play and view.stage is not None
    options = [("", "No map")] + [
        (str(m.id), f"{m.slot} {m.title or 'Map not announced'}")
        for m in view.pool
        if not m.is_tiebreaker
    ]
    context: dict[str, Any] = {
        "access": access,
        "tournament": tournament,
        "nav_current": "schedule",
        "archived": archived,
        "view": view,
        "maps": {m.id: m for m in view.pool},
        "slots": slots,
        "can_plan": can_plan,
        "plan_kinds": [(kind, label) for kind, label in match_plan.KIND_LABELS.items()],
        "map_options": options,
        "map_field": match_plan.map_field,
        "note_field": match_plan.note_field,
        "form_values": match_plan.form_values(view.plan),
        "seed": matchup.seed_line(db, tournament, clock.now()),
        "ai_estimate": ai_prediction.latest_estimate(db, match, view.left, view.right),
        "can_ask_ai": (
            request.app.state.ai.enabled
            and access.role >= Role.CAPTAIN
            and not archived
            and view.prediction is not None
        ),
        **extra,
    }
    return render(request, "pages/tournament/match.html", context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def match_page(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _page(request, access, db, find_match(db, access, mid))


@router.post("/plan")
async def save_plan(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    match = find_match(db, access, mid)
    fields = await request.form()
    values = {key: value for key, value in fields.items() if isinstance(value, str)}
    try:
        changed = match_plan.save_plan(
            db, access.tournament, match, values, access.user.osu_id, clock.now()
        )
    except PlanError as refused:
        if refused.status == 409:
            raise UserFacingError(409, refused.message) from refused
        return _page(
            request,
            access,
            db,
            match,
            refused.status,
            form_values=values,
            form_error=refused.message,
            form_error_field=refused.field,
        )
    flash(request, "success", "Plan saved." if changed else "The plan is unchanged.")
    return RedirectResponse(f"/t/{access.tournament.id}/matches/{match.id}", status_code=303)


@router.post("/predict-ai")
async def predict_ai(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
    ai: AiProvider = Depends(get_ai),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Ask the configured AI for an estimate of this match and keep it beside the statistical one.
    A missing provider answers like an unknown page. The per-person limit counts every ask that
    reaches the provider, failed or not, but not one refused for having nothing to estimate."""
    if not ai.enabled:
        raise UserFacingError(404, "This page does not exist, or you do not have access to it.")
    match = find_match(db, access, mid)
    try:
        prepared = ai_prediction.prepare(db, access.tournament, match)
    except CannotPredict as refused:
        raise UserFacingError(409, str(refused)) from refused  # costs no ask
    limiter: SlidingWindowLimiter = request.app.state.ai_limiter
    key = str(access.user.osu_id)
    if not limiter.allow(key):
        wait = limiter.retry_after_seconds(key)
        minutes = max(1, round(wait / 60))
        raise UserFacingError(
            429,
            f"You asked for several AI estimates lately. Try again in about {minutes} minutes.",
            headers={"Retry-After": str(wait)},
        )
    try:
        await ai_prediction.estimate(
            db, ai, prepared, f"{settings.ai_provider}:{settings.ai_model}", clock.now()
        )
    except AiError:
        return _page(request, access, db, match, 502, ai_error=AI_FAILED)
    flash(request, "success", "AI estimate saved.")
    return RedirectResponse(f"/t/{access.tournament.id}/matches/{match.id}", status_code=303)

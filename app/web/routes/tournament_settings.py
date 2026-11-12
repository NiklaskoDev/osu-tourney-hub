"""Tournament settings: lineup size per stage, freemod rule, scoring, our team, final placing."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clock import Clock
from app.domain.modes import mode_name
from app.domain.parsing import parse_uint
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos import tournaments as tournaments_repo
from app.services.tournament_settings import (
    FREEMOD_RULE_LABELS,
    SCORING_LABELS,
    SettingsError,
    SettingsForm,
    effective_lineup_size,
    update_settings,
)
from app.web.deps import TournamentAccess, captain_access, get_clock, get_db, require_writable
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/settings")

LINEUP_FIELD_PREFIX = "lineup_size_"


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    # Re-read: the tournament row on `access` predates the change a failed save leaves behind.
    tournament = tournaments_repo.get(db, access.tournament.id) or access.tournament
    stages = stages_repo.list_for_tournament(db, tournament.id)
    teams = teams_repo.list_for_tournament(db, tournament.id)
    context = {
        "access": access,
        "tournament": tournament,
        "nav_current": "settings",
        "mode": mode_name(tournament.gamemode),
        "archived": tournament.status == "archived",
        "stage_rows": [
            {
                "stage": stage,
                "lineup_size": effective_lineup_size(stage, tournament),
                "lineup_set": stage.lineup_size is not None,
            }
            for stage in stages
        ],
        "freemod_options": list(FREEMOD_RULE_LABELS.items()),
        "scoring_options": list(SCORING_LABELS.items()),
        "team_options": [(str(team.id), team.name) for team in teams],
        "our_team_value": str(tournament.our_team_id or ""),
        **extra,
    }
    return render(request, "pages/tournament/settings.html", context, status_code=status_code)


def _lineup_fields(values: dict[str, str]) -> dict[int, str]:
    """`lineup_size_<stage id>` fields by stage id; fields with another name are ignored."""
    fields = {}
    for key, value in values.items():
        if not key.startswith(LINEUP_FIELD_PREFIX):
            continue
        stage_id = parse_uint(key.removeprefix(LINEUP_FIELD_PREFIX))
        if stage_id is not None:
            fields[stage_id] = value
    return fields


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    access: TournamentAccess = Depends(captain_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _page(request, access, db)


@router.post("")
async def save_settings(
    request: Request,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    fields = await request.form()
    values = {key: value for key, value in fields.items() if isinstance(value, str)}
    form = SettingsForm(
        freemod_rule=values.get("freemod_rule", ""),
        scoring=values.get("scoring", ""),
        lineup_sizes=_lineup_fields(values),
        our_team=values.get("our_team"),
        final_placing=values.get("final_placing"),
    )
    try:
        update_settings(db, access.tournament, form, access.is_admin, clock.now())
    except SettingsError as refused:
        return _page(
            request,
            access,
            db,
            refused.status,
            form_error=refused.message,
            form_error_field=refused.field,
            form_values=values,
        )
    flash(request, "success", "Settings saved.")
    return RedirectResponse(f"/t/{access.tournament.id}/settings", status_code=303)

"""Home page: the sign-in screen for visitors, the tournament list for users."""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.config import Settings
from app.domain.models import User
from app.repos import tournaments as tournaments_repo
from app.services.access import ROLE_LABELS
from app.web.deps import current_user, get_db, get_settings, is_admin
from app.web.templating import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    user: User | None = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    if user is None:
        return render(request, "pages/sign_in.html")
    admin = is_admin(user, settings)
    entries = (
        tournaments_repo.list_all(db, user.osu_id)
        if admin
        else tournaments_repo.list_for_member(db, user.osu_id)
    )
    rows = [
        {
            "tournament": entry.tournament,
            "role": ROLE_LABELS.get(entry.member_role or "", "Admin"),
        }
        for entry in entries
    ]
    return render(request, "pages/dashboard.html", {"rows": rows, "viewer_is_admin": admin})

"""Jinja environment (autoescape on) and the single `render` entry point for pages."""

from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.db.connection import open_db
from app.domain.models import User
from app.web.deps import is_admin, load_session_user
from app.web.session import csrf_token, pop_flashes

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["app_version"] = __version__


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render a page with the shared context: viewer, admin flag, CSRF token and flash messages."""
    user = _viewer(request)
    page_context = {
        "user": user,
        "is_admin": user is not None and is_admin(user, request.app.state.settings),
        "csrf_token": csrf_token(request) if user else "",
        "flashes": pop_flashes(request),
        **(context or {}),
    }
    return templates.TemplateResponse(
        request, template, page_context, status_code=status_code, headers=headers
    )


def _viewer(request: Request) -> User | None:
    """The logged-in user. Routes that already resolved them leave it on `request.state`; error
    pages and other routes that did not look it up here."""
    if hasattr(request.state, "user"):
        return request.state.user
    with closing(open_db(request.app.state.settings.db_path)) as conn:
        user = load_session_user(request, conn)
    request.state.user = user
    return user

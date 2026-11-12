"""Application factory. `uvicorn app.main:app` builds the app from the environment on first use."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.clients.http import make_http_client
from app.clients.osu import OsuClient
from app.clients.ratelimit import RateLimiter
from app.clock import Clock, SystemClock
from app.config import Settings
from app.db.connection import open_db
from app.db.migrate import migrate
from app.web.error_pages import install_error_handlers
from app.web.routes import auth, dashboard, health, me, members, styleguide, tournament
from app.web.security import (
    AUTH_LIMIT_PER_MINUTE,
    SecurityHeadersMiddleware,
    SlidingWindowLimiter,
    require_csrf,
)
from app.web.session import MAX_AGE_SECONDS

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
AUTH_WINDOW = timedelta(minutes=1)


def create_app(
    settings: Settings, clock: Clock, http_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Build the app. `http_transport` replaces the network for every osu! call (tests)."""
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    http = make_http_client(http_transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = open_db(settings.db_path)
        try:
            applied = migrate(conn, clock)
        finally:
            conn.close()
        logger.info("Started osu-tourney-hub %s (migrations applied: %s)", __version__, applied)
        try:
            yield
        finally:
            await http.aclose()

    # No interactive API docs: the app serves HTML pages, not a public API.
    app = FastAPI(
        title="osu-tourney-hub",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(require_csrf)],
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.osu = OsuClient(
        settings, http, RateLimiter(settings.osu_api_rate_per_min), clock, transport=http_transport
    )
    app.state.auth_limiter = SlidingWindowLimiter(AUTH_LIMIT_PER_MINUTE, AUTH_WINDOW, clock)
    # Added last = outermost, so the security headers also cover session and error responses.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        # The __Host- prefix makes browsers accept the cookie only if it is Secure, has no Domain
        # and is scoped to "/"; it is skipped on plain-http development setups.
        session_cookie="__Host-session" if settings.uses_https else "session",
        max_age=MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.uses_https,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.uses_https)
    install_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in (
        health.router,
        auth.router,
        dashboard.router,
        me.router,
        tournament.router,
        members.router,
        styleguide.router,
    ):
        app.include_router(router)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    # Lets `uvicorn app.main:app` read the environment when the server starts, while importing
    # this module (tests, tooling) stays free of side effects and does not need the variables.
    global _app
    if name != "app":
        raise AttributeError(name)
    if _app is None:
        _app = create_app(Settings.from_env(), SystemClock())
    return _app

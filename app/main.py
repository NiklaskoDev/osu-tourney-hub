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
from app.clients.wybin import WybinClient
from app.clock import Clock, SystemClock
from app.config import Settings
from app.db.connection import open_db
from app.db.migrate import migrate
from app.services.skimmer import Skimmer
from app.web.error_pages import install_error_handlers
from app.web.routes import (
    admin,
    admin_setup,
    auth,
    availability,
    capture,
    coverage,
    dashboard,
    feeds,
    health,
    history,
    lineup,
    lobbies,
    matches,
    me,
    members,
    pickems,
    pool,
    scanner,
    schedule,
    sheet_io,
    styleguide,
    tournament,
    tournament_settings,
)
from app.web.security import (
    AUTH_LIMIT_PER_MINUTE,
    RedactFeedTokens,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    SlidingWindowLimiter,
    require_csrf,
)
from app.web.session import MAX_AGE_SECONDS
from app.workers.jobs import build_jobs
from app.workers.scheduler import Scheduler

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
AUTH_WINDOW = timedelta(minutes=1)
REFRESH_WINDOW = timedelta(minutes=10)  # "Refresh my scores": once per person
OPPONENT_CAPTURE_WINDOW = timedelta(hours=1)  # once per team
IMPORT_LIMIT_PER_HOUR = 10
LOBBY_LINK_LIMIT_PER_HOUR = 20  # lobby links a person may add; each one costs an osu! request


def create_app(
    settings: Settings,
    clock: Clock,
    http_transport: httpx.AsyncBaseTransport | None = None,
    start_workers: bool = True,
) -> FastAPI:
    """Build the app. `http_transport` replaces the network for every osu! and wybin call
    (tests); `start_workers=False` keeps the background jobs from starting."""
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, RedactFeedTokens) for existing in access_log.filters):
        access_log.addFilter(RedactFeedTokens())
    http = make_http_client(http_transport)
    wybin = WybinClient(http, settings.wybin_min_interval_seconds)
    osu = OsuClient(
        settings,
        http,
        RateLimiter(settings.osu_api_rate_per_min),
        clock,
        transport=http_transport,
        # One request at a time at the scanner's pace: it never bursts past its share.
        scan_limiter=RateLimiter(settings.scanner_budget_per_min, burst=1),
    )
    skimmer = Skimmer(settings, clock, osu)
    scheduler = Scheduler(settings.db_path, clock, build_jobs(settings, clock, wybin, osu, skimmer))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = open_db(settings.db_path)
        try:
            applied = migrate(conn, clock)
        finally:
            conn.close()
        logger.info("Started osu-tourney-hub %s (migrations applied: %s)", __version__, applied)
        if start_workers:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
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
    app.state.http = http
    app.state.osu = osu
    app.state.skimmer = skimmer
    app.state.wybin = wybin
    app.state.scheduler = scheduler
    app.state.auth_limiter = SlidingWindowLimiter(AUTH_LIMIT_PER_MINUTE, AUTH_WINDOW, clock)
    app.state.refresh_limiter = SlidingWindowLimiter(1, REFRESH_WINDOW, clock)
    app.state.opponent_capture_limiter = SlidingWindowLimiter(1, OPPONENT_CAPTURE_WINDOW, clock)
    app.state.lobby_link_limiter = SlidingWindowLimiter(
        LOBBY_LINK_LIMIT_PER_HOUR, timedelta(hours=1), clock
    )
    app.state.import_limiter = SlidingWindowLimiter(
        IMPORT_LIMIT_PER_HOUR, timedelta(hours=1), clock
    )
    # Added last = outermost, so the security headers also cover session and error responses. The
    # body limit goes first (innermost of the three): it needs the session to render its error page.
    app.add_middleware(RequestBodyLimitMiddleware)
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
        pool.router,
        capture.router,
        sheet_io.router,
        lineup.router,
        lobbies.router,
        coverage.router,
        availability.router,
        schedule.router,
        matches.router,
        history.router,
        pickems.router,
        feeds.router,
        tournament_settings.router,
        admin.router,
        scanner.router,
        admin_setup.router,
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

"""Application factory. `uvicorn app.main:app` builds the app from the environment on first use."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.clock import Clock, SystemClock
from app.config import Settings
from app.db.connection import open_db
from app.db.migrate import migrate
from app.web.routes import health, styleguide
from app.web.security import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings, clock: Clock) -> FastAPI:
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = open_db(settings.db_path)
        try:
            applied = migrate(conn, clock)
        finally:
            conn.close()
        logger.info("Started osu-tourney-hub %s (migrations applied: %s)", __version__, applied)
        yield

    # No interactive API docs: the app serves HTML pages, not a public API.
    app = FastAPI(
        title="osu-tourney-hub",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.clock = clock
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.uses_https)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(styleguide.router)
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

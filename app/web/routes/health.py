import logging
import sqlite3
from contextlib import closing

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.db.connection import BUSY_TIMEOUT_MS

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """Liveness for the container healthcheck: 200 when the database can be read, else 503.

    The file is opened read-only so a probe can never create a missing database.
    """
    uri = f"{request.app.state.settings.db_path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)) as conn:
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.Error as error:
        logger.error("Health check failed: %s", type(error).__name__)
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok", "version": __version__})

import logging
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.db.connection import open_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """Liveness for the container healthcheck: 200 when the database can be read, else 503."""
    try:
        conn = open_db(request.app.state.settings.db_path)
        try:
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as error:
        logger.error("Health check failed: %s", type(error).__name__)
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok", "version": __version__})

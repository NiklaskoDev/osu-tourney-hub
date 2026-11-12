import tomllib
from pathlib import Path

import app

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_healthz_returns_ok_and_version(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": app.__version__}


def test_reported_version_comes_from_pyproject(client):
    declared = tomllib.loads(PYPROJECT.read_text("utf-8"))["project"]["version"]

    assert client.get("/healthz").json()["version"] == declared


def test_healthz_returns_503_without_details_when_database_is_unreadable(client, settings):
    settings.db_path.write_bytes(b"this is not a sqlite database" * 100)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "error"}


def test_healthz_returns_503_when_schema_is_missing(client, settings):
    settings.db_path.unlink()
    for leftover in settings.db_path.parent.glob("app.db-*"):
        leftover.unlink()

    response = client.get("/healthz")

    assert response.status_code == 503


def test_the_probe_never_recreates_a_missing_database(client, settings):
    settings.db_path.unlink()
    for leftover in settings.db_path.parent.glob("app.db-*"):
        leftover.unlink()

    for _ in range(2):
        assert client.get("/healthz").status_code == 503

    assert not settings.db_path.exists()
    assert list(settings.db_path.parent.glob("app.db*")) == []


def test_the_probe_does_not_write_to_the_database(client, settings):
    before = settings.db_path.stat().st_mtime_ns

    assert client.get("/healthz").status_code == 200

    assert settings.db_path.stat().st_mtime_ns == before


def test_interactive_api_docs_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404

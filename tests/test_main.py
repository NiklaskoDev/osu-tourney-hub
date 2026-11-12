from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import ConfigError
from app.db.connection import open_db
from app.db.migrate import MigrationError


def test_startup_creates_data_dir_and_migrates_database(client, settings):
    assert settings.db_path.exists()
    with closing(open_db(settings.db_path)) as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [1, 2, 3]


def test_startup_refuses_a_database_from_a_newer_version(settings, clock):
    settings.data_dir.mkdir(parents=True)
    with closing(open_db(settings.db_path)) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        conn.execute("INSERT INTO schema_migrations VALUES (999, 'x')")

    with pytest.raises(MigrationError), TestClient(main.create_app(settings, clock)):
        pass


def test_importing_the_module_does_not_need_configuration(monkeypatch):
    monkeypatch.delenv("OSU_CLIENT_ID", raising=False)
    monkeypatch.setattr(main, "_app", None)

    with pytest.raises(ConfigError, match="OSU_CLIENT_ID"):
        main.app  # noqa: B018 - attribute access is what builds the app


def test_uvicorn_entry_point_builds_the_app_from_the_environment(valid_env, monkeypatch):
    for name, value in valid_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main, "_app", None)

    built = main.app

    assert built.state.settings.osu_client_id == "12345"
    assert main.app is built


def test_unknown_module_attribute_still_raises():
    with pytest.raises(AttributeError):
        main.does_not_exist  # noqa: B018

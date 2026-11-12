from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@dataclass
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 12, 12, 30, 15, tzinfo=UTC))


@pytest.fixture
def valid_env(tmp_path) -> dict[str, str]:
    """A complete, valid environment with dummy values and a temporary data directory."""
    return {
        "OSU_CLIENT_ID": "12345",
        "OSU_CLIENT_SECRET": "dummy-client-secret",
        "PUBLIC_URL": "https://tourney.example.com",
        "SESSION_SECRET": "s" * 32,
        "ADMIN_OSU_IDS": "1001,1002",
        "DATA_DIR": str(tmp_path / "data"),
    }


@pytest.fixture
def settings(valid_env) -> Settings:
    return Settings.from_env(valid_env)


@pytest.fixture
def client(settings, clock) -> Iterator[TestClient]:
    with TestClient(create_app(settings, clock)) as test_client:
        yield test_client

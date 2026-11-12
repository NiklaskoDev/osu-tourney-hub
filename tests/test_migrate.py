import sqlite3
from contextlib import closing

import pytest

from app.db.connection import open_db, transaction
from app.db.migrate import MIGRATIONS_DIR, MigrationError, discover_migrations, migrate

EXPECTED_TABLES = {"schema_migrations", "users", "app_settings", "job_state", "feed_tokens"}


@pytest.fixture
def conn(tmp_path):
    with closing(open_db(tmp_path / "test.db")) as connection:
        yield connection


def table_names(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_fresh_database_gets_all_migrations(conn, clock):
    applied = migrate(conn, clock)

    available = [m.version for m in discover_migrations(MIGRATIONS_DIR)]
    assert applied == available
    assert EXPECTED_TABLES <= table_names(conn)
    recorded = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert [row["version"] for row in recorded] == available
    assert {row["applied_at"] for row in recorded} == {"2026-11-12T12:30:15Z"}


def test_rerun_is_a_no_op(conn, clock):
    migrate(conn, clock)

    assert migrate(conn, clock) == []
    count = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    assert count == len(discover_migrations(MIGRATIONS_DIR))


def test_database_newer_than_code_is_refused(conn, clock):
    migrate(conn, clock)
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (999, 'x')")

    with pytest.raises(MigrationError, match="newer version"):
        migrate(conn, clock)


def test_only_pending_migrations_are_applied(conn, clock, tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_a.sql").write_text(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        "CREATE TABLE a (id INTEGER);",
        "utf-8",
    )
    assert migrate(conn, clock, directory) == [1]

    (directory / "0002_b.sql").write_text("CREATE TABLE b (id INTEGER);", "utf-8")

    assert migrate(conn, clock, directory) == [2]
    assert {"a", "b"} <= table_names(conn)


def test_failed_migration_leaves_nothing_behind(conn, clock, tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_a.sql").write_text(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        "utf-8",
    )
    (directory / "0002_broken.sql").write_text(
        "CREATE TABLE half_done (id INTEGER); INSERT INTO missing_table VALUES (1);", "utf-8"
    )

    with pytest.raises(MigrationError, match="0002_broken.sql"):
        migrate(conn, clock, directory)

    assert "half_done" not in table_names(conn)
    assert not conn.in_transaction
    versions = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    assert versions == {1}


def test_gap_in_migration_numbers_is_rejected(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", "utf-8")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;", "utf-8")

    with pytest.raises(MigrationError, match="no gaps"):
        discover_migrations(tmp_path)


def test_badly_named_migration_file_is_rejected(tmp_path):
    (tmp_path / "init.sql").write_text("SELECT 1;", "utf-8")

    with pytest.raises(MigrationError, match="NNNN_name.sql"):
        discover_migrations(tmp_path)


def test_deleting_a_user_removes_their_feed_tokens(conn, clock):
    migrate(conn, clock)
    conn.execute(
        "INSERT INTO users (osu_id, username, created_at) VALUES (?, ?, ?)",
        (1, "sample", "2026-11-12T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO feed_tokens (token_hash, osu_id, created_at) VALUES (?, ?, ?)",
        ("hash", 1, "2026-11-12T00:00:00Z"),
    )

    conn.execute("DELETE FROM users WHERE osu_id = ?", (1,))

    assert conn.execute("SELECT COUNT(*) AS n FROM feed_tokens").fetchone()["n"] == 0


def test_feed_token_for_unknown_user_is_rejected(conn, clock):
    migrate(conn, clock)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO feed_tokens (token_hash, osu_id, created_at) VALUES (?, ?, ?)",
            ("hash", 42, "2026-11-12T00:00:00Z"),
        )


def test_connection_uses_wal_and_enforces_foreign_keys(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_transaction_commits_on_success_and_rolls_back_on_error(conn):
    conn.execute("CREATE TABLE t (id INTEGER)")

    with transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute("INSERT INTO t VALUES (2)")
        raise RuntimeError("boom")

    assert [row["id"] for row in conn.execute("SELECT id FROM t")] == [1]
    assert not conn.in_transaction

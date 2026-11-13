import sqlite3
from contextlib import closing

import pytest

from app.db.connection import open_db, transaction
from app.db.migrate import MIGRATIONS_DIR, MigrationError, discover_migrations, migrate

EXPECTED_TABLES = {
    "schema_migrations",
    "users",
    "app_settings",
    "job_state",
    "feed_tokens",
    "admin_wybin_links",
    "tournaments",
    "teams",
    "team_players",
    "tournament_members",
    "stages",
    "pool_maps",
    "scores",
    "notes",
    "lineups",
    "lineup_entries",
    "capture_queue",
    "capture_log",
}


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


def add_tournament(conn, **overrides):
    values = {
        "name": "Spring Cup",
        "gamemode": 0,
        "is_solo": 0,
        "created_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO tournaments ({columns}) VALUES ({marks})",  # noqa: S608 - test-only keys
        tuple(values.values()),
    )
    return cursor.lastrowid


@pytest.fixture
def migrated(conn, clock):
    migrate(conn, clock)
    return conn


def test_deleting_a_tournament_removes_everything_that_belongs_to_it(migrated):
    tournament_id = add_tournament(migrated)
    team_id = migrated.execute(
        "INSERT INTO teams (tournament_id, name) VALUES (?, 'Sample Team')", (tournament_id,)
    ).lastrowid
    migrated.execute(
        "UPDATE tournaments SET our_team_id = ? WHERE id = ?", (team_id, tournament_id)
    )
    migrated.execute(
        "INSERT INTO team_players (team_id, osu_id, username) VALUES (?, 5, 'Sample')", (team_id,)
    )
    migrated.execute(
        "INSERT INTO tournament_members (tournament_id, osu_id, role) VALUES (?, 5, 'player')",
        (tournament_id,),
    )
    stage_id = migrated.execute(
        "INSERT INTO stages (tournament_id, name, sort_order, is_qualifier) VALUES (?, 'Q', 1, 1)",
        (tournament_id,),
    ).lastrowid
    migrated.execute(
        "INSERT INTO pool_maps (stage_id, slot, bracket, required_mods, is_freemod, "
        "is_tiebreaker, sort_order) VALUES (?, 'NM1', 'NoMod', '', 0, 0, 1)",
        (stage_id,),
    )

    migrated.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))

    for table in ("teams", "team_players", "tournament_members", "stages", "pool_maps"):
        assert migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608


def test_membership_roles_are_limited_to_the_known_ones(migrated):
    tournament_id = add_tournament(migrated)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated.execute(
            "INSERT INTO tournament_members (tournament_id, osu_id, role) VALUES (?, 5, 'owner')",
            (tournament_id,),
        )


def test_a_person_can_only_be_on_a_roster_once(migrated):
    tournament_id = add_tournament(migrated)
    insert = "INSERT INTO tournament_members (tournament_id, osu_id, role) VALUES (?, 5, 'player')"
    migrated.execute(insert, (tournament_id,))

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        migrated.execute(insert, (tournament_id,))


def test_roster_entries_may_exist_before_the_person_has_logged_in(migrated):
    tournament_id = add_tournament(migrated)

    migrated.execute(
        "INSERT INTO tournament_members (tournament_id, osu_id, role) VALUES (?, 999, 'guest')",
        (tournament_id,),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"gamemode": 4},
        {"status": "deleted"},
        {"scoring": "v3"},
        {"freemod_rule": "anything"},
        {"source": "import"},
        {"wybin_id": 1},
    ],
)
def test_tournament_constraints(migrated, overrides):
    add_tournament(migrated, wybin_id=1)

    with pytest.raises(sqlite3.IntegrityError):
        add_tournament(migrated, **overrides)


def test_a_new_tournament_is_active_with_v2_scoring(migrated):
    tournament_id = add_tournament(migrated)

    row = migrated.execute(
        "SELECT status, scoring, freemod_rule FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()

    assert tuple(row) == ("active", "v2", "none")


def test_deleting_a_user_keeps_their_tournaments_but_clears_the_owner(migrated):
    migrated.execute(
        "INSERT INTO users (osu_id, username, created_at) "
        "VALUES (1, 'Owner', '2026-01-01T00:00:00Z')"
    )
    tournament_id = add_tournament(migrated, owner_osu_id=1)

    migrated.execute("DELETE FROM users WHERE osu_id = 1")

    row = migrated.execute("SELECT owner_osu_id FROM tournaments WHERE id = ?", (tournament_id,))
    assert row.fetchone()[0] is None


# --- 0003: scores, notes, lineups ---

SCORE_COLUMNS = (
    "(beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, source, source_ref, created_at)"
)


def add_score_row(conn, **overrides):
    values = {
        "beatmap_id": 1001,
        "osu_id": 5,
        "gamemode": 0,
        "mods": "",
        "score": 800_000,
        "scoring": "v2",
        "accuracy": 97.0,
        "source": "manual",
        "source_ref": "ref-1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return conn.execute(
        f"INSERT INTO scores ({columns}) VALUES ({marks})",  # noqa: S608 - test-only keys
        tuple(values.values()),
    ).lastrowid


@pytest.mark.parametrize(
    "overrides",
    [
        {"score": -1},
        {"score": 2_000_000_001},
        {"accuracy": 100.5},
        {"accuracy": -0.1},
        {"gamemode": 4},
        {"scoring": "v3"},
        {"source": "guess"},
        {"passed": 2},
    ],
)
def test_score_constraints(migrated, overrides):
    with pytest.raises(sqlite3.IntegrityError):
        add_score_row(migrated, **overrides)


def test_the_same_score_from_the_same_source_cannot_be_stored_twice(migrated):
    add_score_row(migrated)

    with pytest.raises(sqlite3.IntegrityError):
        add_score_row(migrated)
    add_score_row(migrated, osu_id=6)  # another player's row with the same reference is fine


def test_scores_of_people_who_never_logged_in_can_be_stored(migrated):
    assert add_score_row(migrated, osu_id=987654)


def test_deleting_a_tournament_keeps_its_scores_without_the_link(migrated):
    tournament_id = add_tournament(migrated)
    add_score_row(migrated, tournament_id=tournament_id)

    migrated.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))

    assert migrated.execute("SELECT tournament_id FROM scores").fetchone()[0] is None


def make_stage(conn):
    tournament_id = add_tournament(conn)
    stage_id = conn.execute(
        "INSERT INTO stages (tournament_id, name, sort_order, is_qualifier) VALUES (?, 'Q', 1, 0)",
        (tournament_id,),
    ).lastrowid
    return tournament_id, stage_id


def add_lineup(conn, tournament_id, stage_id, match_id=None, status="draft"):
    return conn.execute(
        "INSERT INTO lineups (tournament_id, stage_id, match_id, status, updated_at) "
        "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        (tournament_id, stage_id, match_id, status),
    ).lastrowid


def test_a_stage_has_one_lineup_without_a_match_but_may_have_one_per_match(migrated):
    tournament_id, stage_id = make_stage(migrated)
    add_lineup(migrated, tournament_id, stage_id)

    with pytest.raises(sqlite3.IntegrityError):
        add_lineup(migrated, tournament_id, stage_id)
    add_lineup(migrated, tournament_id, stage_id, match_id=1)
    add_lineup(migrated, tournament_id, stage_id, match_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        add_lineup(migrated, tournament_id, stage_id, match_id=2)


def test_a_lineup_status_is_draft_or_final(migrated):
    tournament_id, stage_id = make_stage(migrated)

    with pytest.raises(sqlite3.IntegrityError):
        add_lineup(migrated, tournament_id, stage_id, status="published")


def test_notes_are_limited_to_2000_characters_and_known_visibilities(migrated):
    tournament_id, stage_id = make_stage(migrated)
    map_id = migrated.execute(
        "INSERT INTO pool_maps (stage_id, slot, bracket, required_mods, is_freemod, "
        "is_tiebreaker, sort_order) VALUES (?, 'NM1', 'NoMod', '', 0, 0, 1)",
        (stage_id,),
    ).lastrowid
    insert = (
        "INSERT INTO notes (tournament_id, pool_map_id, author_osu_id, visibility, body, "
        "updated_at) VALUES (?, ?, 5, ?, ?, '2026-01-01T00:00:00Z')"
    )

    migrated.execute(insert, (tournament_id, map_id, "team", "x" * 2000))
    for visibility, body in (("team", "x" * 2001), ("everyone", "ok")):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(insert, (tournament_id, map_id, visibility, body))
    migrated.execute("DELETE FROM pool_maps WHERE id = ?", (map_id,))
    assert migrated.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


# --- 0004: score capture queue and log ---

QUEUE_INSERT = (
    "INSERT INTO capture_queue (osu_id, beatmap_id, gamemode, priority, requested_at) "
    "VALUES (?, ?, ?, 1, '2026-01-01T00:00:00Z')"
)
LOG_INSERT = (
    "INSERT INTO capture_log (osu_id, beatmap_id, gamemode, last_pulled_at) "
    "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')"
)


@pytest.mark.parametrize("insert", [QUEUE_INSERT, LOG_INSERT])
def test_a_pull_is_one_row_per_player_beatmap_and_mode(migrated, insert):
    migrated.execute(insert, (5, 1001, 0))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(insert, (5, 1001, 0))
    migrated.execute(insert, (6, 1001, 0))  # another player
    migrated.execute(insert, (5, 1002, 0))  # another map
    migrated.execute(insert, (5, 1001, 1))  # another mode


@pytest.mark.parametrize("insert", [QUEUE_INSERT, LOG_INSERT])
def test_capture_rows_only_accept_real_game_modes(migrated, insert):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(insert, (5, 1001, 4))


def test_a_queued_pull_starts_with_no_attempts(migrated):
    migrated.execute(QUEUE_INSERT, (5, 1001, 0))

    assert migrated.execute("SELECT attempts FROM capture_queue").fetchone()[0] == 0

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
    "matches",
    "availability",
    "lobbies",
    "lobby_pool_hits",
    "lobby_players",
    "scan_open",
    "scan_gaps",
    "scan_stats",
    "practice_targets",
    "plan_items",
    "predictions",
    "match_events",
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


# --- 0005: matches and availability ---

MATCH_INSERT = (
    "INSERT INTO matches (tournament_id, wybin_match_id, status, updated_at) "
    "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')"
)
SLOT_INSERT = (
    "INSERT INTO availability (tournament_id, osu_id, slot_start, status) VALUES (?, ?, ?, ?)"
)


def _tournament(migrated) -> int:
    cursor = migrated.execute(
        "INSERT INTO tournaments (name, gamemode, is_solo, created_at) "
        "VALUES ('Cup', 0, 0, '2026-01-01T00:00:00Z')"
    )
    return int(cursor.lastrowid)


def test_a_match_only_accepts_the_known_statuses(migrated):
    tid = _tournament(migrated)
    for number, status in enumerate(("scheduled", "live", "done")):
        migrated.execute(MATCH_INSERT, (tid, number, status))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(MATCH_INSERT, (tid, 99, "postponed"))
    with pytest.raises(sqlite3.IntegrityError):  # sync never cancels a match
        migrated.execute(MATCH_INSERT, (tid, 98, "cancelled"))


def test_a_wybin_match_id_is_stored_once(migrated):
    tid = _tournament(migrated)
    migrated.execute(MATCH_INSERT, (tid, 5001, "scheduled"))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(MATCH_INSERT, (tid, 5001, "scheduled"))


def test_matches_without_a_wybin_id_may_repeat(migrated):
    tid = _tournament(migrated)

    migrated.execute(MATCH_INSERT, (tid, None, "scheduled"))
    migrated.execute(MATCH_INSERT, (tid, None, "scheduled"))


def test_deleting_a_tournament_deletes_its_matches_and_availability(migrated):
    tid = _tournament(migrated)
    migrated.execute(MATCH_INSERT, (tid, 1, "scheduled"))
    migrated.execute(SLOT_INSERT, (tid, 5, "2026-11-16T18:00:00Z", "yes"))

    migrated.execute("DELETE FROM tournaments WHERE id = ?", (tid,))

    assert migrated.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
    assert migrated.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 0


def test_deleting_a_team_keeps_the_match_with_an_empty_side(migrated):
    tid = _tournament(migrated)
    team = migrated.execute("INSERT INTO teams (tournament_id, name) VALUES (?, 'A')", (tid,))
    migrated.execute(
        "INSERT INTO matches (tournament_id, team1_id, status, updated_at) "
        "VALUES (?, ?, 'scheduled', '2026-01-01T00:00:00Z')",
        (tid, team.lastrowid),
    )

    migrated.execute("DELETE FROM teams WHERE id = ?", (team.lastrowid,))

    assert migrated.execute("SELECT team1_id FROM matches").fetchone()[0] is None


def test_an_availability_slot_is_one_row_per_person_and_slot(migrated):
    tid = _tournament(migrated)
    migrated.execute(SLOT_INSERT, (tid, 5, "2026-11-16T18:00:00Z", "yes"))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(SLOT_INSERT, (tid, 5, "2026-11-16T18:00:00Z", "maybe"))
    migrated.execute(SLOT_INSERT, (tid, 6, "2026-11-16T18:00:00Z", "maybe"))  # another person
    migrated.execute(SLOT_INSERT, (tid, 5, "2026-11-16T18:30:00Z", "no"))  # another slot


def test_availability_only_accepts_yes_maybe_and_no(migrated):
    tid = _tournament(migrated)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(SLOT_INSERT, (tid, 5, "2026-11-16T18:00:00Z", "perhaps"))


# --- 0006: lobbies, scanner bookkeeping and practice targets ---

LOBBY_INSERT = (
    "INSERT INTO lobbies (mp_match_id, name, first_seen_at, discovered_by) "
    "VALUES (?, 'room', '2026-11-12T12:00:00Z', ?)"
)
HIT_INSERT = "INSERT INTO lobby_pool_hits (mp_match_id, game_id, pool_map_id) VALUES (1, 5, ?)"
PLAYER_INSERT = "INSERT INTO lobby_players (mp_match_id, tournament_id, osu_id) VALUES (1, ?, 7)"
TARGET_INSERT = (
    "INSERT INTO practice_targets (tournament_id, pool_map_id, osu_id, target_score, target_acc, "
    "note, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'x')"
)


def _pool_map(migrated, tournament_id: int) -> int:
    stage = migrated.execute(
        "INSERT INTO stages (tournament_id, name, sort_order, is_qualifier) VALUES (?, 'Q', 1, 0)",
        (tournament_id,),
    )
    pool_map = migrated.execute(
        "INSERT INTO pool_maps (stage_id, slot, bracket, required_mods, is_freemod, "
        "is_tiebreaker, sort_order) VALUES (?, 'NM1', 'NoMod', '', 0, 0, 1)",
        (stage.lastrowid,),
    )
    return int(pool_map.lastrowid)


def test_a_lobby_is_stored_once_and_only_with_a_known_discovery_source(migrated):
    for number, source in enumerate(("scan", "wybin", "manual")):
        migrated.execute(LOBBY_INSERT, (number, source))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(LOBBY_INSERT, (0, "scan"))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(LOBBY_INSERT, (9, "guess"))
    row = migrated.execute("SELECT is_complete FROM lobbies WHERE mp_match_id = 0").fetchone()
    assert row[0] == 0


def test_hits_and_players_leave_with_their_lobby(migrated):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)
    migrated.execute(LOBBY_INSERT, (1, "scan"))
    migrated.execute(HIT_INSERT, (pool_map,))
    migrated.execute(PLAYER_INSERT, (tid,))

    migrated.execute("DELETE FROM lobbies WHERE mp_match_id = 1")

    assert migrated.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 0
    assert migrated.execute("SELECT COUNT(*) FROM lobby_players").fetchone()[0] == 0


def test_a_hit_is_stored_once_per_game_and_pool_map_and_leaves_with_its_map(migrated):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)
    migrated.execute(LOBBY_INSERT, (1, "scan"))
    migrated.execute(HIT_INSERT, (pool_map,))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(HIT_INSERT, (pool_map,))
    migrated.execute("DELETE FROM pool_maps WHERE id = ?", (pool_map,))

    assert migrated.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 0
    assert migrated.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 1


def test_a_player_is_noted_once_per_lobby_and_tournament_and_leaves_with_the_tournament(migrated):
    tid = _tournament(migrated)
    migrated.execute(LOBBY_INSERT, (1, "scan"))
    migrated.execute(PLAYER_INSERT, (tid,))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(PLAYER_INSERT, (tid,))
    migrated.execute("DELETE FROM tournaments WHERE id = ?", (tid,))

    assert migrated.execute("SELECT COUNT(*) FROM lobby_players").fetchone()[0] == 0
    assert migrated.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 1  # lobbies are public


def test_a_skipped_range_must_not_end_before_it_starts_and_needs_a_known_reason(migrated):
    gap = "INSERT INTO scan_gaps (start_id, end_id, reason, created_at) VALUES (?, ?, ?, 'x')"
    migrated.execute(gap, (5, 5, "skipped_backlog"))
    migrated.execute(gap, (1, 9, "admin_skip"))
    migrated.execute(gap, (7, 8, "fetch_error"))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(gap, (9, 1, "skipped_backlog"))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(gap, (1, 9, "bored"))
    assert migrated.execute("SELECT backfilled_until FROM scan_gaps").fetchone()[0] is None


def test_scan_counters_start_at_zero_and_a_minute_is_one_row(migrated):
    migrated.execute("INSERT INTO scan_stats (minute) VALUES ('2026-11-12T12:30')")

    row = migrated.execute(
        "SELECT ids_scanned, not_found, matches_with_hits, requests, head_id, errors "
        "FROM scan_stats"
    ).fetchone()
    assert tuple(row) == (0, 0, 0, 0, None, 0)
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("INSERT INTO scan_stats (minute) VALUES ('2026-11-12T12:30')")


def test_an_open_lobby_is_tracked_once(migrated):
    sql = "INSERT INTO scan_open (mp_match_id, first_seen_at, next_check_at) VALUES (1, 'a', 'b')"
    migrated.execute(sql)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(sql)
    row = migrated.execute("SELECT events_fetched, last_event_id FROM scan_open").fetchone()
    assert tuple(row) == (0, None)


def test_a_map_has_one_team_target_and_one_target_per_player(migrated):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)
    migrated.execute(TARGET_INSERT, (tid, pool_map, None, 800_000, None, None))
    migrated.execute(TARGET_INSERT, (tid, pool_map, 7, 700_000, None, None))
    migrated.execute(TARGET_INSERT, (tid, pool_map, 8, None, 97.5, None))

    with pytest.raises(sqlite3.IntegrityError):  # a second team target
        migrated.execute(TARGET_INSERT, (tid, pool_map, None, 900_000, None, None))
    with pytest.raises(sqlite3.IntegrityError):  # a second target for the same player
        migrated.execute(TARGET_INSERT, (tid, pool_map, 7, 1, None, None))


@pytest.mark.parametrize(
    ("score", "accuracy", "note"),
    [
        (None, None, None),
        (-1, None, None),
        (2_000_000_001, None, None),
        (None, 100.5, None),
        (None, -1, None),
        (1, None, "x" * 501),
    ],
)
def test_a_target_needs_a_figure_inside_the_limits(migrated, score, accuracy, note):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(TARGET_INSERT, (tid, pool_map, 7, score, accuracy, note))


def test_targets_leave_with_their_tournament(migrated):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)
    migrated.execute(TARGET_INSERT, (tid, pool_map, 7, 1, None, None))

    migrated.execute("DELETE FROM tournaments WHERE id = ?", (tid,))

    assert migrated.execute("SELECT COUNT(*) FROM practice_targets").fetchone()[0] == 0


# --- 0007: plan, predictions and match events ---

PLAN_INSERT = (
    "INSERT INTO plan_items (match_id, kind, pool_map_id, position, note) VALUES (?, ?, ?, ?, ?)"
)
PREDICTION_INSERT = (
    "INSERT INTO predictions (match_id, kind, created_at, win_prob_team1, payload, model_version) "
    "VALUES (?, ?, 'x', ?, '{}', 'stat-1')"
)
EVENT_INSERT = (
    "INSERT INTO match_events (match_id, seq, kind, team_side, winner_side, source) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _match(migrated, tournament_id: int) -> int:
    cursor = migrated.execute(
        "INSERT INTO matches (tournament_id, status, updated_at) VALUES (?, 'scheduled', 'x')",
        (tournament_id,),
    )
    return int(cursor.lastrowid)


def test_a_plan_item_is_one_kind_and_position_per_match_and_needs_a_known_kind(migrated):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)
    match = _match(migrated, tid)
    migrated.execute(PLAN_INSERT, (match, "ban", pool_map, 1, None))
    migrated.execute(PLAN_INSERT, (match, "pick", pool_map, 1, None))  # same position, other kind

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(PLAN_INSERT, (match, "ban", pool_map, 1, None))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(PLAN_INSERT, (match, "tiebreaker", pool_map, 1, None))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(PLAN_INSERT, (match, "ban", pool_map, 0, None))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(PLAN_INSERT, (match, "ban", pool_map, 2, "x" * 301))


def test_a_plan_leaves_with_its_match_and_with_its_map(migrated):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)
    match = _match(migrated, tid)
    migrated.execute(PLAN_INSERT, (match, "ban", pool_map, 1, None))
    migrated.execute("DELETE FROM pool_maps WHERE id = ?", (pool_map,))
    assert migrated.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0] == 0

    other_map = _pool_map(migrated, tid)
    migrated.execute(PLAN_INSERT, (match, "ban", other_map, 1, None))
    migrated.execute("DELETE FROM matches WHERE id = ?", (match,))
    assert migrated.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("kind", "chance", "ok"),
    [
        ("stat", 0.0, True),
        ("ai", 1.0, True),
        ("stat", 1.01, False),
        ("stat", -0.1, False),
        ("guess", 0.5, False),
    ],
)
def test_a_prediction_has_a_known_kind_and_a_chance_between_zero_and_one(
    migrated, kind, chance, ok
):
    match = _match(migrated, _tournament(migrated))

    if ok:
        migrated.execute(PREDICTION_INSERT, (match, kind, chance))
    else:
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(PREDICTION_INSERT, (match, kind, chance))


def test_predictions_and_events_leave_with_their_match(migrated):
    match = _match(migrated, _tournament(migrated))
    migrated.execute(PREDICTION_INSERT, (match, "stat", 0.5))
    migrated.execute(EVENT_INSERT, (match, 1, "pick", 1, 2, "wybin"))

    migrated.execute("DELETE FROM matches WHERE id = ?", (match,))

    assert migrated.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
    assert migrated.execute("SELECT COUNT(*) FROM match_events").fetchone()[0] == 0


def test_a_match_event_is_one_row_per_sequence_number_with_known_values(migrated):
    match = _match(migrated, _tournament(migrated))
    migrated.execute(EVENT_INSERT, (match, 1, "ban", 1, None, "mp"))
    migrated.execute(EVENT_INSERT, (match, 2, "pick", None, 1, "manual"))  # side may be unknown

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(EVENT_INSERT, (match, 1, "pick", 2, 2, "mp"))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(EVENT_INSERT, (match, 3, "swap", 1, 1, "mp"))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(EVENT_INSERT, (match, 3, "pick", 3, 1, "mp"))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(EVENT_INSERT, (match, 3, "pick", 1, 1, "guess"))


def test_a_match_event_keeps_its_row_when_its_map_is_deleted(migrated):
    tid = _tournament(migrated)
    pool_map = _pool_map(migrated, tid)
    match = _match(migrated, tid)
    migrated.execute(
        "INSERT INTO match_events (match_id, seq, kind, pool_map_id, beatmap_id, source) "
        "VALUES (?, 1, 'pick', ?, 5, 'mp')",
        (match, pool_map),
    )

    migrated.execute("DELETE FROM pool_maps WHERE id = ?", (pool_map,))

    row = migrated.execute("SELECT pool_map_id, beatmap_id FROM match_events").fetchone()
    assert tuple(row) == (None, 5)


# --- 0008: a score may have no accuracy ---

INSERT_SCORE = (
    "INSERT INTO scores (beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, source, "
    "source_ref, created_at) VALUES (1001, 2002, 0, '', 800000, 'v2', ?, 'manual', ?, "
    "'2026-11-01T00:00:00Z')"
)


def insert_score_row(conn, accuracy, ref="r1"):
    conn.execute(INSERT_SCORE, (accuracy, ref))


@pytest.fixture
def before_0008(conn, clock, tmp_path):
    """A database at migration 0007 with one score, then migrated to the current schema."""
    older = tmp_path / "older"
    older.mkdir()
    for migration in discover_migrations(MIGRATIONS_DIR):
        if migration.version < 8:
            (older / migration.name).write_text(migration.sql, "utf-8")
    migrate(conn, clock, older)
    insert_score_row(conn, 97.5)
    assert migrate(conn, clock) == [8]
    return conn


def test_0008_keeps_the_scores_and_the_indexes(before_0008):
    rows = before_0008.execute("SELECT osu_id, score, accuracy, source_ref FROM scores").fetchall()
    assert [tuple(row) for row in rows] == [(2002, 800_000, 97.5, "r1")]
    names = {
        row["name"]
        for row in before_0008.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {"scores_beatmap_player_mods", "scores_osu_id", "scores_tournament_id"} <= names
    assert "scores_new" not in table_names(before_0008)


def test_0008_accepts_an_empty_accuracy_but_still_rejects_one_outside_0_to_100(before_0008):
    insert_score_row(before_0008, None, "r2")
    stored = before_0008.execute("SELECT accuracy FROM scores WHERE source_ref = 'r2'").fetchone()
    assert stored[0] is None

    with pytest.raises(sqlite3.IntegrityError):
        insert_score_row(before_0008, 101, "r3")
    with pytest.raises(sqlite3.IntegrityError):
        insert_score_row(before_0008, -1, "r4")


def test_0008_keeps_the_uniqueness_of_a_score_per_source_and_player(before_0008):
    with pytest.raises(sqlite3.IntegrityError):
        insert_score_row(before_0008, 90.0, "r1")

"""Applies `migrations/NNNN_*.sql` in order and records each in `schema_migrations`.

A migration file must not contain its own BEGIN/COMMIT: the runner wraps it together with the
`schema_migrations` row, so a failed migration leaves nothing behind. Applied files are never
edited; a schema change is a new file.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.clock import Clock, to_utc_iso

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILE_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """The migration set or the database is not in a state the app can start from."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_PATTERN.match(path.name)
        if not match:
            raise MigrationError(f"Migration file name does not match NNNN_name.sql: {path.name}")
        migrations.append(Migration(int(match.group(1)), path.name, path.read_text("utf-8")))
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("Migration numbers must start at 0001 and have no gaps or duplicates")
    return migrations


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not has_table:
        return set()
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, clock: Clock, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations and return their versions. Refuses a newer database."""
    migrations = discover_migrations(directory)
    known = {m.version for m in migrations}
    applied = applied_versions(conn)
    newer = sorted(applied - known)
    if newer:
        raise MigrationError(
            "The database was migrated by a newer version of the app "
            f"(unknown migration {newer[-1]:04d}); refusing to start"
        )
    done = []
    for migration in migrations:
        if migration.version not in applied:
            _apply(conn, migration, to_utc_iso(clock.now()))
            done.append(migration.version)
    return done


def _apply(conn: sqlite3.Connection, migration: Migration, applied_at: str) -> None:
    # executescript() cannot take parameters and commits any open transaction, so the transaction
    # is part of the script. Both interpolated values are program-generated: an int parsed from
    # the file name by a digits-only pattern and a fixed-format timestamp.
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n"
        "INSERT INTO schema_migrations (version, applied_at) "  # noqa: S608
        f"VALUES ({migration.version}, '{applied_at}');\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"Migration {migration.name} failed: {error}") from error

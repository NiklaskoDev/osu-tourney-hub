"""SQL for job_state: when each background job last ran and how it ended."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class JobState:
    name: str
    last_run_at: str | None
    last_ok_at: str | None
    last_error: str | None
    cursor: str | None


def record_start(conn: sqlite3.Connection, name: str, now: str) -> None:
    conn.execute(
        "INSERT INTO job_state (name, last_run_at) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET last_run_at = excluded.last_run_at",
        (name, now),
    )


def record_success(conn: sqlite3.Connection, name: str, now: str) -> None:
    conn.execute(
        "UPDATE job_state SET last_ok_at = ?, last_error = NULL WHERE name = ?", (now, name)
    )


def record_failure(conn: sqlite3.Connection, name: str, message: str) -> None:
    conn.execute("UPDATE job_state SET last_error = ? WHERE name = ?", (message, name))


def get(conn: sqlite3.Connection, name: str) -> JobState | None:
    row = conn.execute(
        "SELECT name, last_run_at, last_ok_at, last_error, cursor FROM job_state WHERE name = ?",
        (name,),
    ).fetchone()
    return JobState(**row) if row else None

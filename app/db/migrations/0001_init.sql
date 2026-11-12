-- Phase 0-1: schema bookkeeping, users, settings, job state, calendar feed tokens.
-- Times are UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SSZ).

CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE users (
    osu_id          INTEGER PRIMARY KEY,
    username        TEXT NOT NULL,
    avatar_url      TEXT,
    country         TEXT,
    timezone        TEXT,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

CREATE TABLE app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE job_state (
    name        TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_ok_at  TEXT,
    last_error  TEXT,
    cursor      TEXT
);

CREATE TABLE feed_tokens (
    token_hash TEXT PRIMARY KEY,
    osu_id     INTEGER NOT NULL REFERENCES users (osu_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX feed_tokens_osu_id ON feed_tokens (osu_id);

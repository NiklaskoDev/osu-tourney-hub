-- Phase 6: multiplayer lobbies that played pool maps, the lobby scanner's bookkeeping and practice
-- targets. Times are UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SSZ).

-- One row per lobby, keyed by its osu! match id, so a lobby is stored once however it was found.
-- The scanner only stores lobbies with at least one pool-map hit; links from wybin and links
-- pasted by a member are stored either way.
CREATE TABLE lobbies (
    mp_match_id     INTEGER PRIMARY KEY,
    name            TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    first_seen_at   TEXT NOT NULL,
    last_scanned_at TEXT,
    is_complete     INTEGER NOT NULL DEFAULT 0 CHECK (is_complete IN (0, 1)),
    discovered_by   TEXT NOT NULL CHECK (discovered_by IN ('scan', 'wybin', 'manual'))
);

-- A game of a lobby that was played on a pool map. `mods` are the game's required mods.
CREATE TABLE lobby_pool_hits (
    mp_match_id INTEGER NOT NULL REFERENCES lobbies (mp_match_id) ON DELETE CASCADE,
    game_id     INTEGER NOT NULL,
    pool_map_id INTEGER NOT NULL REFERENCES pool_maps (id) ON DELETE CASCADE,
    played_at   TEXT,
    mods        TEXT,
    PRIMARY KEY (mp_match_id, game_id, pool_map_id)
);
CREATE INDEX lobby_pool_hits_pool_map ON lobby_pool_hits (pool_map_id);

-- Players of a lobby who are registered in a tournament (on one of its teams or its roster).
-- `osu_id` is not a foreign key: opponents never log in.
CREATE TABLE lobby_players (
    mp_match_id   INTEGER NOT NULL REFERENCES lobbies (mp_match_id) ON DELETE CASCADE,
    tournament_id INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    osu_id        INTEGER NOT NULL,
    team_id       INTEGER REFERENCES teams (id) ON DELETE SET NULL,
    PRIMARY KEY (mp_match_id, tournament_id, osu_id)
);
CREATE INDEX lobby_players_tournament ON lobby_players (tournament_id, osu_id);

-- Lobbies the scanner saw while they were still open, waiting for their final scan after they
-- close. `last_event_id` is the newest event that is final (read already); the final scan only
-- asks for events after it. `next_check_at` paces the incremental scans of long lobbies.
CREATE TABLE scan_open (
    mp_match_id    INTEGER PRIMARY KEY,
    first_seen_at  TEXT NOT NULL,
    last_event_id  INTEGER,
    events_fetched INTEGER NOT NULL DEFAULT 0,
    next_check_at  TEXT NOT NULL
);

-- Ranges of match ids the scanner did not read: it was too far behind ('skipped_backlog'), an admin
-- skipped them ('admin_skip'), or osu! kept failing for them ('fetch_error', adjacent ids merged).
-- A single deleted or private id is only counted in `scan_stats`.
-- `backfilled_until` is NULL until an admin asks for a backfill of the range; from then on it is
-- the last id backfilled (`start_id - 1` before the first one, `end_id` when done).
CREATE TABLE scan_gaps (
    id               INTEGER PRIMARY KEY,
    start_id         INTEGER NOT NULL,
    end_id           INTEGER NOT NULL,
    reason           TEXT NOT NULL CHECK (reason IN ('skipped_backlog', 'admin_skip', 'fetch_error')),
    created_at       TEXT NOT NULL,
    backfilled_until INTEGER,
    CHECK (start_id <= end_id)
);

-- Per-minute counters of the scanner (rolling 24 hours, pruned hourly). `minute` is UTC
-- YYYY-MM-DDTHH:MM; `head_id` is the newest match id osu! listed that minute; `errors` counts ids
-- given up on after repeated failures.
CREATE TABLE scan_stats (
    minute            TEXT PRIMARY KEY,
    ids_scanned       INTEGER NOT NULL DEFAULT 0,
    not_found         INTEGER NOT NULL DEFAULT 0,
    matches_with_hits INTEGER NOT NULL DEFAULT 0,
    requests          INTEGER NOT NULL DEFAULT 0,
    head_id           INTEGER,
    errors            INTEGER NOT NULL DEFAULT 0
);

-- What a player (or, with a NULL `osu_id`, every player of the team) should reach on a map.
-- UNIQUE treats NULLs as distinct, so the second index keeps one team target per map.
CREATE TABLE practice_targets (
    id            INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    pool_map_id   INTEGER NOT NULL REFERENCES pool_maps (id) ON DELETE CASCADE,
    osu_id        INTEGER,
    target_score  INTEGER CHECK (target_score IS NULL OR target_score BETWEEN 0 AND 2000000000),
    target_acc    REAL CHECK (target_acc IS NULL OR target_acc BETWEEN 0 AND 100),
    note          TEXT CHECK (note IS NULL OR length(note) <= 500),
    set_by        INTEGER,
    updated_at    TEXT NOT NULL,
    UNIQUE (pool_map_id, osu_id),
    CHECK (target_score IS NOT NULL OR target_acc IS NOT NULL)
);
CREATE UNIQUE INDEX practice_targets_one_team_target
    ON practice_targets (pool_map_id) WHERE osu_id IS NULL;
CREATE INDEX practice_targets_tournament ON practice_targets (tournament_id);

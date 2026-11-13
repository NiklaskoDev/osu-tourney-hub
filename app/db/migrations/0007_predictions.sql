-- Phase 7-8: the captain's pick/ban plan, saved predictions and the picks and bans that were
-- actually played. Times are UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SSZ). Phase 7 writes the first
-- two tables; `match_events` is filled by phase 8 and lives here because applied migration files
-- are never edited.

-- Our team's planned protects, bans and picks for one match, in the order the captain gave them.
CREATE TABLE plan_items (
    id          INTEGER PRIMARY KEY,
    match_id    INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('ban', 'pick', 'protect')),
    pool_map_id INTEGER NOT NULL REFERENCES pool_maps (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL CHECK (position >= 1),
    note        TEXT CHECK (note IS NULL OR length(note) <= 300),
    set_by      INTEGER,
    UNIQUE (match_id, kind, position)
);

-- A frozen snapshot of a prediction. The payload is JSON because it is read whole, never queried
-- by field.
CREATE TABLE predictions (
    id             INTEGER PRIMARY KEY,
    match_id       INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('stat', 'ai')),
    created_at     TEXT NOT NULL,
    win_prob_team1 REAL NOT NULL CHECK (win_prob_team1 BETWEEN 0 AND 1),
    expected_score TEXT,
    payload        TEXT NOT NULL,
    model_version  TEXT NOT NULL
);
CREATE INDEX predictions_match ON predictions (match_id, created_at);

-- What really happened in a match: protects, bans, picks and who won each map.
CREATE TABLE match_events (
    match_id     INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('protect', 'ban', 'pick', 'tiebreaker')),
    team_side    INTEGER CHECK (team_side IN (1, 2)),
    pool_map_id  INTEGER REFERENCES pool_maps (id) ON DELETE SET NULL,
    beatmap_id   INTEGER,
    winner_side  INTEGER CHECK (winner_side IN (1, 2)),
    team1_score  INTEGER,
    team2_score  INTEGER,
    source       TEXT NOT NULL CHECK (source IN ('wybin', 'mp', 'manual')),
    PRIMARY KEY (match_id, seq)
);

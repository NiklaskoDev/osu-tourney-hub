-- Phase 3: scores, notes and lineups behind the mappool sheet.
-- Times are UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SSZ).

-- A score belongs to a player and a beatmap, not to a tournament: the same beatmap + mods in
-- another tournament shows it again. `osu_id` is deliberately not a foreign key to users, so
-- scores of people who never logged in can be stored. `tournament_id` only records where a
-- hand-entered score was typed in, for the "from <tournament>" hint; it is empty for scores
-- captured from osu! and after that tournament is deleted.
CREATE TABLE scores (
    id            INTEGER PRIMARY KEY,
    beatmap_id    INTEGER NOT NULL,
    osu_id        INTEGER NOT NULL,
    gamemode      INTEGER NOT NULL CHECK (gamemode BETWEEN 0 AND 3),
    mods          TEXT NOT NULL,
    score         INTEGER NOT NULL CHECK (score BETWEEN 0 AND 2000000000),
    scoring       TEXT NOT NULL CHECK (scoring IN ('v2', 'v1', 'lazer', 'unknown')),
    accuracy      REAL NOT NULL CHECK (accuracy BETWEEN 0 AND 100),
    max_combo     INTEGER,
    misses        INTEGER,
    passed        INTEGER NOT NULL DEFAULT 1 CHECK (passed IN (0, 1)),
    achieved_at   TEXT,
    source        TEXT NOT NULL CHECK (source IN ('manual', 'api', 'mp', 'import')),
    source_ref    TEXT NOT NULL,
    source_url    TEXT,
    entered_by    INTEGER,
    tournament_id INTEGER REFERENCES tournaments (id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (source, source_ref, osu_id)
);
CREATE INDEX scores_beatmap_player_mods ON scores (beatmap_id, osu_id, mods);
CREATE INDEX scores_osu_id ON scores (osu_id);
CREATE INDEX scores_tournament_id ON scores (tournament_id);

CREATE TABLE notes (
    id             INTEGER PRIMARY KEY,
    tournament_id  INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    pool_map_id    INTEGER NOT NULL REFERENCES pool_maps (id) ON DELETE CASCADE,
    author_osu_id  INTEGER NOT NULL,
    visibility     TEXT NOT NULL CHECK (visibility IN ('private', 'team')),
    body           TEXT NOT NULL CHECK (length(body) <= 2000),
    updated_at     TEXT NOT NULL
);
CREATE INDEX notes_pool_map_id ON notes (pool_map_id);
CREATE INDEX notes_author ON notes (author_osu_id);

-- `match_id` is a plain number until the matches table exists (phase 5). A stage has at most one
-- lineup without a match; the second index enforces that, because UNIQUE treats NULLs as distinct.
CREATE TABLE lineups (
    id             INTEGER PRIMARY KEY,
    tournament_id  INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    stage_id       INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    match_id       INTEGER,
    status         TEXT NOT NULL CHECK (status IN ('draft', 'final')),
    updated_by     INTEGER,
    updated_at     TEXT NOT NULL,
    UNIQUE (stage_id, match_id)
);
CREATE UNIQUE INDEX lineups_one_per_stage ON lineups (stage_id) WHERE match_id IS NULL;

CREATE TABLE lineup_entries (
    lineup_id    INTEGER NOT NULL REFERENCES lineups (id) ON DELETE CASCADE,
    pool_map_id  INTEGER NOT NULL REFERENCES pool_maps (id) ON DELETE CASCADE,
    osu_id       INTEGER NOT NULL,
    mods         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (lineup_id, pool_map_id, osu_id)
);

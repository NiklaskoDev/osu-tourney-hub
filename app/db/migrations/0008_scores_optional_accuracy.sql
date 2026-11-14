-- Plan 003: a hand-entered score may leave accuracy empty (quick score entry). SQLite cannot drop a
-- NOT NULL constraint, so `scores` is rebuilt with exactly the columns, CHECKs, UNIQUE and defaults
-- of 0003; the only difference is that `accuracy` may be NULL. No table references `scores`.
-- Back up /data before updating: a rebuilt table cannot be undone from inside the database.
CREATE TABLE scores_new (
    id            INTEGER PRIMARY KEY,
    beatmap_id    INTEGER NOT NULL,
    osu_id        INTEGER NOT NULL,
    gamemode      INTEGER NOT NULL CHECK (gamemode BETWEEN 0 AND 3),
    mods          TEXT NOT NULL,
    score         INTEGER NOT NULL CHECK (score BETWEEN 0 AND 2000000000),
    scoring       TEXT NOT NULL CHECK (scoring IN ('v2', 'v1', 'lazer', 'unknown')),
    accuracy      REAL CHECK (accuracy IS NULL OR accuracy BETWEEN 0 AND 100),
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
INSERT INTO scores_new SELECT * FROM scores;
DROP TABLE scores;
ALTER TABLE scores_new RENAME TO scores;
CREATE INDEX scores_beatmap_player_mods ON scores (beatmap_id, osu_id, mods);
CREATE INDEX scores_osu_id ON scores (osu_id);
CREATE INDEX scores_tournament_id ON scores (tournament_id);

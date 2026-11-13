-- Phase 5: matches from wybin and each member's availability.
-- Times are UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SSZ).

-- Every match of a tournament, ours and the others. A team or stage wybin names but the app does
-- not know stays NULL; the label still says what the match is.
CREATE TABLE matches (
    id              INTEGER PRIMARY KEY,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    stage_id        INTEGER REFERENCES stages (id) ON DELETE SET NULL,
    wybin_match_id  INTEGER UNIQUE,
    label           TEXT,
    team1_id        INTEGER REFERENCES teams (id) ON DELETE SET NULL,
    team2_id        INTEGER REFERENCES teams (id) ON DELETE SET NULL,
    scheduled_at    TEXT,
    score1          INTEGER,
    score2          INTEGER,
    mp_match_id     INTEGER,
    status          TEXT NOT NULL CHECK (status IN ('scheduled', 'live', 'done')),
    updated_at      TEXT NOT NULL
);
CREATE INDEX matches_tournament_time ON matches (tournament_id, scheduled_at);

-- One row per member and 30-minute slot that is not "no": a slot without a row counts as "no".
-- `slot_start` is the UTC start of the slot, aligned to :00 or :30.
CREATE TABLE availability (
    tournament_id INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    osu_id        INTEGER NOT NULL,
    slot_start    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('yes', 'maybe', 'no')),
    PRIMARY KEY (tournament_id, osu_id, slot_start)
);
CREATE INDEX availability_osu_id ON availability (osu_id);

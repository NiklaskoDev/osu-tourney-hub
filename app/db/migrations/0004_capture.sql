-- Phase 4: automatic score capture from the osu! API.
-- Times are UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SSZ).

-- Pulls waiting to run: one row per player and beatmap. A lower `priority` runs first (0 = asked
-- for by a person, 1 = automatic, 2 = lowest). Asking again for a queued pair keeps the more
-- urgent priority.
CREATE TABLE capture_queue (
    osu_id       INTEGER NOT NULL,
    beatmap_id   INTEGER NOT NULL,
    gamemode     INTEGER NOT NULL CHECK (gamemode BETWEEN 0 AND 3),
    priority     INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (osu_id, beatmap_id, gamemode)
);
CREATE INDEX capture_queue_order ON capture_queue (priority, requested_at);

-- When a pair was last pulled successfully, so automatic runs skip it inside CAPTURE_REFRESH_HOURS.
CREATE TABLE capture_log (
    osu_id         INTEGER NOT NULL,
    beatmap_id     INTEGER NOT NULL,
    gamemode       INTEGER NOT NULL CHECK (gamemode BETWEEN 0 AND 3),
    last_pulled_at TEXT NOT NULL,
    PRIMARY KEY (osu_id, beatmap_id, gamemode)
);

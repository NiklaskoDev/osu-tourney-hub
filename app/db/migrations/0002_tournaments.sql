-- Phase 1-2: tournaments and who belongs to them. Access checks (phase 1) use tournaments and
-- tournament_members; the rest is filled by the wybin import and manual setup (phase 2).
-- Times are UTC ISO-8601 text (YYYY-MM-DDTHH:MM:SSZ). Every foreign key cascades unless noted.

CREATE TABLE admin_wybin_links (
    osu_id         INTEGER PRIMARY KEY REFERENCES users (osu_id) ON DELETE CASCADE,
    wybin_user_id  INTEGER NOT NULL UNIQUE,
    last_checked_at TEXT
);

CREATE TABLE tournaments (
    id            INTEGER PRIMARY KEY,
    wybin_id      INTEGER UNIQUE,
    slug          TEXT,
    name          TEXT NOT NULL,
    acronym       TEXT,
    gamemode      INTEGER NOT NULL CHECK (gamemode BETWEEN 0 AND 3),
    is_solo       INTEGER NOT NULL CHECK (is_solo IN (0, 1)),
    team_size_min INTEGER,
    team_size_max INTEGER,
    freemod_rule  TEXT NOT NULL DEFAULT 'none'
                  CHECK (freemod_rule IN ('none', 'each_player_mod', 'two_distinct_mods')),
    scoring       TEXT NOT NULL DEFAULT 'v2' CHECK (scoring IN ('v2', 'v1', 'lazer')),
    owner_osu_id  INTEGER REFERENCES users (osu_id) ON DELETE SET NULL,
    source        TEXT CHECK (source IN ('wybin', 'manual')),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'hidden')),
    final_placing TEXT,
    our_team_id   INTEGER REFERENCES teams (id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    archived_at   TEXT,
    last_synced_at TEXT
);

CREATE TABLE teams (
    id             INTEGER PRIMARY KEY,
    tournament_id  INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    wybin_team_id  INTEGER,
    name           TEXT NOT NULL,
    is_ours        INTEGER NOT NULL DEFAULT 0 CHECK (is_ours IN (0, 1)),
    seed           INTEGER,
    UNIQUE (tournament_id, wybin_team_id)
);

CREATE TABLE team_players (
    team_id             INTEGER NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    osu_id              INTEGER NOT NULL,
    username            TEXT NOT NULL,
    timezone_iana       TEXT,
    timezone_offset_min INTEGER,
    is_captain          INTEGER NOT NULL DEFAULT 0 CHECK (is_captain IN (0, 1)),
    PRIMARY KEY (team_id, osu_id)
);

-- osu_id is deliberately not a foreign key to users: roster entries exist before the first login.
CREATE TABLE tournament_members (
    tournament_id     INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    osu_id            INTEGER NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('captain', 'co_captain', 'player', 'guest')),
    is_substitute     INTEGER NOT NULL DEFAULT 0 CHECK (is_substitute IN (0, 1)),
    temp_captain_until TEXT,
    source            TEXT CHECK (source IN ('wybin', 'manual')),
    added_by          INTEGER,
    PRIMARY KEY (tournament_id, osu_id)
);
CREATE INDEX tournament_members_osu_id ON tournament_members (osu_id);

CREATE TABLE stages (
    id               INTEGER PRIMARY KEY,
    tournament_id    INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    wybin_stage_id   INTEGER UNIQUE,
    name             TEXT NOT NULL,
    sort_order       INTEGER NOT NULL,
    start_at         TEXT,
    end_at           TEXT,
    best_of          INTEGER,
    bans             INTEGER,
    protects         INTEGER,
    is_qualifier     INTEGER NOT NULL CHECK (is_qualifier IN (0, 1)),
    lineup_size      INTEGER,
    pool_released    INTEGER NOT NULL DEFAULT 0 CHECK (pool_released IN (0, 1))
);

CREATE TABLE pool_maps (
    id               INTEGER PRIMARY KEY,
    stage_id         INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    slot             TEXT NOT NULL,
    bracket          TEXT NOT NULL,
    required_mods    TEXT NOT NULL,
    is_freemod       INTEGER NOT NULL CHECK (is_freemod IN (0, 1)),
    is_tiebreaker    INTEGER NOT NULL CHECK (is_tiebreaker IN (0, 1)),
    sort_order       INTEGER NOT NULL,
    beatmap_id       INTEGER,
    beatmapset_id    INTEGER,
    artist           TEXT,
    title            TEXT,
    version          TEXT,
    mapper           TEXT,
    star_rating      REAL,
    bpm              REAL,
    length_s         INTEGER,
    cs               REAL,
    ar               REAL,
    od               REAL,
    hp               REAL,
    wybin_entry_id   INTEGER,
    UNIQUE (stage_id, slot)
);
CREATE INDEX pool_maps_beatmap_id ON pool_maps (beatmap_id);

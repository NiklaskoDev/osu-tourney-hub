# 001 — osu! tourney hub (new project)

Status: draft (decisions D1–D14 decided 2026-11-12; plan awaiting final approval)
Type: new project
Version impact: minor — first version `0.1.0` (pre-stable `0.x`, see `docs/ai/versioning.md`). Later phases are minor bumps (`0.2.0`, …). Releasing stays the programmer's call.

## Goal
A self-hosted website in one Docker container (Unraid) for an osu! tournament team: osu! login; admins (by osu! user id from the container settings) set up or import tournaments from wybin.xyz, and the tournaments an admin signed up for on wybin are picked up automatically; each tournament has its own space visible only to its members, with captains, a mappool sheet with scores, notes, averages and lineup suggestions, an availability sheet (UTC or local time), an MP lobby skimmer that saves lobbies where pool maps were played, and matchup comparison with predictions for picks, bans, results and pickems (statistical by default, AI optional).

Chosen scout features (all in scope): 1 automatic score capture · 2 stage-format and game-mode awareness · 3 match schedule and time planning · 5 match history and prediction review · 6 live match mode · 7 finer team roles · 8 cross-tournament score reuse and archive · 9 sheet import/export · 10 practice coverage tracker.

Out of scope: 4 Discord webhook notifications (declined, retrofit easy). Also not planned: wybin login or writing anything back to wybin, osu! chat/IRC, staff-side tournament tools (refereeing, bracket management), tournaments where the admin is staff only.

## Reuse
- Repo holds only guideline files. Reused as-is or extended: `.gitignore`, `.gitattributes`, `CHANGELOG.md`, `.github/workflows/{ci,release,security}.yml`, `.github/dependabot.yml`, `docs/ai/unraid-template.xml` (copied to `unraid/osu-tourney-hub.xml`), `.env.example` (replaced with the real variables).
- `C:\Users\Niklas\Documents\vibecoding\osumpskimmer\osu_mp_scores.py` is ported (not imported) into `app/clients/osu.py` and `app/services/mp_parse.py`:
  - `get_token()` → `OsuClient.client_token()` (client credentials, scope `public`, cached until `expires_in - 60 s`).
  - `http()` retry rules (403/404 → not found, 429/5xx → exponential backoff, max 5 tries, 30 s timeout) → `OsuClient._request()` on `httpx.AsyncClient`, behind the shared rate limiter.
  - `fetch_match()` paging (`limit=100`, `before=<min event id>`, stop at `first_event_id`) → `OsuClient.fetch_match(mp_id)`; add an `after=<last event id>` variant for live mode.
  - `collect()` score extraction (accuracy ×100, `max_combo`, mods from strings or `{acronym}` objects, `count_miss`/`miss`, `match.pass`, `match.team`) → `mp_parse.extract_game_scores(game, users)`.
  - `match_id()` and `map_id()` link parsers → `app/services/links.py` unchanged in behaviour (plus tests).
  - Range scan (`-r START-END`, every id fetched, 404/403 skipped, parallel workers) → the skimmer's continuous forward scan (D5), now with a persisted cursor, budget and gap records.
- Nothing else exists; every structure below is new because the repo is empty.

## Code layout
Python package `app/` (FastAPI, Jinja templates, htmx), tests in `tests/`. Files ≤ ~400 lines; split when larger.

Root
- `pyproject.toml` — project metadata, **version source of truth** (`[project].version`), ruff and pytest config. `dependencies = []`; runtime pins live in requirements files so CI and Docker install the same thing.
- `requirements.txt` — runtime dependencies, exact pins (`==`).
- `requirements-dev.txt` — `-r requirements.txt` plus `pytest`, `ruff`, exact pins.
- `Dockerfile` — multi-stage build (see Steps, phase 0).
- `.dockerignore` — `.git`, `.github`, `.env*` (keep nothing), `.venv`, `venv`, `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`, `tests`, `docs`, `*.db`, `*.sqlite*`, `data/`.
- `docker/entrypoint.sh` — PUID/PGID handling, drop to non-root, exec uvicorn.
- `unraid/osu-tourney-hub.xml` — Unraid template.
- `unraid/icon.png` — 256×256 icon (simple monogram in the accent colour; no osu! logo, it is trademarked).
- `.env.example` — every variable (see Security).
- `README.md` — what it is, osu! OAuth app setup, `docker build`, `docker run`, Unraid curl install, reverse proxy/HTTPS note, backup note, configuration table.
- `CHANGELOG.md` — replace the placeholder `[0.1.0] - YYYY-MM-DD` block: keep `## [Unreleased]` with `### Added` lines per phase.

`app/`
- `__init__.py` — `__version__` via `importlib.metadata.version("osu-tourney-hub")`.
- `main.py` — `create_app(settings, clock, http_transport=None)`: middleware, routers, static, templates, lifespan (migrate DB, start/stop workers).
- `config.py` — `Settings` dataclass built from env (`Settings.from_env()`), validation, errors listing missing names (never values).
- `clock.py` — `Clock` protocol + `SystemClock`; tests inject `FakeClock`.
- `db/connection.py` — `open_db(path)` (sqlite3, `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=5000`), `transaction()` context manager. All SQL parameterised.
- `db/migrate.py` — applies `db/migrations/NNNN_*.sql` in order, records in `schema_migrations(version, applied_at)`; refuses to start if DB is newer than code.
- `db/migrations/0001_init.sql` … — one file per phase that adds tables (numbers below).
- `repos/*.py` — SQL per aggregate, plain functions taking a connection: `users.py`, `tournaments.py`, `members.py`, `stages.py`, `pool.py`, `scores.py`, `notes.py`, `availability.py`, `lineups.py`, `matches.py`, `lobbies.py`, `predictions.py`, `practice.py`, `jobs.py`, `feeds.py`.
- `clients/http.py` — shared `httpx.AsyncClient` factory (timeouts 10 s connect / 30 s read, `User-Agent: osu-tourney-hub/<version> (+<repo url>)`).
- `clients/ratelimit.py` — async token bucket with priorities (`LIVE > INTERACTIVE > CAPTURE > SCAN`).
- `clients/osu.py` — `OsuClient` (see Interfaces).
- `clients/wybin.py` — `WybinClient` (see Interfaces) + response parsing into dataclasses in `clients/wybin_models.py`.
- `clients/ai.py` — `AiProvider` protocol, `NullProvider`, `AnthropicProvider` (Messages API) and `OpenAICompatibleProvider` (chat completions at `AI_BASE_URL`, default OpenAI) on `httpx`, no SDKs (D4).
- `domain/modes.py` — game modes (0 osu, 1 taiko, 2 fruits/catch, 3 mania), allowed mods per mode, mod bitmask ↔ acronym, `canonical_mods()` (sorted acronyms, drops `NF`, `NM` for empty), bracket name → slot prefix (`Nomod/NoFail→NM`, `Hidden→HD`, `HardRock→HR`, `DoubleTime→DT`, `Freemod→FM`, `Tiebreaker→TB`, else first letters).
- `domain/models.py` — dataclasses shared across services (Score, PoolMap, StageFormat, TeamRef, …).
- `services/links.py` — `match_id()`, `map_id()` from the reference script.
- `services/access.py` — role checks (pure functions).
- `services/wybin_sync.py` — import/sync a tournament; track admin sign-ups.
- `services/score_capture.py` — API score pulls queue + MP auto-fill.
- `services/mp_parse.py` — game/score extraction from osu! match events.
- `services/sheet.py` — best score per player/map/mods, personal and team averages.
- `services/lineup.py` — lineup suggestion.
- `services/coverage.py` — practice coverage grid, targets, flags.
- `services/availability.py` — slot grid, UTC/local conversion, overlay, best-slot finder.
- `services/ical.py` — RFC 5545 VCALENDAR text (hand-written; ~60 lines, no dependency).
- `services/skimmer.py` — continuous forward scan of every new MP id: head tracking, cursor, workers, open-lobby re-checks, gaps, stats (D5).
- `services/prediction.py` — statistical model (per-map win probability, pick/ban simulation, series probability, qualifier seed, pickems).
- `services/history.py` — actual picks/bans/winners, prediction review, opponent habits.
- `services/live.py` — live match follower + in-process pub/sub.
- `services/csv_io.py` — CSV export and import (stdlib `csv`), public Google Sheets link fetched as CSV (D7).
- `services/archive.py` — archive / read-only rules.
- `workers/scheduler.py` — asyncio loop running registered jobs at intervals; stores `last_run_at/last_ok_at/last_error/cursor` in `job_state`; one failing job never stops others; graceful cancel on shutdown.
- `workers/jobs.py` — job definitions: `wybin_track_signups` (6 h), `wybin_sync_active` (WYBIN_POLL_MINUTES), `score_capture_drain` (continuous, rate-limited), `skimmer_head` (60 s), `skimmer_workers` (continuous, `SCANNER_WORKERS` tasks), `skimmer_open_check` (5 min), `skimmer_stats_prune` (1 h), `live_follow` (started per live match).
- `web/deps.py` — `get_db`, `current_user`, `require_login`, `require_admin`, `require_member(tid, min_role)`, `require_writable(tid)`.
- `web/security.py` — security headers middleware (CSP etc.), CSRF check, small in-memory per-IP rate limiter for `/auth/*` and expensive endpoints.
- `web/templating.py` — Jinja env (autoescape on), filters (`fmt_score`, `fmt_acc`, `fmt_dt_utc`, `rel_time`), `render(request, template, ctx)` that adds user, csrf token, nav.
- `web/routes/*.py` — one router per area: `auth.py`, `me.py`, `admin.py`, `dashboard.py`, `tournament.py`, `members.py`, `pool.py`, `lineup.py`, `availability.py`, `schedule.py`, `lobbies.py`, `matches.py`, `pickems.py`, `live.py`, `coverage.py`, `io.py`, `health.py`.
- `templates/base.html`, `templates/layout/{shell,tournament_nav}.html`, `templates/components/*.html` (Jinja macros: button, field, table, tabs, badge, alert, empty_state, dialog, countdown, pagination), `templates/pages/<area>/*.html`, `templates/partials/<area>/*.html` (htmx fragments).
- `static/css/tokens.css` (design tokens), `static/css/app.css` (components), `static/js/htmx.min.js` + `static/js/htmx-ext-sse.js` (vendored, pinned version noted in `static/js/VENDOR.md` with source URL, version, sha256, licence), `static/js/app.js` (local-time rendering, copy-to-clipboard, timezone detection; no inline scripts), `static/icons/sprite.svg` (Lucide subset, ISC licence, noted in `static/icons/LICENSE`).

`tests/`
- `conftest.py` — temp DB, `FakeClock`, `httpx.MockTransport` router for osu!/wybin/AI, logged-in client fixtures per role.
- `fixtures/osu/*.json`, `fixtures/wybin/*.json` — **synthetic** (structure copied from real responses, all names/ids/discord ids invented; no real personal data).
- `test_<area>.py` per area (see Tests).

## Data
SQLite file `/data/app.db` (`DATA_DIR` env, default `/data`). Times stored as UTC ISO-8601 text (`YYYY-MM-DDTHH:MM:SSZ`). Booleans as INTEGER 0/1 with `CHECK`. Every FK `ON DELETE CASCADE` unless noted. Migrations listed with the phase that adds them.

`0001_init.sql` (phase 0–1)
- `schema_migrations(version INTEGER PK, applied_at TEXT NOT NULL)`
- `users(osu_id INTEGER PK, username TEXT NOT NULL, avatar_url TEXT, country TEXT, timezone TEXT /* IANA, user-chosen, nullable */, session_version INTEGER NOT NULL DEFAULT 1, created_at, last_login_at)` — `session_version` bumped on logout-all/delete to invalidate cookies.
- `app_settings(key TEXT PK, value TEXT NOT NULL)`
- `job_state(name TEXT PK, last_run_at, last_ok_at, last_error TEXT, cursor TEXT)`
- `feed_tokens(token_hash TEXT PK /* sha256 of random token */, osu_id INTEGER NOT NULL REFERENCES users, created_at)` — iCal feed access (Decision D8).

`0002_tournaments.sql` (phase 1–2)
- `admin_wybin_links(osu_id INTEGER PK REFERENCES users, wybin_user_id INTEGER NOT NULL UNIQUE, last_checked_at)`
- `tournaments(id INTEGER PK, wybin_id INTEGER UNIQUE, slug TEXT, name TEXT NOT NULL, acronym TEXT, gamemode INTEGER NOT NULL CHECK(gamemode BETWEEN 0 AND 3), is_solo INTEGER NOT NULL, team_size_min INTEGER, team_size_max INTEGER, freemod_rule TEXT NOT NULL DEFAULT 'none' CHECK(freemod_rule IN ('none','each_player_mod','two_distinct_mods')), scoring TEXT NOT NULL DEFAULT 'v2' CHECK(scoring IN ('v2','v1','lazer')), owner_osu_id INTEGER REFERENCES users ON DELETE SET NULL, source TEXT CHECK(source IN ('wybin','manual')), status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','hidden')), final_placing TEXT, our_team_id INTEGER /* FK teams, set after sync */, created_at, archived_at, last_synced_at)`
- `teams(id INTEGER PK, tournament_id INTEGER NOT NULL REFERENCES tournaments, wybin_team_id INTEGER, name TEXT NOT NULL, is_ours INTEGER NOT NULL DEFAULT 0, seed INTEGER, UNIQUE(tournament_id, wybin_team_id))` — solo tournaments: one team per player, `name` = username.
- `team_players(team_id INTEGER REFERENCES teams, osu_id INTEGER NOT NULL, username TEXT NOT NULL, timezone_iana TEXT, timezone_offset_min INTEGER, is_captain INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(team_id, osu_id))` — opponents included (public osu! data + wybin timezone). Discord ids from wybin are **not** stored.
- `tournament_members(tournament_id REFERENCES tournaments, osu_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('captain','co_captain','player','guest')), is_substitute INTEGER NOT NULL DEFAULT 0, temp_captain_until TEXT, source TEXT CHECK(source IN ('wybin','manual')), added_by INTEGER, PRIMARY KEY(tournament_id, osu_id))` — `osu_id` deliberately not an FK to `users` so roster entries exist before first login.
- `stages(id INTEGER PK, tournament_id REFERENCES tournaments, wybin_stage_id INTEGER UNIQUE, name TEXT NOT NULL, sort_order INTEGER NOT NULL, start_at TEXT, end_at TEXT, best_of INTEGER, bans INTEGER, protects INTEGER, is_qualifier INTEGER NOT NULL, lineup_size INTEGER /* manual; wybin has none */, pool_released INTEGER NOT NULL DEFAULT 0)`
- `pool_maps(id INTEGER PK, stage_id REFERENCES stages, slot TEXT NOT NULL /* NM1, FM2, TB */, bracket TEXT NOT NULL, required_mods TEXT NOT NULL /* canonical, '' for NM/FM/TB */, is_freemod INTEGER NOT NULL, is_tiebreaker INTEGER NOT NULL, sort_order INTEGER NOT NULL, beatmap_id INTEGER /* NULL = custom/placeholder */, beatmapset_id INTEGER, artist TEXT, title TEXT, version TEXT, mapper TEXT, star_rating REAL, bpm REAL, length_s INTEGER, cs REAL, ar REAL, od REAL, hp REAL, wybin_entry_id INTEGER, UNIQUE(stage_id, slot))`, index `(beatmap_id)`.

`0003_sheet.sql` (phase 3)
- `scores(id INTEGER PK, beatmap_id INTEGER NOT NULL, osu_id INTEGER NOT NULL, gamemode INTEGER NOT NULL, mods TEXT NOT NULL /* canonical */, score INTEGER NOT NULL, scoring TEXT NOT NULL CHECK(scoring IN ('v2','v1','lazer','unknown')), accuracy REAL NOT NULL /* 0–100 */, max_combo INTEGER, misses INTEGER, passed INTEGER NOT NULL DEFAULT 1, achieved_at TEXT, source TEXT NOT NULL CHECK(source IN ('manual','api','mp','import')), source_ref TEXT NOT NULL /* api: osu score id; mp: '<match>:<game>'; manual/import: uuid4 */, source_url TEXT, entered_by INTEGER, tournament_id INTEGER REFERENCES tournaments ON DELETE SET NULL /* manual/import only; drives the "from <tournament>" hint; approved 2026-11-13 */, created_at, UNIQUE(source, source_ref, osu_id))`, indexes `(beatmap_id, osu_id, mods)`, `(osu_id)`. Keyed by beatmap + mods, not by tournament → feature 8 reuse is automatic.
- `notes(id INTEGER PK, tournament_id REFERENCES tournaments, pool_map_id REFERENCES pool_maps, author_osu_id INTEGER NOT NULL, visibility TEXT NOT NULL CHECK(visibility IN ('private','team')), body TEXT NOT NULL CHECK(length(body) <= 2000), updated_at)`
- `lineups(id INTEGER PK, tournament_id REFERENCES tournaments, stage_id REFERENCES stages, match_id INTEGER /* FK matches, nullable */, status TEXT NOT NULL CHECK(status IN ('draft','final')), updated_by INTEGER, updated_at, UNIQUE(stage_id, match_id))`
- `lineup_entries(lineup_id REFERENCES lineups, pool_map_id REFERENCES pool_maps, osu_id INTEGER NOT NULL, mods TEXT NOT NULL DEFAULT '', PRIMARY KEY(lineup_id, pool_map_id, osu_id))`

`0004_capture.sql` (phase 4)
- `capture_queue(osu_id INTEGER, beatmap_id INTEGER, gamemode INTEGER, priority INTEGER NOT NULL, requested_at, attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(osu_id, beatmap_id, gamemode))`
- `capture_log(osu_id INTEGER, beatmap_id INTEGER, gamemode INTEGER, last_pulled_at, PRIMARY KEY(osu_id, beatmap_id, gamemode))` — skip re-pulls inside `CAPTURE_REFRESH_HOURS`.

`0005_schedule.sql` (phase 5)
- `matches(id INTEGER PK, tournament_id REFERENCES tournaments, stage_id REFERENCES stages, wybin_match_id INTEGER UNIQUE, label TEXT, team1_id INTEGER REFERENCES teams, team2_id INTEGER REFERENCES teams, scheduled_at TEXT, score1 INTEGER, score2 INTEGER, mp_match_id INTEGER, status TEXT NOT NULL CHECK(status IN ('scheduled','live','done','cancelled')), updated_at)`, index `(tournament_id, scheduled_at)`.
- `availability(tournament_id REFERENCES tournaments, osu_id INTEGER NOT NULL, slot_start TEXT NOT NULL /* UTC, 30-min aligned */, status TEXT NOT NULL CHECK(status IN ('yes','maybe','no')), PRIMARY KEY(tournament_id, osu_id, slot_start))`

`0006_lobbies.sql` (phase 6)
- `lobbies(mp_match_id INTEGER PK, name TEXT, started_at, ended_at, first_seen_at NOT NULL, last_scanned_at, is_complete INTEGER NOT NULL DEFAULT 0, discovered_by TEXT CHECK(discovered_by IN ('scan','wybin','manual')))` — a link is stored once (PK); only lobbies with ≥ 1 pool hit are stored.
- `scan_open(mp_match_id INTEGER PK, first_seen_at TEXT NOT NULL, last_event_id INTEGER, events_fetched INTEGER NOT NULL DEFAULT 0, next_check_at TEXT NOT NULL)` — matches seen while still open, waiting for their final scan (independent of hits so far).
- `scan_gaps(id INTEGER PK, start_id INTEGER NOT NULL, end_id INTEGER NOT NULL, reason TEXT NOT NULL CHECK(reason IN ('skipped_backlog','admin_skip')), created_at TEXT NOT NULL, backfilled_until INTEGER /* last id backfilled, NULL = none */, CHECK(start_id <= end_id))` — ranges deliberately not scanned; single 404/403 ids are only counted, not stored.
- `scan_stats(minute TEXT PK /* UTC YYYY-MM-DDTHH:MM */, ids_scanned INTEGER, not_found INTEGER, matches_with_hits INTEGER, requests INTEGER, head_id INTEGER)` — rolling 24 h, pruned hourly; feeds the admin status.
- Scanner cursor: `job_state('skimmer').cursor` = highest id below which every id is processed (contiguous); `job_state('skimmer_head').cursor` = latest known id.
- `lobby_pool_hits(mp_match_id REFERENCES lobbies, game_id INTEGER NOT NULL, pool_map_id REFERENCES pool_maps, played_at, mods TEXT, PRIMARY KEY(mp_match_id, game_id, pool_map_id))`
- `lobby_players(mp_match_id REFERENCES lobbies, tournament_id REFERENCES tournaments, osu_id INTEGER NOT NULL, team_id INTEGER REFERENCES teams, PRIMARY KEY(mp_match_id, tournament_id, osu_id))` — only players registered in that tournament.
- `practice_targets(id INTEGER PK, tournament_id REFERENCES tournaments, pool_map_id REFERENCES pool_maps, osu_id INTEGER /* NULL = whole team */, target_score INTEGER, target_acc REAL, note TEXT CHECK(length(note) <= 500), set_by INTEGER, updated_at, UNIQUE(pool_map_id, osu_id))`

`0007_predictions.sql` (phase 7–8)
- `plan_items(id INTEGER PK, match_id REFERENCES matches, kind TEXT CHECK(kind IN ('ban','pick','protect')), pool_map_id REFERENCES pool_maps, position INTEGER NOT NULL, note TEXT CHECK(length(note) <= 300), set_by INTEGER, UNIQUE(match_id, kind, position))`
- `predictions(id INTEGER PK, match_id REFERENCES matches, kind TEXT CHECK(kind IN ('stat','ai')), created_at, win_prob_team1 REAL NOT NULL, expected_score TEXT /* e.g. '5-3' */, payload TEXT NOT NULL /* JSON snapshot: per-map probs, predicted bans/picks */, model_version TEXT NOT NULL)` — JSON here because it is a frozen snapshot read whole, never queried by field.
- `match_events(match_id REFERENCES matches, seq INTEGER NOT NULL, kind TEXT CHECK(kind IN ('protect','ban','pick','tiebreaker')), team_side INTEGER CHECK(team_side IN (1,2)), pool_map_id REFERENCES pool_maps ON DELETE SET NULL, beatmap_id INTEGER, winner_side INTEGER, team1_score INTEGER, team2_score INTEGER, source TEXT CHECK(source IN ('wybin','mp','manual')), PRIMARY KEY(match_id, seq))`

Deletion/export of a user (security.md): `/me` page — "Download my data" (JSON of own rows) and "Delete my data" (re-auth via fresh osu! login within 5 min; deletes `users` row, own `scores` with `source in ('manual','import')`, `notes`, `availability`, `feed_tokens`; `api`/`mp` scores are public osu! data and are anonymised by dropping `entered_by`; memberships removed).

## Interfaces
### External clients
`OsuClient` (osu! API v2, base `https://osu.ppy.sh/api/v2`)
- `authorize_url(state)` / `exchange_code(code)` — via Authlib (scope `identify` only; user tokens are used once to read `/me` and **not stored**).
- `get_me(user_token) -> OsuUser`
- `client_token()` — client credentials, scope `public`.
- `get_beatmap(beatmap_id) -> BeatmapMeta` (for manual pools).
- `get_user_beatmap_scores(beatmap_id, user_id, mode) -> list[ApiScore]` — `GET /beatmaps/{id}/scores/users/{user}/all?mode=` (all mod combos of that user on the map; ranked/loved maps only, 404 → empty).
- `list_matches(cursor_string=None, active=None) -> (list[MatchSummary], next_cursor_string)` — `GET /matches?limit=50&sort=id_desc[&active=true]` (documented, scope `public`; returns `id, name, start_time, end_time`). Used for the head id and for open-lobby checks only.
- `fetch_match(mp_id, max_pages=MATCH_MAX_PAGES) -> (match, users, events, truncated)`; `fetch_match_after(mp_id, after_event_id)` for live mode and open-lobby re-checks (`after`, `limit=101` documented).
- Errors: `OsuNotFound`, `OsuUnavailable` (after retries), `OsuAuthError`.

`WybinClient` (base `https://wybin.xyz/api/v1`, anonymous, `WYBIN_MIN_INTERVAL_SECONDS` between requests, default 2 s)
- `get_tournament(slug)` → `/tournament/{slug}`; `get_stages(id)`; `get_mappools(id)` → `stages[].modBrackets[].beatmaps[]` (confirmed keys: stage `id,name,startDate,endDate(ms),bestOf,bans,protects,qualifierStage,battleRoyale`; bracket `id,name,mods(str)`; beatmap `beatmapId,beatmapsetId,beatmapOrder,mods(int bitmask),starRating,bpm,totalLength,circleSize,approachRate,overallDifficulty,hpDrain,placeholder,customMap`); `get_teams(id)`; `get_players(id)` (confirmed: `id,timezone("UTC+8"),timezoneIANA,temporaryTeamCaptain,qualifierSeed,user.id,user.userOsu.{id,username,flag,pp,rank,avatar_url}`); `get_matches(id)` → `stages[].matches[]` (confirmed for solo: `id,label,playerOne,playerTwo,opponentOne,opponentTwo,date(ms),time,opponentOneScore,opponentTwoScore,opponentOneBans[],opponentTwoBans[],multiplayerLink,rescheduled`); `get_user_detailed(wybin_user_id)` → `participantTournaments`.
- Parsers tolerate missing/extra keys; unknown shapes raise `WybinFormatError` (logged with endpoint name, tournament id only). Team-tournament shapes (`opponentOne` team object, `/tournament-teams`) are **not yet confirmed** — phase 2 step 1 records one real response each, then synthesises fixtures.
- Front-page `gamemode` is 0–3; tiebreaker = bracket `mods == "Tiebreaker"`; freemod = `"Freemod"`; `pool_released` = stage has ≥1 non-placeholder beatmap.

### Web routes (all HTML unless noted; state-changing = POST with CSRF; every `/t/{tid}/…` route uses `require_member`, non-members get 404)
| Route | Who | Does |
|---|---|---|
| `GET /healthz` (JSON) | anyone | `{"status":"ok","version"}`; 503 if DB unreadable |
| `GET /auth/login`, `GET /auth/callback`, `POST /auth/logout` | anyone | osu! OAuth; callback upserts `users`, sets session `{osu_id, session_version, csrf}` |
| `GET /me`, `POST /me/timezone`, `GET /me/export` (JSON), `POST /me/delete`, `POST /me/feed-token` | user | profile, timezone, data export/delete, iCal token (shown once) |
| `GET /` | user | tournaments the user can see; admins also see "Add tournament" |
| `GET /admin/scanner` (+ htmx partial refreshed every 15 s), `POST /admin/scanner/{pause,resume}`, `POST /admin/scanner/gaps/{id}/backfill` | admin | scanner status: cursor id, head id, backlog, ids created/min vs scanned/min (15 min and 1 h), requests/min, open lobbies tracked, gaps, last error |
| `GET /admin`, `POST /admin/wybin-link`, `POST /admin/tournaments/import` (wybin URL or slug), `POST /admin/tournaments` (manual), `POST /admin/tournaments/{tid}/{archive,unarchive,hide,sync}`, `POST /admin/tournaments/{tid}/delete` (confirm by typing acronym) | admin | admin console, job status, scanner and AI status |
| `GET /t/{tid}` | member | overview: next match countdown, current stage, recent lobbies |
| `GET/POST /t/{tid}/members` | captain+ (edit), member (view) | roles, substitute flag, temp captain until, add guest by osu id/username |
| `GET /t/{tid}/settings`, `POST …` | captain+ | lineup size per stage, freemod rule, scoring, our team, final placing (admin) |
| `GET /t/{tid}/pool?stage=` | member | mappool sheet |
| `POST /t/{tid}/scores`, `POST /t/{tid}/scores/{id}/delete` | player+ (own), captain+ (any member) | manual score (score, acc, misses, combo, mods for FM, optional link) |
| `POST /t/{tid}/notes`, `POST /t/{tid}/notes/{id}/delete` | player+ (own notes) | note per map, private/team |
| `POST /t/{tid}/capture/refresh` | player+ | queue own API pulls (once per 10 min per user) |
| `GET /t/{tid}/lineup?stage=&match=`, `POST /t/{tid}/lineup/copy-suggestion`, `POST /t/{tid}/lineup` | view: member · edit: captain+ | suggestion vs saved lineup |
| `GET /t/{tid}/availability?tz=utc|local&week=`, `POST /t/{tid}/availability` (htmx cell toggle / drag range) | member (own row), captain+ (any) | grid |
| `GET /t/{tid}/schedule`, `GET /t/{tid}/schedule/slots?match=` | member | upcoming matches, best-slot finder |
| `GET /feeds/{token}.ics` (text/calendar) | token holder | matches of all the user's tournaments |
| `GET /t/{tid}/lobbies?stage=&player=` , `POST /t/{tid}/lobbies` (manual link) | member | scanned links, players found, maps hit |
| `GET /t/{tid}/coverage?stage=`, `POST /t/{tid}/practice-targets` | view: member · edit: captain+ | coverage grid |
| `GET /t/{tid}/matches/{mid}` | member | compare + prediction + captain plan |
| `POST /t/{tid}/matches/{mid}/plan` | captain+ | planned protects/bans/picks |
| `POST /t/{tid}/matches/{mid}/predict-ai` | captain+ | AI prediction (only when enabled; rate-limited) |
| `GET /t/{tid}/matches/{mid}/review` | member | actual vs predicted |
| `GET /t/{tid}/opponents/{team_id}` | member | opponent habits |
| `GET /t/{tid}/pickems?stage=` | member | pickem predictions for the stage |
| `GET /t/{tid}/live/{mid}`, `GET /t/{tid}/live/{mid}/stream` (SSE), `POST /t/{tid}/live/{mid}/{start,stop}` | member / captain+ | live mode |
| `GET /t/{tid}/export/{pool,lineup,availability}.csv`, `POST /t/{tid}/import` (CSV file or Sheets link) | member / captain+ | sheet I/O |

Roles (`services/access.py`): `admin` (env) ⊃ `captain` = `co_captain` = active `temp_captain_until > now` ⊃ `player` ⊃ `guest` (read-only: may read team notes but writes no notes, no availability row, excluded from lineups). `is_substitute` players are excluded from lineup suggestions unless the captain ticks "include substitutes". Archived tournament → every write route returns 409 with "This tournament is archived" (admins can unarchive). Admins see all tournaments.

### Core service functions (pure, unit-tested)
- `sheet.best_scores(scores, scoring) -> dict[(osu_id, pool_map_id), Score]` — highest score per player/map; for FM maps the best per mod set is kept and the overall best shown; NM maps accept only `mods==''`; mod maps accept only `mods == required_mods` (extra `NF` ignored; `HD` on HR/DT maps accepted only if tournament allows — off by default).
- `sheet.personal_average(...)`, `sheet.team_average(...)` — mean of best scores over maps with a score; also coverage `n/total` and mean accuracy. Averages per bracket and overall.
- `lineup.suggest(pool, best, members, lineup_size, freemod_rule, available_ids=None) -> dict[pool_map_id, list[(osu_id, mods)]]` — per map top-`lineup_size` by score; FM respects `freemod_rule` (greedy then swap to satisfy); ties → higher accuracy → more recent; maps with fewer than `lineup_size` scorers are flagged `incomplete`. Optional filter by availability at match time.
- `prediction.map_win_prob(our_expected, opp_expected) -> float` — logistic on relative difference: `1/(1+exp(-(a-b)/(K*(a+b)/2)))`, `K = 0.08` (named constant, tuned in tests to give ~0.73 at 8 % lead); `None` when either side lacks data (shown as "No data").
- `prediction.expected_team_score(team, pool_map)` — sum of top-k best scores of that team's players (scores from API, MP, manual); fallback per missing player = player's median ratio to team on other maps (marked "estimated").
- `prediction.simulate_series(map_probs, best_of, bans, protects, first_pick) -> SeriesResult` — protects: each team protects its highest-advantage map; bans: each bans the opponent's highest-advantage unprotected map; picks alternate greedily (own highest advantage), adjusted by opponent habit weights when history exists; TB at `(n-1):(n-1)`; exact probability via DP over remaining picks; averaged over both first-pick orders. Returns `win_prob`, `expected_score`, predicted protects/bans/picks per side.
- `prediction.qualifier_seed(stage, teams_expected) -> (seed, n_teams)` — rank teams by sum over maps of normalised score (score / map median); teams with < 50 % maps covered are left out and counted.
- `prediction.pickems(stage) -> list[MatchPick]` — `simulate_series` for every scheduled match of the stage.
- `history.review(match) -> ReviewRow[]`, `history.habits(team_id) -> {bracket: pick_rate, ban_rate, first_pick_rate}`.
- `availability.best_slots(tournament, match, window_start, window_end) -> list[SlotScore]` — 30-min slots; score = our players `yes`×2 + `maybe`×1 for the lineup-size best; opponents scored by their wybin timezone with a fixed "reasonable hours" window `10:00–23:00` local; returns top 10.

## Security and user data
Secrets (names only): `OSU_CLIENT_SECRET`, `SESSION_SECRET`, `AI_API_KEY`. Non-secret config: `OSU_CLIENT_ID`, `PUBLIC_URL`, `ADMIN_OSU_IDS`.

`.env.example` / Unraid template / `docker run` variables (all in sync):
| Name | Default | Notes |
|---|---|---|
| `OSU_CLIENT_ID` | — required | osu! OAuth app id |
| `OSU_CLIENT_SECRET` | — required, masked | |
| `PUBLIC_URL` | — required | e.g. `https://tourney.example.com`; callback = `PUBLIC_URL/auth/callback`; all redirects built from it (never from Host header) |
| `SESSION_SECRET` | — required, masked | ≥ 32 chars; app refuses to start otherwise |
| `ADMIN_OSU_IDS` | — required | comma-separated osu! user ids |
| `PUID` / `PGID` | 99 / 100 | |
| `OSU_API_RATE_PER_MIN` | 60 | hard cap 60 (D5) |
| `WYBIN_POLL_MINUTES` | 30 | min 10 |
| `SCANNER_ENABLED` | true | |
| `SCANNER_BUDGET_PER_MIN` | 30 | osu! requests/min the scanner may use, inside `OSU_API_RATE_PER_MIN`; must be < it (startup error otherwise) |
| `SCANNER_WORKERS` | 4 | parallel fetch tasks (1–8); they share the budget |
| `SCANNER_MAX_BACKLOG` | 5000 | ids behind head before skip-ahead (≈ 1 day at measured volume) |
| `SCANNER_OPEN_MAX_HOURS` | 24 | open lobbies are finalised after this |
| `CAPTURE_REFRESH_HOURS` | 24 | |
| `LIVE_POLL_SECONDS` | 10 | min 5 |
| `AI_PROVIDER` | empty = off | per D4 |
| `AI_API_KEY` | empty, masked | |
| `AI_MODEL` | empty | required when provider set; no model id hard-coded |
| `AI_BASE_URL` | empty | only for an OpenAI-compatible/local endpoint |
| `LOG_LEVEL` | info | |

Auth and sessions
- osu! OAuth2 authorization code flow via **Authlib** Starlette client (state + PKCE). Scope `identify`. Only `id, username, avatar_url, country_code` kept.
- Session: Starlette `SessionMiddleware` (signed cookie, `itsdangerous`), `HttpOnly`, `SameSite=Lax`, `Secure` when `PUBLIC_URL` is https, max age 14 days. Cookie holds `osu_id`, `session_version`, `csrf`; `current_user` rejects a cookie whose `session_version` ≠ DB (logout everywhere, deletion).
- Admin = `osu_id in ADMIN_OSU_IDS`, evaluated per request (removing an id from the env revokes on restart).
- CSRF: per-session random token (`secrets.token_urlsafe(32)`), sent as hidden `csrf_token` field and as `X-CSRF-Token` header via `hx-headers` on `<body>`; compared with `secrets.compare_digest` on every POST. Plus `SameSite=Lax`.
- Authorisation on the server for every route via `web/deps.py`; deny by default; tournament existence not revealed to non-members (404).
- Sensitive action re-auth: `/me/delete` and admin tournament delete require a login within the last 5 minutes (session `auth_at`), else redirect to `/auth/login?next=`.
- iCal: random 32-byte token, only its sha256 stored, revocable on `/me`; feed contains only match labels, team names and times.

Headers (middleware): `Content-Security-Policy: default-src 'self'; img-src 'self' https://a.ppy.sh https://assets.ppy.sh data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://osu.ppy.sh`; `X-Content-Type-Options: nosniff`; `Referrer-Policy: same-origin`; `Strict-Transport-Security` when https. htmx configured via `<meta name="htmx-config" content='{"includeIndicatorStyles":false,"allowEval":false}'>` so no inline styles/scripts are needed.

Input validation: every form parsed into a typed model (FastAPI `Form` + explicit checks): score 0–2 000 000 000 (int), accuracy 0–100, misses ≥ 0, mods must be in the mode's allowed set, note ≤ 2000 chars, osu! ids positive ints, links parsed with `links.py` and rebuilt from the id (never stored raw). CSV import: ≤ 1 MB, ≤ 5000 rows, UTF-8, header mapping required; Google Sheets link: only `https://docs.google.com/spreadsheets/d/<id>` accepted, fetched as `…/export?format=csv&gid=<gid>` (no other host → no SSRF).

Personal data touched: osu! id, username, avatar URL, country (public osu! data); user-chosen timezone; wybin timezones of tournament players (public on wybin, needed for slot finder); availability; notes. Not stored: Discord ids, emails, osu! user tokens. Logs carry osu ids and tournament ids only, never note text, tokens or URLs with tokens (iCal path is logged as `/feeds/<redacted>.ics`). AI prompts (if enabled) contain scores, pool data and team labels; osu! usernames are replaced by "Player A/B…" unless an admin enables the `ai_share_usernames` setting in `app_settings` (default off) (D4).

Rate limiting: in-memory per-IP limiter: `/auth/*` 20/min, `/t/*/predict-ai` 5/hour per user, `/t/*/import` 10/hour, `/capture/refresh` 1 per 10 min per user. Errors shown to users are generic; details go to the server log.

## UI
No design system existed → this one was approved (D11). The phase 0 `/styleguide` page is checked before further screens are built.

Character: a compact team tool — dense tables, calm neutral surfaces, one accent. Light theme by default; dark theme via `prefers-color-scheme` (dark grey, not black).

Tokens (`static/css/tokens.css`)
- Neutral (light): `--bg #F6F6F4`, `--surface #FFFFFF`, `--surface-2 #EFEFEC`, `--border #DADAD5`, `--text #1E1F22`, `--text-muted #5B5D63` (6.4:1 on white).
- Neutral (dark): `--bg #17181B`, `--surface #1F2024`, `--surface-2 #26282D`, `--border #34363C`, `--text #E8E8E6`, `--text-muted #A3A5AB`.
- Accent (actions, focus, current selection only): `--accent #B0285F` light (5.9:1 on white) / `#E46A9A` dark; `--accent-contrast #FFFFFF`. A muted nod to osu! pink without neon.
- State (with text label or icon, never colour alone): `--success #2D7A4B`, `--warning #9A5B00`, `--danger #B3261E`; dark variants `#5DBB83`, `#E0A447`, `#EF7A72`.
- Win/loss in predictions: success/danger text with "W"/"L" and the percentage.
- Type: one family, system stack `system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif`; numbers `font-variant-numeric: tabular-nums`; ids/links `ui-monospace, "Cascadia Mono", Consolas, monospace`. Scale: 12 / 14 (body in tables) / 16 (body) / 20 / 24 / 32. Weights 400 and 600.
- Spacing: 4 / 8 / 12 / 16 / 24 / 32 / 48. Radius: 4 (inputs, buttons, badges), 6 (panels, dialogs). Shadows: only on dialogs and menus.
- Motion: 150 ms for state changes (cell toggle, live score update highlight); none under `prefers-reduced-motion`.

Shell: top bar (app name, tournament switcher, user menu); tournament pages have a left nav (Overview, Pool, Lineup, Availability, Schedule, Matches, Pickems, Lobbies, Coverage, Members, Settings) that collapses to a select at < 768 px. Content left-aligned, max width 1280 px for tables, 72ch for text.

Components (Jinja macros + CSS): button (primary, secondary, danger, text-only; one primary per view), field (label, input/select/textarea, hint, inline error), data table (sticky header and first column, horizontal scroll inside its container, row hover, numeric right-aligned), stage tabs, badge (only for role and map slot, neutral style, slot text like `HD2`), alert (info/success/warning/error, with recovery text), empty state (one sentence + next action), dialog (native `<dialog>`, Esc closes, focus trapped), countdown (`<time datetime>` rendered by `app.js`), availability cell (yes/maybe/no via pattern + label, keyboard toggle with Space), pagination, copy button.

Screens and states: each list/table has loading (htmx indicator text "Loading…"), empty (e.g. pool not released: "The pool for this stage is not published on wybin yet. It will appear here automatically."), error (wybin/osu! unreachable: "wybin did not respond. Data shown is from <time>. Try again later."), success (inline confirmation next to the changed row), disabled (archived tournament: write controls disabled with tooltip text). Touch targets ≥ 44 px; 360 px wide works (tables scroll horizontally, nav becomes select).

Copy: sentence case, specific labels ("Save score", "Copy suggested lineup", "Import from wybin"), no filler.

## Steps
Each phase is one branch `feat/<phase-slug>`, one PR, ends with tests, ruff clean, Docker build green. Do them in order. After each phase: add `CHANGELOG.md` lines under `[Unreleased]`.

### Phase 0 — scaffold, container, CI (`feat/scaffold`)
1. `pyproject.toml` (`name = "osu-tourney-hub"`, `version = "0.1.0"`, `requires-python = ">=3.13"`, `dependencies = []`, setuptools package discovery `app*`, package data templates/static/migrations; `[tool.ruff]` line length 100, select `E,F,I,B,UP,S`; `[tool.pytest.ini_options]` testpaths `tests`).
2. `requirements.txt` with exact pins (latest stable at implementation time; set approved in D2): `fastapi`, `uvicorn`, `jinja2`, `httpx`, `authlib`, `itsdangerous`, `python-multipart`, `tzdata`. `requirements-dev.txt`: `-r requirements.txt`, `pytest`, `ruff`.
3. `app/config.py`, `app/clock.py`, `app/db/connection.py`, `app/db/migrate.py`, `0001_init.sql`, `app/main.py`, `web/routes/health.py`, `web/security.py` (headers only), `web/templating.py`, `templates/base.html`, `static/css/{tokens,app}.css`, vendored htmx + sse ext + `VENDOR.md`, Lucide sprite + licence, `static/js/app.js`.
4. A static `/styleguide` page (admin only) rendering every component in all states — used to approve the look before building screens.
5. `Dockerfile`: stage `build` from `python:3.13.<patch>-slim-bookworm` (exact tag) → `python -m venv /opt/venv`, `pip install --no-cache-dir -r requirements.txt`, `pip install --no-deps .`; stage `runtime` same base → copy `/opt/venv`, `docker/entrypoint.sh`; create user `app` (uid 99 gid 100 defaults); `ENV PATH=/opt/venv/bin:$PATH DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`; `VOLUME /data`; `EXPOSE 8080`; `HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"`; `ENTRYPOINT ["/entrypoint.sh"]`. Image must build with plain `docker build -t osu-tourney-hub .`.
6. `docker/entrypoint.sh` (POSIX sh, `set -eu`): if running as root → `groupmod`/`usermod` the `app` user to `PGID`/`PUID` (validate numeric), `chown app:app /data` and its contents only if owner differs, then `exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips '*' --no-server-header`; if not root → exec uvicorn directly. (`setpriv` ships with util-linux in Debian slim; no extra package.) See D10.
7. `.dockerignore`, `.env.example`, `unraid/osu-tourney-hub.xml` (from `docs/ai/unraid-template.xml`: Name `osu-tourney-hub`, Repository `ghcr.io/niklaskodev/osu-tourney-hub:latest`, TemplateURL `https://raw.githubusercontent.com/NiklaskoDev/osu-tourney-hub/main/unraid/osu-tourney-hub.xml`, Category `Tools: Other:`, WebUI port 8080, Data `/data` → `/mnt/user/appdata/osu-tourney-hub`, one `<Config>` per variable in the table; secrets `Mask="true"` empty; `Display="advanced"` for tuning variables), `unraid/icon.png`.
8. `README.md` with: osu! OAuth app creation (callback URL), `docker build -t osu-tourney-hub .`, full `docker run -d --name osu-tourney-hub -p 8080:8080 -v /mnt/user/appdata/osu-tourney-hub:/data -e PUID=99 -e PGID=100 -e OSU_CLIENT_ID=… -e OSU_CLIENT_SECRET=… -e PUBLIC_URL=… -e SESSION_SECRET=… -e ADMIN_OSU_IDS=… ghcr.io/niklaskodev/osu-tourney-hub:latest`, the Unraid curl line `curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-osu-tourney-hub.xml https://raw.githubusercontent.com/NiklaskoDev/osu-tourney-hub/main/unraid/osu-tourney-hub.xml` + "Docker tab → Add Container → Template → osu-tourney-hub", reverse proxy/HTTPS requirement, backup of `/data`, local dev (`python -m venv .venv`, `pip install -r requirements-dev.txt -e .`, run command).
9. CI: in `ci.yml` set Python `3.13`, replace `pip install ruff pytest` + requirements line with `pip install -r requirements-dev.txt`, add `ruff format --check .`. In `release.yml` enable the version check step with `python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"`. `dependabot.yml`: add `pip` (directory `/`) and `docker` (directory `/`) weekly.
10. Update the `CLAUDE.md` Project block (text in "CLAUDE.md Project block" below).
11. Tests: `test_config.py` (missing required vars → error naming them; short `SESSION_SECRET` rejected; `ADMIN_OSU_IDS` parsing incl. spaces/invalid), `test_migrate.py` (fresh DB gets all migrations; re-run is a no-op; DB newer than code → refuses), `test_health.py` (200 + version; 503 with broken DB), `test_security_headers.py` (CSP etc. present). Manual: `docker build` and `docker run` with dummy env → `/healthz` 200, container user is non-root, `/data/app.db` owned by PUID:PGID.

### Phase 1 — login, roles, access (`feat/auth-roles`)
1. `clients/http.py`, `clients/ratelimit.py`, `clients/osu.py` (OAuth parts + `get_me`; port `http()` retry rules).
2. `web/routes/auth.py` (login/callback/logout, `next` param only accepts relative paths starting with `/`), `web/deps.py`, CSRF in `web/security.py`, per-IP limiter on `/auth/*`.
3. `0002_tournaments.sql` (tables used by access checks), `repos/{users,members,tournaments}.py`, `services/access.py`.
4. `web/routes/{dashboard,me,members}.py` + templates: dashboard list, `/me` (timezone select with browser-detected default, export, delete, logout everywhere), members page (role change, substitute toggle, temp captain until date-time, add guest by osu! id — resolved via `OsuClient` client credentials `GET /users/{id}`, remove member). Only a captain or an admin may give or take the `captain` role (co-captains and temporary captains cannot; amended 2026-11-13). Captain cannot demote the last captain; temp captain expiry is evaluated at request time.
5. Tests (`test_auth.py`, `test_access.py`, `test_members.py`, `test_me.py`): callback with mocked osu! creates user and session; bad `state` rejected; open redirect via `next=https://evil` ignored; admin from env sees admin pages, others 403; non-member gets 404 on `/t/{tid}`; guest cannot POST anything; player can't change roles; captain/co-captain/active temp captain can change the roster, but only a captain or admin can give or take the captain role; expired temp captain cannot; substitute flag persists; CSRF missing/wrong → 403; logout-all invalidates old cookie; delete requires recent login and removes own rows; export contains only own data.

### Phase 2 — tournaments, wybin import and tracking, game modes (`feat/wybin`)
1. Record one real response each of `/tournament/{slug}`, `/tournament-teams/{id}` and `/tournament-matches/{id}` for a **team** tournament (≤ 5 requests total, `User-Agent` set), note field names in `clients/wybin_models.py` docstring, then write **synthetic** fixtures (invented names/ids, no discord ids). Do not commit the raw responses.
2. `domain/modes.py`, `clients/wybin.py` + models, `services/wybin_sync.py`: `import_tournament(slug_or_url)`, `sync_tournament(tid)` (upsert stages, pool maps, teams, team players; matches are synced in phase 5; mark our team = team containing the admin who imported it or the tracked admin; members from our team roster with `source='wybin'` — manual members are never removed by sync; wybin `temporaryTeamCaptain`/captain flag → `captain` role only on first sync), `track_signups()` (for each `admin_wybin_links`: `/users-detailed/{id}` → import new `participantTournaments`; never re-import a hidden one).
3. Solo tournaments (`soloTournament` true or `maximumTeamSize == 1`): one team per player; our team = the admin's own entry; lineup size fixed 1.
4. Stage format: `best_of`, `bans`, `protects`, `is_qualifier` from wybin; tiebreaker from bracket; `lineup_size` editable in `/t/{tid}/settings` (default `team_size_min` or 1 for solo).
5. Manual tournaments: `/admin` form (name, acronym, mode, solo, team sizes) + stage form + pool map form (slot, beatmap link/id → `OsuClient.get_beatmap` for metadata, required mods by slot prefix).
6. `workers/scheduler.py` + `wybin_track_signups`, `wybin_sync_active` jobs; admin console shows job state and last error (generic text).
7. Archive: `services/archive.py`, admin archive/unarchive with final placing; `require_writable` on every write route from now on.
8. Tests: parsing of synthetic fixtures (mods bitmask → canonical, bracket → slot, TB/FM flags, ms timestamps → UTC, unsorted stages sorted by `startDate`); import creates expected rows; second sync is idempotent; manual members survive sync; sign-up tracking imports new and skips hidden; solo tournament gets teams of one; wybin 5xx → job records error, data unchanged; archived tournament rejects writes with 409; `canonical_mods` per mode (mania key mods allowed, catch has no `FL`? — use mode tables, test each).

### Phase 3 — mappool sheet, averages, lineup suggestion, cross-tournament reuse (`feat/pool-sheet`) → candidate for `0.1.0`
1. `0003_sheet.sql`, `repos/{scores,notes,lineups}.py`, `services/sheet.py`, `services/lineup.py`.
2. `/t/{tid}/pool`: stage tabs; table rows = maps (slot, title/version, SR, BPM, length, CS/AR/OD/HP), columns = members (best score, acc; FM shows mod), then team average; hover/expand row shows all scores with source link and date, notes (own private + team). "Add score" dialog (htmx) with mod select only for FM/TB maps (mode-specific list). Scores from other tournaments on the same beatmap+mods show automatically with a "from <tournament>" hint. Footer rows: personal averages (overall + per bracket, coverage n/total).
3. `/t/{tid}/lineup`: suggestion table (map → players + mods), toggles "include substitutes", "only players available at match time" (inactive until phase 5 — hide until then), "Copy suggested lineup" (captain+) writes `lineups/lineup_entries` as draft; captain can edit cells and mark final; non-captains read only.
4. Tests: `best_scores` (higher score wins, FM per mod, NM rejects mods, NF ignored, ties by acc then date); averages ignore missing maps and report coverage; `suggest` top-k, freemod rules, substitutes excluded by default, incomplete flag; same beatmap+mods in tournament B shows score entered in A; player can edit only own scores/notes; private note invisible to others; captain copy creates draft, player copy → 403; archived → 409; validation errors (acc 101, unknown mod for mode) shown inline and input preserved.

### Phase 4 — automatic score capture (API) and sheet import/export (`feat/capture-io`)
1. `0004_capture.sql`; `OsuClient.get_user_beatmap_scores`; `services/score_capture.py`: `enqueue_for_tournament(tid, priority)` (members × maps of current + next stage, skip `capture_log` within `CAPTURE_REFRESH_HOURS`), `drain(n)` (rate-limited, `CAPTURE` priority), map each API score → `scores` row (`source='api'`, `source_ref=score.id`, `scoring='lazer'` for standardised `total_score` (D3); `legacy_total_score` not stored), only `passed` scores, only ranked/loved (API returns nothing else), any date (D6). Scores with `achieved_at` older than `OLD_SCORE_DAYS = 182` are shown with an "old" marker (text, not colour only) and still count.
2. Triggers: after pool sync when maps change; daily for active tournaments; "Refresh my scores" button; players of the next scheduled opponent automatically at lower priority; plus `POST /t/{tid}/opponents/{team_id}/capture` (captain+, button on the opponent and match pages, once per team per hour) (D9).
3. `services/csv_io.py`: export pool sheet (one row per map × player best: slot, beatmap_id, player, score, acc, misses, mods, source), lineup (map, players, mods), availability (UTC slot, player, status). Import: CSV upload or Google Sheets link (D7) → preview page with column mapping (slot or beatmap_id, player name or osu id, score, acc, misses, mods), validation report, "Import N scores" (captain+); `source='import'`, duplicates (same player/map/mods/score) skipped.
4. Tests: API score mapping (mods objects, acc ×100, misses key variants), dedupe by score id, queue respects refresh window and priorities, 404 map → empty no error, rate limiter never exceeds budget with FakeClock; CSV export headers/rows exact; import rejects > 1 MB, unknown players listed, re-import adds nothing; Sheets link with other host rejected.

### Phase 5 — availability, schedule, best slot, iCal (`feat/schedule`)
1. `0005_schedule.sql`; matches sync from wybin (`date` ms → UTC; teams via player/opponent objects; `multiplayerLink` → `mp_match_id`; score fields).
2. `/t/{tid}/availability`: week grid (rows = 30-min slots, columns = members), toggle UTC/local (local = user's timezone from `/me`, fallback browser); cells stored in UTC; matches overlay as a labelled band across the row; summary column "available: n / lineup size". Keyboard and drag-select range (htmx POST with range).
3. `/t/{tid}/schedule`: upcoming matches with countdown and opponent; past matches with result; "Find a slot" per match → `best_slots` table with our availability count and opponents' local times.
4. `/me/feed-token` + `/feeds/{token}.ics` (`services/ical.py`, UID `match-<id>@osu-tourney-hub`, DTSTART UTC, 1.5 h default duration as named constant).
5. Lineup page: enable "only players available at match time".
6. Tests: UTC/local conversion incl. DST change (Europe/Berlin) and offset-only wybin timezone `UTC+5:30`; storing local selection writes correct UTC slot; overlay at correct slot; `best_slots` ranking deterministic; opponent outside 10–23 local penalised; iCal output validates (CRLF, folded lines ≤ 75 octets, escaping), revoked token → 404; availability filter in lineup suggestion.
7. Deferred from phase 4 (the tables did not exist yet): the automatic capture for the next scheduled opponent (queue their players at a lower priority than `PRIORITY_AUTOMATIC` with `skip_within`, e.g. in the daily job, once `matches` exist), the "Pull scores" button for a team on the match page posting to the existing `POST /t/{tid}/opponents/{team_id}/capture`, and `GET /t/{tid}/export/availability.csv` (UTC slot, player, status) in `services/csv_io.py` with its test.

### Phase 6 — MP lobby skimmer, MP score auto-fill, coverage tracker (`feat/skimmer`)
1. `0006_lobbies.sql`; `services/mp_parse.py` (port `collect()`).
2. `services/skimmer.py` — continuous forward scan of **every** new match id, whatever the lobby name (D5). Runs only when `SCANNER_ENABLED`; pause/resume from `/admin/scanner` (stored in `app_settings`).
   - **Current pool index** `pool_index() -> dict[beatmap_id, list[(tournament_id, pool_map_id)]]`: maps of every released stage of every `active` tournament whose `end_at` is empty or not yet past (the current round, plus the next round once its pool is out). Rebuilt after each pool sync and every 5 min.
   - **Head** (`skimmer_head`, every 60 s, priority SCAN): `list_matches(sort=id_desc)` page 1 → `head_id` = highest id, saved in `job_state('skimmer_head')`. From the pages read down to the cursor (max 3 pages = 150 ids per run), ids whose `end_time` is null are remembered as "known open".
   - **Start point**: when `job_state('skimmer').cursor` is empty (first run), set it to `head_id`. Never backfill history.
   - **Cursor**: `IdDispenser` hands ids `cursor+1 … head_id` in ascending order to workers and tracks completed ids; the persisted cursor is the highest id below which all ids are done (contiguous), written to `job_state('skimmer').cursor` every 10 s and on shutdown. After a crash at most a few ids are processed twice; inserts are idempotent (PKs/UNIQUE), so this is harmless.
   - **Workers** (`SCANNER_WORKERS` asyncio tasks) take an id and run `scan_id(id)`:
     - known open → insert into `scan_open` (`next_check_at = now`) without fetching (it is fetched once after it closes).
     - otherwise `fetch_match(id, max_pages=MATCH_MAX_PAGES=10)`.
     - 404/403 (deleted or private) → count `not_found` in `scan_stats`, done. 429/5xx after the client's retries → id requeued, max 3 attempts, then counted as `error`, logged with the id, done.
     - success → `process_match(match, users, events, discovered_by='scan')`: games whose `beatmap_id` is in `pool_index()` → `lobbies` insert-or-ignore (stored once), `lobby_pool_hits`, `lobby_players` for players registered in that tournament (any team), `scores` for those players (`source='mp'`, `source_ref='<mp>:<game id>'`, `scoring` from the game's `scoring_type`). No hit → nothing stored except stats.
     - `end_time` null → `scan_open` row with `last_event_id` (hits or not; pool maps may be played later).
   - **Open lobbies** (`skimmer_open_check`, every 5 min): page `list_matches(active=true)` (max 20 pages) → active ids. Each `scan_open` row that is no longer active, or older than `SCANNER_OPEN_MAX_HOURS` → `fetch_match_after(id, last_event_id)`, `process_match` on the new events, delete the row (final; lobby `is_complete=1` if stored). Rows still active for > 2 h get an incremental `after` fetch every 30 min so long lobbies never need a huge final paging.
   - **Budget**: a scanner sub-bucket of `SCANNER_BUDGET_PER_MIN` requests/min inside the global `OSU_API_RATE_PER_MIN` bucket at priority SCAN, so live, interactive and capture requests always go first. Every request (head, open check, fetch pages) counts.
   - **Catch-up**: while `head_id - cursor > 0` workers use the full scanner budget (at the measured volume about 25 ids/min net catch-up with the default 30). If the backlog exceeds `SCANNER_MAX_BACKLOG` (e.g. after downtime), skip ahead to keep results recent: insert `scan_gaps(start=cursor+1, end=head_id-SCANNER_MAX_BACKLOG, reason='skipped_backlog')` and move the cursor to `head_id - SCANNER_MAX_BACKLOG`. An admin can start a backfill per gap; it runs oldest-first only with budget left over when the live backlog is < 100, and records progress in `backfilled_until`.
   - **Stats**: per-minute `scan_stats` rows; `scanner_status()` returns cursor id, head id, backlog, ids created/min (head delta) and ids scanned/min over 15 min and 1 h, requests/min, not-found rate, open lobbies tracked, gaps, paused flag, last error, and an "ETA to catch up" when created/min < scanned/min.
   - **Extra sources** use the same `process_match`: wybin `multiplayerLink`s of the tournament's matches (`discovered_by='wybin'`) and pasted links (`'manual'`) are fetched at INTERACTIVE priority and stored even without a pool hit.
3. `/admin/scanner` page (admin): the status above as a compact key/value table that refreshes every 15 s, the gaps list with "Backfill" buttons, Pause/Resume. A warning line appears when ids created/min ≥ ids scanned/min for 15 min ("The scanner is falling behind. Raise SCANNER_BUDGET_PER_MIN or expect skipped ranges.").
4. `/t/{tid}/lobbies`: table (link, name, date, maps hit as slots, players found grouped by team, our players highlighted with a text marker), filters stage/player, "Add lobby link" form.
5. `/t/{tid}/coverage`: grid maps × members (cell: best score or "—", mod for FM; target met/missed text), "likely lineup" = current suggestion; flags: map where nobody in likely lineup has a score, map where < lineup size have scores; captain sets targets per map (team or player).
6. Tests (all with MockTransport + FakeClock):
   - event paging: port of the reference behaviour with fixture pages; `MATCH_MAX_PAGES` stops and reports `truncated`.
   - any lobby name: a match named `just practice` that played a pool map is stored; a match with no pool map stores nothing but stats.
   - pool index: only released, not-ended stages of active tournaments; archived/hidden tournaments excluded; index updates after a pool sync.
   - first run: empty cursor → cursor = head; no id below head is fetched.
   - cursor advance: out-of-order completion by 4 workers → persisted cursor only moves over contiguous done ids; restart resumes from it.
   - dedupe: the same id processed twice (restart overlap, wybin link plus scan, manual paste) → one `lobbies` row, no duplicate hits, players or scores.
   - gaps: 404 and 403 ids are counted and skipped, cursor passes them; 5xx id retried 3 times then counted as error; backlog > `SCANNER_MAX_BACKLOG` creates one `scan_gaps` row with the exact range and moves the cursor; backfill progresses `backfilled_until` and never runs while the live backlog ≥ 100.
   - open lobbies: a known-open id is not fetched by the worker; once missing from the active listing it is fetched with `after=last_event_id`, newly played pool maps are recorded, the row is deleted and the lobby marked complete; a lobby still active after `SCANNER_OPEN_MAX_HOURS` is finalised; > 2 h active gets incremental fetches every 30 min.
   - budget: over 10 simulated minutes the scanner never exceeds `SCANNER_BUDGET_PER_MIN` and total requests never exceed `OSU_API_RATE_PER_MIN`; an interactive request waiting while the scanner is busy is served first; `SCANNER_BUDGET_PER_MIN ≥ OSU_API_RATE_PER_MIN` fails at startup.
   - status: `scanner_status()` numbers from seeded `scan_stats`; falling-behind warning shown; `/admin/scanner` admin-only.
   - players noted only if registered in that tournament; coverage flags and target evaluation.

### Phase 7 — matchup compare, predictions, pick/ban plan, qualifier seed, pickems (`feat/predictions`)
1. `0007_predictions.sql`; `services/prediction.py` functions above with `model_version = "stat-1"`.
2. `/t/{tid}/matches/{mid}`: side-by-side table per map (our expected, their expected, win %, data quality: actual/estimated/no data), predicted protects/bans/picks per side, series win % and most likely score; captain plan form (protects/bans/picks in order, notes) next to the suggestion; snapshot saved in `predictions` when the page is first viewed per day and when the plan changes.
3. Qualifier stage: `/t/{tid}` and matches page show "Estimated seed: n of m (based on k teams with data)".
4. `/t/{tid}/pickems?stage=`: every match in the stage with predicted winner, win %, expected score; "Copy as text" button.
5. Tests: `map_win_prob` symmetric and monotonic, 0.5 at equal, None without data; `simulate_series` hand-computed BO3/BO5 cases incl. TB and protects; first-pick averaging; pickems over a fixture stage; seed estimate with partial data; opponent habit weights shift picks when history exists (stub until phase 8 → test via injected habits).

### Phase 8 — match history and prediction review (`feat/history`)
1. `services/history.py`: from wybin (`opponentOneBans/opponentTwoBans`, scores) and from the match's MP link (`fetch_match`: each game → pick in order; picking side inferred alternating from first game unless a ref "pick" can't be inferred → `team_side` NULL; winner per game = team with higher summed score, using `match.team` colours mapped to sides by player membership) → `match_events`. Captain can correct events manually (`source='manual'` wins).
2. `/t/{tid}/matches/{mid}/review`: actual vs predicted bans/picks/winners per map, hit rate; `/t/{tid}/opponents/{team_id}`: picks/bans per bracket across rounds, first-pick tendencies, maps never picked.
3. Feed habits into `simulate_series` (weight `HABIT_WEIGHT = 0.3`, constant).
4. Tests: event derivation from synthetic MP fixtures (abort/re-played map counted once — last completed game wins), side mapping via membership, manual override precedence, review metrics, habit aggregation.

### Phase 9 — live match mode (`feat/live`)
1. `services/live.py`: `LiveHub` (in-process pub/sub: `subscribe(match_id) -> asyncio.Queue`, `publish(match_id, event)`), `follow(match)` task: poll `fetch_match_after` every `LIVE_POLL_SECONDS` with `LIVE` priority, update `match_events`, publish `state` events (running score, maps played/remaining, banned, next-pick suggestion via `simulate_series` from current state). Auto-start when a match with `mp_match_id` is within 30 min of start or a captain presses Start; auto-stop when lobby ended or after 4 h.
2. `/t/{tid}/live/{mid}` page with htmx SSE ext (`sse-connect` to `/stream`, swaps partials); stream sends a heartbeat comment every 15 s; reconnect handled by EventSource.
3. Tests: hub delivers to multiple subscribers and drops closed ones; follow loop with FakeClock + scripted fixture pages produces correct running score and next-pick; stops at end; SSE endpoint requires membership and emits `text/event-stream`.

### Phase 10 — optional AI predictions (`feat/ai`)
1. `clients/ai.py` per D4: `predict(context: PredictionContext) -> AiPrediction` with JSON-schema'd output (winner, win %, bans, picks, short reasoning ≤ 600 chars); strict parse, reject on mismatch; timeout 60 s; no retries beyond 1.
2. Off when `AI_PROVIDER` empty or `AI_API_KEY`/`AI_MODEL` missing: UI shows no AI button; admin console shows "AI predictions: off".
3. Context builder sends only: stage format, pool slots with SR/mods, per-map expected scores per team (players as "Player A/B…" unless `ai_share_usernames` is on), stat model output, habits. Result stored as `predictions(kind='ai')`, shown beside the statistical one, labelled "AI estimate".
4. Tests: provider off → button absent and route 404; adapters build correct request (MockTransport) and never log the key; malformed AI output → generic error, nothing stored; per-user rate limit.

### CLAUDE.md Project block (copy into `CLAUDE.md` in phase 0)
```
- Purpose: Self-hosted osu! tournament team hub (Docker/Unraid): osu! login, wybin import, mappool sheets, lineups, availability, MP lobby skimmer, matchup predictions.
- Stack: Python 3.13, FastAPI, Jinja2 + htmx (vendored), SQLite (stdlib sqlite3, SQL migrations), httpx, Authlib; single container, data in /data
- Commands: run `uvicorn app.main:app --reload --port 8080` (variables set in the shell; see .env.example) · test `pytest` · lint `ruff check . && ruff format --check .` · build `docker build -t osu-tourney-hub .`
- Version source of truth: `pyproject.toml` ([project].version)
- Layout:
  - app/clients → osu! API, wybin, AI HTTP clients + rate limiter
  - app/domain → game modes, mods, shared dataclasses (no I/O)
  - app/services → business logic (sheet, lineup, prediction, skimmer, schedule, live, import/export)
  - app/repos → SQL per aggregate; app/db → connection + migrations (db/migrations/NNNN_*.sql)
  - app/workers → background scheduler and jobs
  - app/web → routes, auth/CSRF/security deps; app/templates, app/static → UI
  - tests → pytest, synthetic fixtures in tests/fixtures
  - docker/, Dockerfile, unraid/ → container and Unraid template
```

## Tests
Summary of must-have cases (details per phase above). All tests offline: `httpx.MockTransport`, `FakeClock`, temp SQLite.
- Config: missing/invalid env → clear error naming the variable, never echoing secrets.
- Auth: OAuth callback happy path; state mismatch; open redirect blocked; session invalidation; CSRF enforced on every POST route (parametrised over the route table).
- Access: matrix test — every `/t/{tid}` route × roles (admin, captain, co-captain, temp captain active/expired, player, substitute, guest, non-member, anonymous) → expected status. New routes must be added to the matrix.
- wybin: parsing, idempotent sync, sign-up tracking, solo, failure leaves data unchanged.
- Sheet: best-score rules, averages, cross-tournament reuse, notes visibility, archive read-only.
- Lineup: top-k, FM rules, substitutes, availability filter.
- Capture: API mapping, dedupe, queue/refresh window, rate limit.
- CSV: exact export, import validation and idempotence.
- Schedule: tz/DST conversions, best-slot ranking, iCal format.
- Skimmer: paging port, any lobby name, first-run start at head, contiguous cursor advance and resume, dedupe, 404/403/5xx gaps and skip-ahead gaps, open-lobby re-check until closed, budget and priority, admin status.
- Prediction: math properties and hand-computed series; pickems; seed.
- History: event derivation, overrides, habits.
- Live: hub fan-out, follow loop, SSE auth.
- AI: off by default, adapter requests, malformed output.
- Container (manual, recorded in PR): build from clean clone, run with dummy env, healthcheck healthy, process uid = PUID, Unraid XML parses (CI).

## Decisions for the programmer
All decided on 2026-11-12. D5 by the programmer's own wording; every other one as recommended. No open decisions.
- **D1 Repo and app name** — decided: `osu-tourney-hub`; image `ghcr.io/niklaskodev/osu-tourney-hub`, repo `NiklaskoDev/osu-tourney-hub`.
- **D2 Runtime dependencies** — decided: `fastapi`, `uvicorn`, `jinja2`, `httpx`, `authlib`, `itsdangerous`, `python-multipart`, `tzdata`; dev `pytest`, `ruff`. DB via stdlib `sqlite3` + plain SQL migrations; scheduler, SSE, iCal, CSV, CSRF and rate limiting with the stdlib. No python-dotenv, SQLAlchemy, APScheduler or sse-starlette. htmx (0BSD) and Lucide icons (ISC) are vendored static files.
- **D3 Score metric** — decided: score in the tournament's scoring (ScoreV2 from MP lobbies, lazer standardised from the API) with accuracy beside it; per-tournament `scoring` setting; ScoreV1 scores are kept but left out of averages when the tournament uses V2. Amended by 002: captured lazer scores count on ScoreV2 tournaments, marked.
- **D4 AI provider** — decided: Anthropic Messages API and an OpenAI-compatible endpoint (`AI_BASE_URL`, covers OpenAI/OpenRouter/local Ollama), two small `httpx` adapters, no SDKs, model only from `AI_MODEL`, off unless `AI_PROVIDER`, `AI_API_KEY` (not needed for a local endpoint) and `AI_MODEL` are set; usernames replaced by "Player A/B…" unless `ai_share_usernames` is on.
- **D5 MP lobby discovery** — decided by the programmer: "Continuously scan MPs and note every MP that has played any map of the current mappool, regardless of the name of the lobby". Design: continuous forward scan of every new match id (phase 6 step 2), start at the current head on first run (no history backfill), cursor in the DB, 404/403 skipped and counted, open lobbies re-checked until they close, parallel workers sharing `SCANNER_BUDGET_PER_MIN` (default 30) inside the global `OSU_API_RATE_PER_MIN=60` cap, skip-ahead with a recorded gap when the backlog exceeds `SCANNER_MAX_BACKLOG`, admin status page; wybin and pasted links stay as extra sources. No name filter.
- **D6 Which API scores count** — decided: all passed scores, any date, marked "old" after 182 days.
- **D7 Google Sheets import** — decided: CSV upload plus public Google Sheets links fetched as CSV from `docs.google.com` only.
- **D8 iCal** — decided: per-user secret feed URL, token stored hashed, revocable.
- **D9 Opponent data pulls** — decided: next scheduled opponent automatically, plus an on-demand button per team (captain+); pickems use whatever data exists and show coverage.
- **D10 Container user** — decided: start as root only to apply PUID/PGID and chown `/data`, then run the app as that uid via `setpriv`.
- **D11 Design system** — decided: the proposal in "UI" as written; the phase 0 `/styleguide` page is the visual review point.
- **D12 Automatic members** — decided: every player on our team's wybin roster becomes `player` on sync; guests and extra people are added manually.
- **D13 Visibility** — decided: public repo and public ghcr image.
- **D14 Release cut** — decided: `0.1.0` after phase 3, then one minor per later phase; each release still needs the programmer's explicit instruction.

## Risks / follow-ups
- wybin has no documented public API: endpoints, field names and team-tournament shapes may change without notice, and its terms on automated access are unknown. Mitigation: polite polling (≥ 2 s between requests, 30 min sync), parsers that fail closed with a visible "wybin sync failed" status, manual tournament/pool entry as fallback. Consider asking wybin staff for permission/guidance before release.
- Pools may be hidden on wybin until release; the app only shows what wybin serves anonymously (`pool_released` flag).
- **Scanner throughput (D5).** osu! API v2 docs (osu.ppy.sh/docs, checked 2026-11-12): "limit your usage to no more than 60 requests per minute", exceeding it "may lead to your API tokens being revoked"; terms: "Use the API for good. Don't overdo it", contact pe@ppy.sh for serious long-term use. `GET /matches` (scope `public`, `limit` ≤ 50, `sort`, `active`, `cursor_string`) and `GET /matches/{id}` (`before`/`after`, `limit` ≤ 101) are documented. New-id rate, estimated from five wybin match records (scheduled time → mp id, tournament 144, Oct 2026): 1 500 min → +5 746 ids (3.8/min); 570 min → +2 775 (4.9/min); 120 min → +618 (5.2/min); 6 840 min → +21 743 (3.2/min). So about **3–5 new match ids per minute (~5 000–7 000/day)**, measured at tournament times (busy hours); peak is assumed ≤ 10/min. Cost per id: ≈ 1 request (deleted/private 404s cost 1, open lobbies are deferred and fetched once after closing, multi-page lobbies rare) plus ~2 requests/min for head and open-check listings → ≈ 7–12 requests/min on average, ≤ ~20 at assumed peak. **The default `SCANNER_BUDGET_PER_MIN=30` keeps up with roughly 3× headroom and stays well inside 60/min**, so a full scan is feasible and within the documented guidance; no new decision needed. Uncertainty: the rate is an estimate from wybin data, not from two `/matches` snapshots (no API credentials available while planning); phase 6 step 2's status page shows the real created/min vs scanned/min. Mitigation if it cannot keep up: the status page warns, results stay recent because the scanner skips ahead past `SCANNER_MAX_BACKLOG` and records the range in `scan_gaps` (backfilled only with spare budget), and the admin can raise `SCANNER_BUDGET_PER_MIN` up to just under the 60/min cap; more than that needs contact with osu! staff (pe@ppy.sh).
- osu! API rate budget also limits how fast opponent score pulls fill in; the UI must show data coverage so predictions are not over-trusted.
- Mixing ScoreV1/V2/lazer scores skews averages; D3 setting and `scoring` column keep them apart.
- SQLite with background workers and web requests: WAL + short transactions + `busy_timeout`; single uvicorn worker process required (in-process live pub/sub and scheduler assume one process) — document in README; do not raise `--workers`.
- HTTPS is required for secure cookies and osu! OAuth callback in practice; the container serves HTTP and expects a reverse proxy (SWAG/NPM on Unraid). README must say so.
- Backups: `/data/app.db` (+ `-wal`) is the only state; Unraid appdata backup covers it; document.
- Declined Discord webhooks (retrofit easy): the job/event structure (`workers/jobs.py`, `LiveHub` events) does not block adding it later.
- The guideline's `CHANGELOG.md` has a placeholder `[0.1.0] - YYYY-MM-DD` block; phase 0 removes it so the first real release creates it properly.
- A roster can lose its last captain through account deletion (the last-captain rule guards role changes and removal only). Decide in a later phase whether to block it or let an admin fix it.

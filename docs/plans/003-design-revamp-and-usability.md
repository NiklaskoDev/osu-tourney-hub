# 003 — Design revamp, wide layout, availability timeline, current round, home matches, match stats, quick score entry, scanner start id

Status: approved by the programmer 2026-11-13 (decisions D1–D5 settled 2026-11-13)
Type: feature
Version impact: minor (0.1.0 → 0.2.0) — new pages and behaviour, backward compatible. One relaxing migration, `0008`: accuracy may be empty. The scanner start control needs no schema change. URLs and data formats keep working as before.

## Goal
The programmer asked for these changes:

1. **Filter rows.** Filter and action rows look polished: controls have the same height and line up. Filters apply as soon as they change, without a separate submit button.
2. **Design.** The design gets a general revamp, more refined, in light and dark mode.
3. **Wide layout.** Pages use the full window width. The side nav is pinned to the left edge. Tables, grids and timelines expand. Main tables lose their fixed-height inner scroll boxes.
4. **Availability.** It looks like the reference:
   - one timeline per day and one bar row per player, green/amber/red;
   - a "filled in" check per player;
   - a UTC/Local switch;
   - "Update my availability" and "Share team availability" actions.
5. **Homepage.** Our upcoming matches across the user's tournaments appear at the top.
6. **Current round.** Stage tabs and selects in an active tournament open on the current round, not the first.
7. **Match stats.** A Stats page per match and a new "Results" item in the tournament menu show:
   - per-map winners and totals;
   - per-player score, accuracy and mods.
8. **Quick score entry.** Pick a map by its slot label (NM1, HD2, …), type the score and save. Accuracy and mods are optional.
9. **Scanner start id.** On `/admin/scanner`, an admin can start lobby scanning from a chosen lobby id or osu! match link:
   - An older id is read in the background with spare budget.
   - A newer id skips ahead and records the skipped ids as a range that can be backfilled.

Out of scope:
- a new frontend framework or web fonts;
- a match-stats export;
- pushing data to wybin;
- changing the 30-minute slot model;
- an explicit availability confirmation;
- editing directly on the timeline bars (can follow later if wanted);
- public share links;
- moving the live scanner cursor backwards (see "Scanner start id" below for why);
- releasing.

## Reuse
- **Design system.**
  - `app/static/css/tokens.css` and `app.css`.
  - The palette, accent (osu! pink) and type scale stay; values are refined and tokens are added.
  - Every component keeps its class name, so templates change as little as possible.
- **Components.**
  - `components/field.html`, `button.html`, `table.html`, `tabs.html`, `checkbox.html`, `copy_button.html`, `countdown.html`, `empty_state.html`, `badge.html`, `dialog.html`.
  - One new macro, `components/switch.html`: a two-state switch has no existing component, and a checkbox styled as a switch needs its own markup.
- **Auto-apply.** `app/static/js/app.js` already handles delegated `change` events (`data-nav-select`). Add `data-autosubmit` next to it.
- **Availability.**
  - Existing code:
    - `services/availability.py`: `build_rows`, `Grid`, and the states `yes/maybe/no`. The maybe state already exists in migration 0005.
    - `repos/availability.list_range`, `schedule.roster_columns`, `schedule.bands_between`.
    - `routes/availability.py` and `static/js/availability.js`.
  - The current grid stays as the edit mode.
  - "Filled in" is derived from the stored slots, with no new table.
- **Current round.**
  - `services/pool_sheet.pick_stage` is the single stage chooser, used by pool, lineup, coverage, pickems and sheet_io. Extend it rather than duplicate it.
  - Stage dates come from `stages.start_at/end_at` (0002).
- **Homepage.**
  - `repos/matches.list_for_calendar(conn, osu_id)` already returns our timed matches in the user's active tournaments; the calendar feed uses it via `schedule.feed_events`.
  - The homepage reuses that query plus `schedule._view`.
- **Match stats.**
  - `match_events` (0007) holds per-map pick and tiebreaker rows with `winner_side`, `team1_score` and `team2_score`. `history.store_lobby_events` fills them from the match lobby.
  - Per-player scores are `scores` rows with `source='mp'` and `source_ref='{mp_match_id}:{game_id}'`, written by `repos/scores.insert_mp`.
  - Today only `services/lobbies.process` (the scanner and linked lobbies) writes those scores, so `history.store_lobby_events` will also store them.
  - No new table.
- **Score entry.**
  - `POST /t/{tid}/scores` and `services/score_entry.record_score`/`ScoreForm` keep their contract.
  - `domain/modes.split_mods` parses typed mods like "HDHR".
- **Scanner start id.**
  - `services/links.match_id(text)` already parses a bare id or a `…/matches/<id>` link.
  - The `scan_gaps` reason `'admin_skip'` is already allowed by the 0006 CHECK and unused until now.
  - `scan_repo.insert_gap`, `request_backfill`, `next_backfill` and `set_backfilled_until` are reused.
  - The existing backfill worker reads requested ranges oldest first, only while the live backlog is under `BACKFILL_MAX_LIVE_BACKLOG` (100), and counts its requests against `SCANNER_BUDGET_PER_MIN` through the osu! client.
  - `IdDispenser.skip_to`, `Skimmer.flush_cursor`, `app.state.skimmer`, `scanner_status`, and the global `require_csrf` dependency are reused as they are.

## Code layout
- **Styles**
  - `app/static/css/tokens.css` — refined palette, new tokens (control height, radii, tints, availability colours, layout widths).
  - `app/static/css/app.css` — fluid layout, sidebar, toolbar, restyled controls, tables without inner scroll, timeline, switch, upcoming list, quick-entry bar, match stats, scanner start panel.
- **Layouts**
  - `app/templates/layout/shell.html` — sticky top bar, fluid `.page`, a `page_class` block for narrow pages, a `main` block, and a flashes macro.
  - `app/templates/layout/tournament.html` — sidebar pinned left with the tournament name, plus the new nav item "Results".
- **Components and partials**
  - `app/templates/components/field.html` — new `inline=false` parameter (label beside the control, for toolbars).
  - `app/templates/components/switch.html` — new macro `switch(name, label_off, label_on, checked, value, id)`.
  - `app/templates/components/table.html` — new `scroll=false` parameter. With `scroll=true` it adds `table-wrap--scroll` (wide sheets only).
  - `app/templates/partials/availability_timeline.html` — new: one day's timeline table (macro `day_timeline`).
  - `app/templates/partials/played_row.html` — new: a played-match row shared by schedule and results.
- **Tournament pages**
  - `app/templates/pages/tournament/availability.html` — timeline view plus edit mode.
  - `app/templates/pages/tournament/lobbies.html`, `lineup.html` — toolbar filters with auto-apply; lineup column widths and picker grid.
  - `app/templates/pages/tournament/pool.html` — quick score bar; accuracy optional in the dialog.
  - `app/templates/pages/tournament/results.html` — new: played matches and our players' totals.
  - `app/templates/pages/tournament/match_stats.html` — new: one match's stats.
  - `app/templates/pages/tournament/schedule.html`, `review.html` — "Stats" links.
- **Other pages**
  - `app/templates/pages/dashboard.html` — "Upcoming matches" section on top.
  - `app/templates/pages/admin/scanner.html` — "Start from a lobby id" section, the confirmation panel, a "Why" column and a "Stop" button in the ranges table.
  - `app/templates/pages/styleguide/index.html` — new sections (toolbar, switch, timeline, upcoming list, quick entry) and updated swatches.
- **Scripts**
  - `app/static/js/app.js` — `data-autosubmit`.
  - `app/static/js/availability.js` — behaviour unchanged. The grid init runs only in edit mode; `detectZone` must still run in both modes.
- **Services**
  - `app/services/pool_sheet.py` — `current_stage` next to `pick_stage`; `pick_stage` gains `now`.
  - `app/services/availability.py` — `TimelineRow`, `TimelineRun`, `DayTimeline`, `HourHead`, `BandRun`, `FreeRun`, `build_timelines`.
  - `app/services/schedule.py` — `_calendar_views`, `UpcomingMatch`, `upcoming_for_user`.
  - `app/services/match_stats.py` — new: builds per-match and per-tournament stats from `match_events` and `scores`. Stats are a different concern from `history.py`, which compares predictions and is already 750 lines long.
  - `app/services/history.py` — `store_lobby_events` also stores per-player mp scores.
  - `app/services/score_entry.py` — `parse_optional_accuracy`.
  - `app/services/sheet.py`, `app/services/coverage.py`, `app/services/csv_io.py`, `app/domain/models.py` — handle `accuracy is None`.
  - `app/services/skimmer.py`:
    - pure `StartPlan` and `plan_start(...)`;
    - `start_from(...)`, which applies a plan;
    - `Skimmer.skip_forward(conn, cursor)`;
    - `backfill_eta_minutes(...)`.
- **Repos**
  - `app/repos/scores.py` — `list_mp_for_lobby(conn, mp_match_id)`; accuracy may be `None`.
  - `app/repos/scan.py` — `overlapping_gaps`, `delete_gaps`, `stop_backfill`.
- **Routes**
  - `app/web/routes/availability.py`, `lobbies.py`, `lineup.py`, `pool.py`, `coverage.py`, `pickems.py`, `sheet_io.py`, `dashboard.py`, `history.py` — wiring.
  - `app/web/routes/results.py` — new: `/t/{tid}/results` and `/t/{tid}/matches/{mid}/stats`. Register it in `app/web/routes/__init__.py` / `app/main.py` the same way as the other routers.
  - `app/web/routes/scanner.py` — `POST /admin/scanner/start` and `POST /admin/scanner/gaps/{gap_id}/stop`.
- **Database and docs**
  - `app/db/migrations/0008_scores_optional_accuracy.sql` — new: makes `scores.accuracy` nullable.
  - `README.md` — one paragraph on the start control in the scanner section, next to the backfill text.
  - `CHANGELOG.md` — entries under `[Unreleased]`.

## Data
**Migration `app/db/migrations/0008_scores_optional_accuracy.sql`.**

SQLite cannot drop a NOT NULL constraint, so the migration rebuilds `scores`:

1. Create `scores_new` with exactly the columns, CHECKs, UNIQUE and defaults from 0003. The one difference is `accuracy REAL CHECK (accuracy IS NULL OR accuracy BETWEEN 0 AND 100)`.
2. `INSERT INTO scores_new SELECT * FROM scores;`
3. `DROP TABLE scores;`
4. `ALTER TABLE scores_new RENAME TO scores;`
5. Recreate the three indexes `scores_beatmap_player_mods`, `scores_osu_id` and `scores_tournament_id`.

No table references `scores`, so this is safe with `foreign_keys=ON` inside the migration transaction (`migrate._apply`). Do not edit any existing migration file.

**Scanner start id.** No schema change. Ranges chosen by an admin are `scan_gaps` rows with `reason='admin_skip'`; that value is already allowed by the CHECK in 0006.

## Interfaces
### Current round
- `pool_sheet.current_stage(stages: list[Stage], now: datetime) -> Stage | None`. Pure function; `stages` are in sort order. Rules, in order:
  1. **No dates.** If no stage has `start_at` or `end_at`, return `pick_stage(stages, None)`. That is today's rule: the first stage with a released pool, else the first stage.
  2. **Running.** A stage is running when `(start_at is None or start_at <= now) and (end_at is not None and now < end_at)` and at least one of its dates is set. If several are running, take the one with the latest `start_at`; a missing start counts as earliest.
  3. **Next upcoming.** The stage with the smallest `start_at > now`.
  4. **Last.** The last stage in sort order.

  Compare times as UTC ISO text (`to_utc_iso(now)`), like `capture_stages` does.
- `pool_sheet.pick_stage(stages, raw, now: datetime | None = None)`:
  - If `raw` names a stage, return that stage, as today.
  - Else, if `now` is not None, return `current_stage(stages, now)`.
  - Else keep today's rule.
- Callers pass `now = clock.now() if tournament.status == "active" else None`:
  - `routes/pool.py::_page`;
  - `routes/lineup.py`, both the page and `_stage_context`;
  - `routes/coverage.py`;
  - `routes/pickems.py`;
  - `routes/sheet_io.py`: the import page (lines ~126 and ~156) and the export endpoint (~70), so a CSV without `?stage=` matches the page.

  Add `clock: Clock = Depends(get_clock)` wherever it is missing.
- Lobbies: `lobbies_page(stage: str | None = None, …)`.
  - When the query has no `stage` key at all and the tournament is active, `_page` uses `current_stage(...)`.
  - `stage=` (empty, "All stages") still means all stages.
  - The toolbar form always sends `stage`, so choosing "All stages" sticks.

### Homepage
- `schedule.upcoming_for_user(conn, osu_id: int, now: datetime, limit: int = 5) -> list[UpcomingMatch]`, with `@dataclass(frozen=True) class UpcomingMatch: tournament: Tournament; view: MatchView`.
  - Source: `matches_repo.list_for_calendar`.
  - Keep matches with `status != "done"` and `scheduled_at >= to_utc_iso(now - MATCH_DURATION)`, so a match that is being played still shows. Keep the query's order, then apply `limit`.
  - Load the tournament, teams and stages once per tournament.
  - Extract a private helper `_calendar_views(conn, matches)` that both `feed_events` and `upcoming_for_user` use, so the loop is not duplicated.
- `routes/dashboard.py::index` adds `"upcoming": schedule.upcoming_for_user(db, user.osu_id, clock.now())`. It needs `get_clock`.

### Availability
- `GET /t/{tid}/availability`:
  - New query parameter `edit`; `"1"` means edit mode. The other parameters (`tz`, `zone`, `week`, `member`) are unchanged.
  - Without `edit`, the timeline view renders. With `edit=1`, today's grid renders, with `editable_id` as today.
  - The POST endpoint is unchanged.
- `availability.build_timelines(monday, zone, columns, availability, bands, filled_ids: set[int]) -> list[DayTimeline]`. Pure function. It returns one entry per local day of the week: use `week_slots` and group by local date. A DST day has 46 or 50 slots.
  - `DayTimeline(day_label: str, anchor: str, hours: list[HourHead], rows: list[TimelineRow], bands: list[BandRun], free: list[FreeRun], slot_count: int)`. Example `day_label`: "Monday 16 Nov".
  - `HourHead(label: str, span: int)`.
    - `label` is the hour, e.g. "18:00".
    - `span` is the number of slots in that local hour, normally 2. Build it from the slots so DST days stay correct.
  - `TimelineRow(column: Column, filled: bool, runs: list[TimelineRun], summary: str)`.
    - `summary` is the spoken text, e.g. "Yes 18:00–23:00, Maybe 23:00–24:00, No otherwise".
  - `TimelineRun(state, span, start_label, end_label)` merges consecutive slots that have the same state.
  - `BandRun(label: str | None, span: int, in_match: bool = False)` covers the day's slots (as built: `in_match` marks the runs a match covers, so a match that began the day before still shows its shading without a label). Use `band_covers`. The first run of a match carries the label; all other runs have `label=None`.
  - `FreeRun(yes: int, maybe: int, span: int)` merges consecutive slots that have equal counts.
- `Column` gains `avatar_url: str | None = None`. `schedule.roster_columns` fills it from `Member.avatar_url`; existing constructions keep working.
- `filled_ids` (D1 = A): the osu ids with at least one stored slot (Yes or Maybe) in the shown week. Compute it in the route from the `list_range` result that is already loaded: `{osu_id for osu_id, slots in found.items() if slots}`.
- The route adds `share_url` to the context: the absolute URL of the current view without `member` and `edit`. Build it from `request.url` by dropping those query keys.

### Match stats
- `scores_repo.list_mp_for_lobby(conn, mp_match_id: int) -> list[Score]`:
  - returns rows with `source = 'mp' AND source_ref LIKE ?`, with the parameter `f"{mp_match_id}:%"`;
  - ordered by `achieved_at, id`.
- `history.store_lobby_events(conn, match, lobby_events, now: datetime)`:
  - After the events, inside the same transaction, call `scores_repo.insert_mp(...)` for every game of `mp_parse.parse_games(lobby_events)` whose beatmap is in the pool, or for every game when the pool is empty.
  - Store only scores whose `osu_id` is in `_membership(conn, match)`.
  - Use `to_utc_iso(now)` as `created_at`.
  - Thread `now` through `refresh_from_lobby` and `import_pending`: the route passes `request.app.state.clock.now()`, the scheduler job passes its clock. Update every call site and the tests.
- `match_stats.build_match_stats(conn, match) -> MatchStats` (as built: no `tournament` parameter, it was not needed):
  - `MatchStats(match, stage, team1, team2, maps: list[MapStat], players: list[PlayerLine], has_lobby: bool, lobby_read: bool)`. `lobby_read` is true when `match_events` has any `source='mp'` row for the match.
  - `MapStat(seq, kind, pool_map | None, beatmap_id, winner_side | None, winner_name | None, team1_total | None, team2_total | None, scores: list[PlayerScore])`.
  - `PlayerScore(osu_id, name, side | None, score, accuracy | None, mods, misses, max_combo, passed)`.
  - `PlayerLine(osu_id, name, side, maps_played, mean_score, mean_accuracy | None, maps_won)`.
  - Map rows come from `match_events` picks and tiebreakers, in seq order.
  - Player scores come from `list_mp_for_lobby`, matched by `beatmap_id`. If a map was replayed, the last game wins (the same rule as `derive_mp_events`).
  - Names and sides come from `teams_repo.list_players` of team1 and team2.
- `match_stats.tournament_results(conn, tournament, now) -> ResultsView`, where `ResultsView(matches: list[MatchView], players: list[PlayerLine])`:
  - `matches` is the played list from `schedule.load_schedule(...).past`.
  - `players` aggregates our team's players over every played match that has a lobby. It is an empty list when `our_team_id` is not set.
- Routes in `routes/results.py`, both read-only with `member_access`:
  - `GET /t/{tid}/results`, nav key `results`;
  - `GET /t/{tid}/matches/{mid}/stats`, nav key `results`. Reuse `find_match` from `routes/matches.py`.
- `routes/history.py::refresh_lobby` takes an optional form field `return_to`. When it is `"stats"`, redirect to `/t/{tid}/matches/{mid}/stats`; otherwise redirect to the review page as today.

### Quick score entry
- It reuses `POST /t/{tid}/scores` and adds two form fields:
  - `mods_text` (optional). When the `mods` list is empty and `mods_text` is not blank, set `mods = split_mods(mods_text)`. A `ValueError` becomes `EntryError(..., 422, "mods")`. Do this in the route, before building `ScoreForm`.
  - `entry_origin` (`"quick"` or absent). On a refusal it is passed through as `entry_kind="quick"`, so the error shows on the quick bar and not inside a map dialog.
- Optional accuracy (D2 = B):
  - A blank `ScoreForm.accuracy` becomes `None`.
  - Add `score_entry.parse_optional_accuracy(text) -> float | None`: `None` for blank text, otherwise `parse_accuracy`. `record_score` uses it.
  - `NewScore.accuracy` and `Score.accuracy` become `float | None`.

### Scanner start id
**Why this design (decided, reuse first).**

Moving the live cursor back is not done:
- The next `check_head` would immediately skip ahead again once the distance to the head passes `SCANNER_MAX_BACKLOG`.
- Reading history at live priority would starve the scan of new tournament lobbies.

So an older start id becomes a backfill range. It is read by the existing backfill worker:
- with spare budget only, so never while the live backlog is 100 or more;
- oldest first;
- counted against `SCANNER_BUDGET_PER_MIN`.

A newer start id is a manual skip-ahead, the same mechanism as the automatic one. The skipped ids stay listed and can be backfilled later.

- **`StartPlan`** is a frozen dataclass: `kind`, `start_id`, `end_id`, `size` and (as built) `merged_gap_ids`, the skipped ranges a backfill replaces; finished ranges (`backfill_done`) are not merged. The route plans from the running scanner's live cursor when there is one, else from the saved cursor. `kind` is one of:
  - `"backfill"`: read the ids from `start_id` to `end_id` in the background;
  - `"skip"`: skip the ids from `start_id` to `end_id` and continue live after `end_id`;
  - `"nothing"`: the scanner already continues from this id.
- **`skimmer.plan_start(start_id: int, cursor: int | None, head: int | None, gaps: list[Gap]) -> StartPlan`**. Pure function. Raises `StartError(message)` (a new `ValueError` subclass in `skimmer.py`) with a user-facing message. Checks, in this order:
  1. `start_id < 1`: "Enter a lobby id or an osu! match link."
  2. `cursor is None` or `head is None`: "The scanner has not started yet. Try again once it has read the newest lobby id (within a minute of starting)."
  3. `start_id > head`: f"That lobby id is newer than the newest one osu! has listed ({head}). Enter an id up to {head}."
  4. `start_id == cursor + 1`: `StartPlan("nothing", …, size=0)`.
  5. `start_id <= cursor`, the backfill case. The range is `[start_id, cursor]`, merged with every gap that overlaps it (`gap.start_id <= cursor and gap.end_id >= start_id`).
     - The merged range is `[min(start_id, overlapping.start_id…), max(cursor, overlapping.end_id…)]`.
     - Every existing gap lies at or below the cursor, so the merge never goes above the cursor.
     - Ids in the range that were already scanned live are read again. That is harmless: `INSERT OR IGNORE` and the upserts make it idempotent. It only costs budget.
  6. `cursor + 1 < start_id <= head`, the skip case. The range is `[cursor + 1, start_id - 1]`.
- **`skimmer.start_from(conn, skimmer: Skimmer, plan: StartPlan, now: datetime) -> int | None`** applies a plan in one `transaction(conn)`.
  - Backfill:
    1. `scan_repo.delete_gaps(conn, ids_of_overlapping)`, which merges them. They are bookkeeping rows fully covered by the new range, so nothing is lost; at most a partially backfilled gap is re-read.
    2. `gap_id = scan_repo.insert_gap(conn, start, end, "admin_skip", stamp)`.
    3. `scan_repo.request_backfill(conn, gap_id)`.
    4. Return `gap_id`.
  - Skip:
    1. `scan_repo.insert_gap(conn, start, end, "admin_skip", stamp)` without a backfill request.
    2. `skimmer.skip_forward(conn, end)`.
  - Nothing: return None.
- **`Skimmer.skip_forward(self, conn, cursor: int) -> None`**:
  - Raises `StartError` with the "has not started yet" message when `self._dispenser is None`.
  - Otherwise: `self._dispenser.skip_to(cursor)`, prune `self._known_open` to the ids above the cursor, then `self.flush_cursor(conn)`.
  - It runs in the same event loop as the workers (the route is async, single worker), so no lock is needed. This is the same reasoning the app already relies on for `app.state.ai_inflight`.
- **`skimmer.backfill_eta_minutes(size: int, status: ScannerStatus) -> float | None`**:
  - `spare = status.budget_per_min - (status.requests_per_min_60 or 0)`.
  - Return `None` when `spare <= 0`, else `size / spare`.
  - Use roughly one request per id. The page says the figure is a rough estimate.
- **`scan_repo` additions:**
  - `delete_gaps(conn, ids: list[int]) -> None`.
  - `stop_backfill(conn, gap_id: int) -> bool` stops a requested, unfinished backfill without losing what was read. It runs `UPDATE scan_gaps SET start_id = backfilled_until + 1, backfilled_until = NULL WHERE id = ? AND backfilled_until IS NOT NULL AND backfilled_until < end_id`.
    - The ids already read drop out of the range.
    - The rest stays listed with a "Backfill" button.
    - A stop before anything was read gives `start_id = start_id`, because `backfilled_until = start_id - 1`.
    - It returns whether a row changed.
  - The stop control is needed so that a mistaken huge range does not block the other requested ranges, which are read oldest first, for weeks.
- **`POST /admin/scanner/start`** (`require_admin`; CSRF through the app-wide `require_csrf`):
  - Form fields: `start` (an id or a link) and `confirmed` (`"1"` or absent).
  - Parse with `links.match_id(start.strip())`. `None` → re-render the page with 422 and the field error "Enter a lobby id or an osu! match link."
  - When `settings.scanner_enabled` is false → 409 `UserFacingError("The scanner is switched off (SCANNER_ENABLED is false).")`.
  - Read the cursor and head with the same values `scanner_status` shows (`_int_cursor` for `CURSOR_JOB` and `HEAD_JOB`). Read the gaps with `scan_repo.list_gaps`.
  - `plan_start(...)`; a `StartError` → re-render with 422 and the message on the field. Keep the entered value.
  - **Confirmation.** A backfill plan with `size > settings.scanner_max_backlog`, posted without `confirmed=1`, is not saved. Re-render the page with 200 and `pending_start=plan` plus `pending_eta=backfill_eta_minutes(...)`. The confirmation panel posts the same `start` with `confirmed=1`. The existing setting is reused as the "large" threshold (default 5,000).
  - After applying, redirect 303 to `/admin/scanner` with one of these flashes:
    - backfill: f"Lobby ids {start:,} to {end:,} will be read in the background with spare budget, oldest first."
    - skip: f"The scanner now continues from lobby id {end + 1:,}. Ids {start:,} to {end:,} are listed under Skipped ranges and can be backfilled."
    - nothing: info "The scanner already continues from this lobby id."
- **`POST /admin/scanner/gaps/{gap_id}/stop`** (`require_admin`):
  - `stop_backfill`. If it changes a row: flash success "Backfill stopped. What was read stays; the rest can be backfilled again."
  - Unknown gap → 404 "This skipped range does not exist."
  - Not running → flash info "This range is not being backfilled."
  - Redirect 303 to `/admin/scanner`.

## Security and user data
- No secrets.
- **Tournament pages.** The new results and stats pages are read-only and use `member_access`. The quick bar posts to the existing scores endpoint with its existing access, writable and CSRF checks.
- **Scanner controls.**
  - Both new scanner endpoints are admin-only (`require_admin`) POSTs, protected by the app-wide CSRF dependency.
  - Input is parsed only by `links.match_id`, which accepts digits or a match link and nothing else. Numbers are compared as integers and never put into SQL text.
  - osu! request volume stays within `SCANNER_BUDGET_PER_MIN`: the existing backfill worker enforces it, and nothing new calls osu!.
- **Personal data.**
  - Per-player mp scores of both teams' rostered players are stored. This is public osu! data, and the scanner already stores it the same way.
  - Avatars use `users.avatar_url`, which the CSP `img-src` already allows.
  - The availability share link points to a members-only page and creates no new access.
- **`data-autosubmit`.** Only GET forms use it. Never autosubmit a POST form.

## UI
Read `docs/ai/ui.md` first. Follow these values exactly; leave nothing to taste.

### Design direction: "quiet instrument panel"
Neutral, dense, calm surfaces. The pink accent is only for actions, focus and the current selection; elsewhere colour only encodes state. Hierarchy comes from weight, size and spacing, not boxes.

**Tokens in `tokens.css`.** Keep the existing token names. Change or add these values:

| Token | Light | Dark |
|---|---|---|
| `--bg` | `#f4f4f2` | `#121316` |
| `--surface` | `#ffffff` | `#191b1f` |
| `--surface-2` | `#eeeeeb` | `#202328` |
| `--surface-3` (new; hover, selected nav) | `#e6e6e2` | `#272a30` |
| `--border` (subtle dividers) | `#e2e2dd` | `#2a2d33` |
| `--border-strong` (new; inputs) | `#c9c9c3` | `#3a3e46` |
| `--text` | `#1c1d20` | `#ecebe8` |
| `--text-muted` | `#5b5d63` | `#9fa2aa` |
| `--accent` | `#b0285f` | `#e46a9a` (both unchanged) |
| `--accent-soft` (new; selected nav and tab backgrounds) | `color-mix(in srgb, var(--accent) 12%, var(--surface))` | same formula |
| `--success` | `#2d7a4b` | `#4fb37a` |
| `--warning` | `#9a5b00` | `#d9a03f` |
| `--danger` | `#b3261e` | `#e2675f` |
| `--avail-yes` (new) | `#3f9b63` | `#3f9b63` |
| `--avail-maybe` (new) | `#d29a2c` | `#d29a2c` |
| `--avail-no` (new) | `#c4554d` | `#b5524b` |

- **Contrast.** Bars carry no text. Every text token must keep at least 4.5:1 on `--bg` and on `--surface`. Check each pair with a contrast tool and note the ratios in a CSS comment, as the file already does for `--accent-contrast`.
- **Shape.** `--radius-sm: 6px`, `--radius-md: 8px`, `--radius-lg: 10px` (new). No pills except the switch track and the timeline bars (bar radius `4px`).
- **Controls.** `--control-h: 36px`, and `44px` inside `@media (pointer: coarse)`. This keeps the 44 px touch rule on touch devices and makes desktop compact. `--control-pad-x: 12px`.
- **Type.**
  - Keep `system-ui`; no web font, so no new dependency.
  - Keep the scale 12/14/16/20/24/32.
  - Add `--weight-medium: 500` for buttons, nav and table headers.
  - Body text stays 16 px; tables and toolbars use 14 px.
  - h1 is `--text-xl` (24 px), weight 600, `letter-spacing: -0.01em`.
- **Spacing.** Unchanged scale: 4/8/12/16/24/32/48.
- **Layout.** `--sidebar-w: 15rem`, `--topbar-h: 52px`, `--gutter: clamp(16px, 2.2vw, 32px)`. `--max-text` stays 72ch; `--max-form: 40rem` is new.
- **Shadows.** None on surfaces. `--shadow-overlay` only on dialogs and menus.

### Wide layout
- **Top bar.** `.topbar` gets `position: sticky; top: 0; z-index: 20; height: var(--topbar-h)`, with a full-width surface and a bottom border.
- **Page.**
  - `.page` gets `max-width: none; padding: var(--space-5) var(--gutter) var(--space-7)`.
  - New `.page--narrow { max-width: calc(var(--max-form) + 2 * var(--gutter)); margin-inline: auto }`.
  - `shell.html` gets `{% block page_class %}{% endblock %}` on `<main class="page …">`.
  - Narrow pages: sign-in, error, `me`, the admin tournament forms and tournament settings.
  - Full-width pages: dashboard, styleguide and the admin pages, including the scanner.
- **Tournament layout.**
  - `shell.html` wraps its content in `{% block main %}`. The default block is today's `<main id="main" class="page">` with flashes.
  - `tournament.html` overrides `main` and renders `<div class="tournament-layout">` with `display: grid; grid-template-columns: var(--sidebar-w) minmax(0, 1fr); min-height: calc(100dvh - var(--topbar-h))`.
  - **Sidebar** (`<nav class="side-nav">`):
    - `position: sticky; top: var(--topbar-h); height: calc(100dvh - var(--topbar-h)); overflow-y: auto; background: var(--surface); border-right: 1px solid var(--border); padding: var(--space-4) var(--space-3)`.
    - It starts with the tournament name (weight 600, truncated with an ellipsis), then acronym and status in muted 12 px, then the links.
    - Links: `min-height: 36px` (44 px on coarse pointers), radius-sm, muted text, `--surface-3` on hover.
    - The current link: `--accent-soft` background, `--text` colour, weight 500, and a 3 px accent bar from `box-shadow: inset 3px 0 0 var(--accent)`.
  - **Content.**
    - `<main id="main" class="tournament-layout__content stack">` with `padding: var(--space-5) var(--gutter) var(--space-7)`.
    - Keep `id="main"`; the skip link needs it.
    - Flashes render at the top through a `flashes()` macro, so both `main` variants share the markup.
  - **Below 768 px.** One column. The sidebar shows today's select above the content, with `position: static; height: auto; border-right: 0` and a bottom border.
- **Text width.** Paragraphs keep `max-width: var(--max-text)`. Forms (`form.stack`) get `max-width: var(--max-form)`, except forms that contain a table (`form.stack:has(.table-wrap)`), which get no maximum.
- **Tables.**
  - `.table-wrap` drops `max-height: 28rem` and gets `overflow-x: auto; overflow-y: visible`.
  - From 1024 px, `.table-wrap:not(.table-wrap--scroll) { overflow: visible }`, so `thead th { position: sticky; top: var(--topbar-h) }` sticks to the page.
  - Below 1024 px the header is not sticky, because the wrapper scrolls sideways. This is an accepted trade-off; say so in a CSS comment.
  - `.table-wrap--scroll` (pool sheet, availability edit grid, coverage) gets `max-height: calc(100dvh - var(--topbar-h) - var(--space-6)); overflow: auto`. That is one viewport-tall box, not a small nested one, and it keeps the sticky header and first column.
  - Remove the old `.sheet .table-wrap { max-height: 70vh }`.
  - `.table` text is 14 px. The header has a `--surface-2` background, weight 500 and sentence case. Rows are 40 px high with `--border` row borders.
  - The wrapper keeps `border: 1px solid var(--border); border-radius: var(--radius-md)`. Hovered rows use `--surface-2`.
  - `.table td { white-space: normal }` lets text cells wrap. `.is-numeric`, `th[scope=row]` and `.nowrap` stay `nowrap`.
- **Lineup column balance.**
  - Both lineup tables get a `<colgroup>`: map `col.col-map` with `width: 26rem`, players auto, notes `col.col-notes` with `width: 16rem`.
  - `.map-name` max-width becomes `22rem` from 768 px.
  - In the saved-lineup editor, `.slot-edit` becomes `display: grid; grid-template-columns: repeat(auto-fill, minmax(12rem, 1fr)); gap: var(--space-2)`.
  - Wrap each player select and its mods input in `<div class="slot-edit__pick">`, with the mods input under the select. Each select fills its cell.
  - The empty option reads "Choose player". The label is set in `routes/lineup.py` or `services/lineup_edit.py`.
- **Phone (360 px).** 16 px gutters and no horizontal page scroll. Only wide table wrappers and the availability timeline scroll sideways.

### Components restyled (same class names)
- **`.btn`.** `height: var(--control-h)` (not min-height), `padding: 0 var(--control-pad-x)`, 14 px, weight 500, radius-sm, `line-height: 1`, 16 px icons.
  - `--secondary`: `--surface-2` background, `--border` border, `--surface-3` on hover.
  - `--text`: transparent, accent text, `--surface-2` on hover.
  - `--primary`: accent, 8% darker on hover via color-mix.
- **`.field__control`.**
  - Inputs and selects: `height: var(--control-h)`. Textareas keep an automatic height with a 6rem minimum.
  - `padding: 0 var(--control-pad-x)`, 14 px, `background: var(--surface)`, `border: 1px solid var(--border-strong)`, radius-sm.
  - Hover: `--text-muted` border. Focus: accent border plus the existing outline.
  - Selects: `appearance: none; padding-right: 32px`, with a chevron from a CSS `background-image: url("data:image/svg+xml,...")`. Use one encoded SVG per colour scheme (light stroke `#5b5d63`, dark stroke `#9fa2aa`), swapped in the dark media query. The CSP `img-src` already allows `data:`.
- **`.field--inline`** (new, from `field(..., inline=true)`).
  - `flex-direction: row; align-items: center; gap: var(--space-2)`.
  - The label is 14 px, muted, weight 500, `white-space: nowrap`.
  - The macro still renders the hint and error under the row.
- **`.toolbar`** (new).
  - `display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2) var(--space-4)`.
  - Children are inline fields, switches and buttons, all `--control-h` tall.
  - `.toolbar__end { margin-left: auto; display: flex; gap: var(--space-2) }` holds the actions. Below 768 px it gets `margin-left: 0`.
  - No border and no box. It sits directly above the content it filters, with `margin-bottom: var(--space-3)`.
- **Auto-apply.**
  - Use `<form class="toolbar" method="get" data-autosubmit>`.
  - In `app.js`, a delegated `change` listener calls `form.requestSubmit()` when `event.target.form` has `data-autosubmit`.
  - The fallback submit button sits in `<noscript>`.
  - This is a full GET navigation, not an htmx partial swap. `availability.js` initialises on `DOMContentLoaded` and would not re-bind after a swap, and these pages are fast server renders. (Minor decision, taken.)
- **`.tabs`** (stage tabs). 14 px, weight 500. The active tab gets a 2 px accent underline and `--text`. Structure unchanged.
- **`.badge`.** Radius-sm, `--surface-2`, no border, 12 px mono. Use it only for roles, slots and scanner range reasons.
- **`.switch`** (new macro `components/switch.html`).
  - Markup: `<label class="switch"><span class="switch__text">UTC</span><input type="checkbox" role="switch" class="switch__input" name=… value=… [checked]><span class="switch__track" aria-hidden="true"></span><span class="switch__text">Local</span></label>`.
  - Track: 36×20, radius 10, `--border-strong` background, accent when checked.
  - Thumb: a 16 px `::after` with `transition: transform var(--motion)`.
  - Focus: a visible ring on the track via `.switch__input:focus-visible + .switch__track`.
  - The input is visually hidden, not `display: none`. The label area is at least 44 px on coarse pointers.
- **`.page__header`.** `align-items: center`; the h1, a muted subtitle line and actions at the end. On tournament pages, remove the `<p class="muted">{{ tournament.name }}</p>` subtitle: the sidebar now shows the name.

### Availability
**View mode (default).**

1. **Toolbar.**
   - Left side:
     - `button("Update my availability", variant="primary", href=<same view with edit=1>)`. Shown only when `editable_id` is not None and the tournament is not archived.
     - A `copy_button` "Share team availability" that copies `share_url`; its copied label is "Link copied".
     - `button("Export CSV", variant="text", icon_name="download")`.
   - Right side (`toolbar__end`):
     - The week controls: "Previous week", "This week" and "Next week" buttons, then the `{{ week_label }}` text.
     - The UTC/Local switch inside `<form method="get" data-autosubmit>` with a hidden `week`. When checked, the switch submits `tz=local`.
   - Load `availability.js` in view mode too, so `detectZone` still fills in the browser timezone. Its grid init does nothing when there is no grid; confirm this.
2. **Legend.** Under the toolbar, 12 px muted: square swatches "Available", "Maybe", "Unavailable", "Not filled in" (dashed outline) and "Match" (accent). Text always goes with the colour.
3. **One `<table class="timeline">` per day,** from the macro `day_timeline(day, lineup_size, own_id)` in `partials/availability_timeline.html`.
   - **Wrapper.** `<div class="timeline-wrap" role="region" aria-label="Availability on {{ day.day_label }}" tabindex="0">`. It scrolls sideways on narrow screens.
   - **Table.** `table-layout: fixed; width: 100%; min-width: 64rem`.
   - **Columns.** A `colgroup` with a first `col` of `width: 13rem`, then `day.slot_count` equal cols.
   - **Header row.**
     - `<th scope="col" class="timeline__day">{{ day.day_label }}</th>`.
     - Then, per hour, `<th scope="col" colspan="{{ hour.span }}" class="timeline__hour">{{ hour.label }}</th>`.
     - Hour labels are 12 px muted, left-aligned, with a 1 px `--border` rule on the left.
   - **Match row** (only when the day has a labelled band).
     - `<tr class="timeline__matches"><th scope="row">Matches</th>`, then per BandRun a `<td colspan="{{ run.span }}">`.
     - A labelled run renders `<span class="timeline__band">{{ run.label }}</span>`: accent background at 20%, an accent left bar, 12 px text that truncates.
   - **Player rows.** Each row has exactly one `th`, then one `td` per run. The colspans add up to `slot_count`, so the table stays rectangular.
     - Row: `<tr class="timeline__row{% if not row.filled %} is-unfilled{% endif %}">`.
     - Row header `<th scope="row" class="timeline__who">` contains:
       - the avatar `img.avatar.avatar--round` (24 px, `alt=""`, only when `avatar_url` is set);
       - the name, truncated;
       - a muted "(you)" for `own_id`;
       - when `row.filled`, `icon("check")` in `--success` with `<span class="visually-hidden">filled in</span>`, pushed to the right end of the cell with `margin-left: auto`;
       - the spoken summary, `<span class="visually-hidden">: {{ row.summary }}</span>`.
     - Check that `check` exists in `static/icons/sprite.svg`. If it is missing, add the Lucide `check` symbol (same set as the other icons).
     - Run cells: `<td colspan="{{ run.span }}" aria-hidden="true"><span class="bar" data-state="{{ run.state }}" title="{{ state word }} {{ run.start_label }}–{{ run.end_label }}"></span></td>`. They are hidden from screen readers because the row header's summary already says the same.
     - `.bar`: `display: block; height: 18px; margin: 0 1px; border-radius: 4px`, background `--avail-yes`, `--avail-maybe` or `--avail-no` by `data-state`.
     - `.is-unfilled .bar`: `opacity: .35; background: transparent; outline: 1px dashed var(--text-muted); outline-offset: -1px`.
     - Rows are 36 px high, separated by a 1 px `--border` line, with cell padding `0`.
   - **Free row (last).**
     - `<th scope="row">Free</th>`, then per FreeRun a `<td colspan="{{ run.span }}">`.
     - The cell shows "{{ yes }}/{{ lineup_size }}" in 12 px tabular figures, or nothing when `yes == 0`. A maybe count shows as a muted "+N".
     - When `yes >= lineup_size`, add `is-enough` (success colour, weight 600) and a visually hidden "enough players".
4. **Empty states and alerts** are unchanged: no roster, archived, timezone not saved.
5. **Footnote** below the timelines, one muted line: "Bars show each 30-minute slot. A slot is free for a match when the answer is Yes for its whole 90 minutes."

**Edit mode (`?edit=1`), D5 = A.**
- The page header reads "Update availability", with a "Done" `button(variant="secondary")` that links back to the view URL.
- Toolbar: `<form class="toolbar" method="get" data-autosubmit>` containing:
  - for captains, `field("member", "Editing", inline=true, …)`;
  - the UTC/Local switch;
  - hidden `week`, `zone` and `edit=1`;
  - today's "Show" button, moved into `<noscript>`.
- Then the existing grid exactly as today, rendered with `table(…, scroll=true)`, with its help line, `#avail-status`, the day jump links and its data attributes unchanged.

### Lobbies and lineup filters
- **Lobbies:** `<form class="toolbar" method="get" data-autosubmit>` with `field("stage", "Stage", inline=true, …)` and `field("player", "Player", inline=true, …)`, plus `<noscript>{{ button("Apply filters", type="submit") }}</noscript>`.
- **Lineup:** `<form class="toolbar" method="get" data-autosubmit>` with:
  - the two checkboxes (`.check` now has `min-height: var(--control-h)`);
  - `field("match", "Match", inline=true, …)`;
  - a noscript "Update suggestion" button.
- **Other pages:** grep the remaining templates (`coverage.html`, `import.html`, `pickems.html`) for GET forms with a lone submit next to a select, and convert them the same way. Leave POST forms alone.

### Homepage
At the top of `dashboard.html`, before the tournaments table, add `<section class="stack" aria-labelledby="upcoming-heading"><h2 id="upcoming-heading">Upcoming matches</h2><ol class="upcoming">…</ol></section>`.

- **Item.** Each `<li class="upcoming__item">` is a grid `[time 10rem][match 1fr][tournament auto][action auto]`:
  - time: `local_time`, with "in" and `countdown` under it in 12 px muted;
  - match: "vs {{ opponent_name }}" in weight 500, with stage and `match.label` muted under it;
  - tournament: `badge(acronym or name)`;
  - action: `button("Open match", variant="text", icon_name="chevron-right", href=/t/{tid}/matches/{mid})`.
- **Next match.** The first item gets `upcoming__item--next`: a 3 px accent left bar and the opponent name at h3 size.
- **Look.** No card shadow; rows are separated by `--border`. Below 768 px each item stacks into two lines.
- **Empty.**
  - No upcoming matches: `empty_state("No upcoming matches in your tournaments. They appear here once wybin lists a time for them.")`.
  - An admin who belongs to no tournament gets no section at all.

### Quick score entry (pool page)
- **Placement.** Above the sheet, for `can_write`, when the stage has maps.
- **Form.** `<form class="toolbar quick-score" method="post" action="/t/{tid}/scores" id="quick-score">` with the CSRF field and `<input type="hidden" name="entry_origin" value="quick">`.
- **Fields.** All `inline=true`, with ids prefixed `quick-`:
  - `field("pool_map_id", "Map", options=quick_map_options, required=true)`.
    - Options are `(id, "NM1 · Artist - Title")` for maps that have a `beatmap_id`, in pool order.
    - The first option is `("", "Choose map")`.
    - The native select's type-to-find lets "HD2" jump straight there.
  - For captains, `field("osu_id", "Player", options=player_options, value=<own id>)`.
  - `field("score", "Score", inputmode="numeric", autocomplete="off", required=true, placeholder="e.g. 845 120")`.
  - `field("accuracy", "Acc %", inputmode="decimal", autocomplete="off", placeholder="optional")`.
  - `field("mods_text", "Mods", placeholder="FM/TB only, e.g. HDHR", autocomplete="off")`. Always shown; `_mods` ignores it for fixed-mod maps.
- **Primary action.** `button("Save score", variant="primary", type="submit")` is the page's one primary action. Demote any other primary button on the page to secondary.
- **Errors.**
  - When `entry_kind == "quick"`, show the error on the named field through the existing `error=` argument, and keep the values. Field `mods` errors go on `mods_text`.
  - An error without a field shows the existing page alert.
- **Success.**
  - Redirect to `/t/{tid}/pool?stage=…#quick-score` with the flash "Score saved on {slot}."
  - To get the slot, change `_stage_of_map` into a helper that returns the map rather than only its stage id.
- **Per-map dialog.** It stays, because it has misses, combo and link. Its accuracy field becomes optional (`required=false`), with the hint "Optional. From 0 to 100, for example 97.85."
- Every template already formats accuracy with `fmt_acc`, which shows `None` as "-".

### Match stats and results (D3 = B)
- **Tournament nav.** Add `("results", "Results", "/t/" ~ tournament.id ~ "/results")` after "Schedule".
- **`match_stats.html`** (nav current `results`).
  - **Header.** "{{ team1 }} vs {{ team2 }}", with stage, label and final score in muted text.
  - **Toolbar:**
    - "Back to results".
    - "Review predictions", linking to `/review`.
    - For captains who may correct the match, when the tournament is not archived: a "Read lobby again" form that POSTs to the existing `/review/refresh` with `return_to=stats`. Use the same `_may_correct` rule as the review page.
  - **Section "Maps".** One table.
    - Each row: step (Pick/Tiebreaker), `badge(slot)` with the map name, winner, and team totals in tabular figures ("812 345 – 790 001").
    - Under each map row, a `details.map-detail` holds the per-player table: Player, Team, Score, Acc, Mods, Misses, Combo. The winning side's scores are bold.
    - A map with no scores shows "No player scores for this map".
  - **Section "Players".** A table of `PlayerLine`: Player, Team, Maps, Avg score, Avg acc, Maps won.
  - **Footnote.** "Only players on either team's roster are listed; stand-ins still count in the map totals."
  - **Empty states.**
    - No lobby: "This match has no multiplayer link, so there are no stats."
    - Lobby not read: "The lobby has not been read yet. It is read automatically after the match; a captain can read it now."
- **`results.html`.**
  - **Section "Played matches."** The schedule's played table, with "Stats" as the action. Move the row markup into the macro `played_row(view)` in `partials/played_row.html` and use it on both pages.
  - **Section "Our players."** The aggregate `PlayerLine` table.
  - **Empty.** "No match has been played yet."
- **Links elsewhere.** Schedule "Played" rows get a "Stats" button next to "Compare". `review.html` gets a "Match stats" button in its toolbar.

### Scanner page: start from a lobby id
- **New section "Start from a lobby id",** between "Status" and "Skipped ranges", shown only when `status.enabled`.
  - It is a toolbar form, `method="post" action="/admin/scanner/start"`, containing:
    - the CSRF field;
    - `field("start", "Lobby id or link", inline=true, value=start_entered|default(""), placeholder="e.g. 119876543 or https://osu.ppy.sh/community/matches/119876543", autocomplete="off", error=start_error|default(none))`;
    - `button("Start from this id", variant="secondary", type="submit")`.
  - The section stays secondary because "Pause/Resume scanner" is this page's primary action.
  - Hint text under the form (muted, plain):
    - "An older id is read in the background with spare budget, oldest first, while the live scan carries on."
    - "A newer id than the scanner's position ({{ cursor }}) skips the ids in between; they are listed below and can be backfilled."
    - "The newest id osu! has listed is {{ head }}."
- **Confirmation panel** (when `pending_start` is set). Render `alert("warning", "Read {{ size:, }} lobby ids?")`, in the section, with:
  - the text: "Ids {{ start:, }} to {{ end:, }} will be read with spare budget only. At the current spare rate this takes {{ eta text }} (rough estimate)." Use the existing `_eta_text`, extended with hours and days for long times.
  - When the ETA is None, say instead: "There is no spare budget right now, so it starts once the live scan has caught up."
  - If `pending_start` merges existing ranges, add: "It replaces {{ n }} skipped ranges it covers."
  - A form that re-posts `start` with `confirmed=1`, with the primary button "Start backfill" and a text-button link "Cancel" to `/admin/scanner`.
  - Focus moves to the alert title on render: add `tabindex="-1"` and `data-autofocus`. Check whether `app.js` already has an autofocus helper; if not, focus via the `autofocus` attribute on the confirm button.
- **Ranges table changes.**
  - New column "Why" with the reason in plain words:
    - `skipped_backlog` → "Scanner fell behind";
    - `fetch_error` → "osu! kept failing";
    - `admin_skip` → "Chosen by an admin".
  - The "Backfill" cell, while a backfill is running, shows the progress text and a `button("Stop", variant="text", type="submit")` form posting to `/admin/scanner/gaps/{id}/stop`.
  - The section intro becomes: "Ranges of lobby ids that were not scanned live. A backfill reads them oldest first, and only while the scanner is nearly caught up."
- **States:**
  - scanner switched off: no section, only the existing state text;
  - not started yet: the inline error from `plan_start`;
  - invalid input: an inline error;
  - large range: the confirmation panel;
  - success: a flash.

### Styleguide
- Update the swatches to all tokens, including the new ones.
- Add sections:
  - **Toolbar:** an inline select and a button, aligned, with an auto-apply note.
  - **Switch:** off, on, focus and disabled.
  - **Timeline:** one day with yes/maybe/no runs, an unfilled row and a match band, built from a static fixture in `routes/styleguide.py`.
  - **Upcoming list:** one next item plus two more, with sample names "Team A" and "Team B" labelled as samples.
  - **Quick score bar:** disabled sample.
- Keep the existing sections, restyled.

### States per screen
- **Timeline:** empty roster, archived (no update button), timezone detection, no matches, DST day.
- **Dashboard:** no upcoming matches.
- **Stats:** no lobby, lobby not read, a map without scores.
- **Quick entry:**
  - validation error;
  - archived: fields disabled;
  - no maps with beatmaps: the bar is hidden and a muted line reads "Scores can be added once maps have an osu! beatmap."
- **Scanner start:** as listed in its section.

## Steps
After every phase, `pytest` and `ruff check . && ruff format --check .` pass and `CHANGELOG.md` `[Unreleased]` gets a line.

**Phase 1 — Design system and wide layout**
1. Update `tokens.css` per the token table: values, new tokens, contrast comments.
2. Restructure `shell.html`:
   - sticky top bar;
   - a `main` block whose default is `.page` with flashes;
   - a `page_class` block;
   - a `flashes()` macro.

   Set `page--narrow` on the sign-in, error, me, tournament settings and admin tournament form pages.
3. Restructure `layout/tournament.html`: the sidebar (name header, links, mobile select) and the content `main#main` with flashes.
4. Restyle in `app.css`:
   - `.btn`, `.field__control` (with the select chevron), `.field--inline`, `.toolbar`, `.check`, `.tabs`, `.badge`;
   - `.table-wrap` and `.table`: no inner max-height, the `--scroll` variant, sticky header from 1024 px;
   - `.page__header`;
   - `.alert`, `.empty` and `.dialog`: radius tokens only.
5. Add `inline` to `components/field.html` and `scroll` to `components/table.html`.
   - Pass `scroll=true` for the pool sheet, the coverage sheet and the availability edit grid.
   - Delete the `.sheet .table-wrap` rule.
6. Remove the tournament-name subtitles from tournament page headers.
7. Lineup: colgroups, the `.slot-edit` grid with `.slot-edit__pick`, and the "Choose player" label.
8. Run the app and check the pool, lineup, schedule, lobbies, dashboard, review and scanner pages:
   - at 2000, 1280, 768 and 360 px;
   - in dark and in light;
   - the sidebar sits at the left edge, tables use the full width, and there is no horizontal body scroll at 360 px.

**Phase 2 — Auto-apply filters**

9. Add the `data-autosubmit` handler to `app.js`.
10. Convert the GET filter forms on lobbies and lineup, plus any others the grep finds, to `toolbar`, inline fields and a `<noscript>` button.

**Phase 3 — Current round**

11. Add `current_stage` and extend `pick_stage(…, now=None)` in `pool_sheet.py`.
12. Pass `now` in pool, lineup, coverage, pickems and sheet_io (import and export). Change lobbies to `stage: str | None` with the current-stage default.

**Phase 4 — Homepage upcoming**

13. In `schedule.py`, add `_calendar_views`, refactor `feed_events` onto it, then add `UpcomingMatch` and `upcoming_for_user`.
14. In `dashboard.py`, add the clock and `upcoming`. Add the dashboard section and the `.upcoming` CSS.

**Phase 5 — Availability timeline**

15. Add `Column.avatar_url` and fill it in `roster_columns`.
16. Add `build_timelines` and its dataclasses to `services/availability.py`. Spoken labels use the existing `STATE_LABELS`; a run ending at midnight gets the end label "24:00".
17. In `routes/availability.py`:
    - the `edit` parameter;
    - the view and edit contexts;
    - `filled_ids` derived from `found`;
    - `share_url`;
    - `availability.js` loaded in both modes.
18. Add `components/switch.html`, `partials/availability_timeline.html`, the new `availability.html`, and the timeline, switch and legend CSS.

**Phase 6 — Quick score entry and optional accuracy**

19. Add `app/db/migrations/0008_scores_optional_accuracy.sql` (see Data).
20. Allow a missing accuracy everywhere it is read:
    - `Score.accuracy` and `NewScore.accuracy` become `float | None`.
    - Add `score_entry.parse_optional_accuracy` and use it in `record_score`.
    - `sheet.py`: the sort key uses `accuracy if accuracy is not None else -1.0`, and `_mean` of accuracies skips `None`.
    - `coverage.py`: a target accuracy against a `None` accuracy is not met.
    - `csv_io.py`: the export writes an empty cell.
21. In `routes/pool.py`:
    - handle `mods_text` and `entry_origin`;
    - add `quick_map_options` to the context;
    - flash the slot on success and redirect with the `#quick-score` anchor.
22. In `pool.html`, add the quick bar and make the dialog's accuracy optional.

**Phase 7 — Match stats and Results**

23. Add `scores_repo.list_mp_for_lobby`. Make `history.store_lobby_events` take `now` and store per-player mp scores. Thread `now` through `refresh_from_lobby` and `import_pending`, and update the callers.
24. Add `services/match_stats.py` with `build_match_stats` and `tournament_results`.
25. Add `routes/results.py` and register it. Add the "Results" nav item, and `return_to=stats` to `refresh_lobby`.
26. Add the templates `match_stats.html`, `results.html` and `partials/played_row.html`, and the Stats links on the schedule and review pages.

**Phase 8 — Scanner start id**

27. In `repos/scan.py`, add `delete_gaps` and `stop_backfill`.
28. In `services/skimmer.py`, add:
    - `StartError`, `StartPlan`, `plan_start`;
    - `Skimmer.skip_forward`;
    - `start_from`;
    - `backfill_eta_minutes`.

    Extend `_eta_text` in `routes/scanner.py` with hours and days: "about 3 hours", "about 2 days".
29. In `routes/scanner.py`, add `POST /start` and `POST /gaps/{gap_id}/stop`. Re-render with `_context(...)` plus `start_entered`, `start_error` and `pending_start`/`pending_eta`.
30. In `pages/admin/scanner.html`, add the start section, the confirmation panel, the "Why" column and the "Stop" button. Add one paragraph to `README.md`'s scanner section.

**Phase 9 — Styleguide and changelog**

31. Update the styleguide sections and fixture.
32. Complete the `CHANGELOG.md` `[Unreleased]` "Added" and "Changed" entries. Do not bump the version.

## Tests
Follow the existing style: `tests/test_*.py`, fixtures in `tests/fixtures`, and the app client the way `test_pool_page.py`, `test_availability_page.py` and `test_scanner_page.py` use it.

**Current round** (`tests/test_stage_pick.py`, new):
- Stages A[1–7], B[8–14], C[15–21]:
  - now = 10 → B;
  - now = 7.5, A ended and B not started → B, the next upcoming;
  - now = 30 → C, the last.
- No dates at all → the first stage with a released pool (today's rule).
- Overlapping running stages → the later start.
- A stage with only an `end_at` that has not passed counts as running.
- `pick_stage` with a valid `raw` ignores `now`; `pick_stage(now=None)` behaves exactly as today.

**Current round on pages:**
- Active tournament: `GET /t/{id}/pool` without `?stage` marks the current stage's tab `aria-current`. Lineup, coverage and pickems do the same, and the import page preselects that stage.
- Archived tournament → the old rule.
- Lobbies: no `stage` parameter → the current stage is selected; `stage=` → "All stages".

**Homepage** (`tests/test_schedule.py`, `tests/test_pages.py`):
- `upcoming_for_user`:
  - lists only our matches in active tournaments where the user is a member;
  - leaves out done matches and matches that started more than 90 minutes ago;
  - keeps a match that started 30 minutes ago;
  - orders by time and respects `limit`.
- `feed_events` output is unchanged (its existing tests pass).
- The dashboard renders "Upcoming matches" with the first item marked as next, and the empty state when there are none.

**Availability:**
- `build_timelines` (`tests/test_availability.py`):
  - runs merge equal neighbouring states;
  - each day's spans add up to `slot_count`;
  - a DST day in `Europe/Berlin` has 46 or 50 slots, and the hour spans add up;
  - `filled` is true exactly for the ids in `filled_ids`;
  - a band's label is on its first run only;
  - free runs merge equal counts;
  - a mixed row gets the correct summary text.
- Page (`tests/test_availability_page.py`):
  - The default view renders `table.timeline` per day and no slot buttons.
  - In every timeline row the colspans add up to the slot count.
  - A player with one Yes slot that week shows the check. A player with no stored slots shows no check, and the row has `is-unfilled`.
  - `?edit=1` renders today's grid with buttons in the viewer's own column. In edit mode, a captain passing `member=` gets that player's column.
  - "Update my availability" is absent for a guest or non-roster viewer, and when the tournament is archived.
  - The switch is checked when `tz=local`.
  - The share URL contains neither `member` nor `edit`.

**Quick score entry and optional accuracy** (`tests/test_pool_page.py`, `tests/test_sheet.py`):
- Posting `pool_map_id`, `score` and `entry_origin=quick` with no accuracy saves the score with a NULL accuracy. Then:
  - the sheet shows "-" for its accuracy;
  - the average skips it;
  - the lineup tiebreak ranks a NULL accuracy below a real one.
- `mods_text`:
  - `"HDHR"` on a freemod map stores `HDHR` in canonical form;
  - `"XYZ"` → 422, with the error on the quick bar's Mods field and the values kept;
  - on a fixed-mod map it is ignored.
- A player posting another player's `osu_id` → 403; the existing rule still holds.
- The success redirect contains `#quick-score`, and the flash names the slot.
- Coverage: a target accuracy is not met by a score without accuracy.
- The CSV export writes an empty accuracy cell for such a score.

**Migration** (`tests/test_migrate.py`):
- After 0008, existing score rows and the three indexes are still there.
- A NULL accuracy is accepted; 101 is still rejected.

**Match stats:**
- `history.store_lobby_events` (`tests/test_history.py`):
  - stores mp scores for both teams' rostered players;
  - skips non-members and warm-up maps;
  - running it twice adds no duplicate rows.
- `build_match_stats` (`tests/test_match_stats.py`, new):
  - maps come in event seq order;
  - a replayed map uses the last game;
  - player lines have the right means and maps won;
  - no lobby → `has_lobby=False`; lobby not read → `lobby_read=False`.
- `tournament_results` aggregates only our players, and returns no players when no team is marked as ours.
- Routes:
  - `/t/{id}/results` and `/t/{id}/matches/{mid}/stats` return 200 for members;
  - a match of another tournament → 404;
  - a non-member gets the existing access refusal;
  - "Read lobby again" with `return_to=stats` redirects to stats, and without it to review as before;
  - the tournament nav contains "Results".

**Scanner start id** (`tests/test_skimmer.py`, `tests/test_scanner_page.py`):
- `plan_start`, with cursor 1000 and head 1200:
  - 900 → backfill [900, 1000], size 101;
  - 1001 → nothing;
  - 1100 → skip [1001, 1099];
  - 1200 → skip [1001, 1199];
  - 1201 → `StartError` naming 1200;
  - 0 → `StartError`;
  - cursor None or head None → `StartError` "has not started".
- Merging:
  - with gaps [950, 960] and [880, 920], start 900 → backfill [880, 1000], with both gaps as overlapping (a gap ending below the start, e.g. [800, 850], does not overlap and stays as it is);
  - with gap [500, 600], start 900 → that gap is not merged.
- `start_from`, backfill:
  - deletes the merged gaps;
  - inserts one `admin_skip` gap with `backfilled_until = start - 1`;
  - leaves the stored cursor unchanged.
- `start_from`, skip:
  - inserts an `admin_skip` gap without a backfill request;
  - moves the dispenser cursor to `start - 1`;
  - saves the cursor (`job_state` `skimmer`);
  - the next `take()` returns `start`.
- `skip_forward` before the first `check_head` → `StartError`.
- After a skip followed by `check_head` with the same head, no extra `skipped_backlog` gap appears when the distance is at most `SCANNER_MAX_BACKLOG`.
- `backfill_one` picks up the new admin range and never runs while the live backlog is 100 or more (the existing rule, re-asserted with an `admin_skip` gap).
- `stop_backfill`:
  - a gap [100, 199] with `backfilled_until = 149` becomes [150, 199], not requested;
  - a gap that was never requested → False;
  - a finished gap → False.
- `backfill_eta_minutes`:
  - budget 60 and 20 requests per minute → `size / 40`;
  - no spare budget → None.
- Routes:
  - A non-admin gets 403 on `POST /admin/scanner/start` and on `/gaps/{id}/stop`.
  - A missing CSRF token is refused (the existing global check).
  - Input `"https://osu.ppy.sh/community/matches/900"` and `"900"` give the same plan.
  - `"abc"` → 422 with the field error and the value kept.
  - With the scanner switched off → 409.
  - A backfill of more than `scanner_max_backlog` ids without `confirmed` → 200, the confirmation panel shows, and no gap is stored. With `confirmed=1`, the gap is stored and the response redirects 303 with the flash.
  - A small backfill is stored without confirmation.
  - The ranges table shows the "Why" text and a "Stop" button for a running backfill.

**UI wiring:**
- The lobbies and lineup filter forms carry `data-autosubmit` and a `<noscript>` submit.
- `field(inline=true)` renders the `field--inline` class.
- The styleguide renders the new sections (`tests/test_styleguide.py`).

## Decisions for the programmer
All decided by the programmer on 2026-11-13:
- **D1 — "Filled in" check: A.** A player has the check when they saved at least one Yes or Maybe slot in the shown week. No schema change.
- **D2 — Optional accuracy: B.** Migration 0008 rebuilds `scores` with a nullable accuracy. An empty accuracy:
  - shows "-";
  - ranks below any real accuracy in tiebreaks;
  - is skipped in averages and in accuracy targets.
- **D3 — Match stats: B.** A Stats page per match, plus a "Results" item in the tournament menu with the played matches and our players' totals.
- **D4 — Share team availability: A.** The button copies a link to the current week view, which only members can open.
- **D5 — Editing: A** (the planner's recommendation, recorded as the default). "Update my availability" opens today's 30-minute grid as an edit mode. Editing directly on the timeline bars can follow later if wanted.

The planner settled the scanner start id itself (reuse first, no open decision):
- An older id becomes a backfill range read with spare budget. The live cursor is not moved back, because skip-ahead would undo that and it would starve the live scan.
- A newer id is a recorded skip-ahead that can be backfilled.
- A start id above the head is refused.
- A backfill larger than `SCANNER_MAX_BACKLOG` ids needs a confirmation.
- Running backfills get a "Stop" control.

## Risks / follow-ups
- **Score table rebuild.** Migration 0008 rebuilds `scores` and cannot be undone without a backup. The usual advice to back up `/data` before updating applies; say so in the CHANGELOG entry.
- **Sticky headers.** Below 1024 px wide, and inside `table-wrap--scroll`, header stickiness depends on the wrapper. On mid-size screens a wide table scrolls sideways inside its wrapper and its header does not stick to the page. This is accepted and documented in the CSS.
- **Shell restructure.** Moving `main#main` into `tournament.html` touches every tournament page. Keep the `id` for the skip link and the flashes on both paths, and check one page of each kind.
- **Stand-ins.** Per-player stats cover only players on either team's roster. Stand-ins count in the map totals (via team colour) but get no player row; the page footnote says so.
- **Older lobbies.** Lobbies read before this change have events but no per-player scores until "Read lobby again" runs. The background `import_pending` reads each match only once. A one-off re-read job would cost osu! API budget and is not planned.
- **Undated stages.** The current-round rule relies on wybin stage dates. Tournaments whose stages have no dates keep the old behaviour.
- **Scanner start.**
  - A backfill starting far back re-reads ids the live scan already covered, which costs budget but is harmless. It only runs while the live scan is nearly caught up, so a range of millions of ids can take weeks. The confirmation shows a rough ETA, and "Stop" keeps what was read.
  - Requested ranges are read oldest first, so an admin range with a low start id is served before other requested ranges.
  - `skip_forward` relies on the single-process, single-event-loop setup (one uvicorn worker), as the scanner already does.

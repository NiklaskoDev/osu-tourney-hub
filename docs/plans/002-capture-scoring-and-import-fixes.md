# 002 — Captured scores on ScoreV2 tournaments, Google Sheets redirect, phase 4 follow-ups

Status: approved
Type: fix
Version impact: none. Unreleased; entries go under `[Unreleased]` in `CHANGELOG.md`. Branch `fix/capture-scoring`.

## Goal
1. Fix score capture so it works against the real osu! API. The client must send `x-api-version`; see "Finding" below.
2. Let captured osu! scores count on ScoreV2 tournaments, the way the programmer picks under Decision 1.
3. Let the Google Sheets import follow Google's redirect to its CSV download host (`*-sheets.googleusercontent.com`) and nothing else.
4. Fix two phase 4 review findings: the request body is buffered before the size check, and a `finally` can hide the wybin sync error.
5. Tighten three phase 4 tests.

Out of scope:
- Storing ScoreV1 (`legacy_total_score`) for ScoreV1 tournaments.
- Any change to MP lobby scores (phase 5).

### Finding: what osu! actually returns (verified 2026-11-13)
- **Official API docs** ([osu.ppy.sh/docs](https://osu.ppy.sh/docs/index.html), "Score" object, version 20220705 or later). A score has three totals:
  - `total_score`: "lazer's standardised scoring".
  - `classic_total_score`: lazer's "classic scoring".
  - `legacy_total_score`: "score V1", for stable-set scores only, 0 on lazer scores.
  - There is also `total_score_without_mods`, marked for internal use. `legacy_score_id` is set only for stable-set scores.
  - The docs mention ScoreV2 nowhere. **No field carries a stable ScoreV2 figure.**
- **Lazer standardised is not ScoreV2.** The current osu! source ([`ScoreProcessor.ComputeTotalScore`](https://github.com/ppy/osu/blob/master/osu.Game/Rulesets/Scoring/ScoreProcessor.cs); osu! mode does not override it) computes:
  - `500000·acc·comboProgress + 500000·acc^5·accuracyProgress + bonus`, with combo weighted by `combo^0.5`.
  - It started as a near 1:1 port of stable ScoreV2 ([ppy/osu#23638](https://github.com/ppy/osu/pull/23638)) and was changed after that.
  - `classic_total_score` is only a rescale of standardised ([ppy/osu#24924](https://github.com/ppy/osu/pull/24924), osu-web `getClassicTotalScore`).
  - For a stable-set score, `total_score` is converted from its V1 score.
  - Stable ScoreV2 is an unranked mod, so osu! keeps no V2 play to return.
- **Latent bug in phase 4.** The API docs say version 0 applies when the `x-api-version` header is missing. osu-web's [`ScoreTransformer`](https://github.com/ppy/osu-web/blob/master/app/Transformers/ScoreTransformer.php) then sends the legacy format: `score` holds `legacy_total_score`, there is no `total_score` and no `ended_at`.
  - `OsuClient` sends no `x-api-version` (`app/clients/osu.py:217`).
  - `_parse_api_score` requires `total_score`, so against the real API **every captured score is silently dropped**.
  - The tests pass only because `tests/support.py` `FakeOsu` always answers in the new format.
  - Confirmed from the docs and source only, not against the live API. Step 12 checks it live.

## Reuse
- `OsuClient._app_get` / `_request` (`app/clients/osu.py`): gain an optional `headers` argument instead of getting a second request path.
- `sheet.counts_for`, `sheet.uncounted_reason` and `pool_sheet._status` (`app/services/`): the one place the rule "which scores count" lives.
- The `.old-flag` marker in `app/templates/pages/tournament/pool.html`: reused for the new "lazer" marker. No new component.
- `UserFacingError` (`app/web/errors.py`) and `require_csrf` (`app/web/security.py`): the size guard goes in front of the only app-wide form read.
- `FakeOsu` (`tests/support.py`): extended, not replaced.

## Code layout
- `app/clients/osu.py`: send `x-api-version: 20220705` on the user-beatmap-scores request.
- `app/services/sheet.py`: the counting rule from Decision 1.
- `app/services/pool_sheet.py`: status text and cell flag for lazer scores counted on a V2 tournament.
- `app/templates/pages/tournament/pool.html`: "lazer" marker in the cell and a hint line.
- `app/services/sheets_link.py`: allow-list of redirect hosts; port/userinfo checks on redirects.
- `app/web/security.py`: `reject_oversized_body`, called first in `require_csrf`.
- `app/workers/jobs.py`: `sync()` queues pulls only after a successful sync.
- `tests/support.py`, `tests/test_osu_client.py`, `tests/test_sheet.py`, `tests/test_pool_page.py`, `tests/test_capture_routes.py`, `tests/test_sheets_link.py`, `tests/test_csv_io.py`, `tests/test_security.py`, `tests/test_scheduler.py` or `tests/test_capture_routes.py` (job test): tests.
- `CHANGELOG.md`, `docs/plans/001-osu-tourney-hub.md` (one-line amendment to D3).

## Data
- Option A of Decision 1 (recommended): no data change. Captured scores stay `scoring='lazer'`.
- Option D: migration `app/db/migrations/0005_lazer_on_v2.sql` with `ALTER TABLE tournaments ADD COLUMN count_lazer_on_v2 INTEGER NOT NULL DEFAULT 1 CHECK (count_lazer_on_v2 IN (0, 1));`.
- No option stores a second score figure, because osu! provides no ScoreV2 figure (see Finding).

## Interfaces
- `OsuClient._app_get(url, priority, params=None, headers=None)` and `_request(..., headers=None)`. Extra headers are merged with `Authorization`, which always wins.
- New constant `SCORE_API_VERSION = "20220705"` in `app/clients/osu.py`. It is sent only by `get_user_beatmap_scores`. Other endpoints keep their current format on purpose: their parsers are written for it.
- `sheet.counts_for(score, scoring) -> bool` and `sheet.uncounted_reason(...)` keep their signatures under option A.
- New `sheet.is_stand_in(score, scoring) -> bool`: True when a lazer score counts on a V2 tournament.
- `sheets_link._allowed_redirect(url: str) -> str` replaces `_same_host`. It raises `CsvImportError(NOT_SHARED)` otherwise.
- `security.reject_oversized_body(request) -> None` raises `UserFacingError(413, "This upload is larger than 2 MB. Files may be at most 1 MB.")`.
- New constant `MAX_REQUEST_BODY_BYTES = 2 * MAX_CSV_BYTES` (2 097 152) in `app/web/security.py`. Import `MAX_CSV_BYTES` from `app.services.csv_text`.

## Security and user data
- No new secrets.
- Sheets fetch: the redirect allow-list grows by one tight host pattern. Every hop is still checked: https only, no port, no userinfo, at most `MAX_REDIRECTS`, 1 MB cap.
- The address is still rebuilt from the sheet id. A private sheet still redirects to `accounts.google.com` and is refused as `NOT_SHARED`.
- Body cap: state-changing requests with `Content-Length` over 2 MB are refused before the form is parsed. This applies app-wide, because `require_csrf` reads the form before any route runs; a check in `start_import` alone would come too late.

## UI
- Pool page, ScoreV2 tournament (option A or D on):
  - A counted lazer score shows the small marker `lazer`, with the title "Lazer standardised score from osu!, not ScoreV2". Same markup and class as `old`.
  - The score list status reads "Best (lazer score)" / "Counts (lazer score)".
  - The hint under the table gets one sentence: "Lazer scores from osu! count too and are marked lazer; they are close to ScoreV2 but not the same."
- No other screens change. Option D also adds a checkbox to the settings page; see Decision 1.

## Steps
1. **API version header.** In `app/clients/osu.py`:
   - Add `headers` to `_request` (merged into the dict built at line 217) and pass it through `_app_get`.
   - In `get_user_beatmap_scores`, call `_app_get(url, priority, {"mode": ...}, headers={"x-api-version": SCORE_API_VERSION})`.
   - Update the docstring of `get_user_beatmap_scores`. The comment says why: without the header osu! answers in the pre-2022 format, without `total_score`.
2. **FakeOsu mirrors osu-web.** In `tests/support.py` `_user_scores`:
   - When `x-api-version` is missing or below `20220705`, answer in the legacy shape. For each score, drop `total_score`, `legacy_total_score`, `classic_total_score` and `ended_at`; add `score` = `legacy_total_score` and `created_at` = `ended_at`.
   - Otherwise answer as now.
   - Also add `"classic_total_score": 1_100_000` and `"legacy_score_id": None` to the default in `add_api_score`.
3. **Counting rule (option A).** In `app/services/sheet.py`:
   - `counts_for` returns `score.passed and (score.scoring == scoring or is_stand_in(score, scoring))`.
   - `is_stand_in` is `scoring == "v2" and score.scoring == "lazer"`.
   - `uncounted_reason` uses the same test.
   - Update the module docstring: V1 never mixes into V2; lazer counts on V2, marked.
   - `rank_key` stays as is: score, then accuracy.
4. **Status and marker.** In `app/services/pool_sheet.py`:
   - `_status` appends " (lazer score)" to "Best"/"Counts" when `sheet.is_stand_in(score, tournament.scoring)`.
   - Add `lazer: bool` to `Cell` (set in `_map_row` from `sheet.is_stand_in(found.overall, tournament.scoring)`; pass `tournament` into `_map_row`) and to `ScoreLine`.
5. **Template.** In `pool.html`:
   - `score_cell(score, old, show_mods, lazer)` renders `<span class="old-flag" title="Lazer standardised score from osu!, not ScoreV2">lazer</span>` after the `old` flag. Call it with `cell.lazer`.
   - Add the hint sentence (UI section) to the paragraph at line 63, only when `tournament.scoring == "v2"`.
   - Read `docs/ai/ui.md` first.
6. **Option D only** (skip under A):
   - Add migration 0005.
   - Add `count_lazer_on_v2: bool = True` to `Tournament`.
   - Load it in `repos/tournaments.py`.
   - Extend `set_rules` with it.
   - Add `SettingsForm.count_lazer_on_v2`.
   - Add a checkbox "Count lazer scores from osu! in ScoreV2 averages" in `settings.html`, shown only when scoring is ScoreV2.
   - `sheet.is_stand_in(score, scoring, allowed)` gets the flag. Callers pass `tournament.count_lazer_on_v2`.
7. **Docs in code.**
   - Fix the docstrings that say captured scores never count on V2: `score_capture.py` line 6, `repos/scores.py` `insert_api`, `domain/models.py` ~207.
   - Add to D3 in `docs/plans/001-osu-tourney-hub.md`: "Amended by 002: captured lazer scores count on ScoreV2 tournaments, marked."
8. **Sheets redirect.** In `app/services/sheets_link.py`:
   - Add `CSV_DOWNLOAD_HOST = re.compile(r"doc-[0-9a-z-]{1,40}-sheets\.googleusercontent\.com")`, or the Decision 2 alternative.
   - Replace `_same_host` with `_allowed_redirect(url)`. It requires `scheme == "https"`, `port is None` (catch `ValueError` as refused), `username is None`, `password is None`, and `hostname == SHEETS_HOST or CSV_DOWNLOAD_HOST.fullmatch(hostname)`.
   - Update the module docstring.
   - `MAX_REDIRECTS`, `_read` and the 1 MB cap are unchanged.
9. **Body cap.** In `app/web/security.py`:
   - Add `reject_oversized_body(request)`. Unsafe methods only.
   - If `content-length` is present and not a decimal integer → 400 "The request could not be read."
   - If it is above `MAX_REQUEST_BODY_BYTES` → 413.
   - If it is absent and `transfer-encoding` is present → handled by Decision 3.
   - Call it first in `require_csrf`, before the form is read.
   - Leave the route's `upload.read(MAX_CSV_BYTES + 1)` check in place: it still gives the exact 1 MB message.
10. **Wybin sync job.** In `app/workers/jobs.py` `sync()`, drop the `try/finally`. Call `enqueue_active(...)` after `await sync_active(...)` returns. If the sync fails, the hourly `CAPTURE_ENQUEUE` job queues whatever the sync managed to add. Keep the comment, reworded.
11. **Tests.** Write all cases listed below. Run `pytest` and `ruff check . && ruff format --check .`.
12. **Manual checks (programmer).**
    - a) Live score pull. Use the programmer's own osu! app credentials and a ranked map they have a score on. Press "Refresh my scores" on the pool page, then wait for the drain. The score must appear with source "osu!".
    - b) Redirect host. Use one public sheet the programmer provides. Run the command below and confirm the printed host matches `CSV_DOWNLOAD_HOST`. It prints only the status and the host, never the signed URL:

      `python -I -c "import httpx,sys; r=httpx.get(sys.argv[1]); print(r.status_code, httpx.URL(r.headers.get('location','')).host)" "https://docs.google.com/spreadsheets/d/<ID>/export?format=csv"`

      Then import that sheet through the app and expect the preview.
    - Do not paste the sheet id or the URL into the plan or the changelog.
13. **`CHANGELOG.md` `[Unreleased]`.**
    - Fixed: scores from osu! are pulled in the current score format (they were left out before).
    - Fixed: the Google Sheets import follows Google's download redirect.
    - Fixed: oversized uploads are refused before they are read.
    - Fixed: a failed wybin sync reports its own error.
    - Changed: lazer scores from osu! count on ScoreV2 tournaments, marked "lazer".

## Tests
- `test_osu_client`: the score request carries `x-api-version: 20220705` (`osu.requests[-1].headers`).
- `test_osu_client`: if the header were missing, the fake answers in the legacy shape. Pin this with a direct `httpx` call to the fake transport without the header: the response has `score` and no `total_score`.
- `test_osu_client` (tester note 3): `test_a_map_osu_has_no_scores_for_gives_an_empty_list`.
  - Set `osu.failures.append(404)` so the 404 is explicit, not implied by an empty dict.
  - Assert exactly one request to `/beatmaps/7777/scores/users/2002/all` (no retry), and that the result is `[]`.
- `test_sheet`:
  - On "v2", a passed lazer score counts and `is_stand_in` is True.
  - A v1 score still does not count on "v2".
  - On "lazer", a v2 score does not count.
  - On "v1", a lazer score does not count.
  - In `best_scores`, a 950 000 lazer score beats a 940 000 v2 score on "v2".
- `test_pool_page`:
  - Replace the assertion at line 126 (`"Not counted: Lazer standardised score"`) with: on a V2 tournament a lazer score shows `Best (lazer score)` and the cell contains `>lazer</span>`.
  - On a V1 tournament, the lazer score still shows `Not counted: Lazer standardised score`.
- `test_capture_routes`: a captured score counts on the default (V2) tournament and appears in the team average.
- `test_sheets_link`:
  - A redirect to `https://doc-0s-6g-sheets.googleusercontent.com/export/x` is followed and its CSV returned.
  - Refused (`NOT_SHARED`), each parametrised:
    - `http://doc-0s-6g-sheets.googleusercontent.com/...`
    - `https://doc-0s-6g-sheets.googleusercontent.com:8443/...`
    - `https://user@doc-0s-6g-sheets.googleusercontent.com/...`
    - `https://evil.googleusercontent.com/...`
    - `https://doc-0s-6g-sheets.googleusercontent.com.evil.example/...`
    - `https://lh3.googleusercontent.com/...`
    - `https://accounts.google.com/...`
  - The 1 MB cap applies on the googleusercontent hop too.
  - `https://docs.google.com:444/...` is now refused as well.
- `test_security`:
  - A POST with `Content-Length: 2097153` gets 413 and the form is never read (assert that no CSRF error is raised, i.e. the status is 413, not 403).
  - Exactly 2 097 152 passes the guard.
  - A non-numeric `Content-Length` gets 400.
  - A test for the Decision 3 case.
- Job test (`test_capture_routes`, next to `test_a_wybin_sync_queues_the_pulls_for_maps_it_brought_in`): when `sync_active` raises, the job's recorded error is the wybin error and nothing is queued. When it succeeds, pulls are queued (existing test).
- `test_csv_io` (tester note 1): `test_the_export_follows_the_stage_in_the_address` asserts the full `finals.text`, header line and row, with `==` instead of `in`. Check that the other export tests already use full equality; leave them.
- `test_csv_io` (tester note 2): a new test, `test_players_not_on_the_roster_are_named_once_each_in_the_preview`.
  - Upload rows for `ghost` twice, the guest `player2004` once and a good row.
  - Assert `plan.unknown_players == ["ghost", "player2004"]` through `csv_io.plan_import`.
  - Assert the preview contains exactly `<p>ghost, player2004. Add them on the Members page`.
  - A second case with `SHOWN_UNKNOWN + 2` unknown names asserts `and 2 more`.
  - Remove the vague line 492 assertion from `test_bad_rows_are_listed_and_do_not_stop_the_good_ones`.

## Decisions for the programmer
1. **How captured scores count on ScoreV2 tournaments.** osu! offers no ScoreV2 figure (see Finding), so true ScoreV2 cannot be stored. Options:
   - **A.** Count lazer standardised as a stand-in on V2 tournaments, with a visible `lazer` marker. No schema change, reversible in code. Averages then mix two similar but different formulas.
   - **B.** On V2 tournaments, rank and average captured scores by accuracy only. This needs a second ranking path in `sheet.py`/`lineup.py` and separate averages. Large change; mixed columns confuse the sheet.
   - **C.** Keep as is: captured scores count only on lazer-scoring tournaments, and V2 tournaments rely on hand-entered and (phase 5) MP scores.
   - **D.** Like A, but a per-tournament switch, default on. Needs schema migration 0005, a settings checkbox and repo changes.
   - Sub-question for A/D: should the stand-in rule apply to all `scoring='lazer'` rows (hand-entered in a lazer tournament too), or only `source='api'`? Recommendation: all lazer rows. They are the same metric.
   - **Recommendation: A.** It does what was asked, with no schema change, and the marker keeps it honest. Choose D only if some V2 tournaments must stay strictly V2.
2. **Redirect host match.**
   - (a) Tight pattern `doc-[0-9a-z-]{1,40}-sheets.googleusercontent.com`. It must be confirmed by manual check 12b.
   - (b) Any `*.googleusercontent.com` host. It also hosts arbitrary user content, so it is looser than needed.
   - **Recommendation: (a)**, widened only if 12b shows a different host.
3. **Unsafe request without `Content-Length` (chunked body).**
   - (a) Refuse with 411 "Send the file again from the page." Browsers and htmx always send `Content-Length` for forms.
   - (b) Allow it. Starlette then spools the body to a temp file with no cap.
   - **Recommendation: (a).**
4. **`x-api-version` fix.** It is a bug fix to the external API call; no new dependency or service is involved. Keep it in this plan, or split it out as a hotfix first? **Recommendation: keep it here.** Decision 1 is meaningless without it, and the branch is unreleased.

### Decided (programmer, 2026-11-13)
1. Option A: lazer standardised scores count on ScoreV2 tournaments with a visible `lazer` marker, no schema change; applies to all lazer-scored scores (captured and hand-entered).
2. (a): the tight `doc-...-sheets.googleusercontent.com` pattern.
3. (a): refuse unsafe requests without `Content-Length` with 411.
4. The `x-api-version` fix stays in this plan.

Implementation note (2026-11-13): the body-size guard became `RequestBodyLimitMiddleware` in `app/web/security.py` instead of a call in `require_csrf`, because FastAPI parses a route's form fields before any dependency runs, so a dependency would come after Starlette had already buffered the body.

Minor choice already made: the wybin sync job queues pulls only after a successful sync, rather than guarding the `finally`. The hourly enqueue job covers a failed sync, and this is simpler.

## Risks / follow-ups
- Option A/D: averages on V2 tournaments mix lazer standardised (combo `^0.5`, 500k/500k split) with real V2 from MP lobbies. They can differ by tens of thousands on non-FC plays. The marker makes this visible; it does not remove it.
- A stable-set score's `total_score` is osu!'s conversion from V1, which is an estimate.
- Step 12a can only be checked live. If osu! still drops scores after the header fix, stop and report. Do not iterate on guesses.
- Google may change its download host. Then the import fails with `NOT_SHARED`, and the pattern needs one line changed.
- Follow-up (not planned): `legacy_total_score` could feed ScoreV1 tournaments for stable-set scores.

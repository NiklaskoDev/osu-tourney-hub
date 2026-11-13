# Changelog

Versions follow `{major}.{minor}.{hotfix}[-prN]` — see `docs/ai/versioning.md`.
Sections per version: Added, Changed, Fixed, Removed, Security.

## [Unreleased]
### Added
- Project scaffold: web app with a health check at `/healthz`, configuration from environment variables, database migrations, security headers, and a design styleguide page.
- Docker image (`docker build -t osu-tourney-hub .`) that runs as the user and group set by `PUID`/`PGID`, with data in `/data`.
- Unraid template (`unraid/osu-tourney-hub.xml`) and icon.
- README with osu! OAuth setup, `docker run`, Unraid install and configuration table.
- Log in with osu! (OAuth 2 with state and PKCE). A session ends 14 days after the last visit; "Log out everywhere" on the new Profile page ends all of them.
- Tournament access by role: administrators (`ADMIN_OSU_IDS`), captains, co-captains, temporary captains, players and guests. People outside a tournament get "not found"; archived tournaments are read-only.
- Tournament list, tournament overview and a Members page: change roles, mark substitutes, set a temporary captain period, add a guest by osu! user id, remove members. The last captain cannot be demoted or removed.
- Profile page: timezone, download my data, delete my data (needs a login from the last five minutes), log out everywhere.
- Database tables for tournaments, rosters, teams, stages and pool maps (migration 0002).
- `OSU_API_RATE_PER_MIN` is now used: all osu! API calls share one rate limit, retry on errors and back off.
- Admin page: import a tournament from wybin by address or slug (stages, map pool with NM/HD/HR/DT/FM/TB slots, teams, players; the players of your team become members), sync it on demand, archive, restore, hide or delete it (delete needs a recent login and the acronym typed).
- Tournaments that are not on wybin can be created by hand, with stages and a map pool whose title, difficulty and length are looked up on osu!.
- Link your wybin account on the Admin page: tournaments you sign up for are imported automatically (checked every six hours; ones that ended over 30 days ago arrive archived; a hidden tournament is never imported again).
- Active wybin tournaments are updated every `WYBIN_POLL_MINUTES`; a failed sync keeps the stored data and shows a generic error on the Admin page. New setting `WYBIN_MIN_INTERVAL_SECONDS` spaces the requests to wybin.
- Tournament overview shows game mode, team format, our team, stages with their format and whether the pool is published. New Settings page for captains: scoring type, freemod rule and lineup size per stage; administrators also set our team and the final placing.
- Game modes: mod tables per mode (osu!, taiko, catch, mania incl. key mods) for required mods on map slots.
- An archived tournament is never rewritten by an import or sync, a deleted wybin tournament is not imported again automatically (an admin can still import it by hand), and a re-synced map pool keeps each map's row when wybin reorders it.
- A failed or partial wybin job, including an unreadable map pool endpoint, shows on the Admin page with how many tournaments it affected.
- Importing or syncing a tournament whose map pool cannot be read now shows a warning instead of plain success.
- Mappool sheet per stage on the new Pool page: every member's best score and accuracy on each map, a team average per map, personal and team averages (overall and per mod bracket, with how many maps they cover), map stats, and all scores and notes per map. Players add their own scores (captains add them for anyone) and notes (for the team or only themselves). Only scores in the tournament's scoring type count; scores older than six months are marked old and still count. A score entered in one tournament shows in another with the same beatmap and mods, with a "from <tournament>" hint.
- Lineup page: the players with the best scores per map for a stage (substitutes only when asked for, freemod rules respected, maps with too few scores flagged). Captains copy the suggestion as a draft, edit it and mark it final; everyone else reads it.
- Download my data and Delete my data now include the scores and notes you added.
- Automatic score capture: each roster player's scores on the maps of the current and next stage are pulled from the osu! API (ranked and loved maps, passed scores, any date) and stored once per osu! score id as lazer standardised scores. Pulls are queued after a wybin sync and every hour, skipped within `CAPTURE_REFRESH_HOURS` (new setting, default 24), and run at a lower rate-limit priority than anything a person waits for. Two new jobs show on the Admin page.
- "Refresh my scores" on the Pool page pulls your own scores at once (once per 10 minutes); captains can pull every player of a team (`POST /t/{id}/opponents/{team}/capture`, once per team per hour; the button comes with the match pages).
- Pool page: "Export CSV" (best score per player and map) for everyone on the roster, and the Lineup page exports the saved lineup. Cells that look like spreadsheet formulas are exported as text.
- Import scores (captains): upload a CSV file (1 MB, 5000 rows, UTF-8) or paste a public Google Sheets link. A preview shows the detected columns, which rows will be imported and which are refused and why; scores already on the sheet are skipped, so importing twice adds nothing. Only docs.google.com and the download address Google redirects it to are ever contacted.
- Matches from wybin (migration 0005): every sync also stores the tournament's matches with their teams, stage, time, result and lobby link. Team-tournament match data is read tolerantly because its shape on wybin is not confirmed; if the matches cannot be read the stored ones stay and the sync warns.
- Availability page: a week grid with a row per 30-minute slot and a column per player, shown in UTC or in your own timezone (daylight-saving changes included). Click a slot to cycle Yes, Maybe, No, drag down your column or use Shift with the arrow keys to set a range; captains can edit anyone's row. Our matches are shaded across their rows, and each row counts how many players are free against the lineup size. Slots are read aloud with whose they are, the time and the answer.
- Schedule page: upcoming matches with opponent, start time and countdown, played matches with the result, "Find a slot" (best start times in the next two weeks by who is free for the whole match, with the opponents' local times and a penalty outside 10:00 to 23:00) and "Pull scores" for the opponent.
- Calendar feed: create a private `.ics` link on your profile (shown once, only its hash is stored, revocable) that lists your team's matches; its address is kept out of the access log.
- Lineup page: "Only players available at match time" keeps players who answered Yes for the whole of the chosen match.
- Scores of the next opponent's players are pulled automatically, after our own roster.
- Availability CSV export (`/t/<id>/export/availability.csv`); availability is part of "Download my data" and "Delete my data", and leaves with a member who is removed.
- Lobby scanner (migration 0006): reads every new osu! multiplayer lobby id, whatever the lobby is called, and keeps lobbies in which a map of a current pool was played, with the players found (only those registered in the tournament, opponents included) and their scores, which count on the pool sheet as multiplayer scores. It starts at the newest lobby (no history), reads open lobbies once after they close, retries a failing id after a growing pause and, after three failures, records it as a skipped range ("fetch_error") with a Backfill button and counts it as an error (migration 0006 allows that reason and has an `errors` counter), skips deleted or private ones, jumps ahead and records the skipped range when it falls too far behind, and stays inside its own osu! request budget below the overall limit. New settings `SCANNER_ENABLED`, `SCANNER_BUDGET_PER_MIN`, `SCANNER_WORKERS`, `SCANNER_MAX_BACKLOG`, `SCANNER_OPEN_MAX_HOURS`; the app does not start when the scanner budget is not below `OSU_API_RATE_PER_MIN`.
- Admin page "Scanner status" (the scanner's jobs also show in the job table): errors of the last hour, cursor, newest id, backlog, new and scanned ids per minute, requests per minute, open lobbies, skipped ranges with a "Backfill" button, last error, pause and resume; refreshes every 15 seconds and warns when the scanner falls behind.
- Lobbies page: lobbies with pool maps played, maps and players by team (our team marked), filters by stage and player, and "Add a lobby link" for lobbies the scanner missed. Lobby links from wybin matches are read as well (new job "Read multiplayer lobbies linked on wybin", every `WYBIN_POLL_MINUTES`), stored even without a pool map; a link osu! does not have is given up on after three tries.
- Coverage page: who has a score on which map, the likely lineup, flags for maps without enough scores, and practice targets that captains set per team or player (shown as met or missed).
- The Pool page's per-map score list marks lazer scores like the sheet does.
- Styleguide and the availability cell: the cell's accessible name carries its state ("Mon 16 Nov 18:00: Yes").

### Changed
- Archiving a hidden tournament is refused until it is restored, mirroring hiding an archived one.
- `/styleguide` is now for administrators only; the `STYLEGUIDE_PREVIEW` setting is removed.
- `/healthz` opens the database read-only, so a missing database file is reported as unhealthy instead of being created.
- Only a captain or an administrator can make someone captain, take the captain role away or remove a captain. Co-captains and temporary captains keep every other Members action; the captain option and the Remove button are hidden from them, and the server refuses the change.
- Guests may read team notes on the Pool page (as before); they still cannot write or delete notes. Now covered by a test.
- Deleting a tournament says on the Admin page that lineups and notes go with it and that scores are kept without their tournament link.
- README: port 8080 must only be reachable through the reverse proxy; corrected which settings are read yet.
- Lazer scores from osu! count on ScoreV2 tournaments (osu! offers no ScoreV2 figure), marked "lazer" on the Pool page; ScoreV1 scores still never count there.

### Fixed
- A refused lineup save keeps the "include substitutes" choice; `subs` in the lineup address only counts when it is `1`.
- Scores from osu! are pulled in the current score format; before, osu! answered in an older format and every pulled score was left out.
- The Google Sheets import follows Google's redirect to its CSV download address (`doc-...-sheets.googleusercontent.com`) and refuses any other target.
- A failed wybin sync reports its own error instead of one from the score queue.

### Security
- State-changing requests larger than 2 MB, with an unreadable `Content-Length`, or without a `Content-Length` (chunked) are refused before their body is read.
- CSRF token on every state-changing request, a per-IP limit of 20 requests a minute on the login routes, and session cookies that are HttpOnly, Secure (on https) and SameSite=Lax.

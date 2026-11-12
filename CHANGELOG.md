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
- Not in this version: importing matches and schedules from wybin. It arrives with the match schedule in a later phase (the tables for it do not exist yet).
- An archived tournament is never rewritten by an import or sync, a deleted wybin tournament is not imported again automatically (an admin can still import it by hand), and a re-synced map pool keeps each map's row when wybin reorders it.
- A failed or partial wybin job, including an unreadable map pool endpoint, shows on the Admin page with how many tournaments it affected.

### Changed
- `/styleguide` is now for administrators only; the `STYLEGUIDE_PREVIEW` setting is removed.
- `/healthz` opens the database read-only, so a missing database file is reported as unhealthy instead of being created.
- README: port 8080 must only be reachable through the reverse proxy; corrected which settings are read yet.

### Security
- CSRF token on every state-changing request, a per-IP limit of 20 requests a minute on the login routes, and session cookies that are HttpOnly, Secure (on https) and SameSite=Lax.

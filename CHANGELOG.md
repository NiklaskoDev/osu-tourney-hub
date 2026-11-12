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

### Changed
- `/styleguide` is now for administrators only; the `STYLEGUIDE_PREVIEW` setting is removed.
- `/healthz` opens the database read-only, so a missing database file is reported as unhealthy instead of being created.
- README: port 8080 must only be reachable through the reverse proxy; corrected which settings are read yet.

### Security
- CSRF token on every state-changing request, a per-IP limit of 20 requests a minute on the login routes, and session cookies that are HttpOnly, Secure (on https) and SameSite=Lax.

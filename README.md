# osu! tourney hub

Self-hosted website for an osu! tournament team, shipped as one Docker container (built for
Unraid). Log in with osu!, import tournaments from wybin, keep a mappool sheet with scores, lineup
suggestions, availability, an MP lobby skimmer and matchup predictions.

Status: early development. The container, configuration, database migrations, `/healthz`, login
with osu!, tournament roles and member management, tournaments (imported from wybin or entered by
hand) with stages, map pools and teams, the mappool sheet (scores, notes, averages) with lineup
suggestions, and an admin-only design `/styleguide` exist; the other features arrive phase by phase (see `docs/plans/`).

## Set up the osu! OAuth application

1. On osu!, open Settings > OAuth > New OAuth Application.
2. Name it, and set the callback URL to `<PUBLIC_URL>/auth/callback`, for example
   `https://tourney.example.com/auth/callback`.
3. Keep the client ID and client secret; they become `OSU_CLIENT_ID` and `OSU_CLIENT_SECRET`.

## Run it

### Build from source

```sh
docker build -t osu-tourney-hub .
```

### docker run

```sh
docker run -d --name osu-tourney-hub \
  -p 8080:8080 \
  -v /mnt/user/appdata/osu-tourney-hub:/data \
  -e PUID=99 -e PGID=100 \
  -e OSU_CLIENT_ID=... \
  -e OSU_CLIENT_SECRET=... \
  -e PUBLIC_URL=https://tourney.example.com \
  -e SESSION_SECRET=... \
  -e ADMIN_OSU_IDS=... \
  ghcr.io/niklaskodev/osu-tourney-hub:latest
```

Use `osu-tourney-hub` instead of the `ghcr.io/...` name to run a locally built image. Generate
`SESSION_SECRET` with `openssl rand -hex 32` (at least 32 characters, or the app refuses to start).

### Unraid

```sh
curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-osu-tourney-hub.xml https://raw.githubusercontent.com/NiklaskoDev/osu-tourney-hub/main/unraid/osu-tourney-hub.xml
```

Then: Docker tab > Add Container > Template > `osu-tourney-hub`, fill in the required fields, Apply.

### HTTPS and reverse proxy

The container serves plain HTTP on port 8080. osu! login and secure cookies need HTTPS, so put a
reverse proxy in front (SWAG, Nginx Proxy Manager, Traefik, ...) that terminates TLS and forwards to
port 8080. `PUBLIC_URL` must be the public https address; it is the only address the app uses to
build redirects.

Port 8080 must only be reachable through that reverse proxy. The server is started with
`--forwarded-allow-ips '*'`, so it believes the `X-Forwarded-For` and `X-Forwarded-Proto` headers of
whoever connects. Someone who can reach port 8080 directly can forge them, which defeats the per-IP
limit on the login routes and the secure-cookie handling. Do not publish the port on the internet or
your LAN: on Unraid use a custom Docker network shared with the proxy, or bind it to the proxy host
only (`-p 127.0.0.1:8080:8080`).

The reverse proxy must buffer request bodies (nginx does by default). The app refuses a form post that has no `Content-Length`, which is what a streamed (chunked) body looks like, with `411 Length Required`.

The live match page keeps one server-sent event stream open per viewer (`/t/<id>/live/<match>/stream`). The app answers it with `X-Accel-Buffering: no` and a heartbeat every 15 seconds; a proxy that buffers responses or closes idle connections sooner than that delays or breaks the live updates.

### Backup and a single process

All state lives in `/data` (`app.db`, plus `app.db-wal` and `app.db-shm` while running). Back up that
folder; the Unraid appdata backup covers it. Run exactly one container and one server process:
background jobs and live updates run in-process, so do not add uvicorn `--workers`.

## Logging in and roles

Everyone logs in with their osu! account; the app reads only the osu! user id, username, avatar and
country. The osu! ids in `ADMIN_OSU_IDS` are administrators and can open every tournament. Inside a
tournament there are four roles: captain and co-captain (can manage members; only captains and
administrators can make or remove a captain), player, and guest (read-only, may read team notes).
A player can be made temporary captain until a set time. Captains add guests by osu!
user id on the Members page. Each person can download or delete their data on the Profile page.
A session ends 14 days after the last visit; "Log out everywhere" on the Profile page ends all of them.

## Tournaments

Administrators use the Admin page. "Import from wybin" takes a tournament address or slug (for
example `https://wybin.xyz/4cwc25/home`) and copies its stages, map pool, teams and players. The
players of your team (the team that holds the importing admin) become members; captains on wybin
become captains in the app, and later syncs only add people. Active wybin tournaments are refreshed
every `WYBIN_POLL_MINUTES`. Link your wybin account to have the tournaments you sign up for
imported automatically (checked every six hours; tournaments that ended more than 30 days ago
arrive archived). Tournaments that are not on wybin can be created by hand, with stages and maps
(title and difficulty come from osu!). Archive a tournament to make it read-only; hide it to remove
it from everyone but administrators.

The app reads wybin anonymously and only reads: at most one request every `WYBIN_MIN_INTERVAL_SECONDS`
seconds, and nothing is ever written back. wybin has no published API terms, so its response
format may change; the Admin page then shows a failed job and keeps the stored data.

## Availability, schedule and calendar

Every sync of a wybin tournament also reads its matches. The Availability page shows one week with a
row per 30-minute slot and a column per player, in UTC or in your own timezone (saved on your
profile, else the browser's). Slots are stored in UTC, so everyone sees the same instants. Click a
slot to cycle Yes, Maybe and No, or drag down your own column (or hold Shift with the arrow keys
and press Space) to set a range; captains can edit anyone's column. A slot with no answer counts as
No. Your team's matches are shaded and last 90 minutes for planning.

The Schedule page lists upcoming and played matches. "Find a slot" suggests start times in the next
two weeks: a player counts when they answered Yes for the whole match (Maybe counts half), and a
start is marked down for each opponent for whom it falls outside 10:00 to 23:00 local time
(opponent timezones come from wybin). The Lineup page can keep only players available at a match.

On your profile, "Create calendar link" makes a private `.ics` address for your calendar app with
your team's matches. Anyone with the address can read it, it is shown once, and you can revoke it
or replace it at any time. Keep the reverse proxy from logging the full request path of
`/feeds/...` if you can; the app itself logs it as `/feeds/<redacted>.ics`.

## Scores from osu!, CSV export and import

The app pulls each roster player's scores on the maps of the current and the next stage from the
osu! API (ranked and loved maps only; osu! keeps nothing for others). New pulls are queued after a
pool sync and every hour, and skipped when the same player and map were pulled within
`CAPTURE_REFRESH_HOURS`. "Refresh my scores" on the Pool page queues your own pulls at once (once per
10 minutes); captains can pull a whole team (once per team per hour). Pulls share the osu! rate
limit at a lower priority than anything a person is waiting for. Captured scores are lazer
standardised scores, so they count in tournaments whose scoring is set to lazer on the Settings page.

The Pool page has "Export CSV" (everyone on the roster) and, for captains, "Import scores": upload a
CSV file (up to 1 MB and 5000 rows, UTF-8) or paste the link of a Google Sheet shared with "Anyone
with the link". Only `https://docs.google.com/spreadsheets/d/...` links are accepted and only
docs.google.com is contacted. The import shows a preview with the detected columns and every refused
row before anything is stored; scores already on the sheet are skipped. The Lineup page exports the
saved lineup.

## Lobby scanner, lobbies and coverage

The scanner reads every new osu! multiplayer lobby id, whatever the lobby is called, and keeps the
lobbies in which a map of a current pool was played: the released stages of active tournaments whose
end date has not passed. On its first run it starts at the newest lobby and never reads history.
Each stored lobby lists the pool maps played and the players found; only players registered in the
tournament (on a team or the roster, opponents included) are noted, and their scores are added to the
pool sheet as multiplayer scores. A lobby that is still open is read once after it closes (long ones
in steps), deleted or private lobbies are skipped and counted, and a lobby is stored once however it
was found. Lobby links on wybin and links a player pastes on the Lobbies page are read too, and
stored even when no pool map was played in them.

The scanner uses at most `SCANNER_BUDGET_PER_MIN` osu! requests a minute, inside `OSU_API_RATE_PER_MIN`
and at a lower priority than anything a person waits for. The estimate is about 3 to 5 new lobbies a
minute, so the default 30 leaves room to spare. The admin page "Scanner status" shows the real
figures, refreshed every 15 seconds, warns when the scanner falls behind, and lists ranges it skipped
(after `SCANNER_MAX_BACKLOG` lobbies behind it jumps ahead) with a "Backfill" button; a backfill only
runs while the scanner is nearly caught up. "Pause scanner" stops it until resumed.

The Coverage page shows for each map of a stage who has a score (from any source), marks the players
of the likely lineup (the current suggestion), flags maps where nobody in it, or fewer than the lineup
size, has a score, and shows captains' practice targets (a score and/or accuracy for the team or one
player) as met or missed.

## Optional AI estimate

With `AI_PROVIDER`, `AI_MODEL` and (for a hosted provider) `AI_API_KEY` set, a captain can press
"Ask the AI for an estimate" on a match page. The answer (expected winner and chance, bans, picks and
a short reasoning) is stored and shown under "AI estimate", next to the statistical prediction, which
stays the default. A captain can ask five times an hour. Providers are `anthropic` (Messages API) and
`openai` (chat completions at `AI_BASE_URL`, so OpenAI, OpenRouter or a local server), called with
plain HTTP requests; no SDK is installed. A reply that does not match the expected structure is
dropped and nothing is saved. The button is disabled while a request runs, and a second ask for the
same match while one is under way is refused.

Use a normal model such as `claude-sonnet-5-5`. Thinking-only and extended-thinking modes are not
supported: the `anthropic` provider forces a tool call to get a structured answer, which those modes
reject (the log then names this as the reason; the page only says the AI could not answer).
`AI_BASE_URL` must be `https://`, except for `localhost`, `host.docker.internal`, `127.0.0.1`, `::1`,
names without a dot (a Docker service such as `http://ollama:11434/v1`), `*.local` names and
private addresses (`10.x`, `172.16-31.x`, `192.168.x`), where plain `http://` is accepted, for example
Ollama on your LAN. Plain http to a public host stops the start. IPv6 LAN addresses need `https://` or a container
name instead.

What is sent: the stage format, the pool slots with star rating and mods, each team's expected score
per map, each player's best scores, the statistical model's output and the teams' pick and ban
habits. Not sent: osu! ids, map titles, notes or availability. Usernames and team names are replaced
by "Player A", "Player B" and "Team A", "Team B" unless an administrator ticks "Send osu! usernames
and team names to the provider" on the Admin page. The API key stays in the environment: it is sent
only as a request header and is never logged or shown. The Admin page shows "AI predictions: off"
while the setup is incomplete.

## Configuration

All settings are environment variables. `.env.example` lists them with comments.

| Name | Default | Notes |
|---|---|---|
| `OSU_CLIENT_ID` | required | osu! OAuth application ID |
| `OSU_CLIENT_SECRET` | required, secret | osu! OAuth application secret |
| `PUBLIC_URL` | required | e.g. `https://tourney.example.com`, no path |
| `SESSION_SECRET` | required, secret | at least 32 characters |
| `ADMIN_OSU_IDS` | required | comma-separated osu! user ids of the administrators |
| `PUID` / `PGID` | 99 / 100 | user and group the container writes files as |
| `OSU_API_RATE_PER_MIN` | 60 | overall osu! API budget, hard cap 60 |
| `WYBIN_POLL_MINUTES` | 30 | minutes between wybin syncs, minimum 10 |
| `WYBIN_MIN_INTERVAL_SECONDS` | 2 | pause between two requests to wybin, 1 to 60 |
| `SCANNER_ENABLED` | true | scan new multiplayer lobbies |
| `SCANNER_BUDGET_PER_MIN` | 30 | osu! requests per minute for the scanner, lower than `OSU_API_RATE_PER_MIN` |
| `SCANNER_WORKERS` | 4 | parallel scanner tasks, 1 to 8 |
| `SCANNER_MAX_BACKLOG` | 5000 | lobby ids the scanner may lag before it skips ahead |
| `SCANNER_OPEN_MAX_HOURS` | 24 | open lobbies are finalised after this |
| `CAPTURE_REFRESH_HOURS` | 24 | minimum hours before a player's map score is fetched again, at least 1 |
| `LIVE_POLL_SECONDS` | 10 | seconds between two reads of a followed lobby in live match mode, minimum 5 |
| `AI_PROVIDER` | empty (off) | `anthropic` or `openai` (any OpenAI-compatible endpoint); empty keeps AI predictions off |
| `AI_API_KEY` | empty, secret | not needed for an `openai` provider with its own `AI_BASE_URL` (a local server) |
| `AI_MODEL` | empty | the model name, for example `claude-sonnet-5-5`; AI stays off without it |
| `AI_BASE_URL` | empty | address of an OpenAI-compatible or local endpoint, for example `http://192.168.1.20:11434/v1`; `https` unless the host is local or private |
| `LOG_LEVEL` | info | debug, info, warning, error, critical |
| `DATA_DIR` | `/data` | set by the image; change only outside Docker |

Read by this version: the five required variables, `OSU_API_RATE_PER_MIN`, `WYBIN_POLL_MINUTES`,
`WYBIN_MIN_INTERVAL_SECONDS`, the five `SCANNER_*` variables, `CAPTURE_REFRESH_HOURS`,
`LIVE_POLL_SECONDS`, the four `AI_*` variables, `LOG_LEVEL`, `DATA_DIR` (and `PUID`/`PGID` by the container start-up script). `SCANNER_BUDGET_PER_MIN` must be lower
than `OSU_API_RATE_PER_MIN` while the scanner is enabled, or the app does not start; when it is not set
it is 30, or one less than `OSU_API_RATE_PER_MIN` if that is 30 or lower. A wrong `AI_PROVIDER`,
`AI_MODEL` or `AI_BASE_URL` also stops the start; a provider without a model or key does not, it
just keeps AI predictions off (the log names the missing variable).

## Local development

Requires Python 3.13.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e .     # Windows: .venv\Scripts\pip
```

Export the required variables in the shell (see `.env.example`; the app does not read `.env`
itself) and use a local `DATA_DIR`, then:

```sh
uvicorn app.main:app --reload --port 8080
pytest
ruff check . && ruff format --check .
```

Logging in needs a real osu! OAuth application (callback `http://localhost:8080/auth/callback`)
and your own osu! user id in `ADMIN_OSU_IDS`. Administrators can open `/styleguide` (every design
token and component in every state) at `http://localhost:8080/styleguide`. The tests never contact
osu!: they use a fake osu! and a login shortcut that exists only inside the test suite.

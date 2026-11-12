# osu! tourney hub

Self-hosted website for an osu! tournament team, shipped as one Docker container (built for
Unraid). Log in with osu!, import tournaments from wybin, keep a mappool sheet with scores, lineup
suggestions, availability, an MP lobby skimmer and matchup predictions.

Status: early development. The container, configuration, database migrations, `/healthz` and a
design `/styleguide` exist; the features arrive phase by phase (see `docs/plans/`).

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

### Backup and a single process

All state lives in `/data` (`app.db`, plus `app.db-wal` and `app.db-shm` while running). Back up that
folder; the Unraid appdata backup covers it. Run exactly one container and one server process:
background jobs and live updates run in-process, so do not add uvicorn `--workers`.

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
| `SCANNER_ENABLED` | true | scan new multiplayer lobbies |
| `SCANNER_BUDGET_PER_MIN` | 30 | osu! requests per minute for the scanner, lower than `OSU_API_RATE_PER_MIN` |
| `SCANNER_WORKERS` | 4 | parallel scanner tasks, 1 to 8 |
| `SCANNER_MAX_BACKLOG` | 5000 | lobby ids the scanner may lag before it skips ahead |
| `SCANNER_OPEN_MAX_HOURS` | 24 | open lobbies are finalised after this |
| `CAPTURE_REFRESH_HOURS` | 24 | minimum hours before a player's map score is fetched again |
| `LIVE_POLL_SECONDS` | 10 | live match mode polling, minimum 5 |
| `AI_PROVIDER` | empty (off) | optional AI predictions |
| `AI_API_KEY` | empty, secret | not needed for a local endpoint |
| `AI_MODEL` | empty | required when a provider is set |
| `AI_BASE_URL` | empty | only for an OpenAI-compatible or local endpoint |
| `LOG_LEVEL` | info | debug, info, warning, error, critical |
| `DATA_DIR` | `/data` | set by the image; change only outside Docker |

The variables from `OSU_API_RATE_PER_MIN` down to `AI_BASE_URL` are accepted already but only take
effect in the versions that add the matching feature.

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

Until login exists, set `STYLEGUIDE_PREVIEW=true` to open `/styleguide` (every design token and
component in every state) at `http://localhost:8080/styleguide`.

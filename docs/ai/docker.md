# Docker images

Applies to every project that ships a container image. Such a project must be usable in **both** ways below, and its README must show both commands.

## 1. Build from source
`docker build -t <app> .` in the repo root must work on a fresh clone with nothing else: no required build arguments, no secrets, no files outside the repo, no prior local build step.

- `Dockerfile` in the repo root. `.dockerignore` excludes `.git`, `.env*`, dependencies and build output.
- Multi-stage build; final stage holds only what runs. Base images pinned to a version tag, never `latest`.
- No secrets in the image, in `ARG`/`ENV` defaults or in build logs. All configuration through environment variables at run time, documented in `.env.example`.
- One process per container. `EXPOSE` every port. `HEALTHCHECK` for long-running services.
- Persistent data under a single path (`/data` or `/config`), declared as a volume; nothing else is written at run time.
- Run as a non-root user. Honour `PUID`/`PGID` if the container writes to a mounted volume (Unraid default is 99/100).
- README gives a complete `docker run` example (ports, volume, variables).

## 2. Unraid template
A template file in the repo lets an Unraid server install the published image with one `curl`.

- File: `unraid/<app>.xml`, created from `docs/ai/unraid-template.xml`. One `<Config>` entry per port, volume and variable the container needs — the same set as the `docker run` example and `.env.example`.
- `<Repository>` points at the published image (`ghcr.io/<owner>/<repo>:latest`). `<TemplateURL>` points at the file's own raw URL.
- Secret variables: `Type="Variable"`, `Mask="true"`, empty default. Never a real value in the template.
- Appdata paths default to `/mnt/user/appdata/<app>`.
- README gives the install command:
  `curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-<app>.xml https://raw.githubusercontent.com/<owner>/<repo>/main/unraid/<app>.xml`
  then: Docker tab → Add Container → Template → `<app>`.

## Publishing
- `release.yml` builds the image on a version tag and pushes it to GitHub Container Registry as `ghcr.io/<owner>/<repo>:<version>`; final releases also move `latest`, pre-releases (`-prN`) do not.
- `ci.yml` builds the image on every pull request and checks that `unraid/*.xml` is well-formed.
- Private repo → the raw template URL and the image both need a GitHub token on the server (`docker login ghcr.io`). Making the image or repo public is a critical decision → programmer.

## Keep in sync
A change to ports, volumes or variables updates all four in the same change: `Dockerfile`, `.env.example`, `unraid/<app>.xml`, README examples. Removing or renaming one of them is a breaking change (major version).

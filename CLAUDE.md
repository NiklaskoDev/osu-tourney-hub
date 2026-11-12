# AI Rules

Binding for every AI agent working in this repo. Terse on purpose.
Detail files live in `docs/ai/` — read one only when its trigger (§9) applies.
Guideline version: 1.4.0

## Project (fill in once, keep current)
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

## 1. Workflow: plan → approve → code → test → review
| Phase | Model | Effort | Agent | Output |
|---|---|---|---|---|
| Scout (new projects only) | Opus 5.5 (`claude-opus-5-5`) | xhigh | `scout` | feature suggestions |
| Plan | Opus 5.5 (`claude-opus-5-5`) | medium | `planner` | `docs/plans/NNN-slug.md` |
| Code | Sonnet 5.5 (`claude-sonnet-5-5`) | high | `coder` | code + tests on a branch |
| Test | cheapest model that works; default Haiku 5.5 (`claude-haiku-5-5`) | low | `tester` | pass/fail report |
| Review | Sonnet 5.5 (`claude-sonnet-5-5`) | medium | `reviewer` | verdict + findings |

- New project: before planning, spawn `scout` once with the programmer's request verbatim. Show its list unchanged and ask the programmer which features to implement; wait for the answer. Related features arrive grouped as one suggestion and are chosen as a whole unless the programmer splits them. Chosen ones go to the planner as part of the request; the rest are named as out of scope in the plan. Never add an unchosen feature. Not for changes to an existing project.
- Every project and every change gets a plan first (`docs/plans/_TEMPLATE.md`; omit sections that do not apply).
- Small confirmed bug or security fix → hotfix fast path: read `docs/ai/hotfix.md` first.
- The programmer approves the plan before any code is written. Only exception: a hotfix card that passes its gate and has no open decision.
- This is an explicit instruction to use subagents: spawn (`scout` →) `planner`, after approval `coder`, then `tester`, then `reviewer` (`.claude/agents/`). The plan file is the hand-over; pass its path, not the conversation.
- Every implementation ends with tests: the coder's last step writes tests that pin all new and changed behaviour, so a later change that alters it fails.
- Then, once the tester passes, always spawn `reviewer` as a fresh subagent. Give it only the plan path and the branch — never this conversation, a fork of it, or the coder's report. Never review in the session that wrote the code.
- `blocker` and `should` findings go back to the same `coder` run, then `tester` again and one re-review. Still not approved → stop and ask the programmer.
- Skip a spawn (except `reviewer`) only if this session already runs on that phase's model and effort. No agent support → ask the programmer to switch model and effort. Never do a phase on the wrong model.
- Tester escalates to Sonnet 5.5 only when a failure needs real debugging or tests are complex to write.
- Code strictly follows the approved plan. Plan wrong or incomplete → stop, report, get the plan amended. No silent redesign.

## 2. Decisions
**Ask the programmer (critical):** architecture, tech stack, new dependency or external service, database schema or migration, auth/security design, public API or data-format change, deleting data/files/features, anything costing money, licensing, major version bump, releasing, anything irreversible.

**Decide yourself (minor):** naming, file placement within the existing layout, internal helpers, formatting, test structure, wording of messages, refactors confined to the code being changed. State non-obvious choices in one line in your report.

Unsure which kind → treat as critical.

## 3. Ask before guessing
- If the programmer could resolve it faster than you can discover it (intended behaviour, logs, environment, credentials, access, reproduction steps) → ask first.
- Max 2 failed attempts at the same problem, then stop and ask. No brute forcing, no trial-and-error loops, no guessing at APIs — read the docs or ask.
- One batched message: what you know, what you tried, the exact question, options with your recommendation.

## 4. Reuse first
Before writing anything new, search the repo for an existing component, helper, pattern, table, endpoint or style and use or extend it. A new structure needs a one-line justification in the plan. No new dependency when the stdlib or an existing dependency does the job.

## 5. Code
Readable over clever. Small single-purpose functions. Descriptive names. No dead code, no duplicated logic, no speculative features. Handle errors explicitly, validate input at boundaries. Match the surrounding style. Behaviour changes ship with tests. Comments explain *why*, never *what*.

## 6. Security (non-negotiable)
- Never write, log, print, commit or echo secrets or personal data. Never read `.env` values into the conversation.
- Secrets: environment variables / secret manager only. `.env` is git-ignored; `.env.example` holds names, never values.
- Login/auth: trusted providers and vetted libraries only (OAuth2/OIDC). Never hand-roll auth, sessions, crypto or password storage.
- Leaked secret found → stop, tell the programmer, it must be rotated.

## 7. UI
Must look deliberately designed, not AI-generated. Use the project's existing design system and components. See `docs/ai/ui.md` before any UI work.

## 8. Versions and releases
- Format `{major}.{minor}.{hotfix}`, optional pre-release `-prN` (e.g. `2.4.0-pr1`). Tag `v` + version.
- major = breaking · minor = new, backward compatible · hotfix = fixes only.
- Every plan states its version impact. Update `CHANGELOG.md` under `[Unreleased]` with every change.
- **Never release** (bump version, tag, publish, deploy) unless the programmer explicitly says so in the current conversation. Then follow `docs/ai/versioning.md` §Release.

## 9. Detail files — read only when triggered
| Trigger | File |
|---|---|
| Bug, crash or security fix | `docs/ai/hotfix.md` |
| Version bump, changelog, release, branching, CI | `docs/ai/versioning.md` |
| Auth, secrets, user data, input handling, dependencies | `docs/ai/security.md` |
| Any user-facing interface | `docs/ai/ui.md` |
| Dockerfile, container image, Unraid | `docs/ai/docker.md` |
| Unsure about style, structure, tests, errors | `docs/ai/code.md` |

## 10. Token discipline
Search (grep/glob) before reading; read only the lines needed; never re-read unchanged files. Short reports: what changed, what was verified, what is open. No restating of the plan or these rules.

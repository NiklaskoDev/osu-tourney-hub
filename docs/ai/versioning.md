# Versioning, branching, release

## Format
`{major}.{minor}.{hotfix}[-prN]` — integers, no leading zeros. Git tag = `v` + version (`v1.4.2`, `v2.0.0-pr3`).

## Which number changes
| Bump | When | Examples | Resets |
|---|---|---|---|
| **major** | Existing users/integrations must change something, or behaviour they rely on breaks. | Removed or renamed public API/CLI/config; changed data or file format without automatic migration; dropped platform/runtime support; removed feature; redesign that changes established workflows. | minor, hotfix → 0 |
| **minor** | New capability, fully backward compatible. | New feature, endpoint, option, screen; deprecation (still working); automatic, lossless DB migration; notable performance or internal improvement. | hotfix → 0 |
| **hotfix** | Fix only. No new features, no API change. | Bug fix, security patch, typo, dependency patch update, doc correction. | — |

Rules:
- Several changes in one release → highest bump wins.
- A fix that requires a breaking change is a major, not a hotfix.
- Docs/CI/test-only changes need no release.
- `0.x.y` = pre-stable: breaking changes may land in a minor. The programmer decides when `1.0.0` ships.
- AI proposes the bump in the plan; a major bump is always the programmer's decision.

## Pre-release
- `X.Y.Z-prN`, N from 1, counts up per candidate: `2.0.0-pr1` → `2.0.0-pr2` → `2.0.0`.
- Orders before its final version. Not for production. Published as a GitHub *pre-release*.
- Use for majors, risky minors, or when the programmer asks. Final release = same code as the last accepted `-prN`, with the suffix dropped.

## Changelog
`CHANGELOG.md`, newest first. Every change adds a line under `## [Unreleased]` in one of: `Added`, `Changed`, `Fixed`, `Removed`, `Security`. Mark breaking entries with `**BREAKING**`. Write for users, not developers.

## Branches and commits
- `main` is always releasable. Changes arrive by pull request with green CI — also where GitHub cannot enforce it (branch protection on private repos needs a paid plan).
- Branches: `feat/<slug>`, `fix/<slug>` (fix with a full plan), `hotfix/<slug>` (fast path, `hotfix.md`), `chore/<slug>`, `release/vX.Y.Z`.
- Commits: `<type>: <imperative summary>` — types `feat fix refactor test docs chore release`. Breaking: `feat!:` plus a `BREAKING:` line in the body. One logical change per commit.
- Never force-push `main`, never rewrite published tags.

## CI (`.github/workflows/`)
| File | Trigger | Does |
|---|---|---|
| `ci.yml` | push to `main`, every PR, called by release | lint, test, build; builds the Docker image if there is a `Dockerfile` |
| `security.yml` | push to `main`, every PR, weekly | secret scan, dependency review |
| `release.yml` | push of a `vX.Y.Z[-prN]` tag | re-runs CI, pushes the Docker image to `ghcr.io` if there is a `Dockerfile`, then creates the GitHub release |

A red CI is never bypassed, skipped or "fixed" by weakening a test. Fix the cause or ask.

## Release
Only on an explicit instruction from the programmer in the current conversation ("release", "ship 1.3.0", "tag a pre-release"). Finishing a feature is not such an instruction.

1. Confirm the version number with the programmer unless they stated it.
2. Check: `main` is up to date with the remote, clean tree, CI green.
3. Set the version in the source-of-truth file (see `CLAUDE.md`).
4. In `CHANGELOG.md` rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and add a fresh empty `[Unreleased]` above it. (For `-prN` keep `[Unreleased]` and add nothing; notes are auto-generated.)
5. Commit `release: vX.Y.Z`. If `main` is protected: on a `release/vX.Y.Z` branch, open a PR, and the release instruction covers merging it once CI is green. Otherwise commit on `main` and push.
6. Tag the resulting commit on `main`: `git tag -a vX.Y.Z -m "vX.Y.Z"`, push the tag.
7. `release.yml` does the rest. Report the release URL or the failing step. On failure: do not delete or move the tag without asking.

# Hotfix fast path

For small, confirmed bug and security fixes. Same roles and rules as the normal flow, smaller artefacts. Read by the main session only — the agents get what they need from the card. Branch `hotfix/<slug>`.

## Gate — all must hold, otherwise use the normal plan flow
- Restores intended behaviour: bug, crash, security hole, typo, patch-level update of an existing dependency.
- No new feature. No change to public API, CLI, config or data format.
- No schema migration, no new dependency, no critical decision (`CLAUDE.md` §2).
- At most 3 source files changed, tests not counted.

A fix that fails the gate can still be released as a hotfix version; it only needs a full plan.

## Steps
0. **Intake** (main session). Missing any of: symptom, reproduction (steps or input), error/log excerpt, affected version → ask once, all together. Do not diagnose on guesses.
1. **Card** — `planner`. Writes `docs/plans/NNN-hotfix-slug.md` from `docs/plans/_HOTFIX.md`. Reads only the files on the failure path, then searches usages of every symbol the fix changes to fill *Affected*. Cause not confirmable from the evidence → returns the question, not a guess.
2. **Go.** Gate holds and the card has no open decision → show the card and continue without waiting. Otherwise stop for the programmer.
3. **Fix** — `coder`. Regression test first, and see it fail for the stated cause. Then the fix. Only files named on the card. Test does not fail, or the fix needs more than the card says → stop and report: the diagnosis is wrong.
4. **Verify** — `tester`. In order, stopping at the first failure:
   1. the regression test
   2. every test listed under *Affected*
   3. lint, build, full suite (too slow locally → the pull request's CI run counts; read only pass/fail)

   Failure → send the excerpt back to the same `coder` run; do not spawn a new one.
5. **Review** — `reviewer`, fresh, given only the card path and the branch. Findings go back to the same `coder` run, then steps 4 and 5 once more; still not approved → ask the programmer.
6. **Close.** Changelog line under `Fixed` or `Security`. Report in at most 5 lines: cause, fix, what was tested, what was not. Propose version `X.Y.(Z+1)`. Never release unless told (`CLAUDE.md` §8).

## Affected parts
Changed code + everything that calls or reads it one level out (callers, importers, endpoints, jobs, screens) + shared data it writes (tables, files, cache, config).

Each part is one row on the card with the test that covers it. No covering test → `coder` adds one. Not automatable (e.g. visual) → listed as a manual check for the programmer. An affected part is never silently left untested.

## Token rules
- Run tests in quiet mode (`-q`, `--silent`, dot reporter); read failures only.
- Hand over the card path and `file:line` references — never file contents or the conversation.
- Read a detail file only if the fix touches its area: `security.md` for auth, secrets, user data; `ui.md` for interface changes.
- No refactor, cleanup, rename or reformatting in a hotfix. Note them under follow-ups.
- Two failed attempts → stop and ask (`CLAUDE.md` §3).

## Branch base
Default `main`. If `main` holds unreleased changes, ask: ship the hotfix together with them (branch from `main`), or alone (branch from the last release tag, release from that branch, then merge it back into `main`).

## Security fixes
The changelog states the impact, not the exploit. Leaked secret → rotation comes before the code fix (`security.md`).

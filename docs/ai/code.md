# Code practices

## Structure
- One responsibility per function, class, module. Hard to name → doing too much.
- Guideline sizes: function ≤ ~40 lines, file ≤ ~400 lines, nesting ≤ 3 levels (use early returns). Exceeding them needs a reason.
- Separate layers: I/O (HTTP, DB, files, UI) at the edges, logic in the middle, no business rules in handlers or views.
- Dependencies point inward; no circular imports. Pass dependencies in rather than reaching for globals.
- Follow the existing layout. New top-level folder or layer = critical decision.

## Naming and readability
- Names say what a thing is or does; no abbreviations beyond the domain's own; booleans read as questions (`isActive`, `has_access`).
- Follow the language's standard conventions and the repo's formatter/linter. Never hand-format against them.
- No magic numbers or strings — named constants. Config lives in config, not in code.
- Prefer plain, explicit code to clever one-liners and metaprogramming.
- Use the type system: type hints / strict types on all public functions; avoid `any`-style escapes.

## Change discipline
- Smallest change that fulfils the plan. No drive-by refactors, renames or reformatting outside the touched code — propose them separately.
- Do not build for hypothetical future needs.
- Third use of the same logic → extract it. Before that, check whether a helper already exists.
- Delete replaced code; no commented-out blocks, no unused flags, parameters or files.
- Leave no `TODO` without an issue reference or a line in the plan's follow-ups.

## Errors
- Fail early and loudly at the point of detection; never swallow exceptions (`except: pass`, empty `catch`).
- Catch only what you can handle; add context when re-raising.
- Expected failures (not found, invalid input) are part of the return contract, not surprises.
- Release resources deterministically (context managers, `finally`, `defer`).
- External calls: timeout always, retry only idempotent operations, with backoff.

## Data and databases
- Schema changes only through versioned migration files, reversible where possible. Never edit an applied migration.
- Constraints live in the database (keys, uniqueness, not-null), not only in code.
- Multi-step writes in a transaction. Queries bounded (pagination/limits); avoid N+1.
- Schema design and destructive migrations are critical decisions.

## Tests
- Every implementation ends with tests that pin the new and changed behaviour: if someone later changes that behaviour, a test must fail. No implementation is done without them.
- Every bug fix starts with a test that fails without the fix; that test stays as the guard.
- Changing existing code that has no tests → first add a test for its current behaviour, then change it.
- Test behaviour through public interfaces, not internals. Names state the expected behaviour.
- Fast, deterministic, independent: no real network, clock, or shared state — inject or fake them.
- Cover: the main path, boundaries, invalid input, the failure path.
- Never delete, skip or loosen a failing test to get green. Fix the code or ask.

## Comments and docs
- Comment the *why*: constraints, trade-offs, non-obvious consequences. Not the *what*.
- Public functions/modules get a short doc comment: purpose, parameters, return, errors.
- Update README, `.env.example`, `CHANGELOG.md` and the Project block in `CLAUDE.md` in the same change that makes them stale.

## Done means
Plan fulfilled · behaviour pinned by tests · lint, tests, build pass locally · independent review approved · no secrets or debug output · changelog line added · report states what was verified and what was not.

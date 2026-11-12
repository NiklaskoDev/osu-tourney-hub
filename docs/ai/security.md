# Security and data handling

## Secrets
- Sources: environment variables locally (`.env`, git-ignored), GitHub Actions secrets in CI, a secret manager in production. Nowhere else.
- `.env.example` lists every variable name with a placeholder and a comment. Keep it in sync.
- Never in: source code, tests, fixtures, logs, error messages, URLs, commit messages, screenshots, AI prompts or replies.
- AI: do not open `.env` or other secret files; refer to variables by name. Need a value → ask the programmer to set it.
- Separate credentials per environment. Least privilege: scope every key/token to what it needs.
- Client-side code (browser, mobile, desktop bundle) contains no secrets. Anything shipped to the user is public.
- Leak (committed, logged, pasted): stop, tell the programmer, rotate the secret. Removing it from git history is not enough.

## Authentication and authorisation
- Use an established identity provider or vetted library via OAuth2 / OpenID Connect (e.g. the platform's native sign-in, Auth0, Clerk, Supabase Auth, Firebase Auth, Keycloak, or the framework's official auth module). Choice of provider is a critical decision → programmer.
- Never implement your own: password hashing scheme, token format, session mechanism, crypto primitive, "remember me", reset flow.
- If passwords must be stored locally: vetted library, Argon2id (or bcrypt), per-user salt. Never reversible, never logged.
- Sessions: `HttpOnly`, `Secure`, `SameSite` cookies or the provider's SDK. No tokens in `localStorage` when a cookie works. Short-lived access tokens, rotated refresh tokens.
- Authorisation is checked on the server for every request, by resource owner, not by what the UI hides. Deny by default.
- Sensitive actions (email/password change, deletion, payment) require re-authentication.

## User data
- Collect the minimum. No field without a stated use in the plan.
- TLS for all transport. Encrypt sensitive data at rest (database/disk encryption; field-level for especially sensitive values).
- No personal data in logs, analytics, error trackers, or URLs. Log IDs, not contents.
- Deletion and export must be possible. Deleting a user removes or anonymises their data.
- Real user data never goes into tests, fixtures, demos, or AI prompts — use synthetic data.
- Sharing data with a new third party is a critical decision → programmer.

## Input and output
- Validate type, length, format and range at every trust boundary (HTTP, CLI, file, queue, external API). Reject, do not repair.
- Database: parameterised queries / ORM only. No string-built SQL.
- Output: context-aware escaping; rely on the framework's auto-escaping, never disable it for user content.
- No `eval`, no shell commands built from user input, no deserialising untrusted data into objects.
- Files: validate type and size, generate server-side names, store outside the web root, block path traversal.
- Web: CSRF protection on state-changing requests, strict CORS allow-list, security headers (CSP, HSTS), rate limits on auth and expensive endpoints.
- Errors shown to users are generic; details go to the server log only.

## Dependencies
- Adding one is a critical decision. Prefer stdlib or what is already installed.
- Require: maintained, widely used, compatible licence, exact name verified (typosquatting).
- Commit the lockfile. CI installs from it (`npm ci`, `pip install -r` with pins, etc.).
- Dependabot and `security.yml` stay enabled; treat their alerts as hotfix candidates.

## AI conduct
- No destructive commands (drop, truncate, `rm -rf`, force-push, history rewrite) without explicit approval.
- No production access, deploys, or data changes without explicit approval.
- Do not weaken a security control (validation, auth check, TLS verification, CSP, lint rule) to make something work. Ask.

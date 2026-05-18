# Stage 7.1 — Web App Skeleton + Auth: Design and Slicing

*Drafted 2026-05-17 alongside ADR-016 + ADR-017 at the kickoff of
Phase 7, before any 7.1 code was written. Living document — actual
slicing may adjust during implementation, but the principles below
are load-bearing and should not be relitigated without an ADR.*

## What Stage 7.1 delivers

The web-layer substrate every Phase 7 feature stage consumes. No
real-data dashboards yet — Stage 7.2 onwards lights up cost / status
/ advisor / harvester / news / audit views against this scaffold.
Deliverable: a runnable `cli/web` daemon, an auth-protected dashboard
shell with placeholder pages, and the seeded-password flow.

At the end of Stage 7.1:

- `src/wobblebot/web/` contains: `app.py` (factory function
  `create_app(config, storage_dict)` returning a FastAPI instance),
  `middleware.py` (auth + CSRF + rate-limit), `auth.py`
  (login/logout/password handling), `dependencies.py` (FastAPI DI
  for ports + auth checks), `routes/` (sub-routers per feature
  area — only `routes/auth.py` ships features in 7.1), `templates/`
  (Jinja2 base + login + layout chrome), `static/` (HTMX +
  base.css).
- `src/wobblebot/cli/web.py` runs uvicorn against `create_app(...)`
  with `--create-user` subcommand for password seeding.
- New `users` SQLite table in operator.db via
  `sqlite_storage_schema.py`. New `User` Pydantic domain model in
  `domain/users.py`. New `StoragePort.create_user` /
  `get_user_by_username` / `update_user_last_login` methods.
- New `WebConfig` Pydantic config block in `config/cli.py` composed
  into `WobbleBotConfig.web: WebConfig | None`. Settings example
  + .env example documented.
- Three navigable empty stub pages: `/dashboard`, `/cost`, `/audit`.
  Each renders the layout with a "Phase 7.X will fill this in"
  placeholder. Validates the shell + nav + auth.
- ~80-100 new unit tests covering auth flow (login OK/wrong-password/
  unknown-user/locked-out), session middleware (cookie set/cleared),
  CSRF protection (token round-trip + mismatch rejection), rate-limit
  bucket, users-table storage round-trip.

The stage closes once mypy + pylint 10.00/10 + black + isort + pytest
are all clean, the unit count has grown by ~80-100, and the deprived-
env walkthrough on `cli/web` (no operator.db / no user seeded /
missing config) all exit cleanly with code 2.

## Critical separation: Stage 7.1 ≠ Stages 7.2-7.5

Stage 7.1 produces **the auth-protected shell only**. No real data,
no charts, no mutation routes. Stage 7.2 introduces the cost +
status dashboards (the first real data). Stage 7.3 adds advisor +
harvester views. Stage 7.4 adds news + audit logs. Stage 7.5 closes
the phase.

**Do not conflate them.** If a Stage 7.1 PR touches feature
templates, queries from observe.db / advise.db / harvest.db, or
implements the pause/resume mutation flow, something is wrong. The
allowed touch is operator.db (users table) plus the four DB-path
config knobs that Stage 7.2+ will use.

## What's already in place

- **SQLite schema infrastructure** — `sqlite_storage_schema.py` +
  `sqlite_storage_rowmap.py`; the `users` table slots in alongside
  the existing operator.db tables.
- **WobbleBotConfig + per-CLI configs** — `config/cli.py` is the
  natural home for `WebConfig`; the `operator: OperatorConfig | None`
  precedent shows how to compose an optional block.
- **cli/_common.py helpers** — `load_operator_env`,
  `add_config_args`, `collect_overrides` — same shape Stage 7.1
  uses for `cli/web`'s argparse + dotenv handling.
- **OperatorService graceful-degrade pattern** (Stage 5.6.C) —
  the dashboard's cross-DB cards will use the same shape later;
  Stage 7.1 doesn't need it.

## Proposed slicing

| Slice | Scope | Estimated size |
|-------|-------|----------------|
| **7.1.A — Users table + domain model + StoragePort methods** | `domain/users.py` (`User` Pydantic + `UserCredentials` for the login form), `sqlite_storage_schema.py` adds the `users` table + index, `sqlite_storage_rowmap.py` adds `row_to_user`, `StoragePort` adds `create_user(username, password_hash)` / `get_user_by_username(username) -> User \| None` / `update_user_last_login(user_id, ts)`. ~30 unit tests; pure persistence, no web. | ~2 hours |
| **7.1.B — WebConfig + web infrastructure scaffolding** | `config/cli.py` adds `WebConfig` (bind_host, bind_port, session_secret_env_var, session_max_age_days, htmx_poll_seconds, rate_limit knobs, four optional DB paths). `WobbleBotConfig.web: WebConfig \| None`. New `src/wobblebot/web/` package — empty `app.py` skeleton, `middleware.py` skeleton, `auth.py` skeleton, `dependencies.py` skeleton, `routes/__init__.py`. `templates/base.html` + `templates/layout.html` + `static/htmx.min.js` + `static/base.css` committed. ~15 unit tests for the config block (validation + round-trip). | ~2 hours |
| **7.1.C — Login / logout / session middleware / CSRF** | `web/auth.py`: bcrypt hashing helpers, login route handler, logout route handler, `current_user_dep` FastAPI dependency. `web/middleware.py`: CSRF synchronizer-token middleware + rate-limit bucket. `templates/login.html` + the CSRF macro in `layout.html`. Tests cover the full auth flow against an in-memory FastAPI TestClient. ~40-50 unit tests. | ~3-4 hours |
| **7.1.D — cli/web daemon + --create-user + stub pages** | `cli/web.py` runs uvicorn against `create_app`; `--create-user` subcommand prompts on stdin for username + password (twice for confirmation) and seeds the row. Three stub routes: `/dashboard`, `/cost`, `/audit` — each renders layout + "Stage 7.X placeholder" body. Deprived-env walkthrough: missing operator.db, no user seeded, bad config path — each exits 2 with clear error. ~15-20 unit tests + manual smoke. | ~2 hours |
| **7.1.E — Stage close** | Roadmap ✅, CLAUDE.md Project Status bump (13th operator entry point: `python -m wobblebot.cli.web`), CHANGELOG entry, project_state memory updated, MEMORY.md index touched if needed. mypy + pylint 10.00/10 + black/isort/pytest all clean. | ~30 min |

**Total: ~10-12 hours of focused implementation.** Two-evening stage.
The deliberate boundary keeps Stage 7.1 a "shell only — no real
data" foundation against which Stages 7.2-7.4 wedge in feature
pages cleanly.

## Design decisions to ratify

ADR-016 + ADR-017 ratify the architecture; the items below are
*implementation-level* decisions that should land at the start of
Slice 7.1.B and stay stable through the stage.

### 1. FastAPI app via factory function, not module-global

**Decision:** `web/app.py` exposes `create_app(*, config:
WobbleBotConfig, storage: StoragePort, ...) -> FastAPI`. Tests
construct a fresh app per test (or per-class via fixture);
production `cli/web` calls it once at startup.

**Why:** Module-global FastAPI app + mutable state at import time
makes tests order-dependent and inhibits parallel test runs. The
factory keeps app state explicit and per-instance.

### 2. Single FastAPI instance, multiple Routers

**Decision:** `web/routes/auth.py` exposes `router = APIRouter(...)`;
`create_app` mounts it onto the app at `/auth/...`. Stage 7.2+ adds
`web/routes/cost.py`, `web/routes/status.py`, etc., all mounted
into the same app via `app.include_router(...)`.

**Why:** APIRouter sub-grouping keeps each feature area's routes
isolated; mounting them inside `create_app` keeps the wiring
declarative + testable.

### 3. Session secret from env, not committed

**Decision:** `WebConfig.session_secret_env_var: str = "WOBBLEBOT_WEB_SESSION_SECRET"`
points to an env var (32+ random bytes recommended); `cli/web`
reads it at startup. Missing env var: exit 2 with a clear message
+ a one-liner showing how to generate one (``python -c "import
secrets; print(secrets.token_urlsafe(32))"``). Documented in
`.env.example`.

**Why:** Cookie signing key is a true secret; settings.yml is
committable in spirit (even if .gitignored in practice). Env
matches the project's existing secret-handling pattern (Kraken
keys, Discord bot tokens, cloud LLM API keys).

### 4. Password hashing via the `bcrypt` package directly

**Decision:** `web/auth.py` uses `bcrypt.hashpw` + `bcrypt.checkpw`
directly. No `passlib` wrapper.

**Why:** `passlib` is a generalized password-context abstraction
designed for multi-algorithm support (PBKDF2, scrypt, argon2,
bcrypt). The project commits to bcrypt only; `passlib`'s abstraction
adds dependencies without value. ~5 lines of `bcrypt` direct calls
beats 5 lines of `passlib` wiring + the dep surface.

### 5. CSRF token storage in the session, validation per POST

**Decision:** Session middleware adds `session["csrf_token"] =
secrets.token_urlsafe(32)` on first GET if not already present. A
`csrf_input(request)` Jinja2 global emits the hidden form input.
A `CsrfMiddleware` rejects POSTs whose form's `csrf_token` value
doesn't match the session's. 403 on mismatch.

**Why:** Synchronizer Token Pattern — standard OWASP-recommended
shape. ~30 lines of code total. No new dep.

### 6. Rate-limit bucket lives in-memory; per-IP

**Decision:** A `dict[str, _IPBucket]` keyed by `request.client.host`
counts login attempts within a 60-second window. Resets on
successful login or window expiry. Lock is a simple `asyncio.Lock`
around bucket mutation.

**Why:** Single-process, single-operator scope. Persistent
rate-limiting (across daemon restarts) would require a SQLite
table; overkill until Phase 8 reliability shows it's needed.

### 7. cli/web's argparse: `--create-user` is a subcommand, not a flag

**Decision:** Use `argparse` subparsers so the CLI has two modes:
`cli/web serve` (the default; runs uvicorn) and `cli/web create-user`
(prompts on stdin + creates the row + exits). The default action
when no subcommand is given is `serve`, so the existing usage
`python -m wobblebot.cli.web` keeps working.

**Why:** Subcommands cleanly separate the two responsibilities;
operator typo of `--create-user` flag would be silently ignored
without subparsers' validation.

### 8. Tests use FastAPI's TestClient against in-memory storage

**Decision:** Per-test fixture creates a fresh `SQLiteStorageAdapter(":memory:")`,
seeds a known test user, builds `create_app(...)` with that
storage, instantiates `TestClient(app)`, runs the test. No real
network, no real cookies-on-disk.

**Why:** TestClient is FastAPI's built-in synchronous test surface;
matches the project's `httpx.MockTransport` testing posture for
adapters. Per-test in-memory storage means tests are independent
+ parallel-safe.

### 9. The three stub pages enforce the navigation contract

**Decision:** `/dashboard` shows "Phase 7.2 — Cost + Status (placeholder)",
`/cost` shows "Phase 7.2 — Cost ledger (placeholder)", `/audit`
shows "Phase 7.4 — Audit log (placeholder)". All require auth.
Each gets one navigation entry in the layout's top nav.

**Why:** Verifying the shell ships requires verifying that nav,
auth-redirect, and template rendering all work end-to-end. Stubs
prove that without committing feature work prematurely.

### 10. No 404 / 500 custom pages in Stage 7.1

**Decision:** FastAPI's default error responses (plain text "404
Not Found" / "500 Internal Server Error") suffice. Custom HTML
error pages (with the layout chrome) are a Stage 7.5 polish item.

**Why:** Operator-facing aesthetics matter less than the feature
content; deferring polish until the feature work lands matches
the project's "don't bike-shed when content is missing" rule.

## Test plan

- **Unit, ~80-100 new tests.** Most weight on the auth flow (login
  matrix: OK / wrong password / unknown user / rate-limited / empty
  username) + the CSRF token round-trip (GET then POST with valid
  token / POST with missing token / POST with mismatched token /
  POST with token from a different session).
- **No integration test in Stage 7.1.** Stage 7.5's smoke walkthrough
  is the integration check. Stage 7.1's TestClient tests are
  effectively integration-grade against in-memory storage.
- **Deprived-env walkthrough:** `cli/web serve` against (a) missing
  operator.db, (b) no users in the table, (c) missing
  `WOBBLEBOT_WEB_SESSION_SECRET` env var, (d) bad `--config` path,
  (e) bad `--profile` name. Each should exit 2 with a clear message,
  no raw traceback. `cli/web create-user` against (a) missing
  operator.db (should mkdir + create), (b) duplicate username (should
  refuse with clear error), (c) stdin EOF during password prompt
  (should refuse).

## What's NOT in scope for Stage 7.1

- Any real-data dashboard. (Stage 7.2+.)
- Any mutation route. (Stage 7.2 brings pause/resume; the mutation
  pattern itself lands then.)
- HTMX usage (just the static file committed). (Stage 7.2+ uses
  it.)
- Custom error pages. (Stage 7.5 polish.)
- Password reset / change-password UI. (Out of phase scope per
  ADR-017.)
- Multi-user features. (Out of phase scope per ADR-017.)
- Public-internet exposure (binding to non-localhost). (Operator's
  reverse-proxy concern; not the daemon's.)

## Stage close criteria

1. ADR-016 + ADR-017 committed in `docs/architecture/decisions.md`.
2. New `users` SQLite table migrates cleanly against fresh + existing
   operator.db files.
3. `cli/web create-user` round-trips: prompts work, hash persists,
   wrong password rejects.
4. `cli/web serve` boots, binds 127.0.0.1:8000, serves the login
   page; valid login → dashboard; logout → login page.
5. CSRF protection verified: a form GET → POST with valid token
   passes; the same form POST without the token returns 403.
6. Rate limit verified: 5 wrong-password attempts within 60s →
   the 6th returns 429; window expiry resets.
7. Three stub pages render with layout chrome + nav.
8. `cli/web` deprived-env walkthrough passes for all five scenarios.
9. mypy clean (now ~85+ src files), pylint 10.00/10 on `src/`,
   black + isort clean.
10. Roadmap + CLAUDE.md + CHANGELOG + project_state memory + MEMORY.md
    index reflect Stage 7.1 ✅.
11. Total unit-test count grows by ~80-100; no test deletions.

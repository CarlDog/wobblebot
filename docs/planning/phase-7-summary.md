# Phase 7 — Closing Summary

**Status: ✅ Complete (2026-05-18).** Five Phase 7 stages closed
across two evenings (7.1 / 7.2 / 7.3 / 7.4 / 7.5). Server-rendered
FastAPI + Jinja2 + HTMX dashboard ships end-to-end: auth-protected
shell with bcrypt + CSRF + per-IP rate-limit → cost + status
dashboards + ADR-013-firewalled mutation flow → advisor + harvester
read-only views → news + audit-log views → integration check.

**Phase 7 spent $0.00 of real money.** The dashboard is
read-mostly; mutations are firewalled per ADR-013 (web UI never
calls `OperatorService.dispatch_command` directly — every state
mutation crosses `pending_commands` so cli/live's
`WHERE status='approved'` poll remains the single source of truth
for "intent → engine"). Running project real-money cost: **$0.085018
unchanged from Phase 6 close.**

This document is the Stage 7.5 deliverable per the roadmap's
"phase close + integration check" charter. Consolidates per-stage
receipts, the architecture story (how ADR-016 + ADR-017 commitments
held up), the shared-snapshot pattern that paid off across views,
the v2 candidates flagged for future hardening, and entry conditions
for Phase 8 (Hardening & v1.0 Release).

## Architecture story

Two ADRs ratified at kickoff drove every implementation decision.

**ADR-016 — Web UI architectural commitments.** FastAPI + Jinja2 +
HTMX. No SPA, no Node build pipeline, no client-side state
management. Routes consume the existing ports via FastAPI DI; no
business logic in route handlers. **Read-mostly with ADR-013-firewalled
mutations:** pause/resume/stop buttons create `PendingCommand` rows in
`awaiting_confirmation`; a two-click confirm flow transitions to
`approved`. cli/live's `WHERE status='approved'` poll remains the
only path from intent to engine — the ADR-002 firewall stays the
single source of truth. `cli/web` daemon binds 127.0.0.1 by default;
operator-managed reverse proxy handles TLS + LAN exposure.

**ADR-017 — Web UI auth model.** Single-operator v1: bcrypt-hashed
password in `operator.db`'s new `users` table; session cookie signed
by Starlette's `SessionMiddleware` (itsdangerous under the hood);
CSRF synchronizer-token middleware with `csrf_input` Jinja2 global
so every form gets a token without per-template wiring; per-IP login
rate-limit (default 5 attempts / 60s, resets on successful login).

Both commitments held through every Phase 7 stage. The factory
pattern (`create_app(...)`) kept tests per-instance + parallel-safe.
Cross-DB graceful-degrade (`live_storage=None` etc.) followed the
Stage 5.6.C OperatorService pattern — cards that need missing DBs
render a "unwired" placeholder rather than 500-ing.

## Per-stage receipts

### Stage 7.1 — Web app skeleton + auth (5 sub-slices)

The substrate. Five sub-slices delivered the auth-protected shell
Phase 7's feature stages then wedged into:

- **7.1.A** — `users` SQLite table + `User` / `UserCredentials`
  domain models + three StoragePort methods (28 tests).
- **7.1.B** — `WebConfig` Pydantic block + six new runtime deps
  (`fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`,
  `bcrypt`, `itsdangerous` — biggest dep-add since Phase 5's
  `discord.py`) + `src/wobblebot/web/` package scaffolding (25 tests).
- **7.1.C** — bcrypt + CSRF + LoginRateLimit + login/logout flow;
  CSRF token rotates on login + logout (session-fixation guard);
  108 tests.
- **7.1.D** — `cli/web` daemon with `serve` (default) + `create-user`
  subcommands; three auth-gated stub pages (`/dashboard`, `/cost`,
  `/audit`) via `require_user`; deprived-env walkthrough green for
  all five scenarios; 40 tests.
- **7.1.E** — roadmap + CLAUDE.md + CHANGELOG + settings.example.yml
  `web:` block + .env.example `WOBBLEBOT_WEB_SESSION_SECRET` close.

Thirteenth operator entry point landed:
`python -m wobblebot.cli.web`.

### Stage 7.2 — Cost + status dashboards + mutation flow (3 sub-slices)

The first real-data dashboards plus the architecturally significant
mutation flow.

- **7.2.A — Cost dashboard.** Reads `operator.db`'s `llm_calls`
  (Phase 6 ledger) for 24h totals + per-day trends + per-provider/role
  breakdown. Pure-function `_rollup` keeps the math testable. Two
  routes: `/cost` (full page) + `/cost/card` (HTMX fragment for
  polled refresh).
- **7.2.B — Status dashboard.** Reads `live.db`'s open orders +
  recent 20 trades. `dashboard.html` combines operator-actions card
  + HTMX-polled status card. Graceful-degrades to "unwired" when
  `live_db` isn't configured.
- **7.2.C — Mutation flow.** Pause/resume/stop buttons →
  `PendingCommand` rows in `awaiting_confirmation` →
  confirm/reject page → `approved` or `rejected`. `channel_id="web"`
  distinguishes web-originated rows from Discord-originated ones.
  Idempotency: re-confirming a row already in a terminal state
  surfaces the existing status, never mutates twice (handles the
  Discord-confirmed-first race). CSRF protected on every POST.
  10-minute TTL on web-originated rows.

29 new unit tests.

### Stage 7.3 — Advisor + harvester views

Two read-only views surfaced existing Phase 3 + Phase 4 data:

- **`/advisor`** — recent `AdvisorSuggestion` rows from `advise.db`,
  with per-expert opinions when MoE-derived (preserving
  `AdvisorRecommendation.expert_opinions` per ADR-007). Confidence
  tags color-coded.
- **`/harvester`** — recent `TransferProposal` + `TransferResult`
  rows from `harvest.db`. Read-only; per ADR-003 `cli/harvest
  --execute` remains the only path that moves money.

11 new unit tests.

### Stage 7.4 — News + audit-log views

The final two read-only surfaces:

- **`/news`** — recent `NewsItem` rows from `news.db`. Filter form
  with source dropdown + free-text coin filter (case-insensitive
  substring match against `mentioned_coins` server-side).
- **`/audit`** — pending_commands + notifications from `operator.db`
  (always wired). Replaces the Stage 7.1.D `/audit` stub; lifecycle
  states color-coded.

13 new unit tests; layout nav adds `/news`; `pages.py` shrinks to
just the bare `/` redirect.

### Stage 7.5 — Phase 7 close + integration check

This commit. One end-to-end TestClient walkthrough exercises every
Phase 7 surface (anonymous redirect → login → all six pages →
pause→confirm→approve →
`WHERE status='approved'` firewall verification → logout). One test,
many assertions. Plus this summary doc + roadmap/CHANGELOG/CLAUDE.md
updates.

## Shared patterns that paid off

**Frozen-dataclass snapshot per view.** Each route module exposes a
`<Domain>Snapshot` dataclass that the template consumes. Encapsulates
the "what to render", "any error string", and "is the cross-DB wired"
flag. Tested directly without going through HTTP — the route handler
is a thin wrapper.

**Graceful-degrade card pattern.** When `live_db` / `advise_db` /
`harvest_db` / `news_db` are unset, the relevant view renders a
"placeholder card" with operator-facing copy explaining what's
missing. Same shape as Stage 5.6.C's `OperatorService.answer_query`
graceful-degrade. Operator failures are visible, not silent.

**`csrf_input` Jinja2 global.** Every form calls `{{ csrf_input(request) }}`;
the global mints (or reuses) a session token and emits the hidden
input. Forgetting the call means the form fails CSRF on submit,
surfacing the omission immediately rather than silently bypassing
protection.

**TestClient against in-memory SQLite.** No network, no real cookies
on disk. Per-test in-memory storage means tests are independent +
parallel-safe. The factory pattern (`create_app(...)`) makes
fresh-app-per-test cheap.

## v2 candidates flagged

Explicitly out of Phase 7 per the roadmap's "out of scope" section,
each documented so it doesn't get pulled into v1 hardening:

- **Multi-user authentication / per-user permissions.** Phase 8+
  candidate. Current single-operator-v1 fits the operator's actual
  surface; multi-user is theoretical.
- **Password reset / change-password UI.** Operator deletes + re-seeds
  via `cli/web create-user`. UI flow adds attack surface (lockout
  recovery, token expiry, email delivery if reset-by-email) without
  matching operator need.
- **WebSocket / SSE real-time updates.** HTMX 15s polling is
  sufficient. Phase 8 reliability work could revisit if profiling
  shows polling overhead matters.
- **Bundled TLS.** Operator-managed reverse proxy. ADR-016 explicitly
  defers this; certbot / nginx / Caddy / Traefik handles TLS at the
  LAN edge.
- **Custom 404 / 500 error pages with full chrome.** FastAPI defaults
  are functional; aesthetic polish defers to v1.0 release.
- **Config-editing through the UI.** `settings.yml` is operator-edited.
  `cli/apply --commit` is the only mutation surface for grid params
  per ADR-012.
- **Web-UI-mediated trading mutations beyond pause/resume/stop.**
  PauseAll, ResumeAll, CancelOpenOrders stay on Discord / CLI paths
  for v1. The Phase 7 mutation surface is intentionally narrow —
  the three most-frequent operator actions.

## Numbers

**1656 unit tests pass** (1460 at Phase 6 close → 1608 at Stage 7.1
close → 1656 at Phase 7 close; +196 across the five Phase 7 stages).
**29 integration tests** opt-in (unchanged from Phase 6 close —
Phase 7's e2e walkthrough is a unit test against in-memory storage).

mypy clean across **96 src files** (89 at Phase 7 entry → 96 after
seven new route modules). pylint **10.00/10**. black + isort clean.

**Six new runtime deps** added in Stage 7.1.B: `fastapi>=0.115`,
`uvicorn[standard]>=0.30`, `jinja2>=3.1`, `python-multipart>=0.0.12`,
`bcrypt>=4.2`, `itsdangerous>=2.2`. Biggest dep-add since Phase 5's
`discord.py>=2.3,<3`.

**Real-money cost:** $0.00 Phase 7. Running project total stays at
**$0.085018**.

## Entry conditions for Phase 8

Phase 8 (Hardening & v1.0 Release) is ready to start. Five stages:

- **Stage 8.0 — Deferred Phase 5 audit refactors.** R5
  `ports/operator.py` split, R3 storage-fallback helper, R2 generic
  poll-loop helper. Three sub-slices, low-risk → high. Surfaced
  during the Phase 5 close audit; queued for proper planning rather
  than landing inline.
- **Stage 8.1 — Reliability & Recovery.** Robust startup/shutdown;
  reload positions, open orders, and pending transfers without
  duplicating actions on restart; reconciliation logic matching DB
  state against exchange state.
- **Stage 8.2 — Background Maintenance Worker.** `cli/maintenance
  --loop` covering DB hygiene (`VACUUM`, retention pruning of
  `price_snapshots` to parquet/CSV archives), log rotation, local +
  configurable-remote backups via SQLite `.backup` API.
- **Stage 8.3 — Performance & Resource Tuning.** Polling intervals,
  batch operations, DB usage tuned for Synology NAS resource
  constraints.
- **Stage 8.4 — Phase 8 / v1.0 Release Check.** Extended soak test
  under low-risk configuration; v1.0 tag; v1.0 changelog;
  known-limitations doc.

**Pending standing item:** CryptoCompare 90-day evaluation due
**2026-08-13** per ADR-010 (deferred from Stage 6.5 close — the
proper 90-day observation window hasn't elapsed yet). Not Phase 8
scope; a calendar-driven standing item.

## Done is done

Phase 7's success criteria from the kickoff:

- ✅ Web UI auth-protected and operator-usable.
- ✅ Pause/resume/stop buttons route through the ADR-013 firewall.
- ✅ Cost + status + advisor + harvester + news + audit views all
  render against real data.
- ✅ HTMX polling for the dashboard (status card) + cost card.
- ✅ Operator-managed reverse proxy posture (no bundled TLS, 127.0.0.1
  default bind).
- ✅ All gates green: 1656 unit tests; mypy 96 src; pylint 10.00/10;
  black + isort clean.
- ✅ Deprived-env walkthrough green for `cli/web` (Stage 7.1.D).
- ✅ Phase 7 closing summary (this document).

Phase 8 starts when the operator picks it up.

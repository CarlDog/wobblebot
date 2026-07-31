# Phase 8 — Closing Summary

**Status: ✅ Complete (2026-07-31, tag `v1.0.0`).** Five Phase 8 stages
(8.0–8.4) shipped between 2026-05-18 and 2026-07-31 — four code stages
in one evening, followed by a ~10-week operator-driven soak that is
this phase's real center of gravity. This document is the Stage 8.4.F
release-ceremony deliverable: per-stage receipts, the soak narrative,
the architecture story (how ADR-018's reconciliation strategy held up
under real incidents), known limitations carried into the release, and
entry conditions for what comes next.

**Phase 8 spent $0.00 of new real money** beyond the running project
ledger. Running project real-money cost at tag: **$0.085018**
(unchanged since Phase 6 close — Phase 8's live activity was tiny
verification trades + fee-neutral grid cycles, not new spend). No
withdrawals; the Harvester key stays separate from the trade key
(ADR-003), verified live in Stage 8.4.C.

## Architecture story

**ADR-018 (Engine Reconciliation Strategy), ratified at Stage 8.1
kickoff, is the decision this whole phase leans on.** The exchange is
authoritative; on startup the engine diffs exchange state against
storage and resolves drift by rule (storage-only orders → `canceled`;
exchange-only orders → logged, never silently adopted). That one
decision is what let the soak survive, rather than merely log,
**four separate real-world interruptions**: a thunderstorm power/DNS
outage (Day 1), a Kraken per-symbol rate-limit storm once the soak
went multi-coin (2026-06-02), a dead-man's-switch disarm-on-failed-cancel
bug the storm exposed (same day), and an 11-day host-wide NAS reboot
that killed the two money-path daemons outright (2026-07-20 →
discovered 2026-07-31). In every case the reconciler resumed cleanly —
no stranded exchange-side orders, no un-reconciled storage drift, no
manual ledger surgery required. That track record is the actual
evidence behind "the soak passed," more than any calendar duration.

**ADR-021 (server-side dead man's switch)** is the phase's other
load-bearing addition: Kraken's own `CancelAllOrdersAfter` timer covers
the failure mode the process-level `finally`-block cancel structurally
cannot — the host itself going dark. It fired in anger (in the sense of
staying correctly armed) through the rate-limit storm and the NAS
reboot alike.

## Per-stage receipts

### Stage 8.0 — Deferred Phase 5 audit refactors ✅ (2026-05-18)

Three pure-reorganization refactors carried over from the Phase 5
close audit (per the global rule against silent reworks during an
audit pass): split the 734-line `ports/operator.py` into three focused
modules with full backward-compat re-exports; extracted graceful-degrade
factory functions in `operator_service.answer_query`; extracted a
shared `cli/_common.run_poll_loop` now used by six poll loops across
five daemons. Zero behavior change; every existing test and import path
stayed green. +11 tests (1711 total).

### Stage 8.1 — Reliability & Recovery ✅ (2026-05-18)

ADR-018 + the reconciler. Fixed the persistence-on-cancel bug the
shadow session surfaced the same day (a successful exchange-side cancel
that never got written back to storage). New `services/reconciler.py`
splits a pure planning function from an async orchestrator; wired into
`cli/live` + `cli/shadow` between adapter construct and first tick —
the daemon now refuses to start rather than tick against unreconciled
state. +21 tests (1732 total).

### Stage 8.2 — Background Maintenance Worker ✅ (2026-05-18)

`cli/maintenance` — the fifteenth operator entry point. Three
concurrent scheduled tasks (VACUUM, retention pruning to CSV archive,
local SQLite `.backup`-API snapshots) via the Stage 8.0 poll-loop
helper. +31 tests (1763 total).

### Stage 8.3 — Performance & Resource Tuning ✅ (2026-05-18)

WAL mode + `synchronous=NORMAL` + `foreign_keys=ON` for every on-disk
database; an `EXPLAIN QUERY PLAN` audit confirming every hot read uses
an index (`SEARCH`, never `SCAN`); an operator-runnable
`tools/profile_storage.py` harness. No new indexes needed — the schema
was already sound. +22 tests (1785 total).

### Stage 8.4 — Phase 8 / v1.0 Release Check ✅ (2026-05-18 → 2026-07-31)

The release-readiness stage, and the one that actually took ten weeks:

- **8.4.A–D** (2026-05-18): kickoff design doc; the v1.0 documentation
  freeze (`docs/release/v1.0-known-limitations.md` +
  `docs/release/v1.0-future-improvements.md`); the pre-1.0 one-shot
  audit (LICENSE, pre-commit hook, full-history author sweep, community
  standards — all clean; one README-drift fix); the soak runbook.
- **8.4.E** (2026-05-18 → 2026-07-31): the soak itself — see below.
- **8.4.F** (2026-07-31, this document): release ceremony — phase
  summary, `pyproject.toml` 0.1.0 → 1.0.0, CHANGELOG `[Unreleased]` →
  `[1.0.0]`, annotated `v1.0.0` tag.

## The soak — ten weeks, four real incidents, zero fund loss

The soak was never a quiet clock-watching exercise; it was where every
Stage 8.0–8.3 hardening decision got tested against reality, and where
several genuine defects surfaced and were fixed in focused commits per
the stage-8.4-design.md discipline (fix the defect, don't refreeze the
whole codebase).

**Day 1 (2026-05-18 → 05-19):** a thunderstorm took the host's DNS
resolution down overnight; `cli/live` crashed mid-shutdown, one BUY
filled while the engine was dead. Recovered via manual cancel +
`grid_state` reset + restart. Fix: the shutdown `finally` block now
isolates each cleanup step in its own try/except, so one transient
failure (the balance check) can't skip the cancel that matters.

**2026-05-20 → 05-28 (Days 3–11, "pre-soak"):** the laptop-hosted run
was reframed as pre-soak once the operator's move to a new house forced
a NAS Docker redeployment. Along the way: graceful-shutdown timeouts
across all daemons, a `/health` freshness page + dashboard traffic-light,
a Discord permission fix, a secret-exposure incident during Docker
compose validation (all credentials rotated — `docker compose config
--quiet` is now the only sanctioned validation form), and a NAS-hosted
Ollama model sweep that fixed a stochastic JSON-corruption bug in the
Discord operator daemon.

**2026-06-02 — the multi-coin restart, and the two real defects it
found.** Single-coin BTC had gone offside and parked, giving near-zero
engine coverage, so the soak restarted across five alts. That exposed:
(1) a per-symbol `OpenOrders` rate-limit storm (the engine fetched open
orders once per *symbol* per tick — five coins tripped Kraken's rate
limit, blocking both startup reconciliation and the shutdown cancel);
and (2) the dead-man's-switch silently disarming on a failed shutdown
cancel (a rate-limited fetch failure read as a false all-clear,
triggering `set_dead_mans_switch(0)` while ~15 orders sat open and
unprotected for ~10 minutes). Both fixed same-day in `abf3aa6`: one
global open-orders fetch per tick regardless of coin count, and a fetch
failure that now correctly propagates instead of masquerading as clean.
Live-verified via a purpose-built diagnostic
(`tools/check_dead_mans_switch.py`).

**2026-06-03 — dashboard-confirmed multi-coin thesis.** Three closed,
profitable alt-coin cycles (≈+$0.50 net of fees) validated the core
strategic bet: alts cycle at a 3% grid where single-coin BTC just
parks. (Net trading result, distinct from the fixed real-money *cost*
ledger tracked above.)

**2026-07-20 → 07-31 — the 11-day blackout, discovered via log
review.** The whole NAS host rebooted; `wobblebot-live` and
`wobblebot-harvest` run `restart: "no"` by deliberate design (real-money
daemons should not blind-resume after an uncontrolled host restart), so
both sat exited for eleven days while every `restart: unless-stopped`
daemon auto-recovered and kept the dashboard looking healthy the whole
time. No trades placed, no treasury monitoring, for eleven days — and
it went unnoticed until a full Portainer log pull surfaced the identical
"Up 11 days" uptime across all 30+ containers on the host. On resume:
the reconciler cleanly marked 13 stale storage-only orders `canceled`,
the grid re-laid out at the existing anchor, and a handful of symbols
went offside/parked from eleven days of price drift — all expected,
none fatal, confirmed directly against Kraken (no stranded open orders).
This is the incident that most directly exercises ADR-018's design
promise, and it held. It also exposed the one gap the reconciliation
design was never meant to cover: nothing pushes a stale-heartbeat signal
to the operator when a daemon is simply *not running* — queued to the
v1.1 backlog (P3) as a direct, named consequence of this incident.

**Pass-criteria verdict.** The soak's stated exit bar (engine-correctness
coverage + reconciliation-across-restarts + at least one cycle of every
daemon + no hard-stops that corrupted state — profit and BTC direction
explicitly excluded) is met: every one of the incidents above ended in a
clean, verified recovery with no fund loss and no silent state
corruption. The 11-day blackout is a coverage gap, not a reconciliation
failure — and the reconciliation-across-restarts criterion is the one it
most directly proved.

## Shared patterns that paid off

**Fail-soft over fail-hard on transient infrastructure.** Every fix that
shipped this phase — the shutdown `finally` restructuring, the fetch
failure now propagating instead of masquerading as `(0,0)`, the
dead-man's-switch — followed the same shape: distinguish "this specific
step failed" from "everything is fine," and never let the former render
as the latter.

**Diagnose via a purpose-built tool before shipping the fix.**
`tools/check_dead_mans_switch.py` proved the arm/disarm behavior against
the real Kraken account before the fix landed, rather than trusting
theory. Same discipline `tools/profile_storage.py` and
`tools/check_dead_mans_switch.py` both embody: verify against the real
system, not a mental model of it.

**Reconciliation as a startup gate, not a background sweep.** ADR-018's
choice to run reconciliation once at startup (refuse to start on
failure) rather than continuously in the background kept the design
simple and made every incident's recovery a clean, auditable boot-time
event instead of an ongoing background risk.

## Known limitations carried into v1.0.0

Full detail in `docs/release/v1.0-known-limitations.md`; the headline
items relevant to this close:

- **Live partial-fill Trade-drop** — a partially-filled order that
  refreshes to `canceled`/`expired` (rather than fully `closed`) can
  drop the matching `Trade` row, under-recording a real fill. Blueprint
  fully resolved (unified with the reconciler's fill-vs-cancel logic);
  not yet built. Tracked as the highest-value open item in the v1.1
  backlog (P1).
- **Harvester `--execute` has no replay/idempotency guard.** Flagged as
  the single highest-blast-radius hole in the codebase; a double-tap or
  retry could double-withdraw. Fix designed (a `proposal_id` uniqueness
  guard); not yet built.
- **No stale-heartbeat push alert.** The gap the 2026-07-20 blackout
  exposed, above.
- **CryptoCompare news source retired mid-soak** (2026-05-21, upstream
  business decision — CoinDesk ended free API access). RSS (7 feeds)
  was unaffected and already carried the bulk of the signal; ADR-010's
  90-day evaluation closed early with this outcome.

None of these are money-movement-core defects — Kraken adapter,
Harvester's seven-layer gate structure, the dead man's switch, and
ADR-002's advisory-only firewall were all independently reviewed twice
(fleet-review passes, GitHub issues #12 and #19) and came back clean on
the core each time.

## What's next

**v1.0.0 is `main` as tagged 2026-07-31** — the hardening + soak work
described above, nothing more. All fleet-review fixes, the ADR-022
advisor reorientation, the web UI expansion, and the rest of the P0/P1/P3
work already built on the `v1.1` branch are **deliberately not part of
this tag** — they continue on `v1.1` per the operator's explicit choice
to keep developing there rather than force a large, untested merge at
tag time. See `docs/release/v1.1/README.md` for that backlog and
`docs/planning/roadmap.md`'s Stage 8.4.E digest for what's already
shipped on the branch. Phase 9 (Kraken Securities equities) remains the
phase *after* v1.1's backlog is worked through, not an immediate next
step.

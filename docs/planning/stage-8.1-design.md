# Stage 8.1 — Reliability & Recovery: Design and Slicing

*Drafted 2026-05-18 at Stage 8.1 kickoff, alongside ADR-018. Living
document — actual slicing may adjust during implementation, but the
ADR-018 policy is load-bearing and should not be relitigated without
revisiting the ADR.*

## What Stage 8.1 delivers

Robust startup + shutdown discipline for cli/live + cli/shadow. By
the time Stage 8.1 closes:

- The persistence-on-cancel bug from the 2026-05-18 shadow session
  is fixed. When the engine cancels an order at shutdown, the
  storage row's status transitions to ``"canceled"`` synchronously
  with the adapter call.
- A new ``services/reconciler.py`` pure-function module handles
  startup reconciliation per ADR-018: storage-only orders → mark
  canceled; exchange-only orders → log loud error + continue.
- cli/live + cli/shadow both call the reconciler at boot, after
  storage opens but before the engine first tick.
- Targeted tests cover the reconciler's three diff cases (both
  agree, storage-only, exchange-only) plus the persistence-on-cancel
  shutdown fix.

**Out of scope for Stage 8.1** (per ADR-018 decision 6):

- Harvester pending-transfer reconciliation. Operator manually
  reconciles via Kraken Pro in v1.0; automated reconciliation
  queued for v1.1.
- Per-tick reconciliation. Already covered by Stage 2.2's engine
  tick logic; this stage handles the startup gap only.

## Why now

Three converging pressures:

1. **Concrete shutdown bug.** 2026-05-18 shadow session reproduced
   it cleanly: 3 BUYs cancelled per log, all 3 still
   ``status="open"`` in shadow.db at exit. The fix is ~30 LOC; not
   shipping it leaves cli/live with the same bug against real
   money on next-session startup.
2. **Stage 8.0 cleared the way.** The poll-loop helper extraction
   means there's now one place per daemon to wire reconciliation.
   Without 8.0.C's helper, this stage would touch 5+ files for the
   wiring alone.
3. **Phase 8.2's maintenance worker needs reliable startup.** The
   background-maintenance worker (8.2) assumes the engine can boot
   into a known-good state. Without 8.1's reconciliation,
   maintenance could trip over stale-open rows on every restart.

## Proposed slicing

| Slice | Scope | Risk | Est. |
|-------|-------|------|------|
| **8.1.A — Kickoff** | ADR-018 + `stage-8.1-design.md` + roadmap polish. No code. | Low | (this commit) |
| **8.1.B — Persistence-on-cancel fix** | After `await adapter.cancel_order(o)` succeeds in cli/live (`_cancel_open_orders`) + cli/shadow (`_cancel_open_orders`), write `storage.save_order(o.model_copy(update={"status": "canceled", "updated_at": now}))`. Cancel failures still log; storage failures log + continue (don't block shutdown). Per-symbol tests in cli/live test suite + cli/shadow test suite — verify the persistence write happens AFTER the adapter cancel succeeds, NOT before. | Low | ~1h |
| **8.1.C — Startup reconciliation** | New `services/reconciler.py` with pure function `reconcile_open_orders(exchange_open, storage_open) -> ReconciliationPlan`. New `ReconciliationPlan` frozen dataclass enumerating (storage_only_orders, exchange_only_order_ids). Async orchestrator `apply_reconciliation(adapter, storage, *, symbols) -> ReconciliationReport` queries both sides, computes the plan, writes the storage transitions, logs the orphans. Wiring: `cli/live._main_async` and `cli/shadow._main_async` each call `apply_reconciliation(...)` after storage open + adapter construct but before engine first tick. Tests: pure-function tests for `reconcile_open_orders` + integration test for `apply_reconciliation` via mocked exchange + in-memory storage covering both-agree / storage-only / exchange-only cases. | Medium | ~2-3h |
| **8.1.D — Stage close** | Roadmap ✅ + per-sub-slice receipts. CHANGELOG entry. CLAUDE.md polish (Stage 8.1 close + ADR-018 ratified). project_state memory. | Low | ~30min |

**Total: ~4-5 hours of focused implementation.** Same-day stage.
Modest test count growth (~15-25 new tests across B + C). No
real-money risk — read-only Kraken queries at boot + storage
writes are forensic; the engine doesn't place or cancel any orders
as part of reconciliation.

## Design decisions to ratify

ADR-018 covers the reconciliation *policy*. The items below are
*implementation-level* decisions that should land at the start of
8.1.B / 8.1.C and stay stable through the stage.

### 1. Per-symbol or global reconciliation pass

**Decision:** Global pass. The reconciler queries
``adapter.get_open_orders(symbol=None)`` once at startup — every
symbol the engine could possibly be trading shows up in one
response.

**Why:** Multi-symbol mode (Stage 2.4) has the engine managing
multiple symbols in series within a tick. A reconciler that iterates
per-symbol would make N Kraken API calls instead of 1. The single
call returns everything; the reconciler partitions in memory.

### 2. Persistence-on-cancel uses the in-memory order, not a re-read

**Decision:** After ``await adapter.cancel_order(o)`` succeeds, the
shutdown loop writes ``o.model_copy(update={"status": "canceled",
"updated_at": now})`` directly. No ``storage.get_order(o.id)`` +
re-write round-trip.

**Why:** The shutdown path already has the ``Order`` object in
hand (it just queried ``adapter.get_open_orders``); a re-read is
pure overhead. ``model_copy`` is the canonical "small status
transition" idiom across the project.

### 3. Reconciliation runs as the LAST step before engine kickoff

**Decision:** ``_main_async`` order becomes: load config →
construct adapter → open storage → **reconcile** → install signal
handlers → engine.run_loop().

**Why:** Reconciliation needs both adapter (to query open orders)
and storage (to write the transitions). It must complete before
the engine first tick to keep tick-level reconciliation simple
(tick logic assumes storage matches exchange at tick start).
Signal handlers install AFTER reconciliation so a SIGINT during
reconciliation gets the bare ``asyncio.run`` default handling
(KeyboardInterrupt propagation) — clean exit, no half-applied
reconciliation. Bonus: if reconciliation itself fails, the daemon
exits before any engine state changes happen.

### 4. Reconciler signature: pure function + async orchestrator

**Decision:** Two layers.

- ``reconcile_open_orders(exchange_open, storage_open) ->
  ReconciliationPlan`` — pure function over input lists, returning
  what to do. No I/O. Trivially testable.
- ``apply_reconciliation(adapter, storage) ->
  ReconciliationReport`` — async orchestrator: queries both
  sides, calls the pure function, writes the transitions, logs
  orphans, returns metrics for the caller's logging.

**Why:** Same Stage 2.2 split that paid off for grid layout:
pure math first, I/O wrapper second. Tests exercise the pure
function exhaustively; the orchestrator gets a small smoke test.

### 5. ReconciliationReport carries metrics for session-start logging

**Decision:** ``ReconciliationReport`` frozen dataclass with
``storage_canceled_count``, ``orphan_count``, ``orphan_summaries``.
``cli/live`` / ``cli/shadow`` log it in their session-start log
extras alongside the existing fields.

**Why:** Operator's first signal that reconciliation did anything
is the session-start log line. Surfacing the counts makes
"reconciliation found 3 stale rows" visible without digging.

### 6. Orphan logging shape

**Decision:** Each orphan logged as a separate ERROR-level line
with structured fields: ``exchange_id``, ``symbol``, ``side``,
``price``, ``amount``, ``status_at_exchange``. Plus a single
session-start summary line ``"orphan orders detected at startup,
N total — review Kraken Pro and reconcile manually"`` so the
operator gets one prominent line + the per-order details for
forensic inspection.

**Why:** ERROR level because orphans need operator attention.
Per-order detail so the operator can find them in Kraken Pro
quickly. The summary line is the "you should look at this"
hook; the per-order lines are the data.

### 7. Reconciliation timeout

**Decision:** No special timeout for reconciliation. The
``adapter.get_open_orders()`` call inherits the adapter's normal
timeout (Kraken: 10s per the exchange config). If it times out,
reconciliation surfaces the ``ExchangeError`` and the daemon
fails to start.

**Why:** Refusing to start on a Kraken-down condition is correct
— the alternative (skip reconciliation, boot anyway) means the
engine ticks against unreconciled state, which is what 8.1
exists to fix. Kraken-down at startup is operator-actionable.

### 8. Per-symbol reconciliation when symbols are restricted

**Decision:** ``apply_reconciliation`` accepts an optional
``symbols`` arg. When set, the reconciler IGNORES exchange-only
orders on symbols not in the configured set (those are operator
manual orders on unrelated coins — explicitly not WobbleBot's
business). Storage-only reconciliation still runs against all
storage rows so stale rows in any symbol get cleared.

**Why:** Multi-symbol mode (Stage 2.4) lets the operator
configure which symbols the engine trades. An orphan order on
SOL when the engine is configured only for BTC/USD + ETH/USD is
not noise the engine should flag — that's the operator using
Kraken Pro for SOL on the side. Skipping non-engine symbols
keeps the orphan log signal-rich.

## Test plan

- **Pure-function tests for ``reconcile_open_orders``:** ~10
  tests covering both-agree (no plan), storage-only (one or more
  rows → plan lists them), exchange-only (one or more orders →
  plan lists them), mixed (both sides have non-overlapping
  entries), empty inputs, large inputs (100 orders both sides).
- **Async tests for ``apply_reconciliation``:** ~5 tests using
  the existing ``MockExchangeAdapter`` + in-memory
  ``SQLiteStorageAdapter`` to drive end-to-end behavior.
  Verifies: storage rows actually transition to "canceled";
  orphans log at ERROR level; orphan symbols outside the
  configured set are silently skipped.
- **cli/live + cli/shadow shutdown tests:** ~4 tests verifying
  the persistence-on-cancel fix. Mock adapter that succeeds the
  cancel call → assert the storage row's status flips to
  canceled. Mock adapter that fails the cancel call → assert
  storage stays at "open" (don't lie in the audit trail).
- **Integration:** None new. The Stage 5.7 + Phase 7 e2e suites
  exercise the full path; if reconciliation breaks them, those
  tests will catch it.

Expected counts mid-stage: 8.1.B may add ~4 tests; 8.1.C may
add ~15 tests. ~19 new tests total — close to the ADR's
"~15-25" guidance.

## What's NOT in scope for Stage 8.1

Each documented here so it doesn't get pulled in mid-stage:

- **Harvester reconciliation** — per ADR-018 decision 6, deferred
  to v1.1. ``transfer_results`` rows stay manually-reconciled in
  v1.0.
- **Per-tick reconciliation refinement** — engine tick logic
  already handles ongoing drift. This stage covers the startup
  gap only.
- **Adopting exchange-only orders** — per ADR-018 decision 3,
  rejected. Orphans log + log loudly; operator decides manually.
- **Stage 8.2's maintenance worker** — separate stage.
- **Hardening the `pending_commands` reconciliation** — the
  Stage 5.7 TTL expirer handles abandoned awaiting-confirmation
  rows; nothing more is needed at startup.

## Stage close criteria

1. ``services/reconciler.py`` ships with the pure
   ``reconcile_open_orders`` + async ``apply_reconciliation``.
2. cli/live + cli/shadow shutdown loops persist the
   ``status="canceled"`` transition after each successful
   ``adapter.cancel_order`` call.
3. cli/live + cli/shadow boot path calls ``apply_reconciliation``
   between storage open + engine first tick.
4. ~19 new unit tests pass; ~1730 total.
5. mypy + pylint 10.00/10 + black + isort all clean.
6. Roadmap + CHANGELOG + CLAUDE.md + project_state memory all
   reflect Stage 8.1 ✅.
7. ADR-018 committed in `docs/architecture/decisions.md` (this
   kickoff commit handles it).
8. The 2026-05-18 shadow-session repro no longer leaves stale
   open rows. (Acceptance signal: re-run a quick shadow session,
   inspect shadow.db at exit, all orders show status="canceled".)

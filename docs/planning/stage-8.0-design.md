# Stage 8.0 — Deferred Phase-5-Audit Refactors: Design and Slicing

*Drafted 2026-05-18 at Phase 8 kickoff, before any 8.0 code was
written. Three medium refactors surfaced by the Phase 5 close audit
punch list, parked at that time for proper planning rather than
landing inline. Living document — actual slicing may adjust during
implementation, but the principles below are load-bearing and should
not be relitigated without revisiting the audit findings.*

## What Stage 8.0 delivers

Three sub-slices that pay down Phase 5's code-organization debt
without touching behavior. By the time Stage 8.0 closes:

- `ports/operator.py` is split into three focused modules
  (~250-300 lines each instead of 734 lines in one file).
- `services/operator_service.answer_query` no longer hand-rolls
  six near-identical "missing storage" graceful-degrade blocks.
- Five CLI daemons share one poll-loop helper instead of
  hand-rolling the same `while running: await do_one(); await
  asyncio.sleep(...)` pattern five times.

**Zero behavior change** across all three slices. Every existing
test stays green. Every import path that exists today still
resolves (the operator.py split uses module-level re-exports).
No new public API surface; nothing the operator sees changes.

## Why now

Three of the four Phase 5 close-audit refactor recommendations
(R5, R3, R2) were explicitly deferred per the audit's "fix small
defects, queue bigger reorganization" discipline. Phase 5 closed
2026-05-16; we now have two more phases of code experience
(Phase 6 cloud LLM integration, Phase 7 web UI) to validate that
these refactors still make sense. They do — the patterns the
audit flagged have continued to accrete:

- `ports/operator.py` gained no new content during Phases 6 + 7,
  but `services/operator_service.py` saw new graceful-degrade
  blocks during Phase 7 web view development (each view that reads
  a cross-DB storage repeats the "if storage is None: return
  empty/degraded" shape).
- Phase 6 didn't add CLI daemons but Phase 7's `cli/web` follows
  uvicorn's loop, not the hand-rolled pattern — confirming that
  the existing five daemons are a coherent group worth
  consolidating before a sixth lands.

Phase 8.1's reliability work will touch the engine shutdown +
startup paths heavily. Doing those changes against the current
tangled `cli/live`, `cli/shadow`, `cli/operator`, `cli/harvest`,
`cli/observe`, `cli/news`, `cli/advise` shapes would mean making
the same shutdown-discipline edit seven times. Stage 8.0.C's
poll-loop helper makes 8.1's job dramatically simpler.

## Proposed slicing

| Slice | Scope | Risk | Est. |
|-------|-------|------|------|
| **8.0.A — R5 split `ports/operator.py`** | New `ports/operator_intents.py` (PauseCommand…StopCommand, StatusQuery…HelpQuery, IntentCommand…IntentUnparseable, plus the three discriminated unions OperatorCommand / OperatorQuery / OperatorIntent). New `ports/operator_results.py` (SymbolStatusEntry…HelpResult per-query result types, QueryResult union, CommandResult, plus the entry types). `ports/operator.py` keeps: `OperatorError`, `PendingCommandStatus` + `PendingCommand`, `OperatorPort` ABC. Module re-exports preserve every existing import path. Zero behavior change. | Low | ~1-2h |
| **8.0.B — R3 storage-fallback helper** | `services/operator_service.answer_query` currently does ~6 versions of the pattern `if cross_db_storage is None: return EmptyResult(...)`. Extract a focused helper (likely a small async context manager or guard function) that takes a storage handle + result-construction lambda and returns either the degraded shape or yields the storage. Caller becomes ~3 lines instead of ~6 per query. Each query's degraded-result wire shape stays identical. Zero behavior change. | Low | ~1-2h |
| **8.0.C — R2 generic poll-loop helper** | Five CLI daemons hand-roll the same shutdown-aware poll loop: `cli/observe`, `cli/news`, `cli/advise`, `cli/harvest`, plus `cli/operator`'s notification forwarder AND its TTL expirer. Extract `cli/_common.run_poll_loop(do_one, *, interval_seconds, stop_event, name)` that wraps the canonical pattern. Each call site shrinks from ~15-20 lines of bespoke loop scaffolding to one `await run_poll_loop(...)` call. Shutdown semantics + signal-handler integration live in one place. Zero behavior change for the happy path; gives Phase 8.1 a single edit point for any future shutdown-discipline refinement. | Medium | ~2-3h |
| **8.0.D — Stage close** | Roadmap ✅ + per-sub-slice receipts. CHANGELOG entry. CLAUDE.md polish (no new entry points; just the Phase 8 progress marker + test-count bump). project_state memory updated. | Low | ~30min |

**Total: ~4-7 hours.** Same-day stage. No real-money risk; no
operator-facing CLI changes.

## Design decisions to ratify

### 1. Backward-compatible re-exports for the operator.py split

**Decision:** After splitting `ports/operator.py` into three modules,
the surviving `ports/operator.py` re-exports every type that moved
out. Existing code that does
`from wobblebot.ports.operator import PauseCommand` continues to
work unchanged.

**Why:** ~15 source files + ~20 test files import from
`wobblebot.ports.operator`. Forcing every callsite to learn the new
module structure inflates the diff without delivering value — the
split's payoff is "the file is smaller", not "every callsite knows
about three files."

**How:** `ports/operator.py` ends with `from
wobblebot.ports.operator_intents import *  # noqa: F401,F403` and the
same for results. The `__all__` declarations in the new modules
make the re-exports explicit. Future code MAY import from the
focused modules directly; legacy code doesn't have to.

### 2. PendingCommand stays in operator.py, not results

**Decision:** `PendingCommand` is a persistence-shape contract (it's
written to the `pending_commands` SQLite table by Stage 5.4's
`cli/operator` and read by Stage 5.4's `cli/live`). Conceptually it
sits with the port ABC, not with intent / result types. It stays in
`ports/operator.py`.

**Why:** PendingCommand uses both `OperatorCommand` (the intent
side) and `CommandResult` (the result side). It's neither an intent
nor a result; it's the audit-trail row that wraps a confirmed intent
plus the eventual dispatch result. The natural home is alongside the
port that produces and consumes it.

### 3. Storage-fallback helper API shape: async context manager

**Decision:** The helper is shaped as an `async with` block that
yields the storage (or the degraded-result shape) so the query
handler reads as a single linear flow:

```python
async with degrade_if_unwired(
    self._advise_storage,
    or_else=lambda: RecentSuggestionsResult(suggestions=[]),
) as advise_storage:
    rows = await advise_storage.get_advisor_suggestions(...)
    return RecentSuggestionsResult(suggestions=[...])
```

**Why:** The alternative (a guard function returning `T | None`)
forces every callsite to either branch on the result or use a
`walrus` operator + early return. The context-manager shape lets
the happy path read top-to-bottom without conditionals and keeps
the degraded shape declarative.

Implementation can use `contextlib.asynccontextmanager` so the
helper stays a small pure-function with one decorator.

### 4. Poll-loop helper signature

**Decision:** The helper accepts an `async fn() -> None` (the
"do one cycle" callable), an interval, and a stop event. It owns:

- The `while not stop_event.is_set()` loop
- The `await fn()` inside a `try/except WobbleBotPortError` so one
  bad cycle doesn't kill the daemon
- The `await asyncio.sleep(interval)` between cycles
- Final cleanup logging on exit

Signal handler installation stays at the CLI level (each daemon
already does its own SIGINT/SIGTERM wiring; the loop helper just
respects the shared `stop_event`).

**Why:** Keeping signal handling at the CLI level avoids the
helper claiming too much. The five daemons have slightly different
"on shutdown" semantics (cli/live cancels orders; cli/harvest
records final state; cli/operator drains the forwarder queue);
each owns its own teardown path. The helper just owns the loop
discipline.

### 5. Order: A → B → C, not concurrent

**Decision:** Land 8.0.A first, then 8.0.B, then 8.0.C. Don't
parallelize the slices.

**Why:** Each slice touches a different layer. 8.0.A is pure
module reorganization (lowest blast radius). 8.0.B touches one
service file (medium). 8.0.C touches five CLI files (highest).
Sequential landing means each slice can be reverted independently
if testing reveals an issue, and the test-suite stays a stable
baseline between commits.

## Test plan

**Unit, ~0 net new tests.** All three slices are refactors; the
goal is "every existing test stays green," not "add coverage."
Net test count stays at 1700 through Stage 8.0; any test changes
are mechanical (e.g. updating an import path in a test fixture if
it imported a private helper that moved). The pylint /
mypy / black / isort gates are the active acceptance signal.

Expected counts mid-stage: 8.0.A may add ~5 tests for the
`__all__` declarations + re-export coverage. 8.0.B may add ~3
tests for the context-manager helper. 8.0.C may add ~5 tests for
the poll-loop helper. So roughly +10-15 new tests across the
stage, but every one of them validates the refactor mechanics,
not new behavior.

**Lint gates:**
- pylint **10.00/10** maintained across all three slices.
- mypy clean (no new files trip strict type-checking).
- black + isort clean.

**Deprived-env walkthrough:** none — no new CLIs, no new config
keys, no new env vars.

## What's NOT in scope for Stage 8.0

Each of these is queued for its own stage or phase. Documenting
here so it doesn't get pulled in mid-refactor:

- **R1, R4** — other Phase 5 audit findings. R1 was a small defect
  fixed inline at Phase 5 close; R4 was a doc-only finding handled
  during Phase 5 close. Only R2/R3/R5 needed proper planning.
- **Reconciliation logic on startup.** Reading stale-open
  `pending_commands` rows on cli/live startup, diffing live.db open
  orders against Kraken's actual book, etc. — that's Stage 8.1's
  charter. 8.0 keeps the existing behavior; 8.1 changes it.
- **The persistence-on-cancel fix** documented in the roadmap's
  Stage 8.1 known-issue backlog. Same reason — 8.1 owns shutdown
  + startup discipline as a coherent story.
- **Background maintenance worker.** Stage 8.2.
- **Performance tuning.** Stage 8.3.
- **Splitting `services/operator_service.py`** itself. The 563-line
  file is below the soft cap and its match/case dispatch is the
  single-source-of-truth surface for OperatorPort. No refactor
  pressure.

## Stage close criteria

1. `ports/operator_intents.py` + `ports/operator_results.py` ship;
   `ports/operator.py` ≤ 200 lines.
2. `services/operator_service.answer_query` has at most one
   `if storage is None` guard (factored into the helper); the
   six existing degrade blocks collapse to call sites.
3. Five CLI daemons use the shared poll-loop helper; the bespoke
   `while not stop_event.is_set()` patterns are gone.
4. All ~1700 existing unit tests stay green.
5. pylint **10.00/10** maintained; mypy clean; black + isort
   clean.
6. Roadmap entries for 8.0.A / 8.0.B / 8.0.C / 8.0.D each carry
   completion dates + receipts.
7. project_state memory + MEMORY.md index reflect Stage 8.0 ✅.

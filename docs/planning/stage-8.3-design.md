# Stage 8.3 — Performance & Resource Tuning: Design and Slicing

*Drafted 2026-05-18 at Stage 8.3 kickoff. Living document — actual
slicing may adjust during implementation.*

## What Stage 8.3 delivers

The right v1.0-scoped performance work in a project that targets
a Synology NAS deployment the operator hasn't actually run on yet:

1. **Universal easy wins** — SQLite pragmas that improve performance
   regardless of hardware (WAL mode, synchronous=NORMAL,
   foreign_keys=ON). These cost almost nothing to ship and provide
   meaningful improvements on every deployment.
2. **Index audit** — verify every hot read in the engine + cli/operator
   + cli/web hits an index, not a table scan. Add any missing
   indexes.
3. **Profile harness** — an operator-runnable tool that times the
   storage layer's common operations and reports p50/p99 latency.
   The operator runs it on their Synology to find their actual
   hotspots; gives Stage 8.4's soak test a baseline to compare
   against.

**What Stage 8.3 explicitly does NOT do:**

- Optimize anything without measurement. Premature optimization
  without baseline data is a way to ship complexity for no benefit.
- Cache layers, async query parallelism, batch APIs — defer to
  v1.1 if profiling on the actual Synology reveals they'd help.
- Hard-code Synology-specific tuning. The operator's hardware may
  change; everything 8.3 ships should help any deployment.
- Performance regression tests in CI. The operator's deployment is
  the canonical measurement surface; CI runs on different hardware
  entirely.

## Why now

Phase 8.2 stabilized DB hygiene (VACUUM cadence, retention pruning,
backups). Stage 8.4's v1.0 soak test needs a baseline against which
to verify "still fast enough after weeks of operation" — Stage 8.3
provides that baseline and the easy wins to make it look good from
the start.

## Why no ADR

Same logic as Stage 8.2: operational tuning, not cross-cutting
policy. The pragma defaults and index choices are recoverable —
revisitable in v1.1 if profiling reveals different priorities.
Decisions ratified in this design doc only.

## Proposed slicing

| Slice | Scope | Risk | Est. |
|-------|-------|------|------|
| **8.3.A — Kickoff** | This commit: `stage-8.3-design.md` + roadmap polish + CHANGELOG kickoff entry. No code. | Low | (this commit) |
| **8.3.B — SQLite pragmas** | `SQLiteStorageAdapter.connect()` applies three pragmas after opening the connection: `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, `PRAGMA foreign_keys=ON`. Skip for `":memory:"` DBs (WAL is irrelevant + can break tests that rely on quick fsync semantics). ~5 tests verifying the pragmas land and skip applies. | Low | ~30min |
| **8.3.C — Index audit + profile harness** | Walk every storage query against `sqlite_storage_schema.py`'s indexes. Add any missing indexes for hot reads (engine tick's `get_open_orders`, `get_trades`, etc.). New `tools/profile_storage.py` operator tool: timed loops of save/read/update against an in-memory or on-disk DB, reports p50/p99 in ms. ~10 tests. | Low | ~2-3h |
| **8.3.D — Stage close** | Roadmap ✅, CHANGELOG entry, CLAUDE.md polish (no new entry point — `tools/` scripts don't count). project_state memory + MEMORY.md. | Low | ~30min |

**Total: ~3-4 hours.** Same-day stage.

## Design decisions to ratify

### 1. WAL mode for all on-disk DBs

`PRAGMA journal_mode=WAL` switches SQLite from the default
rollback-journal mode to write-ahead logging. Two payoffs:

- **Concurrent readers don't block writers.** `cli/maintenance`'s
  backup task can read the live DB while `cli/live` is writing
  ticks. Pre-WAL the backup would have to wait for the writer to
  commit + release its exclusive lock.
- **Faster commits in general.** WAL doesn't require an fsync on
  every commit; it batches them.

Tradeoff: WAL creates a `<dbname>.db-wal` sidecar file. Backups
need to capture both the main file and the WAL file — SQLite's
online `.backup` API (Stage 8.2.C) handles this automatically.

### 2. synchronous=NORMAL instead of FULL

SQLite's default `synchronous=FULL` calls `fsync()` on every
commit. NORMAL only fsyncs at WAL checkpoint boundaries — ~50x
faster on a typical commit.

The durability tradeoff: on power loss, NORMAL may lose the last
few committed transactions. For wobblebot's use case this is
acceptable — the engine reconciliation logic (Stage 8.1) catches
state drift on next startup, and the worst-case forensic loss is
"the last 2-3 ticks of order state, recoverable from Kraken's
side anyway".

Combined with WAL mode (decision 1), NORMAL is the SQLite docs'
recommended setting. We're following published guidance, not
inventing a tradeoff.

### 3. foreign_keys=ON

SQLite defaults `foreign_keys=OFF` for legacy compatibility
reasons. Enable per-connection. None of the project's tables
currently use FOREIGN KEY constraints (Stage 5.4's
pending_commands self-contained, no cross-table FKs), but enabling
the pragma is cheap insurance for v1.1 schema additions.

### 4. Skip pragmas for in-memory DBs

In-memory DBs (`:memory:`) don't benefit from WAL (no file to
write-ahead to) and have always-synchronous semantics (no disk
fsync to skip). Applying WAL to an in-memory DB is a no-op in
practice but can confuse test fixtures that introspect
journal_mode. Skip the pragmas when `db_path == ":memory:"`.

### 5. Index audit covers the engine's hot path first

The engine's per-tick storage reads:

- `get_open_orders(symbol)` — fired on every tick (filtered).
- `get_trades(symbol, start_time=...)` — fired by post-tick PnL.
- `save_order(order)` — fired on every order placement.

Each must hit an index. The schema's `sqlite_storage_schema.py`
already declares indexes; the audit verifies them via `EXPLAIN
QUERY PLAN` (run during test) and adds any missing.

### 6. Profile harness output: p50/p99 in ms

The operator's question is "is my Synology fast enough", not "is
the algorithm O(n log n)". p50/p99 in milliseconds is the
operator's mental model; mean/stddev is statistician language.

Profile harness runs N iterations (configurable; default 1000)
of each operation against an empty in-memory DB OR an operator-
specified on-disk path. Logs one structured line per operation:
``{operation, n, p50_ms, p99_ms, total_seconds}``.

### 7. tools/profile_storage.py, not cli/profile_storage

This is a one-shot operator-tunable diagnostic, not a long-running
daemon. Lives under `tools/` (alongside `show_proposals`,
`show_transfers`, etc.) not `cli/` (which is for operator-facing
operational entry points). Convention from earlier phases.

### 8. No CI perf regression check in v1.0

GitHub's CI runners have inconsistent performance characteristics
(noisy neighbors, throttling, different hardware per-run). A
"the engine tick must take <50ms" assertion in CI would either
flake constantly or be so loose it's not useful. The operator's
deployment is the canonical measurement surface. CI perf checks
are a hypothetical v1.1+ concern.

## Test plan

- **Pragma tests:** ~5 tests verifying `PRAGMA journal_mode` /
  `synchronous` / `foreign_keys` return the expected values after
  connect; in-memory DB skips the pragmas.
- **Index audit tests:** ~5 tests that `EXPLAIN QUERY PLAN` on the
  hot reads doesn't show `SCAN TABLE` (only `SEARCH` via index).
- **Profile harness tests:** ~5 tests for the timing helpers
  (p50/p99 math, structured output).

Expected counts mid-stage: ~15 new tests.

**Lint gates:**
- pylint **10.00/10** maintained.
- mypy clean.
- black + isort clean.

## What's NOT in scope for Stage 8.3

- **Profile + optimize specific Synology hotspots.** That's the
  operator's run-the-harness work; Stage 8.4's soak test is when
  the data comes in.
- **Caching layers.** Speculative until profiling justifies.
- **Async query parallelism.** Same.
- **Batch APIs** (e.g. save_orders_bulk). The engine's tick rate
  is 5s; current per-order save is fine.
- **Performance regression CI.** v1.1+ concern; CI hardware noise
  makes it untrustworthy at this scale.
- **Connection pooling.** Each CLI opens its connection at boot
  and reuses for the lifetime of the daemon — no pooling needed.

## Stage close criteria

1. `SQLiteStorageAdapter.connect()` applies the three pragmas for
   on-disk DBs; skips for in-memory.
2. Schema indexes verified against hot reads via `EXPLAIN QUERY
   PLAN`; any missing indexes added.
3. `tools/profile_storage.py` ships with `--db <path> --iterations N
   --operation <name>` flags. Outputs structured p50/p99 lines.
4. ~15 new unit tests pass.
5. mypy + pylint 10.00/10 + black + isort all clean.
6. Roadmap + CHANGELOG + CLAUDE.md + project_state memory reflect
   Stage 8.3 ✅.
7. No new CLI entry points (tools/ scripts don't count).

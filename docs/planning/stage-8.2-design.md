# Stage 8.2 — Background Maintenance Worker: Design and Slicing

*Drafted 2026-05-18 at Stage 8.2 kickoff. Living document — actual
slicing may adjust during implementation, but the principles below
are load-bearing.*

## What Stage 8.2 delivers

A long-running `cli/maintenance` daemon that handles three classes
of operational hygiene with no operator-facing surface change to
the trading or harvester paths:

1. **DB hygiene.** `VACUUM` against each operator DB on a weekly
   cadence. SQLite without `VACUUM` keeps fragmenting; over weeks
   the on-disk size diverges from the logical size. Running
   `VACUUM` reclaims the gap.
2. **Retention pruning + archive.** `price_snapshots` is the only
   high-volume table — `cli/observe` writes ~2880 rows/day per
   symbol at the default 30s cadence. After ~30 days that's
   ~80k rows/symbol of price tape. Operator-tunable retention
   cutoff; rows older than cutoff get archived to CSV in
   `data/archive/` then deleted. Other tables stay forever
   (forensic value > disk pressure for the audit trails).
3. **Local backups.** SQLite's online `.backup` API produces an
   atomic point-in-time snapshot without locking the writing
   daemon. Daily backups to `data/backups/<dbname>-YYYYMMDD.db`
   with a tunable retention (default: keep 7 daily). Remote
   destinations (S3, rclone, etc.) deferred — operator can
   manually sync the local backup directory in v1.0.

Plus a small adjacent win:

4. **Log rotation** via `TimedRotatingFileHandler` opt-in flag in
   `configure_logging`. When the operator sets the flag, logs go
   to `data/logs/<cli>.log` rotated daily with N-day retention;
   defaults stay stdout-only.

By the time Stage 8.2 closes:

- `services/maintenance.py` ships with `vacuum_database`,
  `prune_price_snapshots`, `archive_to_csv` pure-ish helpers.
- `services/backuper.py` ships with `backup_database_locally` +
  retention-pruning of old backup files.
- `cli/maintenance` daemon wires the three subsystems through
  the Stage 8.0.C `run_poll_loop` helper. Three concurrent
  scheduled tasks; each has its own cadence from
  `schedules.maintenance_*`.
- `configure_logging` learns an optional rotating-file handler
  knob.
- Fifteenth operator entry point lands:
  `python -m wobblebot.cli.maintenance`.
- ~25-35 new unit tests across the three services + the CLI.

## Why now

Phase 8.1 just landed the persistence-on-cancel fix + startup
reconciliation. The maintenance worker can now assume known-good
storage state at boot — no stale-open rows tripping VACUUM, no
mid-fix-state half-canceled orders confusing the prune pass.
Stage 8.0.C's `run_poll_loop` consolidation means the daemon's
three scheduled tasks reuse the same battle-tested loop discipline
already powering five other daemons. The pieces are in place.

## Why no ADR

The four subsystems above each carry implementation-level decisions
(archive format, backup retention, prune cadence, log rotation
shape) but none are cross-cutting commitments future stages would
need to re-ratify. They're operational tooling: tune-as-you-go.
The Phase 8.1 / ADR-018 pattern of "policy that other ADRs need
to align with" doesn't apply here. Decisions ratified in this
design doc only.

## Proposed slicing

| Slice | Scope | Risk | Est. |
|-------|-------|------|------|
| **8.2.A — Kickoff** | This commit: `stage-8.2-design.md` + roadmap polish + CHANGELOG kickoff entry. No code. | Low | (this commit) |
| **8.2.B — `services/maintenance.py`** | Three functions: `vacuum_database(storage)` (calls SQLite VACUUM via raw connection), `prune_price_snapshots(storage, *, older_than) -> int` (delete-after-archive count), `archive_to_csv(records, path)` (CSV writer with header). Archive-then-delete discipline: prune calls archive first, only deletes after successful write. ~15 tests. | Low | ~1-2h |
| **8.2.C — `services/backuper.py`** | `backup_database_locally(src_path, dest_dir) -> Path` using `sqlite3.Connection.backup()` for atomic copy. `prune_old_backups(dest_dir, *, keep_n_daily) -> int` deletes oldest backups beyond the retention horizon. `BackupDestination` Protocol for future remote variants. ~10 tests. | Low | ~1-2h |
| **8.2.D — `cli/maintenance` daemon + log rotation** | New `MaintenanceConfig` Pydantic schema (4 retention knobs + 3 cadence knobs in `schedules:`). New `cli/maintenance.py` daemon with three concurrent `run_poll_loop` tasks. `configure_logging` gains optional `rotating_file_path` kwarg using `TimedRotatingFileHandler`. ~10 tests + manual smoke. | Medium | ~2-3h |
| **8.2.E — Stage close** | Roadmap ✅, CHANGELOG entry, CLAUDE.md polish (15th operator entry point: `cli/maintenance`), project_state memory + MEMORY.md. | Low | ~30min |

**Total: ~4-7 hours.** Same-day stage if focused.

## Design decisions to ratify

These are *implementation-level* and should stay stable through
the stage. None are cross-cutting; revisiting them in a later
stage is fine.

### 1. One daemon, multiple scheduled tasks (not one daemon per task)

`cli/maintenance` runs three concurrent `asyncio.Task`s — one per
maintenance kind (vacuum / prune+archive / backup). Each task uses
`cli/_common.run_poll_loop` (Stage 8.0.C) with its own schedule
from `schedules.maintenance_*`. Same shape as `cli/operator`'s
forwarder + TTL-expirer + reaction handler.

**Why:** Operator runs one process to monitor instead of three;
config is in one place; shutdown discipline (signal handler →
stop_event → all three tasks exit cleanly) is one piece of code.

### 2. CSV for the archive format

Pruned `price_snapshots` rows get written to CSV in
`data/archive/<dbname>-YYYY-MM-DD.csv` before deletion. One file
per day per source DB.

**Why:** Zero new dependencies. Universally readable by every tool
the operator might use (pandas, Excel, jq, awk). Parquet would be
more efficient but adds `pyarrow` (~30MB install footprint) for a
benefit measured in MB of disk per year. Operator can convert
CSV → parquet downstream if they care.

### 3. Only `price_snapshots` gets pruned in v1.0

`price_snapshots` is the only table with bounded retention value:
it's a time-series tape for advisor metrics. Once a snapshot is
30 days old, the advisor's rolling-window queries don't touch it
anymore. Archiving + deleting reclaims disk.

**What stays unpruned (intentionally):**

- `orders`, `trades`, `transfer_proposals`, `transfer_results`,
  `applied_suggestions`, `advisor_suggestions`,
  `pending_commands`, `notifications`, `news_items`, `llm_calls`,
  `users` — every audit trail. The operator's forensic queries
  ("what happened on 2026-01-15?") need this data forever. Disk
  pressure is modest (`llm_calls` cost-ledger at <1MB/year for
  hobby-tier cloud-LLM usage; `orders`/`trades` for grid trading
  at maybe 10MB/year).

If the operator wants those pruned eventually, that's a v1.1+
refinement — separate retention knobs per table — not a v1.0
commitment.

### 4. Backup destinations: local-only in v1.0

`services/backuper.py` exposes `backup_database_locally(src, dest_dir)`.
For v1.0 the operator runs `cli/maintenance` against the same
machine; backups land in `data/backups/`. Remote backups (S3,
rclone, rsync) are a v1.1 concern — operator can drop the local
backup directory into any sync tool they already use.

The module exposes a `BackupDestination` Protocol so v1.1 can add
`S3BackupDestination(bucket, prefix)` etc. without restructuring.

### 5. Backup retention: keep N daily

After each new backup write, the daemon deletes backups beyond
`keep_n_daily` (default 7). Simple; predictable. Tiered weekly /
monthly retention deferred until the operator actually fills
their disk with backups.

### 6. VACUUM uses operator DB's raw sqlite3 connection

`SQLiteStorageAdapter` is the project's async wrapper around
`aiosqlite`. The `VACUUM` command can't run inside a transaction;
the cleanest way is a brief raw `sqlite3.connect(path).execute("VACUUM")`
call. No long-held locks needed; VACUUM uses its own internal
transaction.

### 7. Maintenance daemon is operator-started, not auto-launched

`cli/maintenance` is its own daemon the operator chooses to run
(matching `cli/live`, `cli/operator`, `cli/harvest`, etc.). It's
not auto-spawned by any other daemon. The operator's deployment
chooses to run it or not.

**Why:** Single-responsibility per daemon. Operator can run
maintenance on a different machine if they want. Operator can
disable it entirely if they prefer manual disk management.

### 8. Default schedules: vacuum weekly, prune daily, backup daily

- `schedules.maintenance_vacuum: 7d`
- `schedules.maintenance_prune: 1d`
- `schedules.maintenance_backup: 1d`

All operator-tunable. VACUUM is intensive enough that weekly is
the right default; pruning + backup are light enough to run
daily.

### 9. Archive directory + backup directory live under `data/`

- Archive: `data/archive/<dbname>-<YYYY-MM-DD>.csv`
- Backup: `data/backups/<dbname>-<YYYYMMDD-HHMM>.db`

Both created on first write if absent. Operator can symlink these
to a separate disk if they want to keep `data/` on SSD and
archive/backup on HDD.

### 10. Log rotation is opt-in via `configure_logging` kwarg

`configure_logging` learns:

```python
def configure_logging(
    ...,
    rotating_file_path: Path | None = None,
    rotate_when: str = "midnight",
    rotate_backup_count: int = 7,
):
```

When `rotating_file_path` is set, a `TimedRotatingFileHandler` is
added ALONGSIDE the existing stderr handler. Each CLI's config
can opt in via a new `log_file_path` knob in its per-CLI section.
Default stays stdout-only — operator doesn't pay the file-write
cost unless they ask for it.

## Test plan

- **Pure-function tests for maintenance helpers:** ~15 tests
  covering empty DBs, partial prune, archive round-trip, archive
  write failure leaves rows intact (don't delete if write
  failed).
- **Pure-function tests for backuper:** ~10 tests covering local
  backup creation, retention pruning, missing-source-file
  handling.
- **cli/maintenance smoke tests:** ~10 tests covering the three
  scheduled tasks dispatching correctly, deprived-env walkthrough
  (no `maintenance:` section → exit 2), no `data/` parent
  permissions → exit 2.

Expected counts mid-stage: ~25-35 new tests. No deletions.

**Lint gates:**
- pylint **10.00/10** maintained.
- mypy clean (now ~103 src files after the two new services + cli).
- black + isort clean.

## What's NOT in scope for Stage 8.2

- **Remote backup destinations** (S3 / rclone / rsync) — v1.1.
- **Tiered backup retention** (daily / weekly / monthly) — v1.1.
- **Pruning audit tables** (orders, trades, etc.) — operator can
  add knobs in v1.1+ if disk pressure justifies.
- **Per-CLI maintenance** (e.g. cli/live triggering its own
  vacuum) — explicitly NOT; cli/maintenance owns this surface.
- **Compression of archive CSV files** (gzip / zstd) — v1.1+. CSV
  files compress 4-8x but operators handle that downstream if
  they care.

## Stage close criteria

1. `services/maintenance.py` + `services/backuper.py` ship with
   the public functions documented.
2. `cli/maintenance` daemon boots cleanly + three scheduled
   tasks run independently.
3. `configure_logging` accepts the `rotating_file_path` kwarg
   without breaking existing callers (additive).
4. ~25-35 new unit tests pass; ~1760 total.
5. mypy + pylint 10.00/10 + black + isort all clean.
6. Roadmap + CHANGELOG + CLAUDE.md + project_state memory
   reflect Stage 8.2 ✅. CLAUDE.md mentions fifteenth operator
   entry point: `python -m wobblebot.cli.maintenance`.
7. Deprived-env walkthrough green for `cli/maintenance`: no
   config section → exit 2; bad `data/` path → exit 2.

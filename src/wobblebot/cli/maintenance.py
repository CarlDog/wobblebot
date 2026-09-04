"""Maintenance daemon — DB hygiene + retention + backups (Stage 8.2.D).

Run as a module::

    python -m wobblebot.cli.maintenance
    python -m wobblebot.cli.maintenance --profile conservative

Long-running daemon with seven concurrent scheduled tasks:

- **vacuum** — runs SQLite ``VACUUM`` against each configured DB
  on ``schedules.maintenance_vacuum`` cadence (default weekly).
- **prune+archive** — exports `price_snapshots` rows older than
  the retention horizon to CSV in ``data/archive/`` then deletes
  them, on ``schedules.maintenance_prune`` cadence (default daily).
  Since ADR-036 the same cycle also runs the per-table retention
  registry (``maintenance.retention:`` — news_items /
  conversation_turns / notifications), archives write gzipped, and
  the backup task dedupes same-day copies
  (``min_backup_interval_hours``).
- **backup** — atomic point-in-time SQLite ``.backup`` of every
  configured DB into ``data/backups/`` on
  ``schedules.maintenance_backup`` cadence (default daily). Old
  backups beyond the retention horizon are pruned after each
  write.
- **verify** — v1.1: restoration smoke test against each DB's
  LATEST backup on ``schedules.maintenance_verify`` cadence
  (default monthly). ``PRAGMA integrity_check`` + a representative
  ``SELECT`` against every table; notifies (when ``operator_db`` is
  wired) on any failure. Backups have been written since Day 1 and
  never verified before this — a silently-corrupt backup is
  otherwise only discovered the day it's needed.
- **reconcile** — 2026-08-22, born from a confirmed silent-trade-loss
  incident: one account-wide Kraken ``TradesHistory`` fetch per
  cycle, diffed against ``maintenance.reconcile_source_db`` for every
  symbol in ``live.symbols`` (the actually-traded set), on
  ``schedules.maintenance_reconcile`` cadence (default daily). A
  missing trade notifies at ``critical`` — it means the SellGuard's
  cost-basis replay for that symbol no longer matches reality; a
  trade whose order is still locally open is deferred (persistence
  pending), never paged. Reader-key credentials, storage opened
  read-only; never writes, never backfills. Implementation in
  ``cli/maintenance_reconcile.py`` (split out so this module stays
  DB-hygiene-only); shared diff logic in
  ``services/trade_reconciliation.py``; deeper manual diagnostic in
  ``tools/reconcile_trade_history.py``.
- **capital report** — ADR-040 stage 1 (2026-08-22): three read-only
  viability checks on ``schedules.maintenance_capital`` cadence
  (default daily). (1) does ``order_size_usd`` clear ``ordermin`` /
  ``costmin`` at every grid level; (2) is the HELD position itself
  above ``ordermin`` (a position below it cannot be sold at all);
  (3) is ``max_daily_spend_usd`` consumption tracking NET capital
  deployed, or is it charging for round-trips that already returned.
  Deliberately computes each condition rather than reading the
  engine's refusal reasons — the safety caps are evaluated before the
  exchange sees the order, so the same defect reports differently
  depending on which gate trips first. Advisory only: it notifies and
  writes nothing. Implementation in ``cli/maintenance_capital.py``;
  pure checks in ``services/capital_reporter.py``.
- **ledger sync** — ADR-040 follow-up (2026-08-22): ingests Kraken's
  ``Ledgers`` into ``operator.db`` on ``schedules.maintenance_ledger``
  cadence (default daily), so non-trade balance movements stop being
  invisible. Born from a real gap: SOL/ETH/ADA balances each exceeded
  their replayed quantity by exactly their net staking income (gross
  minus Kraken's 30% cut) while unstaked BTC/XRP/DOGE matched to the
  digit — the account was earning money nothing recorded. Fetches
  ACCOUNT-WIDE rather than per traded symbol: keying off
  ``live.symbols`` would have skipped ``BABY`` (staked but never
  traded) and the USD deposits recording the operator's own capital.
  Upserts on the exchange's ledger id, so re-ingesting is a no-op and
  no watermark can drift. Implementation in
  ``cli/maintenance_ledger.py``; aggregation in
  ``services/staking_income.py``.

Per `stage-8.2-design.md`:

- One daemon, multiple scheduled tasks (decision 1).
- Operator-started; not auto-spawned (decision 7).
- Only `price_snapshots` gets pruned in v1.0 (decision 3).
- Local-only backups in v1.0 (decision 4).

The seven tasks run independently via the Stage 8.0.C
``run_poll_loop`` helper — one bad cycle on any task doesn't kill
the others. Shutdown via SIGINT/SIGTERM flips the shared
``stop_event``; all seven tasks exit at their next loop iteration.

Per the Phase 8.1 reconciliation work the maintenance daemon
assumes known-good storage state at boot — no stale-open rows
from a prior session tripping VACUUM or prune.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    CONFIG_LOAD_ERRORS,
    PermanentAuthHalt,
    add_config_args,
    config_load_exit,
    emit_heartbeat,
    install_signal_handlers,
    load_operator_env,
    missing_section_exit,
    notify,
    run_poll_loop,
    run_with_clean_exit,
    safe_shutdown,
)
from wobblebot.cli.maintenance_capital import run_capital_report_cycle
from wobblebot.cli.maintenance_ledger import run_ledger_sync_cycle
from wobblebot.cli.maintenance_reconcile import run_reconcile_cycle
from wobblebot.config.cli import MaintenanceConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.notifier import NotifierPort
from wobblebot.services.backuper import (
    backup_database_locally,
    find_latest_backup,
    parse_backup_timestamp,
    prune_old_backups,
    verify_backup_restoration,
)
from wobblebot.services.maintenance import prune_price_snapshots, vacuum_database
from wobblebot.services.retention import PRUNABLE_TABLES, prune_table

_LOGGER = logging.getLogger("wobblebot.cli.maintenance")


# --------------------------------------------------------------------- #
# Per-task work functions                                               #
# --------------------------------------------------------------------- #


def _vacuum_all(target_dbs: list[Path]) -> int:
    """Run VACUUM against each configured DB. Per-DB failures are
    logged and the loop continues. Returns count of successful
    VACUUMs."""
    success = 0
    for db_path in target_dbs:
        if not db_path.exists():
            _LOGGER.warning(
                "vacuum target missing; skipping (db_path=%s)",
                db_path,
                extra={"db_path": str(db_path)},
            )
            continue
        try:
            vacuum_database(db_path)
            success += 1
        except (StorageError, FileNotFoundError, OSError) as exc:
            _LOGGER.warning(
                "vacuum failed on %s; continuing",
                str(db_path),
                extra={"error": str(exc)},
            )
    return success


def _resolve_retention_targets(
    maintenance: MaintenanceConfig,
) -> list[tuple[str, Path, int]]:
    """Validate ``maintenance.retention`` against the prunable registry.

    Returns resolved ``(table, db_path, horizon_days)`` triples. Raises
    ``ValueError`` on an unknown table name or a configured horizon
    whose home DB field is unset — both are config errors that must
    fail the boot loudly (ADR-036 decision 1), never silently no-op.
    """
    resolved: list[tuple[str, Path, int]] = []
    for table, days in maintenance.retention.items():
        target = PRUNABLE_TABLES.get(table)
        if target is None:
            raise ValueError(
                f"maintenance.retention names unknown table {table!r}; "
                f"prunable tables: {sorted(PRUNABLE_TABLES)}"
            )
        db_value = getattr(maintenance, target.db_field)
        if db_value is None:
            raise ValueError(
                f"maintenance.retention[{table!r}] needs maintenance.{target.db_field} set"
            )
        resolved.append((table, Path(db_value), days))
    return resolved


def _retention_prunes(retention_targets: list[tuple[str, Path, int]], archive_dir: Path) -> int:
    """Run the ADR-036 registry prunes. Per-table failures are logged
    and the loop continues (same fail-soft shape as ``_vacuum_all``).
    Returns total rows deleted across tables."""
    total = 0
    now = datetime.now(UTC)
    for table, db_path, days in retention_targets:
        older_than = now - timedelta(days=days)
        archive_name = f"{table}-{older_than.strftime('%Y-%m-%d')}.csv.gz"
        try:
            total += prune_table(
                db_path,
                table,
                older_than=older_than,
                archive_dir=archive_dir,
                archive_name=archive_name,
            )
        except (StorageError, FileExistsError, FileNotFoundError, OSError) as exc:
            _LOGGER.warning(
                "retention prune failed on %s; will retry next interval: %s: %s",
                table,
                type(exc).__name__,
                exc,
                extra={"table": table, "error": str(exc), "error_type": type(exc).__name__},
            )
    return total


async def _prune_one_cycle(maintenance: MaintenanceConfig) -> int:
    """Archive + delete eligible price_snapshots from the configured
    prune source DB. Returns rows deleted, 0 if no source configured."""
    if maintenance.prune_source_db is None:
        _LOGGER.debug("no prune_source_db configured; skipping prune cycle")
        return 0
    source_path = Path(maintenance.prune_source_db)
    if not source_path.exists():
        _LOGGER.warning(
            "prune_source_db does not exist; skipping (db_path=%s)",
            source_path,
            extra={"db_path": str(source_path)},
        )
        return 0
    older_than = datetime.now(UTC) - timedelta(
        days=maintenance.prune_price_snapshots_older_than_days
    )
    # .csv.gz since ADR-036 decision 5 — new archives gzip on write;
    # pre-existing raw .csv archives stay as they are.
    archive_name = f"{source_path.stem}-{older_than.strftime('%Y-%m-%d')}.csv.gz"
    storage = SQLiteStorageAdapter(str(source_path))
    try:
        await storage.connect()
    except StorageError as exc:
        _LOGGER.warning(
            "prune: failed to open source db (db_path=%s): %s",
            source_path,
            exc,
            extra={"db_path": str(source_path), "error": str(exc)},
        )
        return 0
    try:
        deleted = await prune_price_snapshots(
            storage,
            older_than=older_than,
            archive_dir=Path(maintenance.archive_dir),
            archive_name=archive_name,
        )
    except (StorageError, FileExistsError, OSError) as exc:
        _LOGGER.warning(
            "prune cycle failed; will retry next interval: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        deleted = 0
    finally:
        await storage.close()
    return deleted


def _backup_all(maintenance: MaintenanceConfig) -> int:
    """Back up every configured DB to local storage + prune old
    backups. Returns count of successful backups."""
    backup_dir = Path(maintenance.backup_dir)
    success = 0
    for db_str in maintenance.target_dbs:
        src = Path(db_str)
        if not src.exists():
            _LOGGER.warning(
                "backup target missing; skipping (db_path=%s)",
                src,
                extra={"db_path": str(src)},
            )
            continue
        # ADR-036 decision 6 — same-day dedupe: the backup cycle fires
        # on daemon start, so restart churn would otherwise burn the
        # keep_n_daily slots on near-identical same-day copies. Age
        # comes from the filename's embedded stamp, not mtime — a
        # copy/rsync refreshes mtime and would fake freshness.
        if maintenance.min_backup_interval_hours > 0:
            latest = find_latest_backup(backup_dir, db_stem=src.stem)
            stamp = parse_backup_timestamp(latest) if latest is not None else None
            if latest is not None and stamp is not None:
                age_hours = (datetime.now(UTC) - stamp).total_seconds() / 3600.0
                if age_hours < maintenance.min_backup_interval_hours:
                    _LOGGER.info(
                        "skipping backup; newest is %.1fh old (< %.0fh) (db_stem=%s)",
                        age_hours,
                        maintenance.min_backup_interval_hours,
                        src.stem,
                        extra={
                            "db_stem": src.stem,
                            "age_hours": round(age_hours, 1),
                            "min_backup_interval_hours": maintenance.min_backup_interval_hours,
                        },
                    )
                    continue
        try:
            backup_database_locally(src, backup_dir)
            success += 1
        except (StorageError, FileNotFoundError, OSError) as exc:
            _LOGGER.warning(
                "backup failed on %s; continuing",
                str(src),
                extra={"error": str(exc)},
            )
            continue
        # Retention: prune older backups for THIS db_stem.
        try:
            removed = prune_old_backups(
                backup_dir,
                db_stem=src.stem,
                keep_n_daily=maintenance.keep_n_daily_backups,
            )
            if removed:
                _LOGGER.info(
                    "pruned old backups (db_stem=%s, removed_count=%s)",
                    src.stem,
                    removed,
                    extra={"db_stem": src.stem, "removed_count": removed},
                )
        except OSError as exc:
            _LOGGER.warning(
                "backup retention prune failed; continuing (db_stem=%s): %s",
                src.stem,
                exc,
                extra={"db_stem": src.stem, "error": str(exc)},
            )
    return success


async def _verify_all(maintenance: MaintenanceConfig, notifier: NotifierPort | None) -> int:
    """Restoration smoke test against each configured DB's LATEST backup.

    Never touches ``target_dbs`` themselves — only the backup copies
    in ``backup_dir``. A DB with no backup yet (added to
    ``target_dbs`` after the last backup cycle) is skipped, not
    treated as a failure; the backup cycle will produce one on its
    own cadence. Returns count of backups that verified clean.
    """
    backup_dir = Path(maintenance.backup_dir)
    verified = 0
    for db_str in maintenance.target_dbs:
        db_stem = Path(db_str).stem
        latest = find_latest_backup(backup_dir, db_stem=db_stem)
        if latest is None:
            _LOGGER.debug(
                "no backup found to verify yet; skipping (db_stem=%s, backup_dir=%s)",
                db_stem,
                backup_dir,
                extra={"db_stem": db_stem, "backup_dir": str(backup_dir)},
            )
            continue
        result = verify_backup_restoration(latest)
        if result.ok:
            verified += 1
            _LOGGER.info(
                "backup verification passed (db_stem=%s, backup_path=%s, table_count=%s)",
                db_stem,
                latest,
                result.table_count,
                extra={
                    "db_stem": db_stem,
                    "backup_path": str(latest),
                    "table_count": result.table_count,
                },
            )
            continue
        _LOGGER.error(
            "BACKUP VERIFICATION FAILED — restoration smoke test could not read this backup "
            "(db_stem=%s, backup_path=%s): %s",
            db_stem,
            latest,
            result.error,
            extra={
                "db_stem": db_stem,
                "backup_path": str(latest),
                "error": result.error,
            },
        )
        await notify(
            notifier,
            level="error",
            title=f"Backup verification failed: {db_stem}",
            message=(
                f"The latest backup for {db_stem} ({latest.name}) failed its "
                f"restoration smoke test: {result.error}. The most recent "
                "GOOD backup may be older than expected -- investigate before "
                "relying on this DB's backups."
            ),
            context={
                "db_stem": db_stem,
                "backup_path": str(latest),
                "error": result.error,
            },
        )
    return verified


# --------------------------------------------------------------------- #
# Signal handlers                                                       #
# --------------------------------------------------------------------- #


# --------------------------------------------------------------------- #
# Main async entry point                                                #
# --------------------------------------------------------------------- #


async def _main_async(  # pylint: disable=too-many-locals,too-many-statements
    config: WobbleBotConfig,
) -> int:
    if config.maintenance is None:
        return missing_section_exit(_LOGGER, "maintenance")

    maintenance = config.maintenance
    target_dbs = [Path(p) for p in maintenance.target_dbs]
    if not target_dbs:
        _LOGGER.error(
            "maintenance.target_dbs is empty; nothing to maintain. "
            "Add the operator's DB paths (live.db / shadow.db / "
            "operator.db etc.) and restart."
        )
        return 2

    # ADR-036 boot validation: a retention entry naming an unknown
    # table or pointing at an unset DB field is a config error, not a
    # skippable cycle failure. Fail loud before any task starts.
    try:
        retention_targets = _resolve_retention_targets(maintenance)
    except ValueError as exc:
        _LOGGER.error(
            "invalid maintenance.retention config: %s; see config/settings.example.yml",
            exc,
            extra={"error": str(exc)},
        )
        return 2

    # Resolve cadences. Missing schedules fall back to the design-doc
    # defaults (vacuum 7d, prune 1d, backup 1d, verify 30d, reconcile 1d).
    vacuum_interval = _resolve_interval(config, "maintenance_vacuum", timedelta(days=7))
    prune_interval = _resolve_interval(config, "maintenance_prune", timedelta(days=1))
    backup_interval = _resolve_interval(config, "maintenance_backup", timedelta(days=1))
    verify_interval = _resolve_interval(config, "maintenance_verify", timedelta(days=30))
    reconcile_interval = _resolve_interval(config, "maintenance_reconcile", timedelta(days=1))
    capital_interval = _resolve_interval(config, "maintenance_capital", timedelta(days=1))
    ledger_interval = _resolve_interval(config, "maintenance_ledger", timedelta(days=1))

    # 2026-08-22: deliberately no dedicated symbol-list config field --
    # reconciling live.symbols, the ACTUALLY-TRADED set, means this can
    # never drift from what the SellGuard's cost basis needs to cover.
    # NOT grid.coins: that is a per-coin OVERRIDE map (coins fall back
    # to defaults without an entry), so it neither lists every traded
    # symbol nor excludes untraded ones -- the branch's first cut used
    # it and would have skipped actively-traded symbols while checking
    # never-traded ones (2026-08-22 review). No live section (a
    # maintenance-only deployment) -> nothing traded -> nothing to
    # reconcile.
    reconcile_symbols: list[Symbol] = list(config.live.symbols) if config.live else []
    reconcile_halt = PermanentAuthHalt("maintenance.reconcile")
    capital_halt = PermanentAuthHalt("maintenance.capital")
    ledger_halt = PermanentAuthHalt("maintenance.ledger")

    # Stage 8.4.E follow-up — when operator_db is configured, open it
    # so the four task cycles can write heartbeat rows. Failure to
    # open is a warning, not fatal: the daemon still maintains DBs;
    # the /health page just won't see liveness for cli/maintenance.
    # v1.1: the same connection backs the SqliteNotifierAdapter the
    # verify cycle uses to report a failed backup — same pattern as
    # cli/live's notifier wiring.
    operator_storage: SQLiteStorageAdapter | None = None
    notifier: SqliteNotifierAdapter | None = None
    if maintenance.operator_db is not None:
        operator_storage = SQLiteStorageAdapter(maintenance.operator_db)
        try:
            await operator_storage.connect()
        except StorageError as exc:
            _LOGGER.warning(
                "failed to open operator.db for heartbeat; /health will show cli/maintenance as "
                "UNKNOWN (path=%s): %s",
                maintenance.operator_db,
                exc,
                extra={"path": maintenance.operator_db, "error": str(exc)},
            )
            operator_storage = None
        else:
            notifier = SqliteNotifierAdapter(operator_storage)

    started_at = time.monotonic()
    stop_event = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop_event, logger=_LOGGER)

    _LOGGER.info(
        "maintenance session start (target_db_count=%s, vacuum_interval_seconds=%s, "
        "prune_interval_seconds=%s, backup_interval_seconds=%s, reconcile_symbol_count=%s)",
        len(target_dbs),
        vacuum_interval.total_seconds(),
        prune_interval.total_seconds(),
        backup_interval.total_seconds(),
        len(reconcile_symbols),
        extra={
            "target_db_count": len(target_dbs),
            "vacuum_interval_seconds": vacuum_interval.total_seconds(),
            "prune_interval_seconds": prune_interval.total_seconds(),
            "backup_interval_seconds": backup_interval.total_seconds(),
            "verify_interval_seconds": verify_interval.total_seconds(),
            "reconcile_interval_seconds": reconcile_interval.total_seconds(),
            "reconcile_symbol_count": len(reconcile_symbols),
            "reconcile_source_db": maintenance.reconcile_source_db,
            "archive_dir": maintenance.archive_dir,
            "backup_dir": maintenance.backup_dir,
            "prune_source_db": maintenance.prune_source_db,
            "keep_n_daily_backups": maintenance.keep_n_daily_backups,
        },
    )

    # Counters for session-end logging.
    vacuum_runs = 0
    prune_total_deleted = 0
    backup_runs = 0
    verify_runs = 0
    reconcile_clean_runs = 0
    capital_findings = 0
    ledger_entries_written = 0

    async def _vacuum_cycle() -> None:
        nonlocal vacuum_runs
        # Stage 8.4.E — any of the three tasks heartbeating keeps the
        # cli/maintenance row fresh on /health.
        await emit_heartbeat(operator_storage, "cli/maintenance")
        # 2026-05-25 follow-up: the sync VACUUM/backup calls block the
        # asyncio event loop for their full duration -- SIGINT queued
        # mid-cycle can't be processed until the work returns. Log
        # start + end + elapsed so an operator watching the log file
        # can tell "shutdown is waiting on VACUUM" vs "shutdown is
        # hung on something else". The wall-clock elapsed value also
        # gives operators a sense of expected duration for future
        # cycles on the same hardware.
        cycle_started = time.monotonic()
        _LOGGER.info(
            "vacuum cycle starting (db_count=%s)",
            len(target_dbs),
            extra={"db_count": len(target_dbs)},
        )
        ok = _vacuum_all(target_dbs)
        vacuum_runs += ok
        _LOGGER.info(
            "vacuum cycle complete (db_count=%s, succeeded=%s, elapsed_seconds=%s)",
            len(target_dbs),
            ok,
            round(time.monotonic() - cycle_started, 2),
            extra={
                "db_count": len(target_dbs),
                "succeeded": ok,
                "elapsed_seconds": round(time.monotonic() - cycle_started, 2),
            },
        )

    async def _prune_cycle() -> None:
        nonlocal prune_total_deleted
        await emit_heartbeat(operator_storage, "cli/maintenance")
        cycle_started = time.monotonic()
        _LOGGER.info(
            "prune cycle starting (source_db=%s, older_than_days=%s)",
            maintenance.prune_source_db,
            maintenance.prune_price_snapshots_older_than_days,
            extra={
                "source_db": maintenance.prune_source_db,
                "older_than_days": maintenance.prune_price_snapshots_older_than_days,
            },
        )
        deleted = await _prune_one_cycle(maintenance)
        # ADR-036 registry prunes ride the same cadence; sync raw-sqlite
        # calls, same event-loop-blocking caveat as VACUUM above.
        deleted += _retention_prunes(retention_targets, Path(maintenance.archive_dir))
        prune_total_deleted += deleted
        _LOGGER.info(
            "prune cycle complete (rows_deleted=%s, retention_table_count=%s, "
            "elapsed_seconds=%s)",
            deleted,
            len(retention_targets),
            round(time.monotonic() - cycle_started, 2),
            extra={
                "rows_deleted": deleted,
                "retention_table_count": len(retention_targets),
                "elapsed_seconds": round(time.monotonic() - cycle_started, 2),
            },
        )

    async def _backup_cycle() -> None:
        nonlocal backup_runs
        await emit_heartbeat(operator_storage, "cli/maintenance")
        cycle_started = time.monotonic()
        _LOGGER.info(
            "backup cycle starting (db_count=%s)",
            len(maintenance.target_dbs),
            extra={"db_count": len(maintenance.target_dbs)},
        )
        ok = _backup_all(maintenance)
        backup_runs += ok
        _LOGGER.info(
            "backup cycle complete (db_count=%s, succeeded=%s, elapsed_seconds=%s)",
            len(maintenance.target_dbs),
            ok,
            round(time.monotonic() - cycle_started, 2),
            extra={
                "db_count": len(maintenance.target_dbs),
                "succeeded": ok,
                "elapsed_seconds": round(time.monotonic() - cycle_started, 2),
            },
        )

    async def _verify_cycle() -> None:
        nonlocal verify_runs
        await emit_heartbeat(operator_storage, "cli/maintenance")
        cycle_started = time.monotonic()
        _LOGGER.info(
            "verify cycle starting (db_count=%s)",
            len(maintenance.target_dbs),
            extra={"db_count": len(maintenance.target_dbs)},
        )
        ok = await _verify_all(maintenance, notifier)
        verify_runs += ok
        _LOGGER.info(
            "verify cycle complete (db_count=%s, verified=%s, elapsed_seconds=%s)",
            len(maintenance.target_dbs),
            ok,
            round(time.monotonic() - cycle_started, 2),
            extra={
                "db_count": len(maintenance.target_dbs),
                "verified": ok,
                "elapsed_seconds": round(time.monotonic() - cycle_started, 2),
            },
        )

    async def _reconcile_cycle() -> None:
        nonlocal reconcile_clean_runs
        await emit_heartbeat(operator_storage, "cli/maintenance")
        cycle_started = time.monotonic()
        _LOGGER.info(
            "reconcile cycle starting (symbol_count=%s, source_db=%s)",
            len(reconcile_symbols),
            maintenance.reconcile_source_db,
            extra={
                "symbol_count": len(reconcile_symbols),
                "source_db": maintenance.reconcile_source_db,
            },
        )
        ok = await run_reconcile_cycle(maintenance, reconcile_symbols, notifier, reconcile_halt)
        reconcile_clean_runs += ok
        _LOGGER.info(
            "reconcile cycle complete (symbol_count=%s, clean=%s, elapsed_seconds=%s)",
            len(reconcile_symbols),
            ok,
            round(time.monotonic() - cycle_started, 2),
            extra={
                "symbol_count": len(reconcile_symbols),
                "clean": ok,
                "elapsed_seconds": round(time.monotonic() - cycle_started, 2),
            },
        )

    async def _capital_cycle() -> None:
        nonlocal capital_findings
        await emit_heartbeat(operator_storage, "cli/maintenance")
        cycle_started = time.monotonic()
        _LOGGER.info(
            "capital report cycle starting (symbol_count=%s)",
            len(reconcile_symbols),
            extra={"symbol_count": len(reconcile_symbols)},
        )
        found = await run_capital_report_cycle(
            maintenance,
            config.grid,
            config.safety,
            reconcile_symbols,
            notifier,
            capital_halt,
        )
        capital_findings += found
        _LOGGER.info(
            "capital report cycle complete (symbol_count=%s, findings=%s, elapsed_seconds=%s)",
            len(reconcile_symbols),
            found,
            round(time.monotonic() - cycle_started, 2),
            extra={
                "symbol_count": len(reconcile_symbols),
                "findings": found,
                "elapsed_seconds": round(time.monotonic() - cycle_started, 2),
            },
        )

    async def _ledger_cycle() -> None:
        nonlocal ledger_entries_written
        await emit_heartbeat(operator_storage, "cli/maintenance")
        cycle_started = time.monotonic()
        _LOGGER.info("ledger sync cycle starting")
        written = await run_ledger_sync_cycle(operator_storage, notifier, ledger_halt)
        ledger_entries_written += written
        _LOGGER.info(
            "ledger sync cycle complete (written=%s, elapsed_seconds=%s)",
            written,
            round(time.monotonic() - cycle_started, 2),
            extra={
                "written": written,
                "elapsed_seconds": round(time.monotonic() - cycle_started, 2),
            },
        )

    try:
        await asyncio.gather(
            run_poll_loop(
                _vacuum_cycle,
                interval_seconds=vacuum_interval.total_seconds(),
                stop_event=stop_event,
            ),
            run_poll_loop(
                _prune_cycle,
                interval_seconds=prune_interval.total_seconds(),
                stop_event=stop_event,
            ),
            run_poll_loop(
                _backup_cycle,
                interval_seconds=backup_interval.total_seconds(),
                stop_event=stop_event,
            ),
            run_poll_loop(
                _verify_cycle,
                interval_seconds=verify_interval.total_seconds(),
                stop_event=stop_event,
            ),
            run_poll_loop(
                _reconcile_cycle,
                interval_seconds=reconcile_interval.total_seconds(),
                stop_event=stop_event,
            ),
            run_poll_loop(
                _capital_cycle,
                interval_seconds=capital_interval.total_seconds(),
                stop_event=stop_event,
            ),
            run_poll_loop(
                _ledger_cycle,
                interval_seconds=ledger_interval.total_seconds(),
                stop_event=stop_event,
            ),
        )
    finally:
        if operator_storage is not None:
            await safe_shutdown(
                [("close_operator_storage", operator_storage.close)],
                logger=_LOGGER,
            )
        _LOGGER.info(
            "maintenance session end (duration_seconds=%s, vacuum_runs=%s, "
            "prune_rows_deleted_total=%s, backup_runs=%s, reconcile_clean_runs=%s, "
            "capital_findings=%s, ledger_entries=%s)",
            round(time.monotonic() - started_at, 1),
            vacuum_runs,
            prune_total_deleted,
            backup_runs,
            reconcile_clean_runs,
            capital_findings,
            ledger_entries_written,
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "vacuum_runs": vacuum_runs,
                "prune_rows_deleted_total": prune_total_deleted,
                "backup_runs": backup_runs,
                "verify_runs": verify_runs,
                "reconcile_clean_runs": reconcile_clean_runs,
                "capital_findings": capital_findings,
                "ledger_entries": ledger_entries_written,
            },
        )
    return 0


def _resolve_interval(config: WobbleBotConfig, name: str, default: timedelta) -> timedelta:
    """Pull a cadence from `schedules:` with a fallback."""
    try:
        return config.schedules.get(name)
    except KeyError:
        return default


# --------------------------------------------------------------------- #
# Entry point                                                           #
# --------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    args = parser.parse_args(argv)

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides={},
        )
    except CONFIG_LOAD_ERRORS as exc:
        return config_load_exit(exc)

    log_format: Any = (
        args.log_format
        if args.log_format is not None
        else (config.maintenance.log_format if config.maintenance else "plain")
    )
    log_file_path = config.maintenance.log_file_path if config.maintenance else None
    configure_logging(log_format=log_format, rotating_file_path=log_file_path)

    run_with_clean_exit(_main_async(config), logger=_LOGGER)


if __name__ == "__main__":
    raise SystemExit(main())

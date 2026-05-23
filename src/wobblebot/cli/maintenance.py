"""Maintenance daemon — DB hygiene + retention + backups (Stage 8.2.D).

Run as a module::

    python -m wobblebot.cli.maintenance
    python -m wobblebot.cli.maintenance --profile conservative

Long-running daemon with three concurrent scheduled tasks:

- **vacuum** — runs SQLite ``VACUUM`` against each configured DB
  on ``schedules.maintenance_vacuum`` cadence (default weekly).
- **prune+archive** — exports `price_snapshots` rows older than
  the retention horizon to CSV in ``data/archive/`` then deletes
  them, on ``schedules.maintenance_prune`` cadence (default daily).
- **backup** — atomic point-in-time SQLite ``.backup`` of every
  configured DB into ``data/backups/`` on
  ``schedules.maintenance_backup`` cadence (default daily). Old
  backups beyond the retention horizon are pruned after each
  write.

Per `stage-8.2-design.md`:

- One daemon, multiple scheduled tasks (decision 1).
- Operator-started; not auto-spawned (decision 7).
- Only `price_snapshots` gets pruned in v1.0 (decision 3).
- Local-only backups in v1.0 (decision 4).

The three tasks run independently via the Stage 8.0.C
``run_poll_loop`` helper — one bad cycle on any task doesn't kill
the others. Shutdown via SIGINT/SIGTERM flips the shared
``stop_event``; all three tasks exit at their next loop iteration.

Per the Phase 8.1 reconciliation work the maintenance daemon
assumes known-good storage state at boot — no stale-open rows
from a prior session tripping VACUUM or prune.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    emit_heartbeat,
    load_operator_env,
    run_poll_loop,
    safe_shutdown,
)
from wobblebot.config.cli import MaintenanceConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.backuper import backup_database_locally, prune_old_backups
from wobblebot.services.maintenance import prune_price_snapshots, vacuum_database

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
                "vacuum target missing; skipping",
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


async def _prune_one_cycle(maintenance: MaintenanceConfig) -> int:
    """Archive + delete eligible price_snapshots from the configured
    prune source DB. Returns rows deleted, 0 if no source configured."""
    if maintenance.prune_source_db is None:
        _LOGGER.debug("no prune_source_db configured; skipping prune cycle")
        return 0
    source_path = Path(maintenance.prune_source_db)
    if not source_path.exists():
        _LOGGER.warning(
            "prune_source_db does not exist; skipping",
            extra={"db_path": str(source_path)},
        )
        return 0
    older_than = datetime.now(UTC) - timedelta(
        days=maintenance.prune_price_snapshots_older_than_days
    )
    archive_name = f"{source_path.stem}-{older_than.strftime('%Y-%m-%d')}.csv"
    storage = SQLiteStorageAdapter(str(source_path))
    try:
        await storage.connect()
    except StorageError as exc:
        _LOGGER.warning(
            "prune: failed to open source db",
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
            "prune cycle failed; will retry next interval",
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
                "backup target missing; skipping",
                extra={"db_path": str(src)},
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
                    "pruned old backups",
                    extra={"db_stem": src.stem, "removed_count": removed},
                )
        except OSError as exc:
            _LOGGER.warning(
                "backup retention prune failed; continuing",
                extra={"db_stem": src.stem, "error": str(exc)},
            )
    return success


# --------------------------------------------------------------------- #
# Signal handlers                                                       #
# --------------------------------------------------------------------- #


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


# --------------------------------------------------------------------- #
# Main async entry point                                                #
# --------------------------------------------------------------------- #


async def _main_async(config: WobbleBotConfig) -> int:
    if config.maintenance is None:
        _LOGGER.error(
            "settings.yml is missing the `maintenance:` section; "
            "see config/settings.example.yml for the template"
        )
        return 2

    maintenance = config.maintenance
    target_dbs = [Path(p) for p in maintenance.target_dbs]
    if not target_dbs:
        _LOGGER.error(
            "maintenance.target_dbs is empty; nothing to maintain. "
            "Add the operator's DB paths (live.db / shadow.db / "
            "operator.db etc.) and restart."
        )
        return 2

    # Resolve cadences. Missing schedules fall back to the design-doc
    # defaults (vacuum 7d, prune 1d, backup 1d).
    vacuum_interval = _resolve_interval(config, "maintenance_vacuum", timedelta(days=7))
    prune_interval = _resolve_interval(config, "maintenance_prune", timedelta(days=1))
    backup_interval = _resolve_interval(config, "maintenance_backup", timedelta(days=1))

    # Stage 8.4.E follow-up — when operator_db is configured, open it
    # so the three task cycles can write heartbeat rows. Failure to
    # open is a warning, not fatal: the daemon still maintains DBs;
    # the /health page just won't see liveness for cli/maintenance.
    operator_storage: SQLiteStorageAdapter | None = None
    if maintenance.operator_db is not None:
        operator_storage = SQLiteStorageAdapter(maintenance.operator_db)
        try:
            await operator_storage.connect()
        except StorageError as exc:
            _LOGGER.warning(
                "failed to open operator.db for heartbeat; "
                "/health will show cli/maintenance as UNKNOWN",
                extra={"path": maintenance.operator_db, "error": str(exc)},
            )
            operator_storage = None

    started_at = time.monotonic()
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    _LOGGER.info(
        "maintenance session start",
        extra={
            "target_db_count": len(target_dbs),
            "vacuum_interval_seconds": vacuum_interval.total_seconds(),
            "prune_interval_seconds": prune_interval.total_seconds(),
            "backup_interval_seconds": backup_interval.total_seconds(),
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

    async def _vacuum_cycle() -> None:
        nonlocal vacuum_runs
        # Stage 8.4.E — any of the three tasks heartbeating keeps the
        # cli/maintenance row fresh on /health.
        await emit_heartbeat(operator_storage, "cli/maintenance")
        ok = _vacuum_all(target_dbs)
        vacuum_runs += ok

    async def _prune_cycle() -> None:
        nonlocal prune_total_deleted
        await emit_heartbeat(operator_storage, "cli/maintenance")
        prune_total_deleted += await _prune_one_cycle(maintenance)

    async def _backup_cycle() -> None:
        nonlocal backup_runs
        await emit_heartbeat(operator_storage, "cli/maintenance")
        ok = _backup_all(maintenance)
        backup_runs += ok

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
        )
    finally:
        if operator_storage is not None:
            await safe_shutdown(
                [("close_operator_storage", operator_storage.close)],
                logger=_LOGGER,
            )
        _LOGGER.info(
            "maintenance session end",
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "vacuum_runs": vacuum_runs,
                "prune_rows_deleted_total": prune_total_deleted,
                "backup_runs": backup_runs,
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
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format: Any = (
        args.log_format
        if args.log_format is not None
        else (config.maintenance.log_format if config.maintenance else "plain")
    )
    rotating_path: Path | None = None
    if config.maintenance is not None and config.maintenance.log_file_path:
        rotating_path = Path(config.maintenance.log_file_path)
    configure_logging(log_format=log_format, rotating_file_path=rotating_path)

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

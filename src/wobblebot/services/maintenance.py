"""Maintenance services — VACUUM, prune+archive (Stage 8.2.B).

Pure-ish service helpers consumed by ``cli/maintenance``:

- :func:`vacuum_database` — runs SQLite ``VACUUM`` against a DB file.
- :func:`archive_price_snapshots_to_csv` — pure CSV writer over a
  list of :class:`PriceSnapshot` rows.
- :func:`prune_price_snapshots` — archive-then-delete discipline:
  reads candidate rows from storage, writes a CSV, deletes the rows
  via storage only after the write succeeds.

Per ``stage-8.2-design.md`` decisions 2, 3, 6:

- CSV format (zero new deps; readable everywhere).
- Only ``price_snapshots`` gets pruned in v1.0. Audit tables stay
  forever per the design doc.
- VACUUM uses a raw ``sqlite3.Connection.execute("VACUUM")`` because
  the command can't run inside ``aiosqlite``'s deferred-transaction
  wrapper. The brief sync call is fine — VACUUM is a maintenance
  operation, not a hot-path read.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from wobblebot.domain.models import PriceSnapshot
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort

_LOGGER = logging.getLogger("wobblebot.services.maintenance")

_CSV_HEADER = ("observed_at", "symbol_base", "symbol_quote", "price", "currency")


# --------------------------------------------------------------------- #
# VACUUM                                                                #
# --------------------------------------------------------------------- #


def vacuum_database(db_path: Path) -> None:
    """Run SQLite ``VACUUM`` against ``db_path``.

    Uses a raw ``sqlite3.connect`` rather than the async adapter
    because VACUUM cannot run inside ``aiosqlite``'s default
    transaction wrapper. The connection is opened, ``VACUUM``
    executes, the connection closes — no other DB activity needed.

    Per ``stage-8.2-design.md`` decision 6: the file is locked
    briefly during VACUUM. The maintenance daemon's schedule should
    not collide with `cli/live`'s tick cadence; defaults are
    weekly cadence with 5s engine ticks, no collision.

    Args:
        db_path: Path to the SQLite DB file.

    Raises:
        FileNotFoundError: If ``db_path`` doesn't exist.
        StorageError: If the VACUUM operation fails.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"VACUUM target does not exist: {db_path}")
    # NB: ``with sqlite3.connect(...)`` manages transactions but does
    # NOT close the connection. Explicit close() avoids leaking the
    # handle into pytest's unraisable warning hook.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("VACUUM")
        conn.commit()
    except sqlite3.Error as exc:
        raise StorageError(f"VACUUM failed on {db_path}: {exc}") from exc
    finally:
        conn.close()
    _LOGGER.info("VACUUM complete", extra={"db_path": str(db_path)})


# --------------------------------------------------------------------- #
# CSV archive writer                                                    #
# --------------------------------------------------------------------- #


def archive_price_snapshots_to_csv(snapshots: list[PriceSnapshot], dest_path: Path) -> int:
    """Write ``snapshots`` to ``dest_path`` as CSV with a header row.

    Pure I/O helper. No side effects beyond writing the file. Per
    ``stage-8.2-design.md`` decision 2 the CSV columns are stable:
    ``observed_at,symbol_base,symbol_quote,price,currency``.

    Creates parent directories if needed. Refuses to overwrite an
    existing file (caller should rename or delete first) so a re-run
    can't silently clobber yesterday's archive.

    Args:
        snapshots: Rows to write. Empty list creates a header-only
            file (and returns 0) so the operator can see "we ran
            but had nothing to archive today".
        dest_path: Destination CSV path.

    Returns:
        Count of rows written (header excluded).

    Raises:
        FileExistsError: If ``dest_path`` already exists.
        OSError: If write fails (disk full, perms, etc.).
    """
    if dest_path.exists():
        raise FileExistsError(f"archive target already exists; refusing to overwrite: {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for snap in snapshots:
            writer.writerow(
                (
                    snap.observed_at.dt.isoformat(),
                    snap.symbol.base,
                    snap.symbol.quote,
                    str(snap.price.amount),
                    snap.price.currency,
                )
            )
    _LOGGER.info(
        "archive write complete",
        extra={"dest_path": str(dest_path), "row_count": len(snapshots)},
    )
    return len(snapshots)


# --------------------------------------------------------------------- #
# Prune + archive                                                       #
# --------------------------------------------------------------------- #


async def prune_price_snapshots(
    storage: StoragePort,
    *,
    older_than: datetime,
    archive_dir: Path,
    archive_name: str,
) -> int:
    """Archive then delete price_snapshots older than ``older_than``.

    Discipline:

    1. Query storage for snapshots with ``observed_at <= older_than``.
    2. If empty: skip (no archive file created, no delete).
    3. Else: write CSV first. If write fails, propagate; nothing
       deleted.
    4. Only after the CSV file exists on disk does this call
       :meth:`StoragePort.delete_price_snapshots`. Failure here is
       logged + raised; the operator's next maintenance run retries
       the DELETE (no data loss since the rows are still in storage).

    Args:
        storage: StoragePort backing the source DB.
        older_than: Cutoff timestamp. Snapshots with ``observed_at <=
            older_than`` are eligible. Must be tz-aware.
        archive_dir: Directory the CSV goes in. Created if missing.
        archive_name: CSV filename (e.g. ``"observe-2026-05-18.csv"``).
            Caller decides naming so it can encode source DB + date.

    Returns:
        Count of rows archived + deleted. 0 if nothing was eligible.

    Raises:
        StorageError: On storage read or delete failure.
        FileExistsError: If the archive target already exists (re-runs
            on the same day need the caller to handle the collision).
    """
    eligible = await storage.get_price_snapshots(end_time=older_than)
    if not eligible:
        _LOGGER.info(
            "no price snapshots eligible for archive",
            extra={"older_than": older_than.astimezone(UTC).isoformat()},
        )
        return 0
    dest = archive_dir / archive_name
    archive_price_snapshots_to_csv(eligible, dest)
    deleted = await storage.delete_price_snapshots(before=older_than)
    _LOGGER.info(
        "price_snapshots prune+archive complete",
        extra={
            "archived_path": str(dest),
            "rows_archived": len(eligible),
            "rows_deleted": deleted,
        },
    )
    return deleted


__all__ = (
    "archive_price_snapshots_to_csv",
    "prune_price_snapshots",
    "vacuum_database",
)

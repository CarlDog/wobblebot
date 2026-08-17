"""Generic table retention — registry + archive-then-delete pruner (ADR-036).

v1.0 pruned exactly one table (``price_snapshots``, in
:mod:`wobblebot.services.maintenance`). ADR-036 extends retention to the
chatty tables via a **code-side registry**: :data:`PRUNABLE_TABLES` maps
each prunable table to its home-DB config field and timestamp column.
The registry is the allowlist — operator config
(``maintenance.retention:``) sets only the horizon in days, and a config
key naming any table outside the registry is rejected at daemon boot.
Forensic tables (``trades``, ``orders``, ``transfer_*``,
``pending_commands``, ...) simply are not in the registry, so no config
mistake can name the money ledger for deletion; ``FORENSIC_TABLES``
exists so a test can pin the two sets disjoint.

Archive-then-delete discipline matches Stage 8.2: rows are written to a
gzipped CSV (ADR-036 decision 5) and only after the file exists on disk
does the DELETE run. v1 archives always — there is no straight-delete
mode until a table actually wants one.

Raw ``sqlite3`` (not the async adapter) for the same reason
``vacuum_database`` uses it: this is a maintenance sweep over tables the
typed :class:`~wobblebot.ports.storage.StoragePort` has no generic
surface for, and adding four typed get+delete pairs per table would
bloat the port for no caller but this one. Table and column names come
exclusively from the registry (never from config or user input), so the
interpolated identifiers are code constants; the cutoff is a bound
parameter.
"""

from __future__ import annotations

import csv
import gzip
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wobblebot.ports.exceptions import StorageError

_LOGGER = logging.getLogger("wobblebot.services.retention")


@dataclass(frozen=True)
class RetentionTarget:
    """Where a prunable table lives and how its age is measured.

    Attributes:
        db_field: ``MaintenanceConfig`` field naming the home DB path
            (e.g. ``"news_db"``). Resolved by the CLI at boot so a
            configured horizon with no DB path is a loud config error,
            not a silent no-op.
        ts_column: TEXT ISO-8601 UTC column the horizon compares
            against. Chosen per table for row age *in our system*
            (news prunes by ``fetched_at``, not ``published_at``, so
            every row is guaranteed its full retention residence).
    """

    db_field: str
    ts_column: str


# The allowlist. A ``maintenance.retention:`` key must match one of
# these names exactly; everything else is a boot-time config error.
PRUNABLE_TABLES: dict[str, RetentionTarget] = {
    "news_items": RetentionTarget(db_field="news_db", ts_column="fetched_at"),
    "conversation_turns": RetentionTarget(db_field="operator_db", ts_column="timestamp"),
    "notifications": RetentionTarget(db_field="operator_db", ts_column="created_at"),
}

# ADR-036 decision 1: the forensic set, denylisted in code. Not read by
# the pruner (the allowlist above is the gate); this exists so a test
# can pin that no forensic table ever appears in PRUNABLE_TABLES.
# `advisor_suggestions` and `llm_calls` are keep-forever by operator
# decision (ADR-036 decision 2); the bounded upserts
# (`daemon_heartbeats`, `status_report_history`, `user_preferences`,
# `users`, `reanchor_snoozes`) don't grow and need no policy.
FORENSIC_TABLES: frozenset[str] = frozenset(
    {
        "orders",
        "trades",
        "transfer_proposals",
        "transfer_results",
        "pending_commands",
        "applied_suggestions",
        "cap_trips",
        "grid_state",
        "engine_state",
    }
)


def write_rows_to_csv_gz(
    header: tuple[str, ...], rows: list[tuple[object, ...]], dest_path: Path
) -> int:
    """Write ``rows`` to ``dest_path`` as a gzipped CSV with a header row.

    Same discipline as ``archive_price_snapshots_to_csv``: creates
    parent directories, refuses to overwrite an existing file so a
    re-run can't silently clobber a prior archive.

    Returns:
        Count of rows written (header excluded).

    Raises:
        FileExistsError: If ``dest_path`` already exists.
        OSError: If the write fails (disk full, perms, etc.).
    """
    if dest_path.exists():
        raise FileExistsError(f"archive target already exists; refusing to overwrite: {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest_path, "wt", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    _LOGGER.info(
        "archive write complete (dest_path=%s, row_count=%s)",
        dest_path,
        len(rows),
        extra={"dest_path": str(dest_path), "row_count": len(rows)},
    )
    return len(rows)


def prune_table(
    db_path: Path,
    table: str,
    *,
    older_than: datetime,
    archive_dir: Path,
    archive_name: str,
) -> int:
    """Archive then delete ``table`` rows older than ``older_than``.

    1. Reject any ``table`` not in :data:`PRUNABLE_TABLES` — defense in
       depth behind the CLI's boot validation.
    2. SELECT rows with ``ts_column <= older_than`` (ISO-8601 text
       comparison; the storage adapter writes uniform UTC isoformat).
    3. If empty: skip — no archive file, no delete.
    4. Write the gzipped CSV first; a write failure propagates with
       nothing deleted.
    5. Only after the archive exists on disk does the DELETE run.

    Returns:
        Count of rows archived + deleted. 0 if nothing was eligible.

    Raises:
        ValueError: ``table`` is not in the prunable registry.
        FileNotFoundError: ``db_path`` doesn't exist.
        StorageError: SQLite read or delete failure.
        FileExistsError: The archive target already exists.
    """
    target = PRUNABLE_TABLES.get(table)
    if target is None:
        raise ValueError(f"table {table!r} is not prunable; allowed: {sorted(PRUNABLE_TABLES)}")
    if not db_path.exists():
        raise FileNotFoundError(f"prune target DB does not exist: {db_path}")
    cutoff = older_than.astimezone(UTC).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            # Interpolated identifiers come only from the code-side
            # registry (validated above), never from config/user input.
            cursor = conn.execute(
                f"SELECT * FROM {table} WHERE {target.ts_column} <= ?",
                (cutoff,),
            )
            rows = cursor.fetchall()
            header = tuple(desc[0] for desc in cursor.description)
        except sqlite3.Error as exc:
            raise StorageError(f"retention read failed on {db_path}:{table}: {exc}") from exc
        if not rows:
            _LOGGER.info(
                "no %s rows eligible for archive (older_than=%s)",
                table,
                cutoff,
                extra={"table": table, "older_than": cutoff},
            )
            return 0
        write_rows_to_csv_gz(header, rows, archive_dir / archive_name)
        try:
            deleted = conn.execute(
                f"DELETE FROM {table} WHERE {target.ts_column} <= ?",
                (cutoff,),
            ).rowcount
            conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"retention delete failed on {db_path}:{table}: {exc}") from exc
    finally:
        conn.close()
    _LOGGER.info(
        "%s prune+archive complete (archived_path=%s, rows_archived=%s, rows_deleted=%s)",
        table,
        archive_dir / archive_name,
        len(rows),
        deleted,
        extra={
            "table": table,
            "archived_path": str(archive_dir / archive_name),
            "rows_archived": len(rows),
            "rows_deleted": deleted,
        },
    )
    return deleted


__all__ = (
    "FORENSIC_TABLES",
    "PRUNABLE_TABLES",
    "RetentionTarget",
    "prune_table",
    "write_rows_to_csv_gz",
)

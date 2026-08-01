"""Backup service — local SQLite ``.backup`` API (Stage 8.2.C).

Uses SQLite's online `backup API <https://www.sqlite.org/backup.html>`_
to produce point-in-time atomic copies WITHOUT locking the source
database against concurrent reads. ``cli/live`` can keep ticking
through the backup window.

Public surfaces:

- :func:`backup_database_locally` — copy `src_path` → `dest_dir`
  with timestamped filename. Returns the destination path.
- :func:`prune_old_backups` — delete oldest backups beyond
  retention horizon.
- :func:`find_latest_backup` — most recent backup file for a DB stem.
- :func:`verify_backup_restoration` — v1.1 restoration smoke test:
  ``PRAGMA integrity_check`` + a representative ``SELECT`` against
  every table. Backups have been written since Day 1 and never
  verified; a silently-corrupt backup file is only discovered the
  day it's needed.
- :class:`BackupDestination` — Protocol for v1.1 remote variants
  (S3 / rclone / etc.). v1.0 only ships the local implementation.

Per ``stage-8.2-design.md`` decisions 4 + 5:

- v1.0: local destinations only. Operator can rclone/rsync the
  resulting directory if they want offsite.
- Retention: keep N daily snapshots (default 7). Tiered weekly /
  monthly retention deferred to v1.1.
- Naming: ``<dbname-stem>-<YYYYMMDD-HHMM>.db`` so filenames sort
  lexicographically by recency.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from wobblebot.ports.exceptions import StorageError

_LOGGER = logging.getLogger("wobblebot.services.backuper")


class BackupDestination(Protocol):
    """Pluggable backup-destination shape for v1.1.

    v1.0 ships only the local-filesystem implementation (the
    :func:`backup_database_locally` function). A v1.1 S3 variant
    might be ``S3BackupDestination(bucket=..., prefix=...)`` with
    the same ``write(src_path) -> str`` shape.

    Keeping the Protocol declared in v1.0 lets test code build
    fake destinations + lets the v1.1 PR drop in without
    restructuring.
    """

    def write(self, src_path: Path) -> str:
        """Copy / upload ``src_path`` to the destination.

        Returns:
            String identifier of the resulting backup (local path,
            S3 URL, etc.).

        Raises:
            StorageError: If the backup write fails.
        """


# --------------------------------------------------------------------- #
# Local backup via SQLite online .backup API                            #
# --------------------------------------------------------------------- #


def backup_database_locally(
    src_path: Path,
    dest_dir: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """Atomic point-in-time backup of ``src_path`` into ``dest_dir``.

    Uses SQLite's online ``.backup`` API which:

    - Doesn't require the source DB to be quiesced — concurrent
      ``cli/live`` ticks can keep writing while the backup proceeds.
    - Produces a fully-valid SQLite file at the destination (not a
      partial / corrupt copy that file-level ``cp`` could leave on
      a busy DB).
    - Streams the page-level snapshot under SQLite's own internal
      lock discipline.

    Destination filename:
    ``<src_path.stem>-<YYYYMMDD-HHMM>.db``

    Args:
        src_path: Path to the source SQLite DB.
        dest_dir: Directory the backup file goes in. Created if
            missing.
        now: Override for the timestamp embedded in the filename
            (test seam). Defaults to ``datetime.now(UTC)``.

    Returns:
        Path to the newly-written backup file.

    Raises:
        FileNotFoundError: If ``src_path`` doesn't exist.
        StorageError: If the SQLite backup operation fails.
    """
    if not src_path.exists():
        raise FileNotFoundError(f"backup source does not exist: {src_path}")
    when = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = when.strftime("%Y%m%d-%H%M")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{src_path.stem}-{stamp}.db"

    # Open BOTH connections with explicit close in try/finally —
    # ``with sqlite3.connect(...)`` doesn't close (sqlite3 Connection's
    # __exit__ only handles commit/rollback per Python docs).
    src_conn = sqlite3.connect(str(src_path))
    try:
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    except sqlite3.Error as exc:
        # If the dest file got partially written, remove it so a
        # subsequent retry isn't blocked by a half-formed artifact.
        if dest_path.exists():
            try:
                dest_path.unlink()
            except OSError:
                pass
        raise StorageError(f"sqlite backup failed for {src_path} → {dest_path}: {exc}") from exc
    finally:
        src_conn.close()

    _LOGGER.info(
        "local backup complete",
        extra={"src": str(src_path), "dest": str(dest_path)},
    )
    return dest_path


def _list_backups_newest_first(dest_dir: Path, *, db_stem: str) -> list[Path]:
    """``<dest_dir>/<db_stem>-*.db`` files, newest mtime first.

    Shared by :func:`prune_old_backups` and :func:`find_latest_backup`
    so "which files belong to this DB, in what order" is defined in
    exactly one place — the naming convention + sort key is a subtle
    correctness rule (the two callers must agree on what "latest"
    means), not incidental duplication.
    """
    if not dest_dir.exists():
        return []
    return sorted(
        dest_dir.glob(f"{db_stem}-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


# --------------------------------------------------------------------- #
# Retention pruning                                                     #
# --------------------------------------------------------------------- #


def prune_old_backups(
    dest_dir: Path,
    *,
    db_stem: str,
    keep_n_daily: int,
) -> int:
    """Delete backups beyond the retention horizon.

    Lists ``<dest_dir>/<db_stem>-*.db`` files, sorts by mtime DESC,
    keeps the newest ``keep_n_daily``, deletes the rest. Returns the
    delete count.

    Args:
        dest_dir: Directory holding backup files.
        db_stem: Filename stem (e.g. ``"wobblebot-live"``). Used to
            scope the prune to backups of THIS db — operator's
            different DBs (live / shadow / operator / harvest /
            observe / news / advise) each get independent
            retention.
        keep_n_daily: Number of newest backups to keep. Files
            beyond this count are deleted.

    Returns:
        Count of files deleted. Zero if ``dest_dir`` doesn't exist
        or has fewer files than the limit.
    """
    if keep_n_daily < 0:
        raise ValueError(f"keep_n_daily must be non-negative; got {keep_n_daily}")
    candidates = _list_backups_newest_first(dest_dir, db_stem=db_stem)
    to_delete = candidates[keep_n_daily:]
    for path in to_delete:
        try:
            path.unlink()
        except OSError as exc:
            _LOGGER.warning(
                "failed to prune old backup; continuing",
                extra={"path": str(path), "error": str(exc)},
            )
    return len(to_delete)


def find_latest_backup(dest_dir: Path, *, db_stem: str) -> Path | None:
    """Return the most recent backup file for ``db_stem``, or ``None``.

    ``None`` covers both "the backup directory doesn't exist yet" and
    "no backups for this stem exist" — the caller (the verify cycle)
    treats both as "nothing to verify this cycle" rather than an error;
    a DB added to ``target_dbs`` after the last backup cycle just
    hasn't produced one yet.
    """
    candidates = _list_backups_newest_first(dest_dir, db_stem=db_stem)
    return candidates[0] if candidates else None


# --------------------------------------------------------------------- #
# Restoration smoke test (v1.1)                                         #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class BackupVerificationResult:
    """Outcome of a restoration smoke test against one backup file.

    Attributes:
        backup_path: The file that was checked.
        ok: True iff the integrity check passed AND every table was
            readable.
        integrity_check_result: Raw ``PRAGMA integrity_check`` output
            (``"ok"`` on success; SQLite's own diagnostic text on
            failure).
        table_count: Number of tables the representative SELECT ran
            against. 0 when the check failed before reaching that step.
        error: Human-readable failure reason, or ``None`` on success.
    """

    backup_path: Path
    ok: bool
    integrity_check_result: str
    table_count: int
    error: str | None = None


def verify_backup_restoration(backup_path: Path) -> BackupVerificationResult:
    """Restoration smoke test: open ``backup_path`` and prove it's usable.

    Two checks, in order:

    1. ``PRAGMA integrity_check`` — SQLite's own page-level structural
       check. Catches truncation, corrupted pages, broken indexes.
    2. A representative ``SELECT COUNT(*)`` against every user table
       (via ``sqlite_master``, so this works identically across every
       operator DB — live / shadow / operator / harvest / observe /
       news / advise — without hardcoding table names). Catches the
       narrower case ``integrity_check`` can miss: a structurally
       "ok" file where a specific table is nonetheless unreadable.

    Only ever opens the backup COPY — never the source/live DB, so a
    verification run can't contend with ``cli/live``'s own connection
    or risk the production file.

    Args:
        backup_path: Path to a backup file (as produced by
            :func:`backup_database_locally`).

    Returns:
        :class:`BackupVerificationResult`. Never raises — a failure to
        even open the file surfaces as ``ok=False`` with ``error`` set,
        so the caller can notify without its own try/except.
    """
    if not backup_path.exists():
        return BackupVerificationResult(
            backup_path=backup_path,
            ok=False,
            integrity_check_result="",
            table_count=0,
            error=f"backup file does not exist: {backup_path}",
        )
    try:
        conn = sqlite3.connect(str(backup_path))
    except sqlite3.Error as exc:
        return BackupVerificationResult(
            backup_path=backup_path,
            ok=False,
            integrity_check_result="",
            table_count=0,
            error=f"failed to open backup: {exc}",
        )
    try:
        integrity_result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity_result != "ok":
            return BackupVerificationResult(
                backup_path=backup_path,
                ok=False,
                integrity_check_result=integrity_result,
                table_count=0,
                error=f"PRAGMA integrity_check failed: {integrity_result}",
            )
        table_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for name in table_names:
            # Table names come from this file's own sqlite_master, not
            # caller/user input -- safe to interpolate as a quoted
            # identifier (SQLite params bind values, not identifiers).
            conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
        return BackupVerificationResult(
            backup_path=backup_path,
            ok=True,
            integrity_check_result=integrity_result,
            table_count=len(table_names),
        )
    except sqlite3.Error as exc:
        return BackupVerificationResult(
            backup_path=backup_path,
            ok=False,
            integrity_check_result="",
            table_count=0,
            error=str(exc),
        )
    finally:
        conn.close()


__all__ = (
    "BackupDestination",
    "BackupVerificationResult",
    "backup_database_locally",
    "find_latest_backup",
    "prune_old_backups",
    "verify_backup_restoration",
)

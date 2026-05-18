"""Backup service — local SQLite ``.backup`` API (Stage 8.2.C).

Uses SQLite's online `backup API <https://www.sqlite.org/backup.html>`_
to produce point-in-time atomic copies WITHOUT locking the source
database against concurrent reads. ``cli/live`` can keep ticking
through the backup window.

Three public surfaces:

- :func:`backup_database_locally` — copy `src_path` → `dest_dir`
  with timestamped filename. Returns the destination path.
- :func:`prune_old_backups` — delete oldest backups beyond
  retention horizon.
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
    if not dest_dir.exists():
        return 0
    candidates = sorted(
        dest_dir.glob(f"{db_stem}-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
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


__all__ = (
    "BackupDestination",
    "backup_database_locally",
    "prune_old_backups",
)

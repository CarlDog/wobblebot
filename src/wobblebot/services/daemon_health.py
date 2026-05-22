"""Daemon-freshness reader for the Stage 8.4.E health-icon work.

Approach B from the design discussion: derive each daemon's liveness
from the *most recent write* it makes to its primary table — no
engine-path changes (the rule during soak is "documentation freeze;
UX polish is fine; touching tick loops is not").

v1.0 coverage is the daemons whose primary writes are *frequent*
(every poll cycle):

* ``cli/observe`` → ``price_snapshots.observed_at`` (every 30s)
* ``cli/news``    → ``news_items.fetched_at`` (every 5 min default)
* ``cli/advise``  → ``advisor_suggestions.created_at`` (every advise cadence)

Daemons whose primary writes are *conditional* (cli/live without
fills, cli/harvest without proposals, cli/operator without
notifications, cli/maintenance with weekly VACUUM) are out of v1.0
scope — Approach B can't detect liveness honestly when the daemon's
job is to be quiet. The v1.1 heartbeat-table approach handles those.

Direct ``aiosqlite`` reads against the DB file path rather than
threading new methods through ``StoragePort``. Read-only
observability tooling; bypassing the port mirrors how
``kraken_health`` uses ``httpx`` directly rather than going through
``ExchangePort``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import aiosqlite

_OBSERVE_THRESHOLD_SECONDS: float = 120.0  # 2x the 30s default cadence + slack
_NEWS_THRESHOLD_SECONDS: float = 900.0  # 2x the 300s default + slack for slow feeds
_ADVISE_THRESHOLD_SECONDS: float = 3600.0  # 2x typical 30-min cadence + slack


class DaemonStatus(StrEnum):
    """Per-daemon freshness state.

    * ``fresh`` — the daemon's primary table has a row within its
      threshold; the daemon is presumed healthy.
    * ``stale`` — the most recent write exceeds the threshold; the
      daemon may be down or wedged.
    * ``unknown`` — the DB file is unwired (path is ``None``), or the
      table has never been written to, or the query failed. Surface
      to the operator without escalating severity — we don't have
      signal, but absence isn't proof of failure.
    """

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DaemonHealth:
    """One detected daemon's health record for the ``/health`` view.

    ``label`` is the operator-facing name (rendered in the page);
    ``name`` is the canonical identifier (used in CSS class hooks).
    ``threshold_seconds`` is included so the UI can show "stale 14m
    > 2m threshold" without the route having to recompute it.
    """

    name: str
    label: str
    status: DaemonStatus
    last_seen: datetime | None
    threshold_seconds: float
    detail: str | None = None  # populated on UNKNOWN to explain why


async def _latest_iso_timestamp(db_path: Path, table: str, column: str) -> str | None:
    """Run ``SELECT MAX(column) FROM table`` against ``db_path`` read-only.

    Returns the max ISO-8601 string (Kraken-style storage convention)
    or ``None`` if the table is empty / the column is null. Raises
    ``aiosqlite.Error`` / ``OSError`` on failure; the caller catches
    and maps to ``UNKNOWN``.

    Read-only ``mode=ro`` URI prevents any accidental write — this is
    observability tooling, not a writer.
    """
    uri = f"file:{db_path}?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as conn:
        async with conn.execute(f"SELECT MAX({column}) FROM {table}") as cursor:
            row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


async def _read_daemon(  # pylint: disable=too-many-arguments
    *,
    name: str,
    label: str,
    db_path: Path | None,
    table: str,
    column: str,
    threshold_seconds: float,
    now: datetime,
) -> DaemonHealth:
    """Read one daemon's most-recent-write and classify it.

    The four failure modes (unwired path, missing file, query error,
    empty table) all collapse to ``UNKNOWN`` with a one-line detail
    string — the UI shows the reason without making the operator
    tail logs.
    """
    if db_path is None:
        return DaemonHealth(
            name=name,
            label=label,
            status=DaemonStatus.UNKNOWN,
            last_seen=None,
            threshold_seconds=threshold_seconds,
            detail="db path not configured",
        )
    if not db_path.exists():
        return DaemonHealth(
            name=name,
            label=label,
            status=DaemonStatus.UNKNOWN,
            last_seen=None,
            threshold_seconds=threshold_seconds,
            detail=f"db file missing: {db_path.name}",
        )
    try:
        latest_iso = await _latest_iso_timestamp(db_path, table, column)
    except (aiosqlite.Error, OSError) as exc:
        return DaemonHealth(
            name=name,
            label=label,
            status=DaemonStatus.UNKNOWN,
            last_seen=None,
            threshold_seconds=threshold_seconds,
            detail=f"query failed: {exc}",
        )
    if latest_iso is None:
        return DaemonHealth(
            name=name,
            label=label,
            status=DaemonStatus.UNKNOWN,
            last_seen=None,
            threshold_seconds=threshold_seconds,
            detail="no rows yet",
        )
    try:
        last_seen = datetime.fromisoformat(latest_iso)
    except ValueError:
        return DaemonHealth(
            name=name,
            label=label,
            status=DaemonStatus.UNKNOWN,
            last_seen=None,
            threshold_seconds=threshold_seconds,
            detail=f"unparseable timestamp: {latest_iso!r}",
        )
    # Normalize to UTC so the threshold comparison is honest if the
    # stored timestamp happens to lack tzinfo (defensive — every CLI
    # writes UTC, but unparseable tz strings would slip past above).
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    age_seconds = (now - last_seen).total_seconds()
    status = DaemonStatus.FRESH if age_seconds <= threshold_seconds else DaemonStatus.STALE
    return DaemonHealth(
        name=name,
        label=label,
        status=status,
        last_seen=last_seen,
        threshold_seconds=threshold_seconds,
    )


async def fetch_daemon_freshness(
    *,
    observe_db: Path | None,
    news_db: Path | None,
    advise_db: Path | None,
    now: datetime | None = None,
) -> list[DaemonHealth]:
    """Read freshness for every v1.0-detectable daemon.

    Args:
        observe_db / news_db / advise_db: Paths to each daemon's
            primary DB. ``None`` is acceptable — that daemon's
            entry comes back ``UNKNOWN`` with a ``detail`` of
            ``"db path not configured"``.
        now: Optional override for the wallclock — test seam. Production
            callers pass nothing; ``datetime.now(UTC)`` is used.

    Returns:
        One :class:`DaemonHealth` per detectable daemon, in display
        order (observe → news → advise). Order matters for the
        ``/health`` template; the roll-up severity calculator
        consumes the same list.
    """
    current = now or datetime.now(UTC)
    return [
        await _read_daemon(
            name="cli/observe",
            label="Price observer",
            db_path=observe_db,
            table="price_snapshots",
            column="observed_at",
            threshold_seconds=_OBSERVE_THRESHOLD_SECONDS,
            now=current,
        ),
        await _read_daemon(
            name="cli/news",
            label="News collector",
            db_path=news_db,
            table="news_items",
            column="fetched_at",
            threshold_seconds=_NEWS_THRESHOLD_SECONDS,
            now=current,
        ),
        await _read_daemon(
            name="cli/advise",
            label="Trading advisor",
            db_path=advise_db,
            table="advisor_suggestions",
            column="created_at",
            threshold_seconds=_ADVISE_THRESHOLD_SECONDS,
            now=current,
        ),
    ]


__all__ = (
    "DaemonStatus",
    "DaemonHealth",
    "fetch_daemon_freshness",
)

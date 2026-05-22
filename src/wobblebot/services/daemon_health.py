"""Daemon-freshness reader for the Stage 8.4.E health-icon work.

Two detection strategies live side-by-side:

1. **Approach B — derive from primary writes.** For daemons whose
   primary table sees frequent writes, we read the latest row
   timestamp and compare against a threshold:

   * ``cli/observe`` → ``price_snapshots.observed_at``
   * ``cli/news``    → ``news_items.fetched_at``
   * ``cli/advise``  → ``advisor_suggestions.created_at``

2. **Heartbeat table.** Daemons whose primary writes are conditional
   (cli/live without fills, cli/harvest without proposals, etc.)
   upsert a row in ``operator.db``'s ``daemon_heartbeats`` table at
   the top of each tick loop. We read those rows directly:

   * ``cli/live``       — heartbeat from the main tick loop
   * ``cli/harvest``    — heartbeat from each poll cycle
   * ``cli/operator``   — heartbeat from the forwarder loop
   * ``cli/maintenance``— heartbeat from each of the three scheduled tasks

Direct ``aiosqlite`` reads against the DB file paths rather than
threading new methods through ``StoragePort``. Read-only
observability tooling; bypassing the port mirrors how
``kraken_health`` uses ``httpx`` directly rather than going through
``ExchangePort``.

Per-daemon thresholds derive from configured cadences via
:func:`derive_thresholds_from_config` — operators who tune any
interval get a proportionally adjusted health threshold for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import aiosqlite

from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.schedules import SchedulesConfig

# Slack added on top of 2 * configured cadence — covers normal jitter
# (network blip, LLM call took an extra minute) without false-yellow.
_DEFAULT_SLACK_SECONDS: float = 300.0  # 5 min

# Defaults match the settings.example.yml values; operator-tunable
# via :func:`derive_thresholds_from_config`. Live here as a single
# source of truth so an operator running with a partial config still
# gets sensible defaults.
_OBSERVE_DEFAULT_CADENCE = timedelta(seconds=30)
_NEWS_DEFAULT_CADENCE = timedelta(minutes=30)
_ADVISE_DEFAULT_CADENCE = timedelta(hours=4)
_LIVE_DEFAULT_CADENCE = timedelta(seconds=5)  # live.tick_seconds
_HARVEST_DEFAULT_CADENCE = timedelta(hours=1)  # schedules.harvest
_OPERATOR_DEFAULT_CADENCE = timedelta(seconds=2)  # operator.forwarder_poll_seconds
# cli/maintenance has three concurrent scheduled tasks; threshold uses
# the SHORTEST default (so any of the three keeping the heartbeat fresh
# is enough).
_MAINTENANCE_DEFAULT_CADENCE = timedelta(days=1)


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
class DaemonHealthThresholds:
    """Per-daemon staleness thresholds in seconds.

    A daemon's heartbeat/primary write older than ``<daemon>_seconds``
    flips its status to STALE. Construct via
    :func:`derive_thresholds_from_config` to derive from operator
    config; the dataclass defaults match ``settings.example.yml`` so
    unit tests + partial configs still get sensible behavior.

    Stage 8.4.E follow-up 2026-05-22 adds live/harvest/operator/
    maintenance to the existing observe/news/advise. The four new
    daemons emit explicit heartbeats (since their primary writes are
    conditional); the original three still derive freshness from
    their writes.
    """

    observe_seconds: float = 2 * 30 + _DEFAULT_SLACK_SECONDS
    news_seconds: float = 2 * 30 * 60 + _DEFAULT_SLACK_SECONDS
    advise_seconds: float = 2 * 4 * 3600 + _DEFAULT_SLACK_SECONDS
    live_seconds: float = 2 * 5 + _DEFAULT_SLACK_SECONDS
    harvest_seconds: float = 2 * 3600 + _DEFAULT_SLACK_SECONDS
    operator_seconds: float = 2 * 2 + _DEFAULT_SLACK_SECONDS
    maintenance_seconds: float = 2 * 86400 + _DEFAULT_SLACK_SECONDS


def _maintenance_min_cadence(schedules: SchedulesConfig) -> timedelta:
    """Pick the shortest configured maintenance task cadence.

    cli/maintenance runs three concurrent scheduled tasks; the
    daemon's heartbeat fires from each of them, so the threshold is
    keyed off whichever runs most frequently. An operator who tunes
    ``schedules.maintenance_prune: 6h`` gets the threshold
    proportionally tightened automatically.
    """
    return min(
        schedules.get_or_default("maintenance_vacuum", timedelta(days=7)),
        schedules.get_or_default("maintenance_prune", _MAINTENANCE_DEFAULT_CADENCE),
        schedules.get_or_default("maintenance_backup", _MAINTENANCE_DEFAULT_CADENCE),
    )


def derive_thresholds_from_config(
    config: WobbleBotConfig,
    *,
    slack_seconds: float = _DEFAULT_SLACK_SECONDS,
) -> DaemonHealthThresholds:
    """Build thresholds from the operator-configured cadences.

    Each daemon's threshold = 2 * its configured cadence + slack.
    Operators who tune any interval get a proportionally tightened or
    relaxed health threshold without code changes — the v1.0
    magic-numbers anti-pattern (cli/advise misclassified as STALE 75%
    of its cycle because hardcoded thresholds assumed a 30-min
    cadence vs the configured 4h) is fixed by reading the actual
    config every time.

    Per-daemon sources:

    * ``cli/observe`` ← ``schedules.observe_prices``
    * ``cli/news``    ← ``schedules.news``
    * ``cli/advise``  ← ``schedules.advise``
    * ``cli/live``    ← ``live.tick_seconds``
    * ``cli/harvest`` ← ``schedules.harvest``
    * ``cli/operator``← ``operator.forwarder_poll_seconds``
    * ``cli/maintenance`` ← ``min(schedules.maintenance_vacuum,
      schedules.maintenance_prune, schedules.maintenance_backup)``

    Defaults match ``settings.example.yml`` so a missing key still
    produces a sensible threshold rather than raising.
    """
    schedules = config.schedules
    live_cadence_seconds = (
        float(config.live.tick_seconds)
        if config.live is not None
        else _LIVE_DEFAULT_CADENCE.total_seconds()
    )
    operator_cadence_seconds = (
        float(config.operator.forwarder_poll_seconds)
        if config.operator is not None
        else _OPERATOR_DEFAULT_CADENCE.total_seconds()
    )
    return DaemonHealthThresholds(
        observe_seconds=(
            schedules.get_or_default("observe_prices", _OBSERVE_DEFAULT_CADENCE).total_seconds() * 2
            + slack_seconds
        ),
        news_seconds=(
            schedules.get_or_default("news", _NEWS_DEFAULT_CADENCE).total_seconds() * 2
            + slack_seconds
        ),
        advise_seconds=(
            schedules.get_or_default("advise", _ADVISE_DEFAULT_CADENCE).total_seconds() * 2
            + slack_seconds
        ),
        live_seconds=live_cadence_seconds * 2 + slack_seconds,
        harvest_seconds=(
            schedules.get_or_default("harvest", _HARVEST_DEFAULT_CADENCE).total_seconds() * 2
            + slack_seconds
        ),
        operator_seconds=operator_cadence_seconds * 2 + slack_seconds,
        maintenance_seconds=_maintenance_min_cadence(schedules).total_seconds() * 2 + slack_seconds,
    )


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


async def _heartbeats_or_empty(operator_db: Path | None) -> dict[str, datetime] | None:
    """Read every row of ``daemon_heartbeats`` from operator.db, read-only.

    Returns:
        ``None`` if the path is unwired or the file doesn't exist (each
        heartbeat-derived daemon row will surface UNKNOWN). An empty
        dict if the table exists but is empty. A populated dict on a
        successful read. The query is wrapped in try/except — any
        aiosqlite error returns None so the health page degrades
        gracefully rather than 500-ing.
    """
    if operator_db is None or not operator_db.exists():
        return None
    uri = f"file:{operator_db}?mode=ro"
    out: dict[str, datetime] = {}
    try:
        async with aiosqlite.connect(uri, uri=True) as conn:
            async with conn.execute("SELECT name, last_beat_at FROM daemon_heartbeats") as cursor:
                rows = await cursor.fetchall()
    except (aiosqlite.Error, OSError):
        return None
    for name, iso_ts in rows:
        try:
            parsed = datetime.fromisoformat(str(iso_ts))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        out[str(name)] = parsed
    return out


def _classify_heartbeat(
    *,
    name: str,
    label: str,
    heartbeats: dict[str, datetime] | None,
    threshold_seconds: float,
    now: datetime,
) -> DaemonHealth:
    """Build a DaemonHealth row from the daemon_heartbeats map.

    The map is None when the operator.db isn't wired or readable; the
    daemon shows UNKNOWN with the appropriate detail.
    """
    if heartbeats is None:
        return DaemonHealth(
            name=name,
            label=label,
            status=DaemonStatus.UNKNOWN,
            last_seen=None,
            threshold_seconds=threshold_seconds,
            detail="operator.db unwired or unreachable",
        )
    last_seen = heartbeats.get(name)
    if last_seen is None:
        return DaemonHealth(
            name=name,
            label=label,
            status=DaemonStatus.UNKNOWN,
            last_seen=None,
            threshold_seconds=threshold_seconds,
            detail="no heartbeat yet",
        )
    age_seconds = (now - last_seen).total_seconds()
    status = DaemonStatus.FRESH if age_seconds <= threshold_seconds else DaemonStatus.STALE
    return DaemonHealth(
        name=name,
        label=label,
        status=status,
        last_seen=last_seen,
        threshold_seconds=threshold_seconds,
    )


async def fetch_daemon_freshness(  # pylint: disable=too-many-arguments
    *,
    observe_db: Path | None,
    news_db: Path | None,
    advise_db: Path | None,
    operator_db: Path | None = None,
    thresholds: DaemonHealthThresholds | None = None,
    now: datetime | None = None,
) -> list[DaemonHealth]:
    """Read freshness for every detectable daemon.

    The three Approach-B daemons (observe / news / advise) derive
    freshness from their primary write tables. The four
    heartbeat-based daemons (live / harvest / operator / maintenance)
    derive freshness from rows in operator.db's ``daemon_heartbeats``
    table — each emits an upsert at the top of its tick loop so the
    classifier has a reliable "the loop ran recently" signal even
    when the daemon would otherwise have nothing to write.

    Args:
        observe_db / news_db / advise_db: Paths to the Approach-B
            DBs. ``None`` → UNKNOWN.
        operator_db: Path to operator.db where heartbeats live.
            ``None`` → the four heartbeat-based daemons show UNKNOWN.
        thresholds: Per-daemon staleness thresholds. ``None`` falls
            back to ``settings.example.yml`` defaults; production
            callers should derive via
            :func:`derive_thresholds_from_config`.
        now: Optional wallclock override (test seam).

    Returns:
        One :class:`DaemonHealth` per daemon in display order:
        observe, news, advise (Approach B), then live, harvest,
        operator, maintenance (heartbeat-based).
    """
    current = now or datetime.now(UTC)
    t = thresholds or DaemonHealthThresholds()
    heartbeats = await _heartbeats_or_empty(operator_db)
    return [
        await _read_daemon(
            name="cli/observe",
            label="Price Observer",
            db_path=observe_db,
            table="price_snapshots",
            column="observed_at",
            threshold_seconds=t.observe_seconds,
            now=current,
        ),
        await _read_daemon(
            name="cli/news",
            label="News Collector",
            db_path=news_db,
            table="news_items",
            column="fetched_at",
            threshold_seconds=t.news_seconds,
            now=current,
        ),
        await _read_daemon(
            name="cli/advise",
            label="Trading Advisor",
            db_path=advise_db,
            table="advisor_suggestions",
            column="created_at",
            threshold_seconds=t.advise_seconds,
            now=current,
        ),
        _classify_heartbeat(
            name="cli/live",
            label="Live Trader",
            heartbeats=heartbeats,
            threshold_seconds=t.live_seconds,
            now=current,
        ),
        _classify_heartbeat(
            name="cli/harvest",
            label="Treasury Harvester",
            heartbeats=heartbeats,
            threshold_seconds=t.harvest_seconds,
            now=current,
        ),
        _classify_heartbeat(
            name="cli/operator",
            label="Operator Interaction",
            heartbeats=heartbeats,
            threshold_seconds=t.operator_seconds,
            now=current,
        ),
        _classify_heartbeat(
            name="cli/maintenance",
            label="Maintenance Worker",
            heartbeats=heartbeats,
            threshold_seconds=t.maintenance_seconds,
            now=current,
        ),
    ]


__all__ = (
    "DaemonStatus",
    "DaemonHealth",
    "DaemonHealthThresholds",
    "derive_thresholds_from_config",
    "fetch_daemon_freshness",
)

"""Backfill mode for ``cli/observe`` — the ``--backfill`` one-shot path.

Private companion module of ``cli/observe`` (its only consumer): the
argparse value parsers, per-symbol cursor resolvers (``--catchup`` /
``--resume``), the backfill orchestration, and result/progress log
rendering. Split out 2026-08-08 when observe.py crossed the 1000-line
pylint gate — the daemon poll loop and the one-shot backfill mode are
separate concerns sharing only ``main()``'s dispatch.

Layer privacy follows the ``cli/_common.py`` convention: module-level
privacy is a documented fact (only ``cli/observe`` and tests import
this), while the two symbols observe actually consumes —
``backfill_main`` and ``parse_rate_limit_arg`` — are public names, so
no cross-module underscore imports are needed. Everything else here
is genuinely module-private.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime, timedelta

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    parse_date_arg,
    partition_or_exit,
    safe_shutdown,
)
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.backfill import (
    DEFAULT_RATE_LIMIT_SECONDS,
    BackfillResult,
    ProgressCallback,
    backfill_range,
)

_LOGGER = logging.getLogger("wobblebot.cli.observe_backfill")


def parse_rate_limit_arg(raw: str) -> float:
    """Parse ``--rate-limit-seconds`` — a non-negative float."""
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --rate-limit-seconds {raw!r}; use a non-negative number"
        ) from exc
    if seconds < 0:
        raise argparse.ArgumentTypeError(f"--rate-limit-seconds must be >= 0, got {seconds}")
    return seconds


# One progress line per this many Kraken requests (~1 request/second at
# the default rate limit, so roughly one line every 10s on a long
# backfill). Chosen to keep the bulk-seed scenario (~2200 requests)
# under ~220 log lines rather than one per chunk.
_PROGRESS_LOG_EVERY_REQUESTS = 10


def _make_progress_logger(symbol: Symbol) -> ProgressCallback:
    """Build a per-chunk progress callback that logs every Nth request.

    Wires ``backfill_range``'s existing ``progress_callback`` seam to
    the operator log so a long backfill isn't a silent terminal (the
    ~37-minute bulk-seed scenario in adaptive-grid.md L205).
    """

    async def _log_progress(partial: BackfillResult) -> None:
        if partial.requests_made % _PROGRESS_LOG_EVERY_REQUESTS != 0:
            return
        cursor = partial.last_opened_at.isoformat() if partial.last_opened_at is not None else "n/a"
        _LOGGER.info(
            "backfill %s: %d bars so far, cursor at %s, %.1fs elapsed",
            symbol,
            partial.bars_fetched,
            cursor,
            partial.elapsed_seconds,
            extra={
                "symbol": str(symbol),
                "bars_fetched": partial.bars_fetched,
                "bars_inserted": partial.bars_inserted,
                "requests_made": partial.requests_made,
                "cursor": cursor if cursor != "n/a" else None,
                "elapsed_seconds": round(partial.elapsed_seconds, 1),
            },
        )

    return _log_progress


def _cursor_to_since(
    latest: datetime | None,
    symbol: Symbol,
    *,
    until: datetime,
    mode: str,
    missing: str,
) -> datetime | None:
    """Translate a stored cursor into a backfill lower bound, or ``None`` to skip.

    Shared by ``--catchup`` and ``--resume``: no stored cursor means the
    symbol needs an explicit seed (WARN), a cursor at/past ``until``
    means nothing to fetch (INFO). Both skip reasons are logged here.
    """
    if latest is None:
        _LOGGER.warning(
            "%s: no %s for %s; seed it with --since or --days",
            mode,
            missing,
            symbol,
            extra={"symbol": str(symbol)},
        )
        return None
    if latest >= until:
        _LOGGER.info(
            "%s: %s already current (latest %s)",
            mode,
            symbol,
            latest.isoformat(),
            extra={"symbol": str(symbol), "latest": latest.isoformat()},
        )
        return None
    return latest


async def _resolve_catchup_since(
    storage: SQLiteStorageAdapter,
    symbol: Symbol,
    *,
    until: datetime,
) -> datetime | None:
    """Resolve one symbol's ``--catchup`` lower bound from stored history.

    Reads the latest ``price_snapshots.observed_at`` — the same cursor
    the startup auto-gap-fill uses. Storage failures propagate as
    ``WobbleBotPortError`` for the caller to handle.
    """
    latest = await storage.get_latest_observed_at(symbol)
    return _cursor_to_since(latest, symbol, until=until, mode="catchup", missing="prior history")


async def _resolve_resume_since(
    storage: SQLiteStorageAdapter,
    symbol: Symbol,
    interval_minutes: int,
    *,
    until: datetime,
) -> datetime | None:
    """Resolve one symbol's ``--resume`` lower bound from ``ohlc_bars``.

    Reads the latest ``opened_at`` at the requested interval — the
    honest "continue where the backfill left off" cursor, deliberately
    NOT ``price_snapshots`` (which daemon polls also feed, overstating
    backfill progress). Storage failures propagate as
    ``WobbleBotPortError`` for the caller to handle.
    """
    latest = await storage.get_latest_ohlc_opened_at(symbol, interval_minutes)
    return _cursor_to_since(
        latest,
        symbol,
        until=until,
        mode="resume",
        missing=f"ohlc bars at {interval_minutes}m",
    )


async def _backfill_one(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    symbol: Symbol,
    interval: int,
    *,
    since: datetime | None,
    until: datetime,
    resume: bool,
    rate_limit_seconds: float,
) -> bool:
    """Run one (symbol, interval) backfill; returns True when it errored.

    ``since=None`` means per-symbol resolution: ``--resume`` reads the
    interval-scoped ``ohlc_bars`` cursor, ``--catchup`` the latest
    stored observation. A resolver skip (nothing to fetch) is not an
    error; a cursor-read failure is.
    """
    symbol_since = since
    if symbol_since is None:
        mode = "resume" if resume else "catchup"
        try:
            if resume:
                symbol_since = await _resolve_resume_since(storage, symbol, interval, until=until)
            else:
                symbol_since = await _resolve_catchup_since(storage, symbol, until=until)
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "%s: failed reading cursor for %s: %s",
                mode,
                symbol,
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            return True
        if symbol_since is None:
            return False  # skip reason already logged by the resolver
    result = await backfill_range(
        adapter,
        storage,
        symbol=symbol,
        since=symbol_since,
        until=until,
        interval_minutes=interval,
        rate_limit_seconds=rate_limit_seconds,
        progress_callback=_make_progress_logger(symbol),
    )
    _log_backfill_result(symbol, result)
    return result.error is not None


async def backfill_main(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-return-statements,too-many-arguments
    config: WobbleBotConfig,
    *,
    since_raw: str | None,
    until_raw: str | None,
    interval_minutes: int,
    symbols_override: list[Symbol] | None,
    days: int | None = None,
    catchup: bool = False,
    resume: bool = False,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    intervals: list[int] | None = None,
) -> int:
    """One-shot backfill mode for ``cli/observe --backfill``.

    Walks each configured symbol through ``services.backfill.backfill_range``
    against Kraken's OHLC endpoint, writes ohlc_bars + price_snapshots,
    prints a per-symbol summary, exits. ``intervals`` (from
    ``--intervals``) overrides the single ``interval_minutes``; stats are
    reported per (symbol, interval).

    Returns 0 on full success; 1 if any symbol's backfill terminated
    on an error; 2 on argument / config / credential failure.
    """
    if config.observe is None:
        _LOGGER.error("settings.yml is missing the `observe:` section")
        return 2

    try:
        since: datetime | None
        if catchup or resume:
            since = None  # resolved per symbol from stored history below
        elif days is not None:
            since = datetime.now(UTC) - timedelta(days=days)
        elif since_raw is not None:
            since = parse_date_arg(since_raw)
        else:
            _LOGGER.error(
                "--backfill requires --since (e.g. --since 2026-04-01), "
                "--days (e.g. --days 30), --catchup, or --resume"
            )
            return 2
        until = parse_date_arg(until_raw) if until_raw is not None else datetime.now(UTC)
    except ValueError as exc:
        _LOGGER.error("invalid date argument", extra={"error": str(exc)})
        return 2

    if since is not None and since >= until:
        _LOGGER.error(
            "--since must be strictly before --until",
            extra={"since": since.isoformat(), "until": until.isoformat()},
        )
        return 2

    try:
        kraken_config = KrakenConfig.from_env()
    except ValueError as exc:
        _LOGGER.error("missing read-only credentials", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(config.observe.db)
    await storage.connect()
    adapter = KrakenAdapter(config=kraken_config)
    symbols = symbols_override if symbols_override is not None else list(config.observe.symbols)

    try:
        # Reuse the daemon-mode partition logic so an unknown symbol
        # logs a warning + still attempts the rest. The per-symbol
        # error path in backfill_range absorbs the eventual Kraken
        # "Unknown asset pair" error.
        exit_code = await partition_or_exit(
            adapter,
            symbols,
            logger=_LOGGER,
            cleanups=[
                ("close_kraken_adapter", adapter.aclose),
                ("close_observe_storage", storage.close),
            ],
        )
        if exit_code is not None:
            return exit_code

        effective_intervals = intervals if intervals else [interval_minutes]
        intervals_label = ",".join(f"{m}m" for m in effective_intervals)
        if since is not None:
            since_label = since.isoformat()
        else:
            since_label = "auto (per-symbol resume)" if resume else "auto (per-symbol catchup)"
        _LOGGER.info(
            "backfill starting: %d symbol(s), %s interval(s), %s -> %s",
            len(symbols),
            intervals_label,
            since_label,
            until.isoformat(),
            extra={
                "symbols": [str(s) for s in symbols],
                "since": since_label,
                "until": until.isoformat(),
                "intervals_minutes": effective_intervals,
            },
        )

        any_error = False
        for symbol in symbols:
            for interval in effective_intervals:
                errored = await _backfill_one(
                    adapter,
                    storage,
                    symbol,
                    interval,
                    since=since,
                    until=until,
                    resume=resume,
                    rate_limit_seconds=rate_limit_seconds,
                )
                any_error = any_error or errored

        _LOGGER.info(
            "backfill done: %d symbol(s), succeeded=%s",
            len(symbols),
            not any_error,
            extra={
                "symbols": [str(s) for s in symbols],
                "succeeded": not any_error,
            },
        )
        return 1 if any_error else 0
    finally:
        await safe_shutdown(
            [
                ("close_kraken_adapter", adapter.aclose),
                ("close_observe_storage", storage.close),
            ],
            logger=_LOGGER,
        )


# Horizon-truncation WARN (P2 slice 1, item 7): a naive deep --since
# "succeeds" with only whatever history Kraken retains — silently. The
# expected-bar math is approximate (bar boundaries, thin markets), so
# only materially-short results warn: under half the implied bars, on
# windows big enough (>= 100 expected) that the shortfall can't be
# boundary noise.
_HORIZON_WARN_MIN_EXPECTED_BARS = 100
_HORIZON_WARN_FRACTION = 0.5


def _warn_if_horizon_truncated(result: BackfillResult) -> None:
    """WARN when a clean backfill returned far fewer bars than its window implies."""
    window_minutes = (result.requested_until - result.requested_since).total_seconds() / 60.0
    expected = window_minutes / result.interval_minutes
    if expected < _HORIZON_WARN_MIN_EXPECTED_BARS:
        return
    if result.bars_fetched >= expected * _HORIZON_WARN_FRACTION:
        return
    _LOGGER.warning(
        "backfill %s @ %dm returned %d bars but the window implies ~%d -- "
        "Kraken's retained history may not reach back to %s",
        result.symbol,
        result.interval_minutes,
        result.bars_fetched,
        int(expected),
        result.requested_since.isoformat(),
        extra={
            "symbol": str(result.symbol),
            "interval_minutes": result.interval_minutes,
            "bars_fetched": result.bars_fetched,
            "expected_bars": int(expected),
            "requested_since": result.requested_since.isoformat(),
        },
    )


def _log_backfill_result(symbol: Symbol, result: BackfillResult) -> None:
    """Render one symbol's backfill outcome to the log.

    On error includes the resume cursor so the operator can re-run
    with ``--since <last_opened_at>`` to pick up where it left off.
    """
    if result.error is not None:
        resume_at = (
            result.last_opened_at.isoformat() if result.last_opened_at is not None else "none"
        )
        _LOGGER.error(
            "backfill %s @ %dm failed after %d bar(s) in %.1fs: %s; resume with --since %s",
            symbol,
            result.interval_minutes,
            result.bars_inserted,
            result.elapsed_seconds,
            result.error,
            resume_at,
            extra={
                "symbol": str(symbol),
                "interval_minutes": result.interval_minutes,
                "error": result.error,
                "resume_at": resume_at if resume_at != "none" else None,
                "bars_inserted_before_failure": result.bars_inserted,
                "elapsed_seconds": round(result.elapsed_seconds, 1),
            },
        )
    else:
        _LOGGER.info(
            "backfill %s @ %dm complete: %d bars inserted, %d snapshots, %d Kraken req, %.1fs",
            symbol,
            result.interval_minutes,
            result.bars_inserted,
            result.snapshots_inserted,
            result.requests_made,
            result.elapsed_seconds,
            extra={
                "symbol": str(symbol),
                "interval_minutes": result.interval_minutes,
                "bars_fetched": result.bars_fetched,
                "bars_inserted": result.bars_inserted,
                "snapshots_inserted": result.snapshots_inserted,
                "requests_made": result.requests_made,
                "elapsed_seconds": round(result.elapsed_seconds, 1),
            },
        )
        _warn_if_horizon_truncated(result)

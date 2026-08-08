"""Observer CLI — collect live market data without trading.

Run as a module::

    python -m wobblebot.cli.observe
    python -m wobblebot.cli.observe --profile conservative
    python -m wobblebot.cli.observe --symbols BTC/USD,ETH/USD

**Read-only.** Uses ``KRAKEN_READER_API_KEY`` (not the trade key). Polls
public Ticker per symbol on the price interval and persists each
observation. Optionally polls private BalanceEx on a slower cadence.
Per ADR-008, this is the data-collection half of Stage 3.0.

Polling cadences live in the top-level ``schedules:`` block of
``settings.yml``: ``schedules.observe_prices`` and
``schedules.observe_balances`` (use ``0s`` to disable the balance
poll). Stage 3.3 Slice C.0 moved all interval fields out of per-CLI
sections into the unified schedules block.

Configuration layering (per ADR-009):
1. Base config — ``config/settings.yml`` (or ``--config`` /
   ``settings.example.yml`` fallback).
2. Profile overrides — ``--profile name``.
3. CLI flag overrides — explicit flags below.

On shutdown: log session totals (price polls, balance polls, duration)
and exit 0. No cleanup needed — read-only operations leave nothing
to undo.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    collect_overrides,
    identity,
    install_signal_handlers,
    load_operator_env,
    parse_symbol_csv,
    partition_or_exit,
    run_poll_loop,
    run_with_clean_exit,
    safe_shutdown,
)
from wobblebot.config.cli import ObserveConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import OHLCBar, Symbol, Timestamp
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.backfill import (
    DEFAULT_RATE_LIMIT_SECONDS,
    BackfillResult,
    ProgressCallback,
    backfill_range,
)

_LOGGER = logging.getLogger("wobblebot.cli.observe")


async def _poll_prices(
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    symbols: list[Symbol],
) -> int:
    """Persist a price snapshot per symbol. Returns count successfully saved."""
    persisted = 0
    for symbol in symbols:
        try:
            price = await adapter.get_current_price(symbol)
            now = Timestamp(dt=datetime.now(UTC))
            await storage.save_price_snapshot(symbol, price, now)
            _LOGGER.debug(
                "price snapshot saved",
                extra={
                    "symbol": str(symbol),
                    "price": str(price.amount),
                    "currency": price.currency,
                    "observed_at": now.dt.isoformat(),
                },
            )
            persisted += 1
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "price poll failed",
                extra={"symbol": str(symbol), "error": str(exc), "error_type": type(exc).__name__},
            )
    return persisted


async def _poll_balances(adapter: KrakenAdapter, storage: SQLiteStorageAdapter) -> int:
    """Persist a balance snapshot. Returns count of entries (or 0 on error)."""
    try:
        balances = await adapter.get_balances()
        if not balances:
            _LOGGER.debug("balance poll: account empty; skipping snapshot")
            return 0
        await storage.save_balance_snapshot(balances)
        _LOGGER.debug("balance snapshot saved", extra={"entries": len(balances)})
        return len(balances)
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "balance poll failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 0


async def _run_loop(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    observe: ObserveConfig,
    price_interval: timedelta,
    balance_interval: timedelta,
    stop_event: asyncio.Event,
) -> int:
    started_at = time.monotonic()
    last_balance_poll = 0.0
    price_polls = 0
    balance_polls = 0
    price_interval_seconds = price_interval.total_seconds()
    balance_interval_seconds = balance_interval.total_seconds()
    _LOGGER.info(
        "observe session start",
        extra={
            "symbols": [str(s) for s in observe.symbols],
            "price_interval_seconds": price_interval_seconds,
            "balance_interval_seconds": balance_interval_seconds,
            "db_path": observe.db,
        },
    )

    async def _one_cycle() -> None:
        nonlocal price_polls, balance_polls, last_balance_poll
        persisted = await _poll_prices(adapter, storage, list(observe.symbols))
        price_polls += persisted

        if balance_interval_seconds > 0:
            elapsed_since_balance = time.monotonic() - last_balance_poll
            if elapsed_since_balance >= balance_interval_seconds:
                persisted_b = await _poll_balances(adapter, storage)
                if persisted_b > 0:
                    balance_polls += 1
                last_balance_poll = time.monotonic()

    try:
        await run_poll_loop(
            _one_cycle,
            interval_seconds=price_interval_seconds,
            stop_event=stop_event,
        )
    finally:
        _LOGGER.info(
            "observe session end",
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "price_snapshots_saved": price_polls,
                "balance_snapshots_saved": balance_polls,
            },
        )
    return 0


def _parse_date_arg(raw: str) -> datetime:
    """Parse an ISO 8601 date or datetime; default tz to UTC if naive.

    Accepts bare dates (``2026-04-01``), full ISO 8601 with ``Z`` or
    ``+HH:MM`` offsets. Bare dates become midnight UTC.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_days_arg(raw: str) -> int:
    """Parse ``--days N`` — a positive integer count of days back from now."""
    try:
        days = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --days {raw!r}; use a positive integer day count"
        ) from exc
    if days <= 0:
        raise argparse.ArgumentTypeError(f"--days must be positive, got {days}")
    return days


def _parse_rate_limit_arg(raw: str) -> float:
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


_INTERVAL_SUFFIX_MINUTES: dict[str, int] = {
    "m": 1,
    "h": 60,
    "d": 1440,
    "w": 10080,
}


def _parse_interval_arg(raw: str) -> int:
    """Parse ``1m`` / ``5m`` / ``1h`` / ``4h`` / ``1d`` / ``1w`` or bare minutes.

    Returns the canonical minute count. Validates against
    ``OHLCBar.ALLOWED_INTERVALS`` (Kraken's published set) so an
    operator can't pass an interval Kraken won't honor.
    """
    text = raw.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("interval cannot be empty")
    suffix = text[-1]
    if suffix in _INTERVAL_SUFFIX_MINUTES and text[:-1].isdigit():
        minutes = int(text[:-1]) * _INTERVAL_SUFFIX_MINUTES[suffix]
    else:
        try:
            minutes = int(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"invalid interval {raw!r}; use 1m/5m/15m/30m/1h/4h/1d/1w "
                f"or a bare minute count"
            ) from exc
    if minutes not in OHLCBar.ALLOWED_INTERVALS:
        raise argparse.ArgumentTypeError(
            f"interval {minutes}m not in Kraken's allowed set "
            f"{sorted(OHLCBar.ALLOWED_INTERVALS)}"
        )
    return minutes


def _parse_intervals_arg(raw: str) -> list[int]:
    """Parse ``--intervals 1m,1h`` — a comma list of ``_parse_interval_arg`` values.

    Deduplicates while preserving operator order (the fetch order).
    """
    parts = [piece for piece in (p.strip() for p in raw.split(",")) if piece]
    if not parts:
        raise argparse.ArgumentTypeError("--intervals cannot be empty")
    minutes: list[int] = []
    for part in parts:
        value = _parse_interval_arg(part)
        if value not in minutes:
            minutes.append(value)
    return minutes


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


async def _backfill_main(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-return-statements
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
            since = _parse_date_arg(since_raw)
        else:
            _LOGGER.error(
                "--backfill requires --since (e.g. --since 2026-04-01), "
                "--days (e.g. --days 30), --catchup, or --resume"
            )
            return 2
        until = _parse_date_arg(until_raw) if until_raw is not None else datetime.now(UTC)
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
                symbol_since = since
                if symbol_since is None:
                    mode = "resume" if resume else "catchup"
                    try:
                        if resume:
                            symbol_since = await _resolve_resume_since(
                                storage, symbol, interval, until=until
                            )
                        else:
                            symbol_since = await _resolve_catchup_since(
                                storage, symbol, until=until
                            )
                    except WobbleBotPortError as exc:
                        _LOGGER.warning(
                            "%s: failed reading cursor for %s: %s",
                            mode,
                            symbol,
                            exc,
                            extra={"symbol": str(symbol), "error": str(exc)},
                        )
                        any_error = True
                        continue
                    if symbol_since is None:
                        continue  # skip reason already logged by the resolver
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
                if result.error is not None:
                    any_error = True

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


async def _run_auto_gap_fill(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    symbols: list[Symbol],
    *,
    threshold_minutes: float,
    max_hours: float,
    now: datetime | None = None,
) -> None:
    """On daemon startup, per-symbol fill the gap from latest snapshot to now.

    Decisions per symbol:
        no prior history    -> skip silently (DEBUG log)
        gap < threshold     -> skip silently (DEBUG log)
        gap > max           -> WARN; operator must run --backfill manually
        threshold..max      -> run backfill_range at 1m granularity

    All failures are absorbed (per-symbol exceptions don't block startup,
    Kraken-unreachable doesn't prevent the daemon from entering the poll
    loop). Side-effect-only: nothing returned.

    The ``now`` kwarg is the test seam — production callers pass
    ``None`` and the function reads the current wallclock.
    """
    if not symbols:
        return
    resolved_now = now if now is not None else datetime.now(UTC)
    threshold = timedelta(minutes=threshold_minutes)
    max_window = timedelta(hours=max_hours)

    for symbol in symbols:
        try:
            latest = await storage.get_latest_observed_at(symbol)
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "auto-gap-fill: failed reading latest observed_at: %s: %s",
                type(exc).__name__,
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue

        if latest is None:
            _LOGGER.debug(
                "auto-gap-fill: no prior history; explicit --backfill needed",
                extra={"symbol": str(symbol)},
            )
            continue

        gap = resolved_now - latest
        gap_minutes = gap.total_seconds() / 60.0
        if gap < threshold:
            _LOGGER.debug(
                "auto-gap-fill: gap below threshold; skipping",
                extra={
                    "symbol": str(symbol),
                    "gap_minutes": round(gap_minutes, 1),
                    "threshold_minutes": threshold_minutes,
                },
            )
            continue
        if gap > max_window:
            _LOGGER.warning(
                "auto-gap-fill: gap exceeds max (%.1f hours > %.1f); skipping "
                "-- run `cli/observe --backfill --since <date>` manually",
                gap.total_seconds() / 3600.0,
                max_hours,
                extra={
                    "symbol": str(symbol),
                    "gap_hours": round(gap.total_seconds() / 3600.0, 2),
                    "max_hours": max_hours,
                    "latest_observed_at": latest.isoformat(),
                },
            )
            continue

        _LOGGER.info(
            "auto-gap-fill: filling %.1f minute gap on %s",
            gap_minutes,
            symbol,
            extra={
                "symbol": str(symbol),
                "gap_minutes": round(gap_minutes, 1),
                "since": latest.isoformat(),
                "until": resolved_now.isoformat(),
            },
        )
        result = await backfill_range(
            adapter,
            storage,
            symbol=symbol,
            since=latest,
            until=resolved_now,
            interval_minutes=1,
        )
        if result.error is not None:
            _LOGGER.warning(
                "auto-gap-fill: backfill failed for %s; continuing to poll loop: %s",
                symbol,
                result.error,
                extra={
                    "symbol": str(symbol),
                    "error": result.error,
                    "bars_inserted": result.bars_inserted,
                },
            )
        else:
            _LOGGER.info(
                "auto-gap-fill: filled %d bars on %s in %.1fs",
                result.bars_inserted,
                symbol,
                result.elapsed_seconds,
                extra={
                    "symbol": str(symbol),
                    "bars_inserted": result.bars_inserted,
                    "snapshots_inserted": result.snapshots_inserted,
                    "elapsed_seconds": round(result.elapsed_seconds, 1),
                },
            )


async def _main_async(config: WobbleBotConfig) -> int:
    if config.observe is None:
        _LOGGER.error("settings.yml is missing the `observe:` section")
        return 2

    try:
        price_interval = config.schedules.get("observe_prices")
    except KeyError as exc:
        _LOGGER.error("missing schedule", extra={"error": str(exc)})
        return 2
    balance_interval = config.schedules.get_or_default("observe_balances", timedelta(seconds=0))

    try:
        kraken_config = KrakenConfig.from_env()  # default vars: read-only key
    except ValueError as exc:
        _LOGGER.error("missing read-only credentials", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(config.observe.db)
    await storage.connect()
    adapter = KrakenAdapter(config=kraken_config)

    exit_code = await partition_or_exit(
        adapter,
        config.observe.symbols,
        logger=_LOGGER,
        cleanups=[
            ("close_kraken_adapter", adapter.aclose),
            ("close_observe_storage", storage.close),
        ],
    )
    if exit_code is not None:
        return exit_code

    if config.observe.autogapfill_enabled:
        await _run_auto_gap_fill(
            adapter,
            storage,
            list(config.observe.symbols),
            threshold_minutes=config.observe.autogapfill_threshold_minutes,
            max_hours=config.observe.autogapfill_max_hours,
        )

    stop_event = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop_event, logger=_LOGGER)

    try:
        return await _run_loop(
            adapter, storage, config.observe, price_interval, balance_interval, stop_event
        )
    finally:
        await safe_shutdown(
            [
                ("close_kraken_adapter", adapter.aclose),
                ("close_observe_storage", storage.close),
            ],
            logger=_LOGGER,
        )


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "observe",
        {
            "symbols": ("symbols", parse_symbol_csv),
            "db": ("db", identity),
            "log_format": ("log_format", identity),
        },
    )


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--symbols", default=None, help="Comma-separated trading pairs (e.g. BTC/USD,ETH/USD)."
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)

    # v1.1 backfill mode. When --backfill is set, cli/observe runs a
    # one-shot fetch of historical OHLC bars instead of entering the
    # poll loop. Exits when done.
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "Run one-shot historical OHLC backfill instead of entering the "
            "poll loop. Requires --since."
        ),
    )
    since_group = parser.add_mutually_exclusive_group()
    since_group.add_argument(
        "--since",
        default=None,
        help=(
            "Backfill lower bound (ISO 8601). Examples: 2026-04-01, "
            "2026-04-01T12:00:00Z. Bare dates are midnight UTC. The "
            "literal `auto` is equivalent to --catchup. Only used with "
            "--backfill."
        ),
    )
    since_group.add_argument(
        "--days",
        type=_parse_days_arg,
        default=None,
        help=(
            "Backfill lower bound as a day count back from now — shorthand "
            "for --since <now minus N days>. Mutually exclusive with "
            "--since. Only used with --backfill."
        ),
    )
    since_group.add_argument(
        "--catchup",
        action="store_true",
        help=(
            "Resolve each symbol's backfill lower bound from its latest "
            "stored observation (the same cursor the startup auto-gap-fill "
            "uses). Symbols with no prior history are skipped with a "
            "warning. Equivalent spelling: --since auto. Only used with "
            "--backfill."
        ),
    )
    since_group.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resolve each symbol's backfill lower bound from its latest "
            "ohlc_bars row at the requested --interval — the honest "
            "'continue where the backfill left off' cursor (unlike "
            "--catchup, unaffected by daemon price polls). Symbols with "
            "no bars at that interval are skipped with a warning. Only "
            "used with --backfill."
        ),
    )
    parser.add_argument(
        "--until",
        default=None,
        help=(
            "Backfill upper bound (ISO 8601). Defaults to now (UTC). " "Only used with --backfill."
        ),
    )
    parser.add_argument(
        "--rate-limit-seconds",
        type=_parse_rate_limit_arg,
        default=DEFAULT_RATE_LIMIT_SECONDS,
        help=(
            "Sleep between Kraken OHLC requests during backfill. Default "
            f"{DEFAULT_RATE_LIMIT_SECONDS}s (the public-API free-tier "
            "limit is ~1 call/second). Lower only on a paid tier. Only "
            "used with --backfill."
        ),
    )
    interval_group = parser.add_mutually_exclusive_group()
    interval_group.add_argument(
        "--interval",
        type=_parse_interval_arg,
        default=1,
        help=(
            "Backfill bar interval. Accepts 1m/5m/15m/30m/1h/4h/1d/1w or "
            "a bare minute count from Kraken's published set. Default 1m "
            "(max-fidelity). Only used with --backfill."
        ),
    )
    interval_group.add_argument(
        "--intervals",
        type=_parse_intervals_arg,
        default=None,
        help=(
            "Comma list of backfill bar intervals fetched back-to-back "
            "per symbol (e.g. 1m,1h — the auditor wants 1m, the "
            "historian 1h). Same accepted forms as --interval; stats "
            "report per (symbol, interval). Mutually exclusive with "
            "--interval. Only used with --backfill."
        ),
    )
    args = parser.parse_args()

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format = config.observe.log_format if config.observe else "plain"
    log_file_path = config.observe.log_file_path if config.observe else None
    configure_logging(log_format=log_format, rotating_file_path=log_file_path)

    if args.backfill:
        symbols_override: list[Symbol] | None = None
        if args.symbols:
            symbols_override = [Symbol.from_string(s) for s in parse_symbol_csv(args.symbols)]
        # `--since auto` is the doc'd equivalent spelling of --catchup.
        catchup = args.catchup or args.since == "auto"
        run_with_clean_exit(
            _backfill_main(
                config,
                since_raw=None if args.since == "auto" else args.since,
                until_raw=args.until,
                interval_minutes=args.interval,
                symbols_override=symbols_override,
                days=args.days,
                catchup=catchup,
                resume=args.resume,
                rate_limit_seconds=args.rate_limit_seconds,
                intervals=args.intervals,
            ),
            logger=_LOGGER,
        )
    else:
        run_with_clean_exit(_main_async(config), logger=_LOGGER)


if __name__ == "__main__":
    raise SystemExit(main())

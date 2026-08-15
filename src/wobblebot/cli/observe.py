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
    missing_section_exit,
    parse_days_arg,
    parse_interval_arg,
    parse_intervals_arg,
    parse_symbol_csv,
    partition_or_exit,
    run_poll_loop,
    run_with_clean_exit,
    safe_shutdown,
)
from wobblebot.cli.observe_backfill import backfill_main, parse_rate_limit_arg
from wobblebot.config.cli import ObserveConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol, Timestamp, fmt_decimal
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.backfill import DEFAULT_RATE_LIMIT_SECONDS, backfill_range

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
                "price snapshot saved (symbol=%s, price=%s, currency=%s, observed_at=%s)",
                symbol,
                fmt_decimal(price.amount),
                price.currency,
                now.dt.isoformat(),
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
                "price poll failed (symbol=%s): %s: %s",
                symbol,
                type(exc).__name__,
                exc,
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
        _LOGGER.debug(
            "balance snapshot saved (entries=%s)",
            len(balances),
            extra={"entries": len(balances)},
        )
        return len(balances)
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "balance poll failed: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 0


# Steady-state hourly-bar top-up (P2 slice 3 follow-up): the TA
# fields on PerformanceSummary null out when the newest 60m bar goes
# stale, and nothing else writes hourly bars while the daemon runs.
# Once per hour, resume each symbol from its interval-scoped cursor;
# a pair with no bars yet seeds one TA-window's worth (well inside
# Kraken's ~720-bar live retention).
_BAR_TOPUP_INTERVAL_MINUTES = 60
_BAR_TOPUP_SEED_BARS = 260  # matches SummaryBuilder's TA window


async def _top_up_bars(
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    symbols: list[Symbol],
    *,
    now: datetime | None = None,
) -> None:
    """Fetch each symbol's missing COMPLETED 60m bars from Kraken.

    Completed bars only: the live OHLC endpoint returns the
    in-progress hour too, and the idempotent INSERT OR IGNORE write
    path would freeze that partial bar's first-seen values forever —
    so ``until`` stops at the top of the current hour. Failures are
    absorbed per symbol (WARN); a Kraken hiccup must never break the
    poll loop. The ``now`` kwarg is the test seam.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    interval = timedelta(minutes=_BAR_TOPUP_INTERVAL_MINUTES)
    # Top of the current hour minus a second: excludes the bar that
    # OPENS at the current hour (in progress), keeps all completed.
    until = resolved_now.replace(minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    for symbol in symbols:
        try:
            since = await storage.get_latest_ohlc_opened_at(symbol, _BAR_TOPUP_INTERVAL_MINUTES)
            if since is None:
                since = resolved_now - interval * _BAR_TOPUP_SEED_BARS
            if until - since < interval:
                continue  # newest completed bar already stored
            result = await backfill_range(
                adapter,
                storage,
                symbol=symbol,
                since=since,
                until=until,
                interval_minutes=_BAR_TOPUP_INTERVAL_MINUTES,
            )
            if result.error is not None:
                _LOGGER.warning(
                    "bar top-up failed for %s: %s",
                    symbol,
                    result.error,
                    extra={"symbol": str(symbol), "error": result.error},
                )
            elif result.bars_inserted:
                _LOGGER.debug(
                    "bar top-up %s: %d bars",
                    symbol,
                    result.bars_inserted,
                    extra={"symbol": str(symbol), "bars_inserted": result.bars_inserted},
                )
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "bar top-up failed for %s: %s",
                symbol,
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )


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
    last_bar_topup = 0.0
    price_polls = 0
    balance_polls = 0
    price_interval_seconds = price_interval.total_seconds()
    balance_interval_seconds = balance_interval.total_seconds()
    _LOGGER.info(
        "observe session start (symbols=%s, price_interval_seconds=%s, "
        "balance_interval_seconds=%s, db_path=%s)",
        [str(s) for s in observe.symbols],
        price_interval_seconds,
        balance_interval_seconds,
        observe.db,
        extra={
            "symbols": [str(s) for s in observe.symbols],
            "price_interval_seconds": price_interval_seconds,
            "balance_interval_seconds": balance_interval_seconds,
            "db_path": observe.db,
        },
    )

    async def _one_cycle() -> None:
        nonlocal price_polls, balance_polls, last_balance_poll, last_bar_topup
        persisted = await _poll_prices(adapter, storage, list(observe.symbols))
        price_polls += persisted

        if balance_interval_seconds > 0:
            elapsed_since_balance = time.monotonic() - last_balance_poll
            if elapsed_since_balance >= balance_interval_seconds:
                persisted_b = await _poll_balances(adapter, storage)
                if persisted_b > 0:
                    balance_polls += 1
                last_balance_poll = time.monotonic()

        if observe.bar_topup_enabled:
            elapsed_since_topup = time.monotonic() - last_bar_topup
            if elapsed_since_topup >= _BAR_TOPUP_INTERVAL_MINUTES * 60:
                await _top_up_bars(adapter, storage, list(observe.symbols))
                last_bar_topup = time.monotonic()

    try:
        await run_poll_loop(
            _one_cycle,
            interval_seconds=price_interval_seconds,
            stop_event=stop_event,
        )
    finally:
        _LOGGER.info(
            "observe session end (duration_seconds=%s, price_snapshots_saved=%s, "
            "balance_snapshots_saved=%s)",
            round(time.monotonic() - started_at, 1),
            price_polls,
            balance_polls,
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "price_snapshots_saved": price_polls,
                "balance_snapshots_saved": balance_polls,
            },
        )
    return 0


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
                "auto-gap-fill: no prior history; explicit --backfill needed (symbol=%s)",
                symbol,
                extra={"symbol": str(symbol)},
            )
            continue

        gap = resolved_now - latest
        gap_minutes = gap.total_seconds() / 60.0
        if gap < threshold:
            _LOGGER.debug(
                "auto-gap-fill: gap below threshold; skipping (symbol=%s, gap_minutes=%s, "
                "threshold_minutes=%s)",
                symbol,
                round(gap_minutes, 1),
                threshold_minutes,
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
        return missing_section_exit(_LOGGER, "observe")

    try:
        price_interval = config.schedules.get("observe_prices")
    except KeyError as exc:
        _LOGGER.error(
            "missing schedule: %s",
            exc,
            extra={"error": str(exc)},
        )
        return 2
    balance_interval = config.schedules.get_or_default("observe_balances", timedelta(seconds=0))

    try:
        kraken_config = KrakenConfig.from_env()  # default vars: read-only key
    except ValueError as exc:
        _LOGGER.error(
            "missing read-only credentials: %s",
            exc,
            extra={"error": str(exc)},
        )
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
        type=parse_days_arg,
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
        type=parse_rate_limit_arg,
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
        type=parse_interval_arg,
        default=1,
        help=(
            "Backfill bar interval. Accepts 1m/5m/15m/30m/1h/4h/1d/1w or "
            "a bare minute count from Kraken's published set. Default 1m "
            "(max-fidelity). Only used with --backfill."
        ),
    )
    interval_group.add_argument(
        "--intervals",
        type=parse_intervals_arg,
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
            backfill_main(
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

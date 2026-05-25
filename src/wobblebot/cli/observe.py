"""Observer CLI — collect live market data without trading.

Run as a module::

    python -m wobblebot.cli.observe
    python -m wobblebot.cli.observe --profile conservative
    python -m wobblebot.cli.observe --symbols BTC/USD,ETH/USD

**Read-only.** Uses ``KRAKEN_API_KEY`` (not the trade key). Polls
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
from wobblebot.services.backfill import BackfillResult, backfill_range

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


async def _backfill_main(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-return-statements
    config: WobbleBotConfig,
    *,
    since_raw: str | None,
    until_raw: str | None,
    interval_minutes: int,
    symbols_override: list[Symbol] | None,
) -> int:
    """One-shot backfill mode for ``cli/observe --backfill``.

    Walks each configured symbol through ``services.backfill.backfill_range``
    against Kraken's OHLC endpoint, writes ohlc_bars + price_snapshots,
    prints a per-symbol summary, exits.

    Returns 0 on full success; 1 if any symbol's backfill terminated
    on an error; 2 on argument / config / credential failure.
    """
    if config.observe is None:
        _LOGGER.error("settings.yml is missing the `observe:` section")
        return 2

    if since_raw is None:
        _LOGGER.error("--backfill requires --since (e.g. --since 2026-04-01)")
        return 2

    try:
        since = _parse_date_arg(since_raw)
        until = _parse_date_arg(until_raw) if until_raw is not None else datetime.now(UTC)
    except ValueError as exc:
        _LOGGER.error("invalid date argument", extra={"error": str(exc)})
        return 2

    if since >= until:
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

        _LOGGER.info(
            "backfill starting",
            extra={
                "symbols": [str(s) for s in symbols],
                "since": since.isoformat(),
                "until": until.isoformat(),
                "interval_minutes": interval_minutes,
            },
        )

        any_error = False
        for symbol in symbols:
            result = await backfill_range(
                adapter,
                storage,
                symbol=symbol,
                since=since,
                until=until,
                interval_minutes=interval_minutes,
            )
            _log_backfill_result(symbol, result)
            if result.error is not None:
                any_error = True

        _LOGGER.info(
            "backfill done",
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
        _LOGGER.error(
            "backfill failed for symbol; re-run with --since to resume",
            extra={
                "symbol": str(symbol),
                "error": result.error,
                "resume_at": (
                    result.last_opened_at.isoformat() if result.last_opened_at is not None else None
                ),
                "bars_inserted_before_failure": result.bars_inserted,
                "elapsed_seconds": round(result.elapsed_seconds, 1),
            },
        )
    else:
        _LOGGER.info(
            "backfill complete for symbol",
            extra={
                "symbol": str(symbol),
                "bars_fetched": result.bars_fetched,
                "bars_inserted": result.bars_inserted,
                "snapshots_inserted": result.snapshots_inserted,
                "requests_made": result.requests_made,
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
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Backfill lower bound (ISO 8601). Examples: 2026-04-01, "
            "2026-04-01T12:00:00Z. Bare dates are midnight UTC. Only "
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
        "--interval",
        type=_parse_interval_arg,
        default=1,
        help=(
            "Backfill bar interval. Accepts 1m/5m/15m/30m/1h/4h/1d/1w or "
            "a bare minute count from Kraken's published set. Default 1m "
            "(max-fidelity). Only used with --backfill."
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
        run_with_clean_exit(
            _backfill_main(
                config,
                since_raw=args.since,
                until_raw=args.until,
                interval_minutes=args.interval,
                symbols_override=symbols_override,
            ),
            logger=_LOGGER,
        )
    else:
        run_with_clean_exit(_main_async(config), logger=_LOGGER)


if __name__ == "__main__":
    raise SystemExit(main())

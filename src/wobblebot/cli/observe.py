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
import signal
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
    load_operator_env,
    parse_symbol_csv,
    run_poll_loop,
    safe_shutdown,
)
from wobblebot.config.cli import ObserveConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import WobbleBotPortError

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
            _LOGGER.info(
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


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


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

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

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
    configure_logging(log_format=log_format)

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

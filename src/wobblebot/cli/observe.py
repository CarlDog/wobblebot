"""Stage 3.0 observer CLI — collect live market data without trading.

Run as a module::

    python -m wobblebot.cli.observe
    python -m wobblebot.cli.observe --symbols BTC/USD,ETH/USD --price-interval-seconds 30
    python -m wobblebot.cli.observe --balance-interval-seconds 0  # disable balance polls

Polls Kraken's public Ticker on a configurable interval for one or
more symbols and persists every observation to the SQLite
``price_snapshots`` table. Optionally also polls the operator's
private ``BalanceEx`` endpoint on a slower cadence and persists to the
existing ``balance_snapshots`` table.

**Read-only.** Uses ``KRAKEN_API_KEY`` (the read-only key, not the
trade key). Places no orders, cancels nothing, moves no funds. Safe
to leave running indefinitely.

Per ADR-008, this is the data-collection half of Stage 3.0; the
trading-simulation half is ``cli/shadow``. Together they form the
sandbox the rest of Phase 3 will iterate against.

Loop:
  1. For each ``symbol`` in ``--symbols``, call
     ``ExchangePort.get_current_price`` and persist via
     ``StoragePort.save_price_snapshot``.
  2. Every ``--balance-interval-seconds`` (default 600s, 0 disables),
     call ``ExchangePort.get_balances`` and persist via
     ``StoragePort.save_balance_snapshot``. Skipped if
     ``--balance-interval-seconds`` is 0.
  3. Sleep ``--price-interval-seconds`` (default 30s).
  4. Repeat until SIGINT/SIGTERM.

On shutdown: log the totals (price polls, balance polls, duration)
and exit 0. No cleanup needed — read-only operations leave nothing
to undo.

Loads credentials from ``KRAKEN_API_KEY`` / ``KRAKEN_API_SECRET``
(NOT the trade key — observer doesn't trade).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import LogFormat, configure_logging
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import WobbleBotPortError


@dataclass(frozen=True)
class _ObserveConfig:
    """All knobs for one ``cli/observe`` session."""

    symbols: tuple[Symbol, ...]
    price_interval_seconds: float
    balance_interval_seconds: float  # 0 = disabled
    db_path: str


_LOGGER = logging.getLogger("wobblebot.cli.observe")


def _parse_symbol(raw: str) -> Symbol:
    parts = raw.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"symbol must be BASE/QUOTE (e.g. BTC/USD); got {raw!r}")
    return Symbol(base=parts[0], quote=parts[1])


def _parse_symbols(raw: str) -> tuple[Symbol, ...]:
    """Same dedupe + ordering as cli/live."""
    seen: dict[str, Symbol] = {}
    for raw_symbol in raw.split(","):
        cleaned = raw_symbol.strip()
        if not cleaned:
            continue
        symbol = _parse_symbol(cleaned)
        seen.setdefault(str(symbol), symbol)
    if not seen:
        raise ValueError("--symbols requires at least one BASE/QUOTE pair")
    return tuple(seen.values())


async def _poll_prices(
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    symbols: tuple[Symbol, ...],
) -> int:
    """Fetch + persist a price snapshot for each symbol. Returns the
    count successfully persisted (per-symbol errors are logged and
    swallowed)."""
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
            _LOGGER.error(
                "price poll failed",
                extra={"symbol": str(symbol), "error": str(exc), "error_type": type(exc).__name__},
            )
    return persisted


async def _poll_balances(
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
) -> int:
    """Fetch + persist all account balances. Returns the count of
    balance entries persisted (or 0 on error)."""
    try:
        balances = await adapter.get_balances()
        if not balances:
            _LOGGER.info("balance poll: account empty; skipping snapshot")
            return 0
        await storage.save_balance_snapshot(balances)
        _LOGGER.info("balance snapshot saved", extra={"entries": len(balances)})
        return len(balances)
    except WobbleBotPortError as exc:
        _LOGGER.error(
            "balance poll failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 0


async def _run_loop(
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    config: _ObserveConfig,
    stop_event: asyncio.Event,
) -> int:
    started_at = time.monotonic()
    last_balance_poll = 0.0  # forces first balance poll to fire on tick 1 if enabled
    price_polls = 0
    balance_polls = 0
    _LOGGER.info(
        "observe session start",
        extra={
            "symbols": [str(s) for s in config.symbols],
            "price_interval_seconds": config.price_interval_seconds,
            "balance_interval_seconds": config.balance_interval_seconds,
            "db_path": config.db_path,
        },
    )
    try:
        while not stop_event.is_set():
            persisted = await _poll_prices(adapter, storage, config.symbols)
            price_polls += persisted

            if config.balance_interval_seconds > 0:
                elapsed_since_balance = time.monotonic() - last_balance_poll
                if elapsed_since_balance >= config.balance_interval_seconds:
                    persisted_b = await _poll_balances(adapter, storage)
                    if persisted_b > 0:
                        balance_polls += 1
                    last_balance_poll = time.monotonic()

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.price_interval_seconds)
            except asyncio.TimeoutError:
                pass  # normal — interval elapsed
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
    """Same Unix/Windows pattern as cli/live."""

    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return  # Windows fallback handled at the top-level KeyboardInterrupt


async def _main_async(config: _ObserveConfig) -> int:
    try:
        kraken_config = KrakenConfig.from_env()  # default vars: KRAKEN_API_KEY / _SECRET
    except ValueError as exc:
        _LOGGER.error("missing read-only credentials", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(config.db_path)
    await storage.connect()
    adapter = KrakenAdapter(config=kraken_config)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(adapter, storage, config, stop_event)
    finally:
        await adapter.aclose()
        await storage.close()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTC/USD")
    parser.add_argument("--price-interval-seconds", type=float, default=30.0)
    parser.add_argument(
        "--balance-interval-seconds",
        type=float,
        default=600.0,
        help="0 disables balance polling entirely.",
    )
    parser.add_argument("--db", default="wobblebot-observe.db")
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()

    log_format: LogFormat = args.log_format
    configure_logging(log_format=log_format)

    try:
        symbols = _parse_symbols(args.symbols)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    config = _ObserveConfig(
        symbols=symbols,
        price_interval_seconds=args.price_interval_seconds,
        balance_interval_seconds=args.balance_interval_seconds,
        db_path=args.db,
    )

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

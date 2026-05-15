"""Operational CLI — run the live grid engine against real Kraken.

Run as a module::

    python -m wobblebot.cli.live
    python -m wobblebot.cli.live --symbols BTC/USD,ETH/USD --tick-seconds 10
    python -m wobblebot.cli.live --max-session-loss-usd 2 --max-runtime-minutes 30

**Real money trading.** Every tick may place, cancel, or refresh real
orders on Kraken. Use ``cli/validate`` first to verify Kraken accepts
your grid config without spending anything; only then run this.

Multi-symbol (Stage 2.4): pass ``--symbols`` as a comma-separated list
(``BTC/USD,ETH/USD,DOGE/USD``). Each tick steps every symbol in series
through the same ``GridEngine`` instance — per-symbol asyncio.Lock
keeps step calls re-entrant-safe, but Stage 2.4 ticks them serially
(measured ~150ms per symbol vs the 5s tick budget; even 30 coins
finish well under one tick).

Loop:
  1. For each ``symbol``, call ``GridEngine.step(symbol)``.
  2. Check session-loss cap; abort if tripped.
  3. Sleep ``--tick-seconds`` (default 5).
  4. Repeat until SIGINT/SIGTERM, runtime cap hit, or loss cap hit.

Hard caps (default-on; override at command line):
  - ``--max-session-loss-usd`` (default $5): aborts on USD-balance
    delta. Conservative — does not credit unrealized base-currency
    inventory at current market. With multi-symbol this is a global
    cap across every coin, computed from the USD balance only.
  - ``--max-runtime-minutes`` (default 60): auto-stops after this long.
  - SafetyConfig flags (per-coin / total exposure / orders / daily
    spend): enforced inside the engine before every placement, *also*
    apply across all configured symbols.

On shutdown (any reason): every open order for **every** configured
symbol is cancelled in the ``finally`` block. Exit codes: 0 clean
(signal/runtime), 1 loss-cap tripped, 2 missing credentials.

Storage: SQLite at ``--db`` (default ``wobblebot-live.db``) — persists
each symbol's ``GridState`` independently, so the engine resumes the
same anchor per coin across restarts.

Loads trade credentials from ``KRAKEN_TRADE_API_KEY`` /
``KRAKEN_TRADE_API_SECRET``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import GridConfig, GridLevels
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import LogFormat, configure_logging
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.grid_engine import GridEngine


@dataclass(frozen=True)
class _SessionConfig:  # pylint: disable=too-many-instance-attributes
    """All knobs for one ``cli/live`` session, bundled to keep argument
    lists from growing into pylint warnings as flags accumulate.

    The R0902 (too-many-instance-attributes) disable is intentional: the
    whole purpose of this dataclass is to bundle every CLI flag into one
    typed value so the loop / wiring functions don't trip
    too-many-arguments. Splitting into sub-bundles would add indirection
    without clarity — every field is a leaf operator-tunable knob."""

    symbols: tuple[Symbol, ...]
    spacing_pct: Decimal
    above: int
    below: int
    order_size_usd: Decimal
    db_path: str
    tick_seconds: float
    max_runtime_seconds: float
    max_session_loss_usd: Decimal
    max_total_exposure_usd: Decimal
    max_per_coin_exposure_usd: Decimal
    max_orders_per_coin: int
    max_daily_spend_usd: Decimal


_LOGGER = logging.getLogger("wobblebot.cli.live")


def _parse_symbol(raw: str) -> Symbol:
    parts = raw.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"symbol must be BASE/QUOTE (e.g. BTC/USD); got {raw!r}")
    return Symbol(base=parts[0], quote=parts[1])


def _parse_symbols(raw: str) -> tuple[Symbol, ...]:
    """Parse ``--symbols BTC/USD,ETH/USD`` into a deduped, ordered tuple.

    Order is preserved (first-seen wins for dedupe). Empty entries from
    trailing commas are silently dropped — operator's intent is clear.
    """
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


def _build_grid_config(
    spacing_pct: Decimal,
    above: int,
    below: int,
    order_size_usd: Decimal,
) -> GridConfig:
    return GridConfig(
        default=GridLevels(
            spacing_percentage=spacing_pct,
            levels_above=above,
            levels_below=below,
            order_size_usd=order_size_usd,
        ),
    )


def _build_safety_config(
    max_total_usd: Decimal,
    max_per_coin_usd: Decimal,
    max_orders: int,
    max_daily_usd: Decimal,
) -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=max_total_usd,
        max_daily_spend_usd=max_daily_usd,
        max_per_coin_exposure_usd=max_per_coin_usd,
        max_orders_per_coin=max_orders,
        emergency_stop=EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal("20"),
            min_exchange_balance_usd=Decimal("0"),
        ),
    )


async def _cancel_all_open(
    adapter: KrakenAdapter,
    symbols: tuple[Symbol, ...],
) -> tuple[int, int]:
    """Cancel every open order across all configured ``symbols``.

    A single unfiltered ``get_open_orders()`` would be more efficient
    than one call per symbol, but per-symbol queries match the
    SafetyConfig accounting model (per-coin caps) and let us log which
    coin each cancel belonged to. At Stage 2.4 scales (single-digit
    coins) the extra round-trips are negligible.

    Returns ``(cancelled, failed)`` summed across symbols.
    """
    cancelled = 0
    failed = 0
    for symbol in symbols:
        try:
            opens = await adapter.get_open_orders(symbol=symbol)
        except WobbleBotPortError as exc:
            _LOGGER.error(
                "shutdown get_open_orders failed",
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue
        for o in opens:
            try:
                await adapter.cancel_order(o)
                cancelled += 1
                _LOGGER.info(
                    "shutdown cancelled",
                    extra={"symbol": str(symbol), "exchange_id": o.exchange_id},
                )
            except WobbleBotPortError as exc:
                failed += 1
                _LOGGER.error(
                    "shutdown cancel failed",
                    extra={
                        "symbol": str(symbol),
                        "exchange_id": o.exchange_id,
                        "error": str(exc),
                    },
                )
    return cancelled, failed


async def _session_usd_balance(adapter: KrakenAdapter) -> Decimal:
    bal = await adapter.get_balance("USD")
    return bal.total if bal else Decimal("0")


async def _run_one_tick(
    adapter: KrakenAdapter,
    engine: GridEngine,
    config: _SessionConfig,
    tick: int,
    started_usd: Decimal,
) -> bool:
    """Execute one engine tick across every configured symbol + post-tick
    cap check. Returns True when the loss cap tripped (caller should
    stop). Engine errors per symbol are logged and swallowed — one bad
    coin must not kill the tick or the session.

    Symbols step in series within the tick. Per ADR-006 decision 5 the
    per-symbol asyncio.Lock makes parallelization safe, but at the 5s
    tick budget vs ~150ms per-symbol observed latency, even a 30-coin
    serial sweep finishes in well under one tick. Parallelism deferred
    to Phase 5 hardening.
    """
    for symbol in config.symbols:
        try:
            result = await engine.step(symbol)
            _LOGGER.info(
                "tick complete",
                extra={
                    "tick": tick,
                    "symbol": str(symbol),
                    "action": result.action,
                    "fills": result.fills,
                    "counters_placed": result.counters_placed,
                    "placed": result.placed,
                    "refusals": result.refusals,
                    "offside": result.offside,
                },
            )
        except WobbleBotPortError as exc:
            _LOGGER.error(
                "symbol step failed; continuing other symbols",
                extra={
                    "tick": tick,
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    current_usd = await _session_usd_balance(adapter)
    session_pnl = current_usd - started_usd
    if session_pnl < -config.max_session_loss_usd:
        _LOGGER.error(
            "session loss cap exceeded; stopping",
            extra={
                "session_pnl_usd": str(session_pnl),
                "limit": str(config.max_session_loss_usd),
                "tick": tick,
            },
        )
        return True
    return False


async def _run_loop(
    adapter: KrakenAdapter,
    engine: GridEngine,
    config: _SessionConfig,
    stop_event: asyncio.Event,
) -> int:
    """Run the engine loop. Returns the process exit code."""
    started_usd = await _session_usd_balance(adapter)
    started_at = time.monotonic()
    _LOGGER.info(
        "session start",
        extra={
            "symbols": [str(s) for s in config.symbols],
            "tick_seconds": config.tick_seconds,
            "max_runtime_seconds": config.max_runtime_seconds,
            "max_session_loss_usd": str(config.max_session_loss_usd),
            "starting_usd": str(started_usd),
        },
    )

    exit_code = 0
    tick = 0
    try:
        while not stop_event.is_set():
            elapsed = time.monotonic() - started_at
            if elapsed >= config.max_runtime_seconds:
                _LOGGER.info(
                    "max runtime reached; stopping",
                    extra={"elapsed_seconds": round(elapsed, 1)},
                )
                break

            tick += 1
            if await _run_one_tick(adapter, engine, config, tick, started_usd):
                exit_code = 1
                break

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.tick_seconds)
            except asyncio.TimeoutError:
                pass  # normal — tick interval elapsed
    finally:
        ended_usd = await _session_usd_balance(adapter)
        cancelled, failed = await _cancel_all_open(adapter, config.symbols)
        _LOGGER.info(
            "session end",
            extra={
                "ticks": tick,
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "starting_usd": str(started_usd),
                "ending_usd": str(ended_usd),
                "session_pnl_usd": str(ended_usd - started_usd),
                "open_orders_cancelled": cancelled,
                "open_orders_cancel_failed": failed,
                "exit_code": exit_code,
            },
        )
    return exit_code


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Install SIGINT/SIGTERM → set stop_event on Unix.

    Windows asyncio doesn't support add_signal_handler; ``main()`` falls
    back to a ``KeyboardInterrupt`` catch around ``asyncio.run`` for
    Ctrl+C handling there.
    """

    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            # Windows: signal handlers can't be installed on the asyncio
            # loop. KeyboardInterrupt at the top level handles Ctrl+C.
            return


async def _main_async(config: _SessionConfig) -> int:
    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADE_API_KEY",
            secret_var="KRAKEN_TRADE_API_SECRET",
        )
    except ValueError as exc:
        _LOGGER.error("missing trade credentials", extra={"error": str(exc)})
        return 2

    grid_config = _build_grid_config(
        config.spacing_pct,
        config.above,
        config.below,
        config.order_size_usd,
    )
    safety_config = _build_safety_config(
        max_total_usd=config.max_total_exposure_usd,
        max_per_coin_usd=config.max_per_coin_exposure_usd,
        max_orders=config.max_orders_per_coin,
        max_daily_usd=config.max_daily_spend_usd,
    )

    storage = SQLiteStorageAdapter(config.db_path)
    await storage.connect()
    adapter = KrakenAdapter(config=kraken_config)
    engine = GridEngine(adapter, storage, grid_config, safety_config)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(
            adapter=adapter,
            engine=engine,
            config=config,
            stop_event=stop_event,
        )
    finally:
        await adapter.aclose()
        await storage.close()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default="BTC/USD",
        help="Comma-separated list of trading pairs (e.g. BTC/USD,ETH/USD). "
        "Engine steps each symbol once per tick, in series.",
    )
    parser.add_argument("--spacing", type=Decimal, default=Decimal("1.0"))
    parser.add_argument("--above", type=int, default=3)
    parser.add_argument("--below", type=int, default=3)
    parser.add_argument("--order-size", type=Decimal, default=Decimal("10"))
    parser.add_argument("--db", default="wobblebot-live.db")
    parser.add_argument("--tick-seconds", type=float, default=5.0)
    parser.add_argument("--max-runtime-minutes", type=float, default=60.0)
    parser.add_argument("--max-session-loss-usd", type=Decimal, default=Decimal("5"))
    parser.add_argument("--max-total-exposure-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--max-per-coin-exposure-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--max-orders-per-coin", type=int, default=20)
    parser.add_argument("--max-daily-spend-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()

    log_format: LogFormat = args.log_format
    configure_logging(log_format=log_format)

    try:
        symbols = _parse_symbols(args.symbols)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    session = _SessionConfig(
        symbols=symbols,
        spacing_pct=args.spacing,
        above=args.above,
        below=args.below,
        order_size_usd=args.order_size,
        db_path=args.db,
        tick_seconds=args.tick_seconds,
        max_runtime_seconds=args.max_runtime_minutes * 60.0,
        max_session_loss_usd=args.max_session_loss_usd,
        max_total_exposure_usd=args.max_total_exposure_usd,
        max_per_coin_exposure_usd=args.max_per_coin_exposure_usd,
        max_orders_per_coin=args.max_orders_per_coin,
        max_daily_spend_usd=args.max_daily_spend_usd,
    )

    try:
        return asyncio.run(_main_async(session))
    except KeyboardInterrupt:
        # Windows fallback path — asyncio signal handlers don't install
        # there, so Ctrl+C surfaces as KeyboardInterrupt at this level.
        # The cleanup in _run_loop's finally already fired before the
        # interrupt propagated this high (it went through asyncio.run).
        # Exit code 0 — clean shutdown initiated by operator.
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

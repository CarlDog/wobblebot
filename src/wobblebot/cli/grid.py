"""Stage 2.3 operational CLI — run the live grid engine against real Kraken.

Run as a module::

    python -m wobblebot.cli.grid
    python -m wobblebot.cli.grid --symbol ETH/USD --tick-seconds 10
    python -m wobblebot.cli.grid --max-session-loss-usd 2 --max-runtime-minutes 30

**Real money trading.** Every tick may place, cancel, or refresh real
orders on Kraken. Use ``cli/validate`` first to verify Kraken accepts
your grid config without spending anything; only then run this.

Loop:
  1. Call ``GridEngine.step(symbol)`` (handles init, fills, counters).
  2. Sleep ``--tick-seconds`` (default 5).
  3. Repeat until SIGINT/SIGTERM, runtime cap hit, or session-loss cap
     hit.

Hard caps (both default-on; override at command line):
  - ``--max-session-loss-usd`` (default $5): aborts when realized + open
    exposure-adjusted PnL since session start drops below this. Computed
    from USD balance delta; conservative — does not credit unrealized
    BTC inventory at current market.
  - ``--max-runtime-minutes`` (default 60): auto-stops after this long.
    Safety bound against runaway sessions.

On shutdown (any reason):
  - Cancels every open order for the configured symbol.
  - Logs final balance snapshot and session P&L.
  - Exits 0 if shutdown was clean (signal or runtime cap), 1 if loss
    cap tripped, 2 on credential error.

Storage: SQLite at ``--db`` (default ``wobblebot-live.db``) — persists
across restarts so the engine can resume the same grid anchor instead
of re-anchoring at a different reference price.

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
    """All knobs for one ``cli/grid`` session, bundled to keep argument
    lists from growing into pylint warnings as flags accumulate.

    The R0902 (too-many-instance-attributes) disable is intentional: the
    whole purpose of this dataclass is to bundle every CLI flag into one
    typed value so the loop / wiring functions don't trip
    too-many-arguments. Splitting into sub-bundles would add indirection
    without clarity — every field is a leaf operator-tunable knob."""

    symbol: Symbol
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

_LOGGER = logging.getLogger("wobblebot.cli.grid")


def _parse_symbol(raw: str) -> Symbol:
    parts = raw.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"--symbol must be BASE/QUOTE (e.g. BTC/USD); got {raw!r}")
    return Symbol(base=parts[0], quote=parts[1])


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
    symbol: Symbol,
) -> tuple[int, int]:
    """Cancel every open order for ``symbol``. Returns ``(cancelled, failed)``."""
    try:
        opens = await adapter.get_open_orders(symbol=symbol)
    except WobbleBotPortError as exc:
        _LOGGER.error("shutdown get_open_orders failed", extra={"error": str(exc)})
        return 0, 0
    cancelled = 0
    failed = 0
    for o in opens:
        try:
            await adapter.cancel_order(o)
            cancelled += 1
            _LOGGER.info("shutdown cancelled", extra={"exchange_id": o.exchange_id})
        except WobbleBotPortError as exc:
            failed += 1
            _LOGGER.error(
                "shutdown cancel failed",
                extra={"exchange_id": o.exchange_id, "error": str(exc)},
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
    """Execute one engine tick + post-tick cap check. Returns True when
    the loss cap tripped (caller should stop). Engine errors are logged
    and swallowed — one bad tick must not kill the session."""
    try:
        result = await engine.step(config.symbol)
        _LOGGER.info(
            "tick complete",
            extra={
                "tick": tick,
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
            "tick failed; continuing",
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
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
            "symbol": str(config.symbol),
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
        cancelled, failed = await _cancel_all_open(adapter, config.symbol)
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
    parser.add_argument("--symbol", default="BTC/USD")
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
        symbol = _parse_symbol(args.symbol)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    session = _SessionConfig(
        symbol=symbol,
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

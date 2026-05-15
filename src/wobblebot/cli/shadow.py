"""Operational CLI — run the grid engine against live prices with simulated execution.

Run as a module::

    python -m wobblebot.cli.shadow --initial-shadow-usd 10000
    python -m wobblebot.cli.shadow --symbols BTC/USD,ETH/USD \
        --initial-shadow-usd 5000 --initial-shadow-btc 0.05

**No real money.** This is the sister command to ``cli/live``: same
engine code, same safety caps, same SIGINT cleanup — but the
``ExchangePort`` is a ``ShadowExchangeAdapter`` that uses live Kraken
for prices and matches orders against a synthetic balance ledger.

Per ADR-008, this is the trading-simulation half of Stage 3.0. Useful
for: iterating the Phase 3 advisor against real market behavior
without burning fees, real-time backtesting against the live tape,
calibrating safety caps before flipping to ``cli/live``.

Loop:
  1. For each ``symbol`` in ``--symbols``, call
     ``GridEngine.step(symbol)``.
  2. Check session-loss cap (synthetic USD-balance delta).
  3. Sleep ``--tick-seconds`` (default 5).
  4. Repeat until SIGINT/SIGTERM, runtime cap hit, or loss cap hit.

Hard caps mirror ``cli/live`` (max session loss, max runtime, per-coin
/ total / daily-spend exposure). They engage against synthetic
state — useful for verifying the cap arithmetic without spending real
money.

On shutdown: cancels every open shadow order for every configured
symbol in the ``finally`` block (same discipline as ``cli/live``).

Storage: SQLite at ``--db`` (default ``wobblebot-shadow.db`` —
deliberately distinct from ``wobblebot-live.db`` so shadow runs cannot
contaminate live trading state or vice versa). Each symbol's
``GridState`` persists across restarts, same as live.

**Initial synthetic balances are operator-supplied**, not inferred
from the operator's real Kraken balances (per ADR-008): muscle-memory
guard against confusing shadow state with live state.

Loads credentials from ``KRAKEN_API_KEY`` / ``KRAKEN_API_SECRET``
(read-only — the shadow needs Kraken only for price discovery, never
for placement).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import GridConfig, GridLevels
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import LogFormat, configure_logging
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.grid_engine import GridEngine


@dataclass(frozen=True)
class _ShadowConfig:  # pylint: disable=too-many-instance-attributes
    """Bundle of every CLI flag for one shadow session.

    R0902 disable: same rationale as cli/live's _SessionConfig — the
    bundle is the point. Splitting into sub-bundles would obscure the
    flow without removing any field."""

    symbols: tuple[Symbol, ...]
    initial_balances: dict[str, Decimal] = field(default_factory=dict)
    spacing_pct: Decimal = Decimal("1.0")
    above: int = 3
    below: int = 3
    order_size_usd: Decimal = Decimal("10")
    db_path: str = "wobblebot-shadow.db"
    tick_seconds: float = 5.0
    max_runtime_seconds: float = 3600.0
    max_session_loss_usd: Decimal = Decimal("100")  # synthetic — be generous
    max_total_exposure_usd: Decimal = Decimal("100")
    max_per_coin_exposure_usd: Decimal = Decimal("100")
    max_orders_per_coin: int = 20
    max_daily_spend_usd: Decimal = Decimal("100")
    maker_fee_rate: Decimal = Decimal("0.0026")
    taker_fee_rate: Decimal = Decimal("0.0040")


_LOGGER = logging.getLogger("wobblebot.cli.shadow")


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


def _build_grid_config(
    spacing_pct: Decimal, above: int, below: int, order_size_usd: Decimal
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
    max_total: Decimal,
    max_per_coin: Decimal,
    max_orders: int,
    max_daily: Decimal,
) -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=max_total,
        max_daily_spend_usd=max_daily,
        max_per_coin_exposure_usd=max_per_coin,
        max_orders_per_coin=max_orders,
        emergency_stop=EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal("20"),
            min_exchange_balance_usd=Decimal("0"),
        ),
    )


async def _cancel_all_open(
    adapter: ShadowExchangeAdapter,
    symbols: tuple[Symbol, ...],
) -> tuple[int, int]:
    """Cancel every open shadow order across all configured symbols.
    Same discipline as cli/live. Returns ``(cancelled, failed)``."""
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
                    "shutdown cancelled (shadow)",
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


async def _shadow_usd_balance(adapter: ShadowExchangeAdapter) -> Decimal:
    bal = await adapter.get_balance("USD")
    return bal.total if bal else Decimal("0")


async def _run_one_tick(
    adapter: ShadowExchangeAdapter,
    engine: GridEngine,
    config: _ShadowConfig,
    tick: int,
    started_usd: Decimal,
) -> bool:
    """Same shape as cli/live's _run_one_tick — symbols in series,
    per-symbol errors swallowed, post-tick session-loss cap check.
    Returns True when the loss cap tripped."""
    for symbol in config.symbols:
        try:
            result = await engine.step(symbol)
            _LOGGER.info(
                "shadow tick complete",
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
                "shadow symbol step failed; continuing other symbols",
                extra={
                    "tick": tick,
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    current_usd = await _shadow_usd_balance(adapter)
    session_pnl = current_usd - started_usd
    if session_pnl < -config.max_session_loss_usd:
        _LOGGER.error(
            "shadow session loss cap exceeded; stopping",
            extra={
                "session_pnl_usd": str(session_pnl),
                "limit": str(config.max_session_loss_usd),
                "tick": tick,
            },
        )
        return True
    return False


async def _run_loop(
    adapter: ShadowExchangeAdapter,
    engine: GridEngine,
    config: _ShadowConfig,
    stop_event: asyncio.Event,
) -> int:
    started_usd = await _shadow_usd_balance(adapter)
    started_at = time.monotonic()
    _LOGGER.info(
        "shadow session start",
        extra={
            "symbols": [str(s) for s in config.symbols],
            "initial_balances": {a: str(v) for a, v in config.initial_balances.items()},
            "tick_seconds": config.tick_seconds,
            "max_runtime_seconds": config.max_runtime_seconds,
            "max_session_loss_usd": str(config.max_session_loss_usd),
            "starting_usd_synthetic": str(started_usd),
            "maker_fee_rate": str(config.maker_fee_rate),
            "taker_fee_rate": str(config.taker_fee_rate),
        },
    )

    exit_code = 0
    tick = 0
    try:
        while not stop_event.is_set():
            elapsed = time.monotonic() - started_at
            if elapsed >= config.max_runtime_seconds:
                _LOGGER.info(
                    "shadow max runtime reached; stopping",
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
                pass
    finally:
        ended_usd = await _shadow_usd_balance(adapter)
        cancelled, failed = await _cancel_all_open(adapter, config.symbols)
        _LOGGER.info(
            "shadow session end",
            extra={
                "ticks": tick,
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "starting_usd_synthetic": str(started_usd),
                "ending_usd_synthetic": str(ended_usd),
                "session_pnl_usd_synthetic": str(ended_usd - started_usd),
                "open_orders_cancelled": cancelled,
                "open_orders_cancel_failed": failed,
                "exit_code": exit_code,
            },
        )
    return exit_code


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Same Unix/Windows pattern as cli/live."""

    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating shadow shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return  # Windows fallback handled at the top-level KeyboardInterrupt


async def _main_async(config: _ShadowConfig) -> int:
    try:
        kraken_config = KrakenConfig.from_env()  # default vars: KRAKEN_API_KEY (read-only)
    except ValueError as exc:
        _LOGGER.error("missing read-only credentials", extra={"error": str(exc)})
        return 2

    grid_config = _build_grid_config(
        config.spacing_pct, config.above, config.below, config.order_size_usd
    )
    safety_config = _build_safety_config(
        max_total=config.max_total_exposure_usd,
        max_per_coin=config.max_per_coin_exposure_usd,
        max_orders=config.max_orders_per_coin,
        max_daily=config.max_daily_spend_usd,
    )

    storage = SQLiteStorageAdapter(config.db_path)
    await storage.connect()
    live_adapter = KrakenAdapter(config=kraken_config)
    shadow_adapter = ShadowExchangeAdapter(
        live_exchange=live_adapter,
        starting_balances=config.initial_balances,
        maker_fee_rate=config.maker_fee_rate,
        taker_fee_rate=config.taker_fee_rate,
    )
    engine = GridEngine(shadow_adapter, storage, grid_config, safety_config)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(shadow_adapter, engine, config, stop_event)
    finally:
        await shadow_adapter.aclose()
        await storage.close()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTC/USD")
    parser.add_argument("--spacing", type=Decimal, default=Decimal("1.0"))
    parser.add_argument("--above", type=int, default=3)
    parser.add_argument("--below", type=int, default=3)
    parser.add_argument("--order-size", type=Decimal, default=Decimal("10"))
    parser.add_argument("--db", default="wobblebot-shadow.db")
    parser.add_argument("--tick-seconds", type=float, default=5.0)
    parser.add_argument("--max-runtime-minutes", type=float, default=60.0)
    parser.add_argument("--max-session-loss-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--max-total-exposure-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--max-per-coin-exposure-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--max-orders-per-coin", type=int, default=20)
    parser.add_argument("--max-daily-spend-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument(
        "--initial-shadow-usd",
        type=Decimal,
        required=True,
        help="Synthetic starting USD balance. Required — no inference from real Kraken.",
    )
    parser.add_argument(
        "--initial-shadow-btc",
        type=Decimal,
        default=Decimal("0"),
        help="Synthetic starting BTC balance (default 0 — most operators will only fund USD).",
    )
    parser.add_argument(
        "--initial-shadow-eth",
        type=Decimal,
        default=Decimal("0"),
        help="Synthetic starting ETH balance (default 0).",
    )
    parser.add_argument("--maker-fee-rate", type=Decimal, default=Decimal("0.0026"))
    parser.add_argument("--taker-fee-rate", type=Decimal, default=Decimal("0.0040"))
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()

    log_format: LogFormat = args.log_format
    configure_logging(log_format=log_format)

    try:
        symbols = _parse_symbols(args.symbols)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    initial_balances: dict[str, Decimal] = {"USD": args.initial_shadow_usd}
    if args.initial_shadow_btc > 0:
        initial_balances["BTC"] = args.initial_shadow_btc
    if args.initial_shadow_eth > 0:
        initial_balances["ETH"] = args.initial_shadow_eth

    config = _ShadowConfig(
        symbols=symbols,
        initial_balances=initial_balances,
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
        maker_fee_rate=args.maker_fee_rate,
        taker_fee_rate=args.taker_fee_rate,
    )

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

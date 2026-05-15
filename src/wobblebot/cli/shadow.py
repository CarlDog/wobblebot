"""Operational CLI — run the grid engine against live prices with simulated execution.

Run as a module::

    python -m wobblebot.cli.shadow
    python -m wobblebot.cli.shadow --profile aggressive
    python -m wobblebot.cli.shadow --symbols BTC/USD,ETH/USD --tick-seconds 10

**No real money.** This is the sister command to ``cli/live``: same
engine code, same safety caps, same SIGINT cleanup — but the
``ExchangePort`` is a ``ShadowExchangeAdapter`` that uses live Kraken
for prices and matches orders against a synthetic balance ledger.

Per ADR-008, this is the trading-simulation half of Stage 3.0. Useful
for: iterating the Phase 3 advisor against real market behavior
without burning fees, real-time backtesting against the live tape,
calibrating safety caps before flipping to ``cli/live``.

Configuration layering (per ADR-009):

1. **Base config** — ``config/settings.yml`` (or ``--config path``,
   or ``config/settings.example.yml`` as a last-resort fallback).
2. **Profile overrides** — ``--profile name`` deep-merges the named
   block from ``profiles:`` over the base.
3. **CLI flag overrides** — explicit flags below win over both YAML
   and profile. Omitted flags inherit YAML values.

Loop:
  1. For each symbol in ``shadow.symbols``, call ``GridEngine.step(symbol)``.
  2. Check session-loss cap (synthetic USD-balance delta).
  3. Sleep ``shadow.tick_seconds`` (default 5).
  4. Repeat until SIGINT/SIGTERM, runtime cap hit, or loss cap hit.

On shutdown: cancels every open shadow order for every configured
symbol in the ``finally`` block (same discipline as ``cli/live``).

Storage: SQLite at ``shadow.db`` (default ``data/wobblebot-shadow.db``
— deliberately distinct from ``wobblebot-live.db`` so shadow runs
cannot contaminate live trading state or vice versa).

**Initial synthetic balances are operator-supplied** via
``shadow.initial_balances`` in the YAML, not inferred from the
operator's real Kraken balances (per ADR-008): muscle-memory guard
against confusing shadow state with live state.

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
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, parse_symbol_csv
from wobblebot.config.cli import ShadowConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.grid_engine import GridEngine

_LOGGER = logging.getLogger("wobblebot.cli.shadow")


# ---------------------------------------------------------------------------
# Loop helpers — mirror cli/live, consume ShadowConfig directly
# ---------------------------------------------------------------------------


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
    shadow: ShadowConfig,
    tick: int,
    started_usd: Decimal,
) -> bool:
    """Mirror cli/live's _run_one_tick — symbols in series, per-symbol
    errors swallowed, post-tick session-loss cap check.
    Returns True when the loss cap tripped."""
    for symbol in shadow.symbols:
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
    if session_pnl < -shadow.max_session_loss_usd:
        _LOGGER.error(
            "shadow session loss cap exceeded; stopping",
            extra={
                "session_pnl_usd": str(session_pnl),
                "limit": str(shadow.max_session_loss_usd),
                "tick": tick,
            },
        )
        return True
    return False


async def _run_loop(
    adapter: ShadowExchangeAdapter,
    engine: GridEngine,
    shadow: ShadowConfig,
    stop_event: asyncio.Event,
) -> int:
    started_usd = await _shadow_usd_balance(adapter)
    started_at = time.monotonic()
    max_runtime_seconds = shadow.max_runtime_minutes * 60.0
    _LOGGER.info(
        "shadow session start",
        extra={
            "symbols": [str(s) for s in shadow.symbols],
            "initial_balances": {a: str(v) for a, v in shadow.initial_balances.items()},
            "tick_seconds": shadow.tick_seconds,
            "max_runtime_seconds": max_runtime_seconds,
            "max_session_loss_usd": str(shadow.max_session_loss_usd),
            "starting_usd_synthetic": str(started_usd),
            "maker_fee_rate": str(shadow.maker_fee_rate),
            "taker_fee_rate": str(shadow.taker_fee_rate),
        },
    )

    exit_code = 0
    tick = 0
    try:
        while not stop_event.is_set():
            elapsed = time.monotonic() - started_at
            if elapsed >= max_runtime_seconds:
                _LOGGER.info(
                    "shadow max runtime reached; stopping",
                    extra={"elapsed_seconds": round(elapsed, 1)},
                )
                break

            tick += 1
            if await _run_one_tick(adapter, engine, shadow, tick, started_usd):
                exit_code = 1
                break

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=shadow.tick_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        ended_usd = await _shadow_usd_balance(adapter)
        cancelled, failed = await _cancel_all_open(adapter, tuple(shadow.symbols))
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


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def _main_async(config: WobbleBotConfig) -> int:
    if config.shadow is None:
        _LOGGER.error("settings.yml is missing the `shadow:` section")
        return 2

    try:
        kraken_config = KrakenConfig.from_env()  # default vars: KRAKEN_API_KEY (read-only)
    except ValueError as exc:
        _LOGGER.error("missing read-only credentials", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(config.shadow.db)
    await storage.connect()
    live_adapter = KrakenAdapter(config=kraken_config)
    shadow_adapter = ShadowExchangeAdapter(
        live_exchange=live_adapter,
        starting_balances=dict(config.shadow.initial_balances),
        maker_fee_rate=config.shadow.maker_fee_rate,
        taker_fee_rate=config.shadow.taker_fee_rate,
    )
    engine = GridEngine(shadow_adapter, storage, config.grid, config.safety)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(
            adapter=shadow_adapter,
            engine=engine,
            shadow=config.shadow,
            stop_event=stop_event,
        )
    finally:
        await shadow_adapter.aclose()
        await storage.close()


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Translate explicit CLI flags into a YAML override dict."""
    shadow_overrides = collect_overrides(
        args,
        "shadow",
        {
            "symbols": ("symbols", parse_symbol_csv),
            "db": ("db", identity),
            "tick_seconds": ("tick_seconds", identity),
            "max_runtime_minutes": ("max_runtime_minutes", identity),
            "max_session_loss_usd": ("max_session_loss_usd", identity),
            "maker_fee_rate": ("maker_fee_rate", identity),
            "taker_fee_rate": ("taker_fee_rate", identity),
            "log_format": ("log_format", identity),
        },
    )

    # grid.default is nested — build manually
    grid_default: dict[str, Any] = {}
    if args.spacing is not None:
        grid_default["spacing_percentage"] = args.spacing
    if args.above is not None:
        grid_default["levels_above"] = args.above
    if args.below is not None:
        grid_default["levels_below"] = args.below
    if args.order_size is not None:
        grid_default["order_size_usd"] = args.order_size
    grid_overrides = {"grid": {"default": grid_default}} if grid_default else {}

    safety_overrides = collect_overrides(
        args,
        "safety",
        {
            "max_total_exposure_usd": ("max_total_exposure_usd", identity),
            "max_per_coin_exposure_usd": ("max_per_coin_exposure_usd", identity),
            "max_orders_per_coin": ("max_orders_per_coin", identity),
            "max_daily_spend_usd": ("max_daily_spend_usd", identity),
        },
    )

    # initial_balances: only override the per-asset entries the operator
    # explicitly supplied; otherwise inherit the YAML/profile value
    # entirely.
    initial_balances: dict[str, Decimal] = {}
    if args.initial_shadow_usd is not None:
        initial_balances["USD"] = args.initial_shadow_usd
    if args.initial_shadow_btc is not None:
        initial_balances["BTC"] = args.initial_shadow_btc
    if args.initial_shadow_eth is not None:
        initial_balances["ETH"] = args.initial_shadow_eth
    if initial_balances:
        shadow_overrides.setdefault("shadow", {})["initial_balances"] = initial_balances

    merged: dict[str, Any] = {}
    for layer in (shadow_overrides, grid_overrides, safety_overrides):
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)

    # All flag defaults are None — explicit-pass detection drives
    # whether the value overrides YAML or inherits.
    parser.add_argument(
        "--symbols", default=None, help="Comma-separated trading pairs (e.g. BTC/USD,ETH/USD)."
    )
    parser.add_argument("--spacing", type=Decimal, default=None)
    parser.add_argument("--above", type=int, default=None)
    parser.add_argument("--below", type=int, default=None)
    parser.add_argument("--order-size", type=Decimal, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--tick-seconds", type=float, default=None)
    parser.add_argument("--max-runtime-minutes", type=float, default=None)
    parser.add_argument("--max-session-loss-usd", type=Decimal, default=None)
    parser.add_argument("--max-total-exposure-usd", type=Decimal, default=None)
    parser.add_argument("--max-per-coin-exposure-usd", type=Decimal, default=None)
    parser.add_argument("--max-orders-per-coin", type=int, default=None)
    parser.add_argument("--max-daily-spend-usd", type=Decimal, default=None)
    parser.add_argument(
        "--initial-shadow-usd",
        type=Decimal,
        default=None,
        help="Override shadow.initial_balances.USD from the YAML.",
    )
    parser.add_argument(
        "--initial-shadow-btc",
        type=Decimal,
        default=None,
        help="Override shadow.initial_balances.BTC from the YAML.",
    )
    parser.add_argument(
        "--initial-shadow-eth",
        type=Decimal,
        default=None,
        help="Override shadow.initial_balances.ETH from the YAML.",
    )
    parser.add_argument("--maker-fee-rate", type=Decimal, default=None)
    parser.add_argument("--taker-fee-rate", type=Decimal, default=None)
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

    log_format = config.shadow.log_format if config.shadow else "plain"
    configure_logging(log_format=log_format)

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Operational CLI — run the live grid engine against real Kraken.

Run as a module::

    python -m wobblebot.cli.live
    python -m wobblebot.cli.live --profile conservative
    python -m wobblebot.cli.live --symbols BTC/USD,ETH/USD --tick-seconds 10
    python -m wobblebot.cli.live --max-session-loss-usd 2 --max-runtime-minutes 30
    python -m wobblebot.cli.live --config /path/to/custom-settings.yml

**Real money trading.** Every tick may place, cancel, or refresh real
orders on Kraken. Use ``cli/preflight`` first to verify Kraken accepts
your config without spending anything; only then run this.

Configuration layering (per ADR-009):

1. **Base config** — ``config/settings.yml`` (or ``--config path``,
   or ``config/settings.example.yml`` as a last-resort fallback).
2. **Profile overrides** — if ``--profile name`` is passed, the named
   block from ``profiles:`` deep-merges over the base.
3. **CLI flag overrides** — explicit flags below win over both YAML
   and profile. Omitted flags inherit YAML values.

Multi-symbol since Stage 2.4: ``--symbols BTC/USD,ETH/USD,DOGE/USD``
or set ``live.symbols:`` in the YAML. Each tick steps every symbol in
series; per-symbol asyncio.Lock keeps them re-entrant-safe.

On shutdown (any reason): every open order for every configured
symbol is cancelled in the ``finally`` block. Exit codes: 0 clean
(signal/runtime), 1 loss-cap tripped, 2 missing credentials.

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
from decimal import Decimal
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    collect_overrides,
    identity,
    load_operator_env,
    parse_symbol_csv,
)
from wobblebot.config.cli import LiveConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.grid_engine import GridEngine

_LOGGER = logging.getLogger("wobblebot.cli.live")


# ---------------------------------------------------------------------------
# Loop helpers — same shape as before, now consume LiveConfig directly
# ---------------------------------------------------------------------------


async def _cancel_all_open(
    adapter: KrakenAdapter,
    symbols: tuple[Symbol, ...],
) -> tuple[int, int]:
    """Cancel every open order across all configured ``symbols``. Returns
    ``(cancelled, failed)`` summed across symbols."""
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
    live: LiveConfig,
    tick: int,
    started_usd: Decimal,
) -> bool:
    """One tick across every configured symbol + post-tick loss cap
    check. Returns True when the loss cap tripped (caller stops)."""
    for symbol in live.symbols:
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
    if session_pnl < -live.max_session_loss_usd:
        _LOGGER.error(
            "session loss cap exceeded; stopping",
            extra={
                "session_pnl_usd": str(session_pnl),
                "limit": str(live.max_session_loss_usd),
                "tick": tick,
            },
        )
        return True
    return False


async def _run_loop(
    adapter: KrakenAdapter,
    engine: GridEngine,
    live: LiveConfig,
    stop_event: asyncio.Event,
) -> int:
    """Run the engine loop. Returns the process exit code."""
    started_usd = await _session_usd_balance(adapter)
    started_at = time.monotonic()
    # ``None`` means "no runtime cap" — operator opts into indefinite
    # mode. SIGINT/SIGTERM and the session-loss cap still apply, so
    # this isn't a way to bypass safety.
    max_runtime_seconds = (
        live.max_runtime_minutes * 60.0 if live.max_runtime_minutes is not None else None
    )
    _LOGGER.info(
        "session start",
        extra={
            "symbols": [str(s) for s in live.symbols],
            "tick_seconds": live.tick_seconds,
            "max_runtime_seconds": max_runtime_seconds,  # None == unlimited
            "max_session_loss_usd": str(live.max_session_loss_usd),
            "starting_usd": str(started_usd),
        },
    )

    exit_code = 0
    tick = 0
    try:
        while not stop_event.is_set():
            elapsed = time.monotonic() - started_at
            if max_runtime_seconds is not None and elapsed >= max_runtime_seconds:
                _LOGGER.info(
                    "max runtime reached; stopping",
                    extra={"elapsed_seconds": round(elapsed, 1)},
                )
                break

            tick += 1
            if await _run_one_tick(adapter, engine, live, tick, started_usd):
                exit_code = 1
                break

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=live.tick_seconds)
            except asyncio.TimeoutError:
                pass  # normal — tick interval elapsed
    finally:
        ended_usd = await _session_usd_balance(adapter)
        cancelled, failed = await _cancel_all_open(adapter, tuple(live.symbols))
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
    """Install SIGINT/SIGTERM → set stop_event on Unix. Windows asyncio
    falls back to KeyboardInterrupt at the top level."""

    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def _main_async(config: WobbleBotConfig) -> int:
    if config.live is None:
        _LOGGER.error("settings.yml is missing the `live:` section")
        return 2

    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADE_API_KEY",
            secret_var="KRAKEN_TRADE_API_SECRET",
        )
    except ValueError as exc:
        _LOGGER.error("missing trade credentials", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(config.live.db)
    await storage.connect()
    adapter = KrakenAdapter(config=kraken_config)
    engine = GridEngine(adapter, storage, config.grid, config.safety)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(
            adapter=adapter, engine=engine, live=config.live, stop_event=stop_event
        )
    finally:
        await adapter.aclose()
        await storage.close()


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Translate explicit CLI flags into a YAML override dict."""
    live_overrides = collect_overrides(
        args,
        "live",
        {
            "symbols": ("symbols", parse_symbol_csv),
            "db": ("db", identity),
            "tick_seconds": ("tick_seconds", identity),
            "max_runtime_minutes": ("max_runtime_minutes", identity),
            "max_session_loss_usd": ("max_session_loss_usd", identity),
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

    # Merge all three top-level overlays
    merged: dict[str, Any] = {}
    for layer in (live_overrides, grid_overrides, safety_overrides):
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def main() -> int:
    load_operator_env()
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

    log_format = config.live.log_format if config.live else "plain"
    configure_logging(log_format=log_format)

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    collect_overrides,
    identity,
    load_operator_env,
    parse_symbol_csv,
)
from wobblebot.config.cli import ShadowConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import StorageError, WobbleBotPortError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.reconciler import apply_reconciliation

_LOGGER = logging.getLogger("wobblebot.cli.shadow")


# ---------------------------------------------------------------------------
# Loop helpers — mirror cli/live, consume ShadowConfig directly
# ---------------------------------------------------------------------------


async def _cancel_all_open(
    adapter: ShadowExchangeAdapter,
    storage: StoragePort,
    symbols: tuple[Symbol, ...],
) -> tuple[int, int]:
    """Cancel every open shadow order across all configured symbols.

    After each successful ``adapter.cancel_order()``, persist the
    ``status="canceled"`` transition back to storage (Stage 8.1.B /
    ADR-018). For shadow this is largely cosmetic — the synthetic
    ledger inside ``ShadowExchangeAdapter`` is the ground truth —
    but matching cli/live's discipline keeps the two paths' code
    shape identical and validates the same forensic-audit property
    in the test harness.

    Returns ``(cancelled, failed)``.
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
                continue
            # Stage 8.1.B: persist the status transition so shadow.db's
            # storage view matches what we just did to the synthetic
            # ledger. Matches cli/live's discipline.
            try:
                await storage.save_order(
                    o.model_copy(
                        update={
                            "status": "canceled",
                            "updated_at": Timestamp(dt=datetime.now(UTC)),
                        }
                    )
                )
            except StorageError as exc:
                _LOGGER.error(
                    "shutdown cancel persistence failed; reconciler will catch on next start",
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


async def _shadow_portfolio_value_usd(
    adapter: ShadowExchangeAdapter,
    symbols: tuple[Symbol, ...],
) -> Decimal:
    """USD-denominated mark-to-market portfolio value for the shadow
    ledger. Mirrors ``cli/live._session_portfolio_value_usd`` — same math
    rationale (USD balance alone misreads a BUY fill as a loss), same
    Kraken-prices feed (the shadow adapter uses live prices). See that
    function's docstring for the full why.
    """
    balances = await adapter.get_balances()
    by_asset = {b.asset: b.total for b in balances}
    total = by_asset.get("USD", Decimal("0"))
    bases_seen: set[str] = set()
    for symbol in symbols:
        if symbol.base in bases_seen:
            continue
        bases_seen.add(symbol.base)
        if symbol.quote != "USD":
            _LOGGER.warning(
                "skipping non-USD-quoted symbol in shadow portfolio value",
                extra={"symbol": str(symbol)},
            )
            continue
        base_balance = by_asset.get(symbol.base, Decimal("0"))
        if base_balance <= 0:
            continue
        price = await adapter.get_current_price(symbol)
        total += base_balance * price.amount
    return total


async def _run_one_tick(
    adapter: ShadowExchangeAdapter,
    engine: GridEngine,
    shadow: ShadowConfig,
    tick: int,
    started_value_usd: Decimal,
) -> bool:
    """Mirror cli/live's _run_one_tick — symbols in series, per-symbol
    errors swallowed, post-tick session-loss cap check.
    Returns True when the loss cap tripped."""
    for symbol in shadow.symbols:
        try:
            result = await engine.step(symbol)
            # Per-symbol per-tick output is DEBUG; matches cli/live's
            # discipline so the operator's shadow terminal doesn't
            # flood at the 5s cadence.
            _LOGGER.debug(
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

    # Stage 8.4 hotfix #2: structural consistency with cli/live. Shadow's
    # balance is synthetic (in-memory ledger; no network) so failure here
    # is far less likely than live's, but the defensive pattern matches.
    try:
        current_value_usd = await _shadow_portfolio_value_usd(adapter, tuple(shadow.symbols))
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "post-tick shadow portfolio-value fetch failed; skipping loss-cap check this tick",
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
        )
        return False
    session_pnl = current_value_usd - started_value_usd
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


async def _run_loop(  # pylint: disable=too-many-locals
    adapter: ShadowExchangeAdapter,
    engine: GridEngine,
    shadow: ShadowConfig,
    storage: StoragePort,
    stop_event: asyncio.Event,
) -> int:
    started_usd = await _shadow_usd_balance(adapter)
    started_value_usd = await _shadow_portfolio_value_usd(adapter, tuple(shadow.symbols))
    started_at = time.monotonic()
    # ``None`` means "no runtime cap" — operator opts into indefinite mode.
    # Stage 3.6a matches the LiveConfig optional-runtime shape.
    max_runtime_seconds = (
        shadow.max_runtime_minutes * 60.0 if shadow.max_runtime_minutes is not None else None
    )
    _LOGGER.info(
        "shadow session start",
        extra={
            "symbols": [str(s) for s in shadow.symbols],
            "initial_balances": {a: str(v) for a, v in shadow.initial_balances.items()},
            "tick_seconds": shadow.tick_seconds,
            "max_runtime_seconds": max_runtime_seconds,  # None == unlimited
            "max_session_loss_usd": str(shadow.max_session_loss_usd),
            "starting_usd_synthetic": str(started_usd),
            "starting_value_usd_synthetic": str(started_value_usd),
            "maker_fee_rate": str(shadow.maker_fee_rate),
            "taker_fee_rate": str(shadow.taker_fee_rate),
        },
    )

    exit_code = 0
    tick = 0
    try:
        while not stop_event.is_set():
            elapsed = time.monotonic() - started_at
            if max_runtime_seconds is not None and elapsed >= max_runtime_seconds:
                _LOGGER.info(
                    "shadow max runtime reached; stopping",
                    extra={"elapsed_seconds": round(elapsed, 1)},
                )
                break

            tick += 1
            if await _run_one_tick(adapter, engine, shadow, tick, started_value_usd):
                exit_code = 1
                break

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=shadow.tick_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        # Stage 8.4 hotfix: same structural fix as cli/live.py — each
        # cleanup step gets its own try/except so a failure in one
        # doesn't skip the others. Shadow's balance fetch is synthetic
        # (no network) so the failure surface is narrower than cli/live,
        # but the pattern matches for consistency.
        try:
            ended_usd = await _shadow_usd_balance(adapter)
            ended_value_usd = await _shadow_portfolio_value_usd(adapter, tuple(shadow.symbols))
            ended_known = True
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "shadow session_end balance fetch failed; PnL unavailable",
                extra={"error": str(exc)},
            )
            ended_usd = started_usd
            ended_value_usd = started_value_usd
            ended_known = False
        try:
            cancelled, failed = await _cancel_all_open(adapter, storage, tuple(shadow.symbols))
        except WobbleBotPortError as exc:
            _LOGGER.error(
                "shadow session_end cancel_all_open raised; reconciler will catch",
                extra={"error": str(exc)},
            )
            cancelled, failed = 0, 0
        ending_usd_str = str(ended_usd) if ended_known else "unknown"
        ending_value_str = str(ended_value_usd) if ended_known else "unknown"
        session_pnl_str = str(ended_value_usd - started_value_usd) if ended_known else "unknown"
        _LOGGER.info(
            "shadow session end",
            extra={
                "ticks": tick,
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "starting_usd_synthetic": str(started_usd),
                "ending_usd_synthetic": ending_usd_str,
                "starting_value_usd_synthetic": str(started_value_usd),
                "ending_value_usd_synthetic": ending_value_str,
                "session_pnl_usd_synthetic": session_pnl_str,
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

    # Stage 8.1.C: startup reconciliation per ADR-018. Same shape
    # as cli/live's path; the synthetic ledger is authoritative for
    # shadow.
    configured_symbols = frozenset(s.base.upper() for s in config.shadow.symbols)
    try:
        report = await apply_reconciliation(
            shadow_adapter, storage, configured_symbols=configured_symbols
        )
    except WobbleBotPortError as exc:
        _LOGGER.error(
            "startup reconciliation failed; refusing to start",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    if report.storage_canceled_count or report.orphan_count:
        _LOGGER.info(
            "startup reconciliation complete (shadow)",
            extra={
                "storage_canceled": report.storage_canceled_count,
                "storage_persistence_failures": report.storage_persistence_failures,
                "orphan_count": report.orphan_count,
            },
        )

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(
            adapter=shadow_adapter,
            engine=engine,
            shadow=config.shadow,
            storage=storage,
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

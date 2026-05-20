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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    collect_overrides,
    identity,
    load_operator_env,
    notify,
    parse_symbol_csv,
)
from wobblebot.config.cli import LiveConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import OperatorError, StorageError, WobbleBotPortError
from wobblebot.ports.notifier import NotifierPort
from wobblebot.ports.operator import CommandResult
from wobblebot.ports.storage import StoragePort
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService
from wobblebot.services.reconciler import apply_reconciliation

_LOGGER = logging.getLogger("wobblebot.cli.live")


# ---------------------------------------------------------------------------
# Loop helpers — same shape as before, now consume LiveConfig directly
# ---------------------------------------------------------------------------


async def _cancel_all_open(
    adapter: KrakenAdapter,
    storage: StoragePort,
    symbols: tuple[Symbol, ...],
) -> tuple[int, int]:
    """Cancel every open order across all configured ``symbols``.

    After each successful ``adapter.cancel_order()``, persist the
    ``status="canceled"`` transition back to storage (Stage 8.1.B /
    ADR-018). Storage failures log and continue — losing the audit
    write doesn't undo the cancellation; the next-startup reconciler
    catches stragglers.

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
                continue
            # Stage 8.1.B: persist the status transition so the
            # storage view matches what we just did to the exchange.
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


async def _session_usd_balance(adapter: KrakenAdapter) -> Decimal:
    bal = await adapter.get_balance("USD")
    return bal.total if bal else Decimal("0")


async def _run_one_tick(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    adapter: KrakenAdapter,
    engine: GridEngine,
    live: LiveConfig,
    tick: int,
    started_usd: Decimal,
    notifier: NotifierPort | None = None,
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
            # Stage 5.5: emit a notification on fills so the operator
            # sees activity in Discord without tailing logs.
            if result.fills > 0:
                await notify(
                    notifier,
                    level="info",
                    title=f"Fills: {symbol} ({result.fills})",
                    message=(
                        f"{result.fills} order(s) filled on {symbol}; "
                        f"{result.counters_placed} counter(s) placed."
                    ),
                    context={
                        "symbol": str(symbol),
                        "fills": result.fills,
                        "counters_placed": result.counters_placed,
                        "tick": tick,
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

    # Stage 8.4 hotfix #2 (2026-05-20): wrap the per-tick balance
    # fetch in try/except. e2b6cfc's earlier fix protected the
    # finally-block call site only; this is the OTHER call site —
    # the post-tick loss-cap evaluator. A transient httpx.ReadTimeout
    # to /0/private/BalanceEx should NOT kill the daemon. Skip the
    # cap check for this tick (next tick will retry), log a warning,
    # and treat as "no cap trip" so the loop continues.
    try:
        current_usd = await _session_usd_balance(adapter)
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "post-tick balance fetch failed; skipping loss-cap check this tick",
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
        )
        return False  # No cap trip; loop continues.
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
        await notify(
            notifier,
            level="error",
            title="Loss cap tripped — session ending",
            message=(
                f"Session PnL {session_pnl} exceeded cap "
                f"-{live.max_session_loss_usd} USD; cli/live stopping."
            ),
            context={
                "session_pnl_usd": str(session_pnl),
                "limit": str(live.max_session_loss_usd),
                "tick": tick,
            },
        )
        return True
    return False


async def _process_pending_commands(
    operator_service: OperatorService,
    operator_storage: StoragePort,
) -> int:
    """Drain approved ``pending_commands`` rows; dispatch + mark each.

    **ADR-002 firewall.** This is the only path from a ``PendingCommand``
    to the engine. The ``status='approved'`` filter on the SELECT is the
    confirm-before-execute gate — rows without operator ✅ never reach
    ``OperatorService.dispatch_command``. Per-row failures (engine
    refusal, ``OperatorError``) mark the row ``failed`` and record the
    error message in the result; the loop continues so one bad command
    doesn't starve the others. Returns the number of rows processed.
    """
    approved = await operator_storage.get_pending_commands(status="approved")
    for pending in approved:
        try:
            cmd_result = await operator_service.dispatch_command(pending.command)
            updated = pending.model_copy(
                update={
                    "status": "dispatched",
                    "dispatched_at": Timestamp(dt=datetime.now(UTC)),
                    "result": cmd_result,
                }
            )
        except OperatorError as exc:
            _LOGGER.error(
                "operator command dispatch failed",
                extra={
                    "pending_id": str(pending.id),
                    "command_kind": pending.command.kind,
                    "error": str(exc),
                },
            )
            updated = pending.model_copy(
                update={
                    "status": "failed",
                    "dispatched_at": Timestamp(dt=datetime.now(UTC)),
                    "result": CommandResult(
                        success=False,
                        command_kind=pending.command.kind,
                        message=f"OperatorError: {exc}",
                        executed_at=Timestamp(dt=datetime.now(UTC)),
                    ),
                }
            )
        try:
            await operator_storage.save_pending_command(updated)
        except WobbleBotPortError as exc:
            # Persistence failure here is bad — the operator's confirm
            # already happened, the engine action already ran, but we
            # can't record the outcome. Log and continue; the row stays
            # in 'approved' status and will be re-dispatched next tick.
            # That's an idempotency hazard for non-idempotent commands;
            # acceptable v1 trade-off given how rare DB failures are.
            _LOGGER.error(
                "failed to persist dispatched pending_command",
                extra={"pending_id": str(pending.id), "error": str(exc)},
            )
    return len(approved)


async def _run_loop(  # pylint: disable=too-many-arguments,too-many-locals
    adapter: KrakenAdapter,
    engine: GridEngine,
    live: LiveConfig,
    storage: StoragePort,
    stop_event: asyncio.Event,
    *,
    operator_service: OperatorService | None = None,
    operator_storage: StoragePort | None = None,
    notifier: NotifierPort | None = None,
) -> int:
    """Run the engine loop. Returns the process exit code.

    When ``operator_service`` and ``operator_storage`` are provided,
    each iteration polls ``pending_commands WHERE status='approved'``
    before stepping the engine and exits cleanly when
    ``engine.is_stop_requested`` is set. When ``notifier`` is provided,
    session-start / session-end / fill / cap-trip events emit
    ``Notification`` rows for the cli/operator forwarder (Stage 5.6).
    """
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
    await notify(
        notifier,
        level="info",
        title="Live session started",
        message=(
            f"Trading {len(live.symbols)} symbol(s): "
            f"{', '.join(str(s) for s in live.symbols)}. "
            f"Starting USD={started_usd}."
        ),
        context={
            "symbols": [str(s) for s in live.symbols],
            "tick_seconds": live.tick_seconds,
            "max_runtime_seconds": max_runtime_seconds,
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

            # Operator interaction poll (Stage 5.4): drain approved
            # pending_commands BEFORE the engine tick so an operator
            # PauseCommand takes effect on the current tick.
            if operator_service is not None and operator_storage is not None:
                try:
                    await _process_pending_commands(operator_service, operator_storage)
                except WobbleBotPortError as exc:
                    _LOGGER.error(
                        "pending_commands poll failed; continuing",
                        extra={"error": str(exc)},
                    )

            # Soft-stop honored after the poll so a StopCommand processed
            # this tick exits cleanly without one more engine step.
            if engine.is_stop_requested:
                _LOGGER.info("engine soft stop requested; exiting cleanly")
                break

            tick += 1
            if await _run_one_tick(adapter, engine, live, tick, started_usd, notifier):
                exit_code = 1
                break

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=live.tick_seconds)
            except asyncio.TimeoutError:
                pass  # normal — tick interval elapsed
    finally:
        # Stage 8.4 hotfix: each cleanup step gets its own try/except so a
        # transient failure in one (e.g. DNS down during a power outage
        # firing _session_usd_balance) can't skip the others. Order
        # cancellation is the most safety-critical cleanup; it must run
        # even if the balance fetch craters. Per the runbook §"Hard stop":
        # "cli/live shutdown leaves orders open on Kraken" is a hard stop
        # — surfaced during the 2026-05-19 soak outage where DNS failed
        # mid-finally and three open BUYs were never cancelled.
        try:
            ended_usd = await _session_usd_balance(adapter)
            ended_usd_known = True
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "session_end balance fetch failed; PnL unavailable",
                extra={"error": str(exc)},
            )
            ended_usd = started_usd
            ended_usd_known = False
        try:
            cancelled, failed = await _cancel_all_open(adapter, storage, tuple(live.symbols))
        except WobbleBotPortError as exc:
            _LOGGER.error(
                "session_end cancel_all_open raised; reconciler will catch stragglers",
                extra={"error": str(exc)},
            )
            cancelled, failed = 0, 0
        session_pnl = ended_usd - started_usd if ended_usd_known else Decimal("0")
        duration_seconds = round(time.monotonic() - started_at, 1)
        ending_usd_str = str(ended_usd) if ended_usd_known else "unknown"
        session_pnl_str = str(session_pnl) if ended_usd_known else "unknown"
        _LOGGER.info(
            "session end",
            extra={
                "ticks": tick,
                "duration_seconds": duration_seconds,
                "starting_usd": str(started_usd),
                "ending_usd": ending_usd_str,
                "session_pnl_usd": session_pnl_str,
                "open_orders_cancelled": cancelled,
                "open_orders_cancel_failed": failed,
                "exit_code": exit_code,
            },
        )
        await notify(
            notifier,
            level="error" if exit_code != 0 else "info",
            title=f"Live session ended (exit {exit_code})",
            message=(
                f"{tick} tick(s), {duration_seconds}s runtime. "
                f"PnL {session_pnl_str} USD ({started_usd} -> {ending_usd_str}). "
                f"Cancelled {cancelled} open order(s); {failed} cancel failure(s)."
            ),
            context={
                "ticks": tick,
                "duration_seconds": duration_seconds,
                "starting_usd": str(started_usd),
                "ending_usd": ending_usd_str,
                "session_pnl_usd": session_pnl_str,
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

    # Stage 5.4: optional operator-interaction wiring. When operator_db
    # is set in settings.yml, open it as a second storage adapter and
    # construct an OperatorService; cli/live's loop will poll it.
    # Stage 5.5: same operator_db backs the SqliteNotifierAdapter that
    # cli/live writes outbound events to (cli/operator forwards them
    # to Discord). Both share the same StoragePort connection.
    operator_storage: SQLiteStorageAdapter | None = None
    operator_service: OperatorService | None = None
    notifier: SqliteNotifierAdapter | None = None
    if config.live.operator_db is not None:
        operator_storage = SQLiteStorageAdapter(config.live.operator_db)
        await operator_storage.connect()
        operator_service = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=tuple(config.live.symbols),
            grid_config=config.grid,
            session_started_at=Timestamp(dt=datetime.now(UTC)),
        )
        notifier = SqliteNotifierAdapter(operator_storage)
        _LOGGER.info(
            "operator interaction enabled",
            extra={"operator_db": config.live.operator_db},
        )

    # Stage 8.1.C: startup reconciliation per ADR-018. Run between
    # storage open + adapter construct and engine first tick. Refuses
    # to start if the adapter is unreachable — booting against
    # unreconciled state is what this stage exists to prevent. The
    # configured-symbols filter narrows orphan logging to the engine's
    # actual trade set (operator manual orders on other coins stay
    # silent per stage-8.1-design.md decision 8).
    configured_symbols = frozenset(s.base.upper() for s in config.live.symbols)
    try:
        report = await apply_reconciliation(adapter, storage, configured_symbols=configured_symbols)
    except WobbleBotPortError as exc:
        _LOGGER.error(
            "startup reconciliation failed; refusing to start",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    if report.storage_canceled_count or report.orphan_count:
        _LOGGER.info(
            "startup reconciliation complete",
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
            adapter=adapter,
            engine=engine,
            live=config.live,
            storage=storage,
            stop_event=stop_event,
            operator_service=operator_service,
            operator_storage=operator_storage,
            notifier=notifier,
        )
    finally:
        await adapter.aclose()
        await storage.close()
        if operator_storage is not None:
            await operator_storage.close()


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

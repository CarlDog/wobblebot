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

Loads trade credentials from ``KRAKEN_TRADER_API_KEY`` /
``KRAKEN_TRADER_API_SECRET``.
"""

# pylint: disable=too-many-lines
# One cohesive daemon loop (session lifecycle, per-tick fetch batching,
# dead man's switch, cool-down gate, shutdown cleanup); splitting it would
# fragment a single control flow across files for no organizational gain.

from __future__ import annotations

import argparse
import asyncio
import logging
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
    emit_heartbeat,
    identity,
    install_signal_handlers,
    load_operator_env,
    notify,
    parse_symbol_csv,
    partition_or_exit,
    run_with_clean_exit,
    safe_shutdown,
)
from wobblebot.config.cli import LiveConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Symbol, Ticker, Timestamp
from wobblebot.ports.exceptions import OperatorError, StorageError, WobbleBotPortError
from wobblebot.ports.notifier import NotifierPort
from wobblebot.ports.operator import CommandResult
from wobblebot.ports.storage import StoragePort
from wobblebot.services.cool_down import check_cool_down
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService
from wobblebot.services.reconciler import apply_reconciliation

_LOGGER = logging.getLogger("wobblebot.cli.live")

# How often (in consecutive ticks) to re-emit a "still not confirmed
# armed" WARNING for the dead man's switch, mirroring GridEngine's
# offside transition + heartbeat pattern -- never a WARNING every tick.
_DMS_UNCONFIRMED_SUMMARY_EVERY_TICKS = 240

# ADR-027: pace successive shutdown CancelOrder calls so the cleanup
# path itself can't re-trigger a Kraken rate-limit storm during the
# most safety-critical cleanup (DMS-armed shutdown).
_INTER_CANCEL_PACING_SECONDS = 0.2


# ---------------------------------------------------------------------------
# Loop helpers — same shape as before, now consume LiveConfig directly
# ---------------------------------------------------------------------------


async def _cancel_all_open(
    adapter: KrakenAdapter,
    storage: StoragePort,
    symbols: tuple[Symbol, ...],
) -> tuple[int, int]:
    """Cancel every open order across all configured ``symbols``.

    Fetches the account's open orders with a SINGLE global ``OpenOrders``
    call (not one per symbol — that burst blew Kraken's private-API rate
    limit on a multi-coin shutdown, 2026-06-02), then cancels the orders
    whose symbol is configured. A fetch failure propagates so the caller
    marks the cancel unclean and LEAVES the dead-man's-switch armed
    (ADR-021) — Kraken's server-side timer becomes the backstop that
    sweeps whatever this cleanup couldn't.

    After each successful ``adapter.cancel_order()``, persist the
    ``status="canceled"`` transition back to storage (Stage 8.1.B /
    ADR-018). Storage failures log and continue — losing the audit
    write doesn't undo the cancellation; the next-startup reconciler
    catches stragglers.

    Successive ``cancel_order`` calls are paced (ADR-027) — a short
    sleep between attempts, none before the first — so this cleanup
    path can't itself re-trigger the rate-limit storm the OpenOrders
    batching above already guards against. The underlying Kraken calls
    (``_public_get``/``_private_post``) additionally retry a rate-limit
    rejection with bounded backoff before it ever reaches this
    function as an ``ExchangeError``.

    Returns ``(cancelled, failed)`` summed across symbols.
    """
    cancelled = 0
    failed = 0
    configured = set(symbols)
    opens = await adapter.get_open_orders()
    attempted = 0
    for o in opens:
        if o.symbol not in configured:
            continue
        if attempted > 0:
            # ADR-027 inter-cancel pacing (see module constant docstring).
            await asyncio.sleep(_INTER_CANCEL_PACING_SECONDS)
        attempted += 1
        try:
            await adapter.cancel_order(o)
            cancelled += 1
            _LOGGER.info(
                "shutdown cancelled",
                extra={"symbol": str(o.symbol), "exchange_id": o.exchange_id},
            )
        except WobbleBotPortError as exc:
            failed += 1
            _LOGGER.error(
                "shutdown cancel failed",
                extra={
                    "symbol": str(o.symbol),
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
            _LOGGER.warning(
                "shutdown cancel persistence failed; reconciler will catch on next start",
                extra={
                    "symbol": str(o.symbol),
                    "exchange_id": o.exchange_id,
                    "error": str(exc),
                },
            )
    return cancelled, failed


def _log_dms_confirmation(
    trigger_at: datetime | None, requested_timeout_seconds: int, unconfirmed_ticks: int
) -> int:
    """Log the dead man's switch arm confirmation and return the updated
    consecutive-unconfirmed-ticks counter.

    Confirmed (``trigger_at`` set): DEBUG, matching the per-tick log
    level convention, and the counter resets to 0. Unconfirmed (the
    2026-06-02 soak lesson — Kraken's response carried no real future
    trigger despite the call not raising): transition + heartbeat
    WARNING logging, mirroring GridEngine's offside pattern, rather
    than a WARNING every tick.
    """
    if trigger_at is not None:
        _LOGGER.debug(
            "dead man's switch confirmed armed", extra={"trigger_at": trigger_at.isoformat()}
        )
        return 0
    unconfirmed_ticks += 1
    if unconfirmed_ticks == 1:
        _LOGGER.warning(
            "dead man's switch arm not confirmed by Kraken's response",
            extra={"requested_timeout_seconds": requested_timeout_seconds},
        )
    elif unconfirmed_ticks % _DMS_UNCONFIRMED_SUMMARY_EVERY_TICKS == 0:
        _LOGGER.warning(
            "dead man's switch still not confirmed armed",
            extra={"consecutive_unconfirmed_ticks": unconfirmed_ticks},
        )
    return unconfirmed_ticks


async def _session_usd_balance(adapter: KrakenAdapter) -> Decimal:
    bal = await adapter.get_balance("USD")
    return bal.total if bal else Decimal("0")


async def _session_portfolio_value_usd(
    adapter: KrakenAdapter,
    symbols: tuple[Symbol, ...],
    tickers: dict[Symbol, Ticker] | None = None,
) -> Decimal:
    """USD-denominated mark-to-market portfolio value: USD balance plus
    each configured symbol's base asset valued at its current price.

    Why this exists (Stage 8.4 soak hotfix, 2026-05-22): a BUY fill drops
    USD by the order size, but that's an asset conversion (USD → base),
    not a loss. The session-loss cap used to be checked against
    USD balance alone, which tripped on the first BUY of any session
    whose order_size_usd > max_session_loss_usd. Using mark-to-market
    portfolio value captures realized + unrealized PnL honestly.

    ``tickers``: an optional pre-fetched ``{symbol: Ticker}`` map (v1.1
    backlog "per-tick price-fetch dedup") — when a symbol is present,
    its ``last`` price is used instead of this function issuing its own
    ``get_current_price`` call. Lets ``_run_one_tick``, which already
    fetches a ``Ticker`` per symbol for the engine step, reuse it here
    rather than doubling the per-tick ``/0/public/Ticker`` GETs. ``None``
    (the default) falls back to fetching here — the session-start /
    session-end call sites in ``_run_loop`` run outside any tick's
    pre-fetch and have nothing to share.
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
            # v1: cap is in USD; non-USD quotes would need a cross-rate
            # conversion we don't ship yet. Skip and leave a breadcrumb.
            _LOGGER.warning(
                "skipping non-USD-quoted symbol in portfolio value",
                extra={"symbol": str(symbol)},
            )
            continue
        base_balance = by_asset.get(symbol.base, Decimal("0"))
        if base_balance <= 0:
            continue
        cached = tickers.get(symbol) if tickers is not None else None
        if cached is not None:
            price_amount = cached.last
        else:
            price = await adapter.get_current_price(symbol)
            price_amount = price.amount
        total += base_balance * price_amount
    return total


async def _run_one_tick(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-branches
    adapter: KrakenAdapter,
    engine: GridEngine,
    live: LiveConfig,
    tick: int,
    started_value_usd: Decimal,
    notifier: NotifierPort | None = None,
) -> bool:
    """One tick across every configured symbol + post-tick loss cap
    check. Returns True when the loss cap tripped (caller stops)."""
    # One global OpenOrders fetch per tick, shared across every symbol.
    # Kraken's OpenOrders returns the whole account in a single call; the
    # engine used to fetch per-symbol, so a 5-coin tick fired it 5x and blew
    # the private-API rate limit (EAPI:Rate limit exceeded, 2026-06-02). On a
    # fetch failure, skip this tick's steps rather than let each symbol fall
    # back to its own fetch (which would worsen the rate limit) — the next
    # tick retries; the loss-cap check + DMS ping below still run.
    tick_open_orders: list[Order] | None
    try:
        tick_open_orders = await adapter.get_open_orders()
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "tick open-orders fetch failed; skipping this tick's steps",
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
        )
        tick_open_orders = None

    # One global TradesHistory fetch per tick, shared across every symbol
    # that has a fill candidate this tick (fleet-review #19 finding 8
    # follow-up). TradesHistory is now paginated (up to 20 pages) to find
    # a thin symbol's trades among heavy volume on others; without this
    # consolidation, a tick where several symbols fill simultaneously
    # would page that same account-wide history once per filling symbol —
    # the same rate-limit-storm shape the OpenOrders consolidation above
    # already fixed once (2026-06-02). Checking candidates is pure
    # storage + the already-fetched open-orders snapshot, no network call,
    # so this costs nothing on the (typical) no-fill tick. A shared-fetch
    # failure falls back to each symbol's own per-symbol fetch, same as
    # before this consolidation existed.
    tick_trades: list[Trade] | None = None
    if tick_open_orders is not None:
        needs_trades = False
        for symbol in live.symbols:
            if await engine.has_pending_fill_candidates(symbol, tick_open_orders):
                needs_trades = True
                break
        if needs_trades:
            try:
                tick_trades = await adapter.get_trade_history(limit=200 * len(live.symbols))
            except WobbleBotPortError as exc:
                _LOGGER.warning(
                    "tick trade-history fetch failed; falling back to per-symbol fetch",
                    extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
                )
                tick_trades = None

    # One ticker fetch per symbol per tick, shared between the engine
    # step (spread guard + current price, ADR-025) and the post-tick
    # loss-cap mark-to-market check below (v1.1 backlog "per-tick
    # price-fetch dedup") -- both used to independently call
    # /0/public/Ticker (or get_current_price) for the same symbol in
    # the same tick. A per-symbol fetch failure just leaves that
    # symbol out of the dict; both call sites already fall back to
    # fetching for themselves when a symbol has no cached entry.
    tick_tickers: dict[Symbol, Ticker] = {}
    for symbol in live.symbols:
        try:
            tick_tickers[symbol] = await adapter.get_ticker(symbol)
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "tick ticker fetch failed; symbol will fetch its own price",
                extra={
                    "tick": tick,
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    for symbol in live.symbols:
        if tick_open_orders is None:
            break
        try:
            result = await engine.step(
                symbol,
                exchange_open_orders=tick_open_orders,
                exchange_trades=tick_trades,
                ticker=tick_tickers.get(symbol),
            )
            # Per-symbol per-tick output is DEBUG so the operator's
            # terminal doesn't flood at the 5s default cadence. The
            # actually-interesting events (fills, cap trips, session
            # end) emit at INFO via their own log lines below + the
            # notification pipeline. To re-enable per-tick visibility
            # temporarily, run with WOBBLEBOT_LOG_LEVEL=DEBUG.
            _LOGGER.debug(
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
            _LOGGER.warning(
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
        current_value_usd = await _session_portfolio_value_usd(
            adapter, tuple(live.symbols), tick_tickers
        )
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "post-tick portfolio-value fetch failed; skipping loss-cap check this tick",
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
        )
        return False  # No cap trip; loop continues.
    session_pnl = current_value_usd - started_value_usd
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
    if not approved:
        _LOGGER.debug("no approved pending_commands to process")
        return 0
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
            _LOGGER.warning(
                "failed to persist dispatched pending_command",
                extra={"pending_id": str(pending.id), "error": str(exc)},
            )
    return len(approved)


async def _run_loop(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements,too-many-branches
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
    started_value_usd = await _session_portfolio_value_usd(adapter, tuple(live.symbols))
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
            "starting_value_usd": str(started_value_usd),
        },
    )
    await notify(
        notifier,
        level="info",
        title="Live session started",
        message=(
            f"Trading {len(live.symbols)} symbol(s): "
            f"{', '.join(str(s) for s in live.symbols)}. "
            f"Starting portfolio value={started_value_usd} USD "
            f"(USD balance={started_usd})."
        ),
        context={
            "symbols": [str(s) for s in live.symbols],
            "tick_seconds": live.tick_seconds,
            "max_runtime_seconds": max_runtime_seconds,
            "max_session_loss_usd": str(live.max_session_loss_usd),
            "starting_usd": str(started_usd),
            "starting_value_usd": str(started_value_usd),
        },
    )

    exit_code = 0
    tick = 0
    dms_unconfirmed_ticks = 0
    # Terminal-visible periodic heartbeat (separate from the operator.db
    # daemon_heartbeats row). After the 2026-05-23 logging-audit demoted
    # per-tick "tick complete" from INFO to DEBUG, a long quiet period
    # left the terminal looking dead. Initialize so the FIRST heartbeat
    # fires `terminal_heartbeat_seconds` after session-start, not right
    # at boot (where it'd duplicate the session-start INFO line).
    last_terminal_heartbeat_at = time.monotonic()
    try:
        while not stop_event.is_set():
            # Stage 8.4.E follow-up — emit a heartbeat at the top of
            # each loop iteration so the /health page can prove the
            # daemon's event loop is processing. No-op when
            # operator_storage isn't wired.
            await emit_heartbeat(operator_storage, "cli/live")

            # ADR-021: pet Kraken's server-side dead man's switch every
            # tick (before any order is placed this iteration). If this
            # loop dies — crash, power loss, network partition — Kraken
            # auto-cancels all open orders once the timer lapses, the
            # safety net the finally-block cancel can't provide when the
            # host is gone. Log-and-continue: never crash a tick over the
            # switch, and a failed ping just leaves the timer at its prior
            # (still-protective) value until the next tick re-pings.
            if live.dead_mans_switch_seconds is not None:
                try:
                    trigger_at = await adapter.set_dead_mans_switch(live.dead_mans_switch_seconds)
                    dms_unconfirmed_ticks = _log_dms_confirmation(
                        trigger_at, live.dead_mans_switch_seconds, dms_unconfirmed_ticks
                    )
                except WobbleBotPortError as exc:
                    _LOGGER.warning(
                        "dead man's switch reset failed; continuing (timer retains prior value)",
                        extra={"error": str(exc), "error_type": type(exc).__name__},
                    )

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
                    _LOGGER.warning(
                        "pending_commands poll failed; continuing: %s",
                        exc,
                        extra={"error": str(exc), "error_type": type(exc).__name__},
                    )

            # Soft-stop honored after the poll so a StopCommand processed
            # this tick exits cleanly without one more engine step.
            if engine.is_stop_requested:
                _LOGGER.info("engine soft stop requested; exiting cleanly")
                break

            tick += 1
            if await _run_one_tick(adapter, engine, live, tick, started_value_usd, notifier):
                exit_code = 1
                break

            # Periodic terminal-visible heartbeat. Cheap (just a log
            # line) — no Kraken/Storage calls. Proves the loop is
            # alive without flooding the terminal at tick cadence.
            now = time.monotonic()
            since_heartbeat = now - last_terminal_heartbeat_at
            if since_heartbeat >= live.terminal_heartbeat_seconds:
                session_elapsed_seconds = now - started_at
                _LOGGER.info(
                    "periodic heartbeat: tick %d, elapsed %dh %02dm, symbols %s",
                    tick,
                    int(session_elapsed_seconds // 3600),
                    int((session_elapsed_seconds % 3600) // 60),
                    ",".join(str(s) for s in live.symbols),
                    extra={
                        "tick": tick,
                        "elapsed_seconds": round(session_elapsed_seconds, 1),
                        "symbols": [str(s) for s in live.symbols],
                    },
                )
                last_terminal_heartbeat_at = now

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
            ended_value_usd = await _session_portfolio_value_usd(adapter, tuple(live.symbols))
            ended_known = True
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "session_end balance fetch failed; PnL unavailable",
                extra={"error": str(exc)},
            )
            ended_usd = started_usd
            ended_value_usd = started_value_usd
            ended_known = False
        try:
            cancelled, failed = await _cancel_all_open(adapter, storage, tuple(live.symbols))
            cancel_clean = failed == 0
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "session_end cancel_all_open raised; reconciler will catch stragglers",
                extra={"error": str(exc)},
            )
            cancelled, failed = 0, 0
            cancel_clean = False
        # ADR-021: disarm the dead man's switch ONLY on a confirmed-clean
        # cancel. If our own cancellation failed or raised, deliberately
        # LEAVE it armed so Kraken's timer becomes the backstop that sweeps
        # the stragglers we couldn't — exactly the failure mode it exists
        # for. Disarm == set timeout 0.
        if live.dead_mans_switch_seconds is not None and cancel_clean:
            try:
                await adapter.set_dead_mans_switch(0)
            except WobbleBotPortError as exc:
                _LOGGER.warning(
                    "dead man's switch disarm failed; Kraken timer will lapse harmlessly",
                    extra={"error": str(exc)},
                )
        session_pnl = ended_value_usd - started_value_usd if ended_known else Decimal("0")
        if exit_code == 1:
            # ADR-024: record the trip for the next session's cool-down
            # gate. Own try/except -- a storage failure here must not
            # mask the cap-trip session-end logging/notification below,
            # and the gate itself fails open on a read error, so a
            # failed write here just means the NEXT session isn't gated
            # (same fail-soft posture, not a crash-loop risk).
            try:
                await storage.record_cap_trip(Timestamp(dt=datetime.now(UTC)), session_pnl)
            except StorageError as exc:
                _LOGGER.warning(
                    "failed to record cap trip for cool-down gate",
                    extra={"error": str(exc)},
                )
        duration_seconds = round(time.monotonic() - started_at, 1)
        ending_usd_str = str(ended_usd) if ended_known else "unknown"
        ending_value_str = str(ended_value_usd) if ended_known else "unknown"
        session_pnl_str = str(session_pnl) if ended_known else "unknown"
        _LOGGER.info(
            "session end",
            extra={
                "ticks": tick,
                "duration_seconds": duration_seconds,
                "starting_usd": str(started_usd),
                "ending_usd": ending_usd_str,
                "starting_value_usd": str(started_value_usd),
                "ending_value_usd": ending_value_str,
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
                f"PnL {session_pnl_str} USD "
                f"(value {started_value_usd} -> {ending_value_str}; "
                f"USD {started_usd} -> {ending_usd_str}). "
                f"Cancelled {cancelled} open order(s); {failed} cancel failure(s)."
            ),
            context={
                "ticks": tick,
                "duration_seconds": duration_seconds,
                "starting_usd": str(started_usd),
                "ending_usd": ending_usd_str,
                "starting_value_usd": str(started_value_usd),
                "ending_value_usd": ending_value_str,
                "session_pnl_usd": session_pnl_str,
                "open_orders_cancelled": cancelled,
                "open_orders_cancel_failed": failed,
                "exit_code": exit_code,
            },
        )
    return exit_code


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def _main_async(  # pylint: disable=too-many-locals
    config: WobbleBotConfig, *, ignore_cool_down: bool = False
) -> int:
    if config.live is None:
        _LOGGER.error("settings.yml is missing the `live:` section")
        return 2

    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADER_API_KEY",
            secret_var="KRAKEN_TRADER_API_SECRET",
        )
    except ValueError as exc:
        _LOGGER.error("missing trade credentials", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(config.live.db)
    await storage.connect()

    # ADR-024: refuse to start a new session for a configurable window
    # after the last session-loss-cap exit (exit_code=1) -- an
    # immediate restart (operator knee-jerk, or a `restart:
    # unless-stopped` policy) would otherwise re-enter the same losing
    # condition (the soak's 4:22am cap-trip-then-restart incident).
    # `--ignore-cool-down` is the terminal-only, one-time bypass; it
    # does not clear the record. Fail-OPEN on a storage-read error
    # (log + proceed) -- this is a safety *feature*, not a safety-
    # *critical* invariant, and failing closed would crash-loop under
    # `restart: unless-stopped` (docker rule 6).
    if not ignore_cool_down:
        try:
            last_trip_at = await storage.get_last_cap_trip_at()
        except StorageError as exc:
            _LOGGER.warning(
                "cool-down check failed to read cap_trips; proceeding",
                extra={"error": str(exc)},
            )
            last_trip_at = None
        status = check_cool_down(
            last_trip_at, now=datetime.now(UTC), window_minutes=config.live.cool_down_minutes
        )
        if status.active:
            _LOGGER.error(
                "session-loss-cap cool-down in effect; refusing to start",
                extra={
                    "resumes_at": status.resumes_at.isoformat() if status.resumes_at else None,
                },
            )
            await storage.close()
            return 4

    adapter = KrakenAdapter(config=kraken_config)

    exit_code = await partition_or_exit(
        adapter,
        config.live.symbols,
        logger=_LOGGER,
        cleanups=[
            ("close_kraken_adapter", adapter.aclose),
            ("close_live_storage", storage.close),
        ],
    )
    if exit_code is not None:
        return exit_code

    # Stage 8.1.C: startup reconciliation per ADR-018, extended by
    # ADR-023. Run between storage open + adapter construct and engine
    # first tick — refuses to start if the adapter is unreachable
    # (booting against unreconciled state is what this stage exists to
    # prevent), and now BEFORE engine construction so a recovered fill's
    # order UUID (ADR-023's needs_counter_order_ids) can be threaded
    # into GridEngine as pending_counters. The configured-symbols filter
    # narrows orphan logging to the engine's actual trade set (operator
    # manual orders on other coins stay silent per stage-8.1-design.md
    # decision 8).
    configured_symbols = frozenset(s.base.upper() for s in config.live.symbols)
    try:
        report = await apply_reconciliation(adapter, storage, configured_symbols=configured_symbols)
    except WobbleBotPortError as exc:
        _LOGGER.error(
            "startup reconciliation failed; refusing to start",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    if report.storage_canceled_count or report.orphan_count or report.recovered_fill_count:
        _LOGGER.info(
            "startup reconciliation complete",
            extra={
                "storage_canceled": report.storage_canceled_count,
                "storage_persistence_failures": report.storage_persistence_failures,
                "orphan_count": report.orphan_count,
                "recovered_fill_count": report.recovered_fill_count,
            },
        )

    engine = GridEngine(
        adapter,
        storage,
        config.grid,
        config.safety,
        pending_counters=list(report.needs_counter_order_ids),
    )

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

    stop_event = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop_event, logger=_LOGGER)

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
        # NOTE: this is the OUTER finally — resource close after
        # _run_loop's INNER finally has already done session-end work
        # (balance fetch, cancel_all_open, session-end log, session-end
        # notification). The inner finally is intentionally NOT routed
        # through safe_shutdown because cancel_all_open is the most
        # safety-critical cleanup in the codebase and is already
        # hardened with per-step try/except (e2b6cfc, Stage 8.4 hotfix).
        # Forcing a hard wall-clock cap on it could exit before all
        # Kraken cancels complete — worse than the original problem.
        # Resource closes here are safe to timeout-bound because by the
        # time we reach this point, orders have already been cancelled.
        phases: list[tuple[str, Any]] = [
            ("close_kraken_adapter", adapter.aclose),
            ("close_live_storage", storage.close),
        ]
        if operator_storage is not None:
            phases.append(("close_operator_storage", operator_storage.close))
        await safe_shutdown(phases, logger=_LOGGER)


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
    # ADR-024: terminal-only bypass for one deliberate restart during an
    # active cool-down window. Not YAML-settable -- a Portainer redeploy
    # or a `restart: unless-stopped` policy can't standing-bypass it,
    # and it does NOT clear the recorded trip.
    parser.add_argument(
        "--ignore-cool-down",
        action="store_true",
        help="Bypass the session-loss-cap cool-down window for this one restart (ADR-024).",
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

    log_format = config.live.log_format if config.live else "plain"
    log_file_path = config.live.log_file_path if config.live else None
    configure_logging(log_format=log_format, rotating_file_path=log_file_path)

    run_with_clean_exit(_main_async(config, ignore_cool_down=args.ignore_cool_down), logger=_LOGGER)


if __name__ == "__main__":
    raise SystemExit(main())

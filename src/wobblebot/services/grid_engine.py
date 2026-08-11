"""GridEngine — the per-symbol micro-grid orchestrator.

One ``step(symbol)`` call advances the engine by one tick:

1. Look up the per-coin config; if disabled, return without touching
   the exchange.
2. Read the current market price.
3. Load (or initialize-and-persist) the symbol's :class:`GridState`.
   On first tick, the reference price is anchored to the price observed
   right now, then the initial grid layout is placed in full (subject
   to safety caps).
4. On subsequent ticks, detect fills by diffing the storage's
   open-orders set against the exchange's, then place a counter-order
   one ``spacing`` away on the opposite side for each fill (per ADR-006
   decision 2).
5. If the current price is outside the grid window, log an "offside"
   event but place no new orders (per ADR-006 decision 1, "stay parked").

Safety caps (slice 2.2.4) gate every placement: per-coin order count,
per-coin USD exposure, total USD exposure across all coins, and
committed daily spend on the BUY side. Refusals are logged as events
and counted in :class:`StepResult`; they never raise.

Per-symbol concurrency is gated by an ``asyncio.Lock``: re-entrant
calls for the same symbol serialize, while different symbols can
proceed in parallel (per ADR-006 decision 5).

Reconciliation against orders that exist on the exchange but not in
storage happens once at daemon startup (``services/reconciler.py``,
ADR-018). A storage-only order recovered with a real fill (ADR-023 —
the order left the open set while the daemon was down, either fully or
partially filled before a cancel/expiry) is queued as a
``pending_counters`` UUID at construction time; this engine places the
matching counter-order on the first tick for that symbol, retrying on
a later tick if placement is refused (never discarded — see
:meth:`_place_pending_counters`).
"""

# pylint: disable=too-many-lines
# One cohesive per-symbol tick orchestrator (init, fill detection,
# counters, safety caps, offside, re-layout, sell guard, spread guard);
# splitting it would fragment a single control flow across files for
# no organizational gain -- same posture as ports/storage.py's disable.

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from wobblebot.config.grid import KRAKEN_MAKER_FEE_RATE, CoinGridConfig, GridConfig
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.exceptions import InsufficientBalance
from wobblebot.domain.grid import (
    GridLevel,
    GridState,
    compute_grid_levels,
    grid_spacing,
    is_offside,
    next_counter_action,
)
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import (
    Amount,
    OrderSide,
    Price,
    Symbol,
    Ticker,
    Timestamp,
    fmt_decimal,
)
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.storage import StoragePort
from wobblebot.services.cost_basis import SellGuard
from wobblebot.services.exposure import (
    daily_spend_usd,
    notional_usd,
    total_exposure_usd,
)
from wobblebot.services.reconciler import _resolve_terminal_order

_PlaceOutcome = Literal["placed", "refused", "sell_deferred"]

_LOGGER = logging.getLogger("wobblebot.services.grid_engine")

# How often (in consecutive offside ticks) to emit a "still parked" INFO
# summary while a symbol stays offside. The per-tick offside WARNING was
# demoted to a transition + heartbeat after the 2026-06-02 soak logged it
# every 5s for ~7h straight. 240 ticks ≈ 20 min at the default 5s cadence.
_OFFSIDE_SUMMARY_EVERY_TICKS = 240

# P3 starvation back-off (the 2026-08-09 re-anchor e2e finding): when a
# layout places ZERO orders (BUYs refused for reserved quote balance +
# SELLs cost-basis-deferred), the no-orders self-heal used to re-attempt
# the full layout EVERY tick, silently and forever. Once starved, retry
# only this often — 60 ticks ≈ 5 min at the default 5s cadence, measured
# in the writer's cadence like every other tick constant. Conditions
# that unstarve a symbol (quote balance freed, price back above cost
# basis) change on market timescales; a 5-minute probe is prompt enough
# while cutting the busy-wait ~60x. A PARTIAL layout (placed >= 1) never
# counts as starved — its standing orders make the no-orders check moot.
_STARVED_RETRY_EVERY_TICKS = 60

# v1.1 backlog "boot-time stale-anchor WARN": an anchor persisted this
# long ago has ridden through enough market time that its reference
# price may no longer reflect a sensible regime -- a WARNING (not just
# INFO) when the auto-re-layout uses it, so the operator notices
# without the engine refusing to trade or forcing a re-anchor itself
# (that's the separate, not-yet-built P3 re-anchor command). Detection
# only, per the backlog item's own framing.
_STALE_ANCHOR_AGE = timedelta(hours=24)


StepAction = Literal[
    "initialized", "stepped", "skipped_disabled", "skipped_paused", "skipped_wide_spread"
]


@dataclass(frozen=True)
class StepResult:  # pylint: disable=too-many-instance-attributes
    """Summary of a single ``GridEngine.step`` invocation.

    R0902 disabled: every field is a leaf summary metric the operator or
    test suite consumes by name; bundling sub-fields would just add an
    indirection without clarifying anything.

    ``action`` distinguishes the three outcomes:

    - ``"initialized"`` — first tick for this symbol; the grid was
      anchored and the initial layout placed (subject to safety caps).
      ``placed`` reports orders placed; ``refusals`` reports orders
      blocked by a safety cap.
    - ``"stepped"`` — normal tick. ``fills`` is the count of orders
      detected as filled this tick; ``counters_placed`` is the count
      of counter-orders placed in response; ``refusals`` is the count
      blocked by a safety cap. ``offside`` is the ADR-006 "stay parked"
      signal.
    - ``"skipped_disabled"`` — the per-coin config has ``enabled: false``;
      no exchange or storage interaction occurred.
    - ``"skipped_wide_spread"`` (ADR-025) — the symbol's bid-ask spread
      exceeded ``safety.max_spread_percentage``; no order placed or
      cancelled this tick, storage untouched.

    ``sells_deferred`` (ADR-032) counts SELL placements the cost-basis
    sell guard deferred — deliberately separate from ``refusals``, whose
    hard-cap meaning (``preflight``'s ``refusals != 0 -> exit 1``) must
    stay reserved for the four exposure/spend caps.
    """

    symbol: Symbol
    action: StepAction
    fills: int = 0
    counters_placed: int = 0
    placed: int = 0
    refusals: int = 0
    sells_deferred: int = 0
    offside: bool = False
    trade_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _SafetyDecision:
    """Result of one ``_check_safety`` evaluation. Carried as a value
    so callers can log the refusal reason without re-deriving it."""

    ok: bool
    reason: str | None = None


class GridEngine:  # pylint: disable=too-many-instance-attributes
    """Per-symbol micro-grid engine.

    Stateless across restarts (state lives in storage). The only
    in-memory state is the per-symbol ``asyncio.Lock`` registry, which
    is rebuilt fresh on each instance.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        exchange: ExchangePort,
        storage: StoragePort,
        grid_config: GridConfig,
        safety_config: SafetyConfig,
        pending_counters: list[UUID] | None = None,
    ) -> None:
        self._exchange = exchange
        self._storage = storage
        self._config = grid_config
        self._safety = safety_config
        # ADR-032 cost-basis sell guard. Constructed here (not injected)
        # so every existing GridEngine(...) call site picks it up for
        # free — the maker fee rate is the code-resident constant per
        # the four-homes safety-carve-out (pricing/fees stay code), the
        # tolerance is the one operator-tunable knob.
        self._sell_guard = SellGuard(
            storage,
            max_loss_percentage=safety_config.sell_guard.max_loss_percentage,
            maker_fee_rate=KRAKEN_MAKER_FEE_RATE,
        )
        # ADR-023: order UUIDs the startup reconciler recovered a real
        # fill for (ReconciliationReport.needs_counter_order_ids). Each
        # needs a counter-order placed; a failed placement stays here
        # and retries next tick rather than being discarded (decision 4).
        self._pending_counter_ids: set[UUID] = set(pending_counters or [])
        self._coin_locks: dict[str, asyncio.Lock] = {}
        # Stage 5.4: operator-driven control surface. In-memory state so
        # pause is per-session — a cli/live restart resets it. Keeping it
        # in memory avoids a schema migration and matches the "operator
        # rebuilds context on restart" mental model. Persist to storage
        # later if operators report surprise.
        self._paused_symbols: set[Symbol] = set()
        self._stop_requested = False
        # Per-symbol count of consecutive offside ticks. Drives transition +
        # heartbeat logging (log once on entry, periodic summary while
        # parked, once on recovery) instead of a WARNING every tick. Absent
        # / 0 means the symbol is onside.
        self._offside_ticks: dict[Symbol, int] = {}
        # ADR-025: same transition + heartbeat pattern for consecutive
        # wide-spread-skip ticks.
        self._wide_spread_ticks: dict[Symbol, int] = {}
        # P3 starvation back-off: ticks since a layout placed 0 orders.
        # Absent = not starved. Same transition + heartbeat pattern.
        self._starved_ticks: dict[Symbol, int] = {}

    def _lock_for(self, symbol: Symbol) -> asyncio.Lock:
        key = symbol.base.upper()
        lock = self._coin_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._coin_locks[key] = lock
        return lock

    async def step(
        self,
        symbol: Symbol,
        *,
        exchange_open_orders: list[Order] | None = None,
        exchange_trades: list[Trade] | None = None,
        ticker: Ticker | None = None,
    ) -> StepResult:
        """Advance the engine by one tick for ``symbol``.

        Safe to call concurrently for different symbols; calls for the
        same symbol serialize via per-coin lock.

        ``exchange_open_orders``: an optional whole-account open-orders
        snapshot the caller fetched once this tick. When provided it is
        used for fill detection instead of a per-symbol exchange call,
        collapsing N ``OpenOrders`` calls per multi-symbol tick into one —
        which keeps multi-coin sessions under Kraken's private-API rate
        limit. When ``None`` (single-symbol callers / shadow / tests), fill
        detection fetches per-symbol as before.

        ``exchange_trades``: an optional whole-account trade-history
        snapshot the caller fetched once this tick (fleet-review #19
        finding 8 follow-up), mirroring ``exchange_open_orders`` — a tick
        where several symbols fill simultaneously would otherwise call
        ``TradesHistory`` (now paginated, up to 20 pages) once per filling
        symbol. When provided it is filtered to ``symbol`` instead of a
        per-symbol exchange call. ``None`` falls back to a per-symbol
        fetch (single-symbol callers / shadow / tests / a failed shared
        fetch this tick).

        ``ticker``: an optional pre-fetched :class:`Ticker` for
        ``symbol`` (v1.1 backlog "per-tick price-fetch dedup") — when
        provided, skips this method's own ``get_ticker`` call. Lets a
        caller that also needs the price for something else this tick
        (``cli/live``'s loss-cap mark-to-market check) fetch once and
        thread it through instead of two uncached ``/0/public/Ticker``
        GETs per held symbol. ``None`` falls back to fetching here
        (single-symbol callers / shadow / tests).
        """
        async with self._lock_for(symbol):
            return await self._step_unlocked(symbol, exchange_open_orders, exchange_trades, ticker)

    async def _step_unlocked(
        self,
        symbol: Symbol,
        exchange_open_orders: list[Order] | None = None,
        exchange_trades: list[Trade] | None = None,
        ticker: Ticker | None = None,
    ) -> StepResult:
        coin_cfg = self._config.for_coin(symbol.base)
        if not coin_cfg.enabled:
            return StepResult(symbol=symbol, action="skipped_disabled")
        if self.is_paused(symbol):
            return StepResult(symbol=symbol, action="skipped_paused")

        # ADR-025: bid/ask ride the same market-data call the engine
        # already made for the current price -- zero extra API cost.
        if ticker is None:
            ticker = await self._exchange.get_ticker(symbol)
        current_price = ticker.last
        if self._is_spread_too_wide(symbol, ticker):
            return StepResult(symbol=symbol, action="skipped_wide_spread")

        grid_state = await self._storage.get_grid_state(symbol)
        if grid_state is None:
            return await self._initialize(symbol, current_price, coin_cfg)

        return await self._tick(
            symbol, current_price, grid_state, coin_cfg, exchange_open_orders, exchange_trades
        )

    def _is_spread_too_wide(self, symbol: Symbol, ticker: Ticker) -> bool:
        """ADR-025 pre-tick market-quality gate.

        A market-quality signal, not a per-order safety-cap invariant —
        gates the whole tick rather than a 5th ``_check_safety`` arm, so
        no order is even attempted against a dislocated market. Uses
        the same transition + heartbeat logging pattern as offside
        (ADR-006): one WARNING on entry, a periodic INFO summary while
        it persists, never a WARNING every tick.
        """
        max_spread = self._safety.max_spread_percentage
        if max_spread is None or ticker.spread_percentage <= max_spread:
            if self._wide_spread_ticks.pop(symbol, 0):
                _LOGGER.info(
                    "%s spread back to normal (%s%%); resuming",
                    symbol,
                    fmt_decimal(ticker.spread_percentage, max_significant=4),
                    extra={
                        "symbol": str(symbol),
                        "spread_percentage": str(ticker.spread_percentage),
                    },
                )
            return False

        consecutive = self._wide_spread_ticks.get(symbol, 0) + 1
        self._wide_spread_ticks[symbol] = consecutive
        if consecutive == 1:
            _LOGGER.warning(
                "%s spread too wide (%s%% > %s%% cap; bid %s / ask %s); skipping tick",
                symbol,
                fmt_decimal(ticker.spread_percentage, max_significant=4),
                max_spread,
                ticker.bid,
                ticker.ask,
                extra={
                    "symbol": str(symbol),
                    "spread_percentage": str(ticker.spread_percentage),
                    "max_spread_percentage": str(max_spread),
                    "bid": str(ticker.bid),
                    "ask": str(ticker.ask),
                },
            )
        elif consecutive % _OFFSIDE_SUMMARY_EVERY_TICKS == 0:
            _LOGGER.info(
                "%s still skipping ticks; spread remains wide (%s%%, %d consecutive ticks)",
                symbol,
                fmt_decimal(ticker.spread_percentage, max_significant=4),
                consecutive,
                extra={
                    "symbol": str(symbol),
                    "spread_percentage": str(ticker.spread_percentage),
                    "consecutive_wide_spread_ticks": consecutive,
                },
            )
        return True

    # ------------------------------------------------------------------ operator control (5.4)

    def pause_symbol(self, symbol: Symbol) -> bool:
        """Pause one symbol's grid.

        Subsequent ``step(symbol)`` calls return ``action="skipped_paused"``
        without touching the exchange or storage. Open orders are NOT
        cancelled — that's a separate operator action (``cancel_open_orders``).

        Returns:
            ``True`` if the symbol was active and is now paused;
            ``False`` if it was already paused (idempotent, no-op).
        """
        if symbol in self._paused_symbols:
            return False
        self._paused_symbols.add(symbol)
        _LOGGER.info("%s paused by operator", symbol, extra={"symbol": str(symbol)})
        return True

    def resume_symbol(self, symbol: Symbol) -> bool:
        """Resume one paused symbol's grid.

        Returns:
            ``True`` if the symbol was paused and is now active;
            ``False`` if it was already active (idempotent, no-op).
        """
        if symbol not in self._paused_symbols:
            return False
        self._paused_symbols.discard(symbol)
        _LOGGER.info("%s resumed by operator", symbol, extra={"symbol": str(symbol)})
        return True

    def is_paused(self, symbol: Symbol) -> bool:
        """Return ``True`` if ``symbol`` is currently paused."""
        return symbol in self._paused_symbols

    def paused_symbols(self) -> frozenset[Symbol]:
        """Snapshot of currently paused symbols (immutable copy)."""
        return frozenset(self._paused_symbols)

    def offside_ticks(self, symbol: Symbol) -> int:
        """Consecutive offside ticks for ``symbol``; 0 = onside.

        ADR-030 visibility accessor — cli/live reads this (not
        ``StepResult.offside``, which is ``False`` on every
        non-"stepped" action) when publishing the symbol's
        ``engine_state`` row.
        """
        return self._offside_ticks.get(symbol, 0)

    def request_stop(self) -> None:
        """Set the soft-stop flag.

        ``cli/live`` polls ``is_stop_requested`` between ticks and exits
        cleanly when set. Idempotent — calling repeatedly is fine.
        """
        if not self._stop_requested:
            _LOGGER.info("soft stop requested by operator")
        self._stop_requested = True

    @property
    def is_stop_requested(self) -> bool:
        """``True`` if any operator has called :meth:`request_stop`."""
        return self._stop_requested

    async def cancel_open_orders(self, symbol: Symbol | None = None) -> tuple[int, int]:
        """Cancel every open order on the exchange for ``symbol`` (or all).

        Reads the open-order set from the exchange (authoritative per
        ADR-006 decision 3) rather than from storage, so a stale storage
        view can't strand orders on Kraken. Per-order failures are
        logged and counted; the batch never aborts mid-way.

        Args:
            symbol: Restrict to one symbol; ``None`` cancels across all.

        Returns:
            ``(cancelled, failed)`` counts.

        Raises:
            ExchangeError: If the open-order fetch itself fails. The cancel
                set is then indeterminate, so the error propagates rather
                than collapsing into ``(0, 0)`` — which an operator "cancel
                all" would otherwise read as a false all-clear while orders
                stay live on the exchange. (The per-order loop below still
                logs-and-counts; the batch never aborts mid-way.)
        """
        opens = await self._exchange.get_open_orders(symbol=symbol)
        cancelled = 0
        failed = 0
        for order in opens:
            try:
                canceled_order = await self._exchange.cancel_order(order)
                await self._storage.save_order(canceled_order)
                cancelled += 1
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _LOGGER.warning(
                    "cancel_open_orders: cancel of %s %s @ %s (%s) failed: %s",
                    order.symbol,
                    order.side,
                    fmt_decimal(order.price.amount),
                    order.exchange_id,
                    exc,
                    extra={
                        "symbol": str(order.symbol),
                        "exchange_id": order.exchange_id,
                        "error": str(exc),
                    },
                )
                failed += 1
        _LOGGER.info(
            "cancel_open_orders complete for %s: %d cancelled, %d failed",
            symbol if symbol else "all symbols",
            cancelled,
            failed,
            extra={
                "symbol": str(symbol) if symbol else None,
                "cancelled": cancelled,
                "failed": failed,
            },
        )
        return (cancelled, failed)

    async def request_reanchor(self, symbol: Symbol) -> tuple[bool, str]:
        """Re-center ``symbol``'s grid on the current price (ADR-031).

        Cancel-FIRST, atomically: the symbol's open orders are canceled
        before anything else, and if ANY cancel fails (or the open-order
        fetch itself fails, leaving the cancel set indeterminate) the
        re-anchor aborts with ``(False, msg)`` — a new anchor is NEVER
        saved over still-live orders. Only on a clean cancel does it
        save a fresh ``GridState`` at the current price (built from the
        CURRENT coin config, so a pending config change lands with the
        new anchor), clear the offside counter, auto-resume if paused,
        and place the new layout IN-PROCESS — never via the next-tick
        auto-re-layout gate, whose ``if not offside:`` guard would
        silently park a symbol whose price moved past the fresh band
        (judge correction A).

        Runs under the per-symbol lock; dispatched between ticks by
        cli/live's pending-command poll, so no step is in flight.
        Returns ``(ok, message)`` — the message carries the old → new
        anchor and counts, and becomes the pending_commands audit
        record (``save_grid_state`` is a destructive upsert; this
        message is where the old anchor survives).
        """
        async with self._lock_for(symbol):
            return await self._reanchor_unlocked(symbol)

    async def _reanchor_unlocked(self, symbol: Symbol) -> tuple[bool, str]:
        # pylint: disable=too-many-locals
        # Same rationale as _tick's disable: every local is a distinct
        # stage signal of a linear procedure (price, old anchor, cancel
        # counts, new state, placement tallies); helper-splitting would
        # obscure the cancel-first -> save -> place ordering that IS
        # the safety argument.
        coin_cfg = self._config.for_coin(symbol.base)
        try:
            ticker = await self._exchange.get_ticker(symbol)
        except ExchangeError as exc:
            return (False, f"re-anchor aborted: price fetch failed ({exc}); nothing changed")
        current_price = ticker.last
        old_state = await self._storage.get_grid_state(symbol)
        old_anchor = str(old_state.reference_price) if old_state else "none"
        try:
            cancelled, failed = await self.cancel_open_orders(symbol=symbol)
        except ExchangeError as exc:
            return (
                False,
                f"re-anchor aborted: open-order fetch failed ({exc}); "
                f"orders may still be LIVE and the anchor is unchanged",
            )
        if failed > 0:
            # The ADR's regression pin: never save a new anchor over
            # orders we could not cancel.
            return (
                False,
                f"re-anchor aborted: {failed} cancel(s) failed "
                f"({cancelled} succeeded); orders may still be LIVE and "
                f"the anchor is unchanged",
            )
        state = GridState(
            symbol=symbol,
            reference_price=current_price,
            spacing_percentage=coin_cfg.spacing_percentage,
            levels_above=coin_cfg.levels_above,
            levels_below=coin_cfg.levels_below,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        await self._storage.save_grid_state(state)
        self._offside_ticks.pop(symbol, None)
        resumed = self.resume_symbol(symbol)
        levels = compute_grid_levels(
            reference_price=state.reference_price,
            spacing_percentage=state.spacing_percentage,
            levels_above=state.levels_above,
            levels_below=state.levels_below,
        )
        placed, refusals, sells_deferred = await self._place_layout(symbol, levels, coin_cfg)
        # A 0/N reanchor layout (the original 2026-08-09 incident) enters
        # the starved back-off immediately — no next-tick busy loop.
        self._note_layout_outcome(symbol, placed, refusals, sells_deferred, len(levels))
        message = (
            f"re-anchored {symbol}: {old_anchor} -> {current_price}; "
            f"cancelled {cancelled}, placed {placed}/{len(levels)}"
            f"{f' ({refusals} refused)' if refusals else ''}"
            f"{f' ({sells_deferred} sells deferred)' if sells_deferred else ''}"
            f"{' (auto-resumed)' if resumed else ''}"
        )
        _LOGGER.info(
            "%s",
            message,
            extra={
                "symbol": str(symbol),
                "old_anchor": old_anchor,
                "new_anchor": str(current_price),
                "cancelled": cancelled,
                "placed": placed,
                "refusals": refusals,
                "sells_deferred": sells_deferred,
                "auto_resumed": resumed,
            },
        )
        return (True, message)

    # ------------------------------------------------------------------ initialization

    async def _place_layout(
        self, symbol: Symbol, levels: list[GridLevel], coin_cfg: CoinGridConfig
    ) -> tuple[int, int, int]:
        """Place every level of a layout; returns (placed, refusals, sells_deferred).

        The one placement loop shared by ``_initialize``, the auto
        re-layout branch, and ``request_reanchor`` — three call sites
        with an identical per-outcome tally that would otherwise drift.
        """
        placed = 0
        refusals = 0
        sells_deferred = 0
        for level in levels:
            outcome = await self._try_place(symbol, level, coin_cfg)
            if outcome == "placed":
                placed += 1
            elif outcome == "sell_deferred":
                sells_deferred += 1
            else:
                refusals += 1
        return (placed, refusals, sells_deferred)

    def _starved_should_attempt(self, symbol: Symbol) -> bool:
        """Gate a no-orders re-layout attempt through the back-off.

        Not starved: always attempt. Starved: count the tick and allow
        an attempt only every ``_STARVED_RETRY_EVERY_TICKS``, with the
        standard periodic heartbeat in between — never a retry (or a
        log line) every tick.
        """
        ticks = self._starved_ticks.get(symbol)
        if ticks is None:
            return True
        ticks += 1
        self._starved_ticks[symbol] = ticks
        if ticks % _STARVED_RETRY_EVERY_TICKS == 0:
            return True
        if ticks % _OFFSIDE_SUMMARY_EVERY_TICKS == 0:
            _LOGGER.info(
                "%s still starved (no orders placeable); parked %d ticks, retrying every %d",
                symbol,
                ticks,
                _STARVED_RETRY_EVERY_TICKS,
                extra={"symbol": str(symbol), "consecutive_starved_ticks": ticks},
            )
        return False

    def _note_layout_outcome(
        self, symbol: Symbol, placed: int, refusals: int, sells_deferred: int, target: int
    ) -> None:
        """Enter/clear the starved state from a layout's outcome.

        Zero placed out of a non-empty target = starved: ONE WARNING
        with the refusal/deferral breakdown (the operator learns once,
        and the command-result echo / logs carry the same counts), then
        the back-off in :meth:`_starved_should_attempt` owns the
        cadence. Any placement clears the state — standing orders make
        the no-orders self-heal moot.
        """
        if placed > 0 or target == 0:
            if self._starved_ticks.pop(symbol, None) is not None:
                _LOGGER.info(
                    "%s recovered from starvation: placed %d/%d",
                    symbol,
                    placed,
                    target,
                    extra={"symbol": str(symbol), "levels_placed": placed, "target_levels": target},
                )
            return
        if symbol not in self._starved_ticks:
            self._starved_ticks[symbol] = 1
            _LOGGER.warning(
                "%s layout starved: placed 0/%d (%d refused, %d sells deferred); "
                "backing off — retrying every %d ticks instead of every tick",
                symbol,
                target,
                refusals,
                sells_deferred,
                _STARVED_RETRY_EVERY_TICKS,
                extra={
                    "symbol": str(symbol),
                    "target_levels": target,
                    "refusals": refusals,
                    "sells_deferred": sells_deferred,
                },
            )

    async def _initialize(
        self,
        symbol: Symbol,
        current_price: Decimal,
        coin_cfg: CoinGridConfig,
    ) -> StepResult:
        state = GridState(
            symbol=symbol,
            reference_price=current_price,
            spacing_percentage=coin_cfg.spacing_percentage,
            levels_above=coin_cfg.levels_above,
            levels_below=coin_cfg.levels_below,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        await self._storage.save_grid_state(state)
        levels = compute_grid_levels(
            reference_price=state.reference_price,
            spacing_percentage=state.spacing_percentage,
            levels_above=state.levels_above,
            levels_below=state.levels_below,
        )
        placed, refusals, sells_deferred = await self._place_layout(symbol, levels, coin_cfg)
        self._note_layout_outcome(symbol, placed, refusals, sells_deferred, len(levels))
        _LOGGER.info(
            "grid initialized for %s: anchor %s, placed %d/%d%s%s",
            symbol,
            fmt_decimal(state.reference_price),
            placed,
            len(levels),
            f" ({refusals} refused)" if refusals else "",
            f" ({sells_deferred} sells deferred)" if sells_deferred else "",
            extra={
                "symbol": str(symbol),
                "reference_price": str(state.reference_price),
                "target_levels": len(levels),
                "levels_placed": placed,
                "refusals": refusals,
                "sells_deferred": sells_deferred,
            },
        )
        return StepResult(
            symbol=symbol,
            action="initialized",
            placed=placed,
            refusals=refusals,
            sells_deferred=sells_deferred,
        )

    # ------------------------------------------------------------------ subsequent ticks

    async def _tick(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments,too-many-positional-arguments,too-many-statements
        # R0914 disable: every local here represents a distinct
        # tick-stage signal (levels, offside, fills, trade_ids,
        # counters_placed, refusals, spacing, target, counter_amount,
        # placed_ok, ...). Splitting into helpers would obscure the
        # tick's linear flow without removing complexity.
        self,
        symbol: Symbol,
        current_price: Decimal,
        state: GridState,
        coin_cfg: CoinGridConfig,
        exchange_open_orders: list[Order] | None = None,
        exchange_trades: list[Trade] | None = None,
    ) -> StepResult:
        levels = compute_grid_levels(
            reference_price=state.reference_price,
            spacing_percentage=state.spacing_percentage,
            levels_above=state.levels_above,
            levels_below=state.levels_below,
        )
        offside = is_offside(current_price, levels)

        fills, trade_ids = await self._detect_fills(symbol, exchange_open_orders, exchange_trades)
        counters_placed = 0
        refusals = 0
        placed = 0
        sells_deferred = 0
        if not offside:
            spacing = grid_spacing(state.reference_price, state.spacing_percentage)

            # ADR-023: place any counters the startup reconciler queued
            # for this symbol before anything else this tick, so the
            # auto-re-layout guard below sees them as already-open and
            # doesn't spuriously re-place the full grid on top.
            if self._pending_counter_ids:
                pc_placed, pc_refusals, pc_deferred = await self._place_pending_counters(
                    symbol, spacing, coin_cfg, grid_ceiling=levels[-1].price
                )
                placed += pc_placed
                refusals += pc_refusals
                sells_deferred += pc_deferred

            for filled in fills:
                target = next_counter_action(
                    filled.side,
                    filled.price.amount,
                    spacing,
                    counter_target_mode=coin_cfg.counter_target_mode,
                    grid_ceiling=levels[-1].price,
                )
                # Per ADR-006 decision 2 the counter is sized to the filled
                # portion, not re-derived from order_size_usd. This keeps
                # cycles base-amount-balanced — without it, each cycle's
                # SELL would be sized in USD at the higher counter price,
                # so the BUY/SELL BTC amounts would mismatch and the
                # cycle would slowly accumulate or shed inventory and
                # bleed value through the spread.
                counter_amount = Amount(value=filled.filled_amount, asset=filled.amount.asset)
                outcome = await self._try_place(symbol, target, coin_cfg, amount=counter_amount)
                if outcome == "placed":
                    counters_placed += 1
                elif outcome == "sell_deferred":
                    sells_deferred += 1
                else:
                    refusals += 1

            # Stage 8.4.E follow-up 2026-05-22 — auto re-layout when
            # storage shows no open orders for this symbol. Triggered
            # after the session-loss-cap-trip + restart scenario stranded
            # the engine: grid_state existed, no orders did, and _tick
            # had nothing to do. Now the engine re-places the layout at
            # the EXISTING anchor (operators set anchors deliberately;
            # we respect that decision and just re-instantiate the
            # orders below them).
            #
            # Guards: skipped while offside (the grid is parked there),
            # and only fires after fill detection so a regular tick with
            # fills doesn't trigger spurious re-layouts on the way to
            # placing counters.
            remaining_open = await self._storage.get_open_orders(symbol=symbol)
            if remaining_open:
                # Orders exist again by any path — starvation (if any) is over.
                self._starved_ticks.pop(symbol, None)
            if not remaining_open and self._starved_should_attempt(symbol):
                anchor_age = datetime.now(UTC) - state.created_at.dt
                drift_percentage = (
                    abs(current_price - state.reference_price) / state.reference_price
                ) * Decimal("100")
                log_extra = {
                    "symbol": str(symbol),
                    "reference_price": str(state.reference_price),
                    "current_price": str(current_price),
                    "level_count": len(levels),
                    "anchor_age_hours": round(anchor_age.total_seconds() / 3600, 1),
                    "drift_percentage": str(drift_percentage),
                }
                if anchor_age >= _STALE_ANCHOR_AGE:
                    # Detect-only (per the backlog item): still re-lays out
                    # normally. A stale anchor that still brackets price
                    # passes silently otherwise -- the operator-initiated
                    # re-anchor command (separate, not-yet-built P3 item)
                    # is the fix flow this warning points at.
                    _LOGGER.warning(
                        "%s has no open orders; re-laying out grid at a stale anchor",
                        symbol,
                        extra=log_extra,
                    )
                else:
                    _LOGGER.info(
                        "%s has no open orders; re-laying out grid at existing anchor",
                        symbol,
                        extra=log_extra,
                    )
                relayout_placed, relayout_refusals, relayout_deferred = await self._place_layout(
                    symbol, levels, coin_cfg
                )
                placed += relayout_placed
                refusals += relayout_refusals
                sells_deferred += relayout_deferred
                self._note_layout_outcome(
                    symbol, relayout_placed, relayout_refusals, relayout_deferred, len(levels)
                )
                # v1.1 backlog "partial-grid placement WARN -> INFO": the
                # placed-vs-target summary an operator should look at,
                # now that per-level insufficient-balance refusals log
                # at DEBUG instead of WARN (see _try_place).
                _LOGGER.info(
                    "grid re-layout complete for %s: placed %d/%d%s%s",
                    symbol,
                    relayout_placed,
                    len(levels),
                    f" ({relayout_refusals} refused)" if relayout_refusals else "",
                    f" ({relayout_deferred} sells deferred)" if relayout_deferred else "",
                    extra={
                        "symbol": str(symbol),
                        "target_levels": len(levels),
                        "levels_placed": relayout_placed,
                        "refusals": relayout_refusals,
                        "sells_deferred": relayout_deferred,
                    },
                )
        elif fills:
            _LOGGER.warning(
                "%d fill(s) on %s detected while offside; counters suppressed",
                len(fills),
                symbol,
                extra={
                    "symbol": str(symbol),
                    "current_price": str(current_price),
                    "fills": len(fills),
                },
            )

        if offside:
            # Transition + heartbeat logging. A sustained downtrend can keep
            # the grid parked for hours; emit ONE WARNING when it goes
            # offside, then only a periodic INFO summary — never a WARNING
            # every tick (the 2026-06-02 soak logged this every 5s for ~7h).
            consecutive = self._offside_ticks.get(symbol, 0) + 1
            self._offside_ticks[symbol] = consecutive
            if consecutive == 1:
                _LOGGER.warning(
                    "%s offside at %s (band %s - %s); parking until price returns",
                    symbol,
                    fmt_decimal(current_price),
                    fmt_decimal(levels[0].price) if levels else "?",
                    fmt_decimal(levels[-1].price) if levels else "?",
                    extra={
                        "symbol": str(symbol),
                        "current_price": str(current_price),
                        "lowest_level": str(levels[0].price) if levels else None,
                        "highest_level": str(levels[-1].price) if levels else None,
                    },
                )
            elif consecutive % _OFFSIDE_SUMMARY_EVERY_TICKS == 0:
                _LOGGER.info(
                    "%s still offside at %s; parked (%d consecutive ticks)",
                    symbol,
                    fmt_decimal(current_price),
                    consecutive,
                    extra={
                        "symbol": str(symbol),
                        "current_price": str(current_price),
                        "consecutive_offside_ticks": consecutive,
                    },
                )
        elif self._offside_ticks.pop(symbol, 0):
            _LOGGER.info(
                "%s back onside at %s; resuming",
                symbol,
                fmt_decimal(current_price),
                extra={"symbol": str(symbol), "current_price": str(current_price)},
            )

        return StepResult(
            symbol=symbol,
            action="stepped",
            fills=len(fills),
            counters_placed=counters_placed,
            placed=placed,
            refusals=refusals,
            sells_deferred=sells_deferred,
            offside=offside,
            trade_ids=trade_ids,
        )

    async def _place_pending_counters(
        self,
        symbol: Symbol,
        spacing: Decimal,
        coin_cfg: CoinGridConfig,
        *,
        grid_ceiling: Decimal,
    ) -> tuple[int, int, int]:
        """Place counter-orders queued by startup reconciliation (ADR-023).

        Each pending UUID names a storage order the reconciler recovered
        a real fill for (fully or partially, before a cancel/expiry) while
        this daemon was down. Orders for a *different* symbol are left
        untouched — they'll place on that symbol's own tick. A placement
        that's refused or sell-guard-deferred stays in the pending set and
        retries next tick (decision 4: discarding it would let the
        auto-re-layout guard re-place a full grid with no counter,
        reproducing the very orphan this recovers).

        Returns ``(placed, refusals, sells_deferred)``.
        """
        placed = refusals = sells_deferred = 0
        for order_id in list(self._pending_counter_ids):
            order = await self._storage.get_order(order_id)
            if order is None:
                _LOGGER.error(
                    "pending recovery counter references missing storage order %s; dropping",
                    order_id,
                    extra={"order_id": str(order_id)},
                )
                self._pending_counter_ids.discard(order_id)
                continue
            if order.symbol != symbol:
                continue
            target = next_counter_action(
                order.side,
                order.price.amount,
                spacing,
                counter_target_mode=coin_cfg.counter_target_mode,
                grid_ceiling=grid_ceiling,
            )
            counter_amount = Amount(value=order.filled_amount, asset=order.amount.asset)
            outcome = await self._try_place(symbol, target, coin_cfg, amount=counter_amount)
            if outcome == "placed":
                placed += 1
                self._pending_counter_ids.discard(order_id)
            elif outcome == "sell_deferred":
                sells_deferred += 1
            else:
                refusals += 1
        return placed, refusals, sells_deferred

    # ------------------------------------------------------------------ helpers

    async def _fill_candidates(
        self, symbol: Symbol, exchange_open_orders: list[Order] | None
    ) -> list[Order]:
        """Storage-open orders no longer confirmed live on the exchange.

        Pure diff (one storage read +, absent a shared snapshot, one
        exchange read) — no trade-history call. Split out from
        ``_detect_fills`` so ``has_pending_fill_candidates`` can answer
        "does this symbol need trade history this tick" without a
        network round-trip to ``TradesHistory``.

        ``exchange_open_orders``: when provided (a whole-account snapshot
        the caller fetched once this tick), it is filtered to ``symbol``
        instead of issuing a per-symbol ``OpenOrders`` call. The adapter's
        per-symbol fetch already pulls the whole account and filters
        client-side, so the filtered snapshot is identical — this just
        avoids re-fetching once per symbol and is what keeps multi-coin
        sessions under Kraken's private-API rate limit. ``None`` falls back
        to a per-symbol fetch (single-symbol callers / shadow / tests).
        """
        stored_open = await self._storage.get_open_orders(symbol=symbol)
        if exchange_open_orders is None:
            exchange_open = await self._exchange.get_open_orders(symbol=symbol)
        else:
            exchange_open = [o for o in exchange_open_orders if o.symbol == symbol]
        live_ids = {o.exchange_id for o in exchange_open if o.exchange_id}
        return [o for o in stored_open if o.exchange_id and o.exchange_id not in live_ids]

    async def has_pending_fill_candidates(
        self, symbol: Symbol, exchange_open_orders: list[Order] | None = None
    ) -> bool:
        """``True`` if ``symbol`` has a storage-open order not confirmed
        live on the exchange — i.e. this tick will need trade history to
        resolve a possible fill.

        Callers juggling several symbols in one tick (``cli/live``) use
        this to decide, before running any symbol's ``step``, whether a
        single shared ``TradesHistory`` fetch is worth making this tick —
        mirroring the ``exchange_open_orders`` consolidation so a tick
        with several simultaneous fills doesn't call ``TradesHistory``
        (now paginated, up to 20 pages) once per filling symbol
        (fleet-review #19 finding 8 follow-up).
        """
        candidates = await self._fill_candidates(symbol, exchange_open_orders)
        return bool(candidates)

    async def _detect_fills(
        self,
        symbol: Symbol,
        exchange_open_orders: list[Order] | None = None,
        exchange_trades: list[Trade] | None = None,
    ) -> tuple[list[Order], list[str]]:
        """Diff storage's open set against the exchange's; record fills.

        Returns ``(filled_orders, saved_trade_ids)`` — the orders that
        transitioned out of the open set this tick (status refreshed
        from the exchange) and the trade IDs persisted as a result.

        ``exchange_open_orders``: see :meth:`_fill_candidates`.

        ``exchange_trades``: when provided (a whole-account trade-history
        snapshot the caller fetched once this tick), it is filtered to
        ``symbol`` instead of issuing a per-symbol ``TradesHistory`` call.
        ``None`` falls back to a per-symbol fetch (single-symbol callers /
        shadow / tests / a failed shared fetch this tick).

        Each candidate is resolved via the shared
        ``services.reconciler._resolve_terminal_order`` (ADR-023): an
        order with ``filled_amount > 0`` is treated as filled whether it
        refreshed to ``closed`` (full fill) or ``canceled``/``expired``
        (a partial fill before the cancel/expiry, the "F1" case a plain
        ``status == "closed"`` check used to silently drop). A clean
        cancel/expire (``filled_amount == 0``) is saved with its real
        status but produces no trade and no counter.
        """
        candidates = await self._fill_candidates(symbol, exchange_open_orders)
        if not candidates:
            return [], []

        # Fetch trade history once and index by exchange_id; cheaper than
        # one round-trip per fill, and sufficient for Stage 2.2 volumes.
        if exchange_trades is None:
            recent_trades = await self._exchange.get_trade_history(symbol=symbol, limit=200)
        else:
            recent_trades = [t for t in exchange_trades if t.symbol == symbol]
        trades_by_order: dict[str, list[Trade]] = {}
        for trade in recent_trades:
            trades_by_order.setdefault(trade.order_id, []).append(trade)

        filled: list[Order] = []
        saved_trade_ids: list[str] = []
        for candidate in candidates:
            resolution = await _resolve_terminal_order(self._exchange, candidate, trades_by_order)
            await self._storage.save_order(resolution.order)
            if resolution.needs_counter:
                filled.append(resolution.order)
                for trade in resolution.trades:
                    await self._storage.save_trade(trade)
                    saved_trade_ids.append(trade.id)
                if resolution.trades:
                    # ADR-032: a newly-saved trade changes this symbol's
                    # cost basis, so the sell guard must not reuse a
                    # cache computed before it.
                    self._sell_guard.invalidate(symbol)
                _LOGGER.info(
                    "grid fill: %s %s %s @ %s",
                    symbol,
                    resolution.order.side.value.upper(),
                    fmt_decimal(resolution.order.filled_amount),
                    fmt_decimal(resolution.order.price.amount),
                    extra={
                        "symbol": str(symbol),
                        "side": resolution.order.side.value,
                        "price": str(resolution.order.price.amount),
                        "amount": str(resolution.order.filled_amount),
                        "exchange_id": resolution.order.exchange_id,
                        "terminal_status": resolution.order.status,
                    },
                )
        return filled, saved_trade_ids

    async def _try_place(
        self,
        symbol: Symbol,
        level: GridLevel,
        coin_cfg: CoinGridConfig,
        amount: Amount | None = None,
    ) -> _PlaceOutcome:
        """Run safety checks then place. Refusals/deferrals are logged
        and never raise.

        ``amount`` overrides the default USD-budget-derived sizing — used
        for counter orders, which must match the filled order's base
        amount (ADR-006 decision 2).

        Three non-placed outcomes:
        - ``"refused"`` — an internal safety cap (``_check_safety``
          returns ok=False), an exchange-side ``InsufficientBalance``
          (Kraken's ``EOrder:Insufficient funds``, routine on the SELL
          side before any cycle has produced base inventory), or a
          generic ``ExchangeError`` -- notably Kraken's client-side
          ``ordermin``/``costmin`` rejection (a fixed ``order_size_usd``
          ÷ a risen price can slide the computed volume under a pair's
          fixed-quantity minimum, e.g. DOGE's 50-unit ordermin at low
          prices). Before this was caught here it propagated out of
          ``_try_place`` uncaught, aborting every remaining level in
          the same layout/re-layout loop instead of just the one
          doomed order.
        - ``"sell_deferred"`` (ADR-032) — the cost-basis sell guard
          deferred a SELL priced enough below the symbol's average cost
          to realize a loss beyond tolerance. Deliberately distinct from
          a refusal: it never counts toward a hard-cap exit code.

        Storage is fully up-to-date between successive ``_try_place``
        calls within one ``step`` (the per-symbol lock prevents
        concurrent step calls; ``save_order`` commits before the next
        iteration begins). So each safety check sees the cumulative
        result of every prior placement in the same tick — no
        in-memory delta tracking needed.
        """
        decision = await self._check_safety(symbol, level, coin_cfg)
        if not decision.ok:
            _LOGGER.warning(
                "%s %s @ %s refused by safety cap: %s",
                symbol,
                level.side.value.upper(),
                fmt_decimal(level.price),
                decision.reason,
                extra={
                    "symbol": str(symbol),
                    "side": level.side.value,
                    "price": str(level.price),
                    "reason": decision.reason,
                },
            )
            return "refused"

        if level.side is OrderSide.SELL and self._safety.sell_guard.enabled:
            assessment = await self._sell_guard.assess(symbol, level.price)
            if not assessment.allowed:
                return "sell_deferred"

        try:
            await self._place_level(symbol, level, coin_cfg, amount=amount)
        except InsufficientBalance as exc:
            # v1.1 backlog "partial-grid placement WARN -> INFO": a
            # per-level insufficient-balance refusal is routine, not a
            # genuine problem -- expected on the SELL side before a
            # cycle has produced base inventory, and on the BUY side
            # whenever the account can't fund the full configured
            # layout. DEBUG here; the placed-vs-target INFO summary
            # (_initialize / the auto-re-layout branch below) is where
            # an operator should look. Reserve WARN for the safety-cap
            # and generic-exchange-error refusals below, which mean
            # something is actually wrong.
            _LOGGER.debug(
                "%s %s @ %s refused: insufficient %s (need %s, have %s)",
                symbol,
                level.side.value.upper(),
                fmt_decimal(level.price),
                exc.asset,
                exc.required,
                exc.available,
                extra={
                    "symbol": str(symbol),
                    "side": level.side.value,
                    "price": str(level.price),
                    "asset": exc.asset,
                    "required": str(exc.required),
                    "available": str(exc.available),
                },
            )
            return "refused"
        except ExchangeError as exc:
            # Kraken's client-side ordermin/costmin rejection (and any
            # other exchange-side refusal) lands here -- must not
            # propagate, or it aborts every remaining level in the same
            # layout/re-layout loop instead of skipping just this one.
            _LOGGER.warning(
                "%s %s @ %s refused by exchange: %s",
                symbol,
                level.side.value.upper(),
                fmt_decimal(level.price),
                exc,
                extra={
                    "symbol": str(symbol),
                    "side": level.side.value,
                    "price": str(level.price),
                    "error": str(exc),
                },
            )
            return "refused"
        return "placed"

    async def _place_level(
        self,
        symbol: Symbol,
        level: GridLevel,
        coin_cfg: CoinGridConfig,
        amount: Amount | None = None,
    ) -> None:
        """Build, place, and persist a single limit order at ``level``.

        Default sizing: amount in base currency = ``order_size_usd /
        level.price``, treating the configured size as a quote-currency
        budget per order (matches the YAML's ``order_size_usd``
        semantics). Pass an explicit ``amount`` to override — counter
        orders use this to match the filled order's base amount so
        cycles balance.
        """
        if amount is None:
            amount = Amount(
                value=coin_cfg.order_size_usd / level.price,
                asset=symbol.base,
            )
        order = Order(
            symbol=symbol,
            side=level.side,
            price=Price(amount=level.price, currency=symbol.quote),
            amount=amount,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        placed = await self._exchange.place_order(order)
        await self._storage.save_order(placed)

    # ------------------------------------------------------------------ safety caps

    async def _check_safety(
        self,
        symbol: Symbol,
        level: GridLevel,
        coin_cfg: CoinGridConfig,
    ) -> _SafetyDecision:
        """Evaluate all four safety caps for a proposed order.

        ``proposed`` is ``coin_cfg.order_size_usd`` — the configured
        per-order USD budget. Existing-order sums use
        ``price.amount * amount.value``, which equals ``order_size_usd``
        modulo Decimal-division rounding (acceptable: cap thresholds
        are operator-set in whole dollars, far above any rounding
        artifact).
        """
        proposed = coin_cfg.order_size_usd
        cap = self._safety

        coin_open = await self._storage.get_open_orders(symbol=symbol)
        if len(coin_open) + 1 > cap.max_orders_per_coin:
            return _SafetyDecision(ok=False, reason="max_orders_per_coin")

        if notional_usd(coin_open) + proposed > cap.max_per_coin_exposure_usd:
            return _SafetyDecision(ok=False, reason="max_per_coin_exposure_usd")

        if await total_exposure_usd(self._storage) + proposed > cap.max_total_exposure_usd:
            return _SafetyDecision(ok=False, reason="max_total_exposure_usd")

        if level.side is OrderSide.BUY:
            # Committed-funds-only rule (canceled/expired BUYs excluded)
            # lives in services.exposure so the risk advisor reports the
            # same headroom this cap enforces. See that module for the
            # 2026-05-22 incident behind it.
            if await daily_spend_usd(self._storage) + proposed > cap.max_daily_spend_usd:
                return _SafetyDecision(ok=False, reason="max_daily_spend_usd")

        return _SafetyDecision(ok=True)

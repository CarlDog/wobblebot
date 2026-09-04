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
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from wobblebot.config.grid import (
    KRAKEN_MAKER_FEE_RATE,
    KRAKEN_TAKER_FEE_RATE,
    CoinGridConfig,
    GridConfig,
)
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
    buy_notional_usd,
    coin_inventory_cost_usd,
    daily_spend_usd,
    notional_usd,
    total_exposure_usd,
    total_inventory_cost_usd,
)
from wobblebot.services.grid_starvation import (
    REASON_EXCHANGE_ERROR,
    REASON_INSUFFICIENT_BALANCE,
    LayoutOutcome,
    StarvationState,
    describe_reasons,
)
from wobblebot.services.reconciler import _resolve_terminal_order

_PlaceOutcome = Literal["placed", "refused", "sell_deferred"]
"""What one level attempt did.

``_try_place`` returns this paired with a reason string, because all
THREE of its refusal paths return ``"refused"`` -- the outcome alone
cannot say whether a level was blocked by a safety cap, by free balance,
or by the exchange's own minimum (2026-09-03).
"""

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
# without the engine refusing to trade or forcing a re-anchor itself.
# The fix flow it points at is the operator-initiated re-anchor (ADR-031),
# which SHIPPED 2026-08-09 as `reanchor <SYMBOL>` in Discord and the
# per-symbol anchor button on the dashboard. This comment claimed it was
# "not yet built" for weeks after it was; corrected 2026-09-03.
#
# Detection only, per the backlog item's own framing. Note it compares the
# age of the persisted GridState row, not of the process, so a restart does
# not reset it -- and a STARVED symbol can never refresh its own anchor, so
# once past the threshold the warning is permanently true and repeats on
# every retry. That is why it drops to INFO while starved.
_STALE_ANCHOR_AGE = timedelta(hours=24)

# How often (in RETRIES, not ticks) to emit the still-starved summary. The
# nominal arithmetic is 12 x 60 ticks x 5s = one line an hour per starved
# symbol; measured on the live container 2026-09-03 the retry cadence is
# ~5m51s rather than 5m, because a tick overruns its budget, so it is closer
# to one line every 70 minutes. Either way the point is the order of
# magnitude, not the exact period. It MUST be counted in retries: the previous heartbeat counted
# ticks against _OFFSIDE_SUMMARY_EVERY_TICKS (240) while the retry gate
# counted the same ticks against 60, and since 240 is an exact multiple of
# 60 the retry branch always matched first and returned. That heartbeat was
# unreachable from the day it was written and never once fired.
_STARVED_SUMMARY_EVERY_RETRIES = 12


StepAction = Literal[
    "initialized",
    "stepped",
    "skipped_disabled",
    "skipped_paused",
    "skipped_wide_spread",
    "held_book_vanish",
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
    - ``"skipped_paused"`` — the operator paused this symbol. NO order is
      placed, but fill detection still runs and ``fills`` reports what was
      recorded: a pause does not cancel standing orders, so they can still
      fill and must still be observed. A fill here gets no counter-order.
    - ``"skipped_wide_spread"`` (ADR-025) — the symbol's bid-ask spread
      exceeded ``safety.max_spread_percentage``; no order placed or
      cancelled this tick, storage untouched.
    - ``"held_book_vanish"`` (ADR-037 decision 2) — this symbol's open
      orders left the exchange mid-session with zero fill and without
      the engine cancelling them (DMS purge, manual cancel on the
      exchange UI). Returned exactly ONCE, on the transition tick; the
      symbol is now paused (``reason=book_vanish``) and subsequent
      ticks return ``"skipped_paused"`` until an operator resume. The
      caller (cli/live) maps this transition to a critical
      notification.

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
        maker_fee_rate: Decimal = KRAKEN_MAKER_FEE_RATE,
        taker_fee_rate: Decimal = KRAKEN_TAKER_FEE_RATE,
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
        # ADR-038: the fee rates are INJECTED (cli/live fetches the
        # account's actual rates from TradeVolume at session start;
        # cli/shadow passes its configured fee model). The constants
        # remain the defaults so every other construction site keeps
        # its meaning — as the documented fallback.
        self._maker_fee_rate = maker_fee_rate
        self._taker_fee_rate = taker_fee_rate
        self._sell_guard = SellGuard(
            storage,
            max_loss_percentage=safety_config.sell_guard.max_loss_percentage,
            maker_fee_rate=maker_fee_rate,
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
        # ADR-037: why a symbol is paused, when the ENGINE paused it
        # (reason="book_vanish"). Operator pauses carry no entry.
        # Per-session like _paused_symbols — a restart clears the hold,
        # which is fine: session-start re-layout after a deliberate
        # restart is the ADR's documented "expected empty" case.
        self._hold_reasons: dict[Symbol, str] = {}
        # ADR-037: count of orders that left the exchange's open set
        # this session with ZERO fill and WITHOUT an engine cancel
        # (engine cancels update storage first, so they never surface
        # as fill-detection candidates). Non-zero + an empty book =
        # the book vanished externally — hold, don't re-lay.
        self._external_cancels: dict[Symbol, int] = {}
        # ADR-038 fee-drift tripwire: count of fills whose realized
        # fee rate matches NEITHER the maker nor the taker rate this
        # engine believes (tolerance 5 bps). Kraken's 2026-07-09 fee
        # doubling billed 0.80% against believed rates of 0.25/0.40
        # for five silent weeks — this counter is what would have
        # caught it on the first fill. cli/live pages on transition.
        self._fee_anomaly_counts: dict[Symbol, int] = {}
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
        self._starved: dict[Symbol, StarvationState] = {}

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
            # A paused symbol still OBSERVES; it just does not ACT.
            #
            # Until 2026-08-12 this returned immediately, which meant a
            # paused symbol's standing orders stayed live on the exchange
            # (pause deliberately does not cancel them) while the engine
            # stopped looking at them entirely. Caught in production: BTC
            # was paused, its buy at 63237.69 filled on 2026-08-11, and
            # because nothing reconciled, storage still called the order
            # `open` four days later, no trade row was ever written, the
            # dashboard showed three open orders when two remained, and
            # ~$5 of BTC sat with no exit. Worse, every downstream number
            # computed off that row — exposure, the caps, the advisor's
            # risk inputs — believed the money was still unspent USD.
            #
            # Pause must mean "stop trading", never "stop seeing".
            #
            # Counter-orders are deliberately NOT placed here: a counter
            # is a new order on the exchange, which is exactly what pause
            # forbids. The fill is now recorded and visible, and the
            # operator has resume / cancel-open-orders to act on it. The
            # inventory-with-no-exit consequence is real but it is now a
            # SHOWN state rather than a silent one.
            fills, _ = await self._detect_fills(symbol, exchange_open_orders, exchange_trades)
            if fills:
                _LOGGER.warning(
                    "%s is PAUSED but %d order(s) filled on the exchange; recorded, "
                    "no counter placed — resume or cancel open orders to act",
                    symbol,
                    len(fills),
                    extra={"symbol": str(symbol), "fills": len(fills), "paused": True},
                )
            return StepResult(symbol=symbol, action="skipped_paused", fills=len(fills))

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
        and place NO orders. They do still run fill detection, because open
        orders are NOT cancelled by a pause — they stay live on the
        exchange and can fill, so the engine must keep recording them
        (fixed 2026-08-12; see the note at the pause gate in
        ``_step_unlocked``). Cancelling is a separate operator action
        (``cancel_open_orders``).

        A fill detected while paused is recorded but gets NO counter-order:
        placing one would be trading, which is what pause forbids.

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
        # ADR-037: an operator resume also clears an engine-initiated
        # hold and its external-cancel evidence — the operator has seen
        # the page and decided trading may continue; the next tick's
        # empty book re-lays normally.
        self._hold_reasons.pop(symbol, None)
        self._external_cancels.pop(symbol, None)
        # 2026-09-03: drop starvation state too. The pause gate returns before
        # the no-orders check, so a symbol starved and THEN paused keeps its
        # tick count frozen; on resume the back-off would swallow up to 59
        # more ticks of silence, and the entry WARNING would not re-fire
        # (it is guarded on the symbol being absent here). The operator would
        # resume and see nothing at all. Covers the ADR-037 book-vanish hold
        # as well — both routes exit through this method.
        self._starved.pop(symbol, None)
        _LOGGER.info("%s resumed by operator", symbol, extra={"symbol": str(symbol)})
        return True

    def is_paused(self, symbol: Symbol) -> bool:
        """Return ``True`` if ``symbol`` is currently paused."""
        return symbol in self._paused_symbols

    def hold_reason(self, symbol: Symbol) -> str | None:
        """ADR-037: why the ENGINE paused ``symbol`` (``"book_vanish"``),
        or ``None`` for an active symbol / a plain operator pause."""
        return self._hold_reasons.get(symbol)

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

    async def cancel_open_orders(  # pylint: disable=too-many-locals,too-many-branches
        self, symbol: Symbol | None = None
    ) -> tuple[int, int]:
        # Same rationale as _tick's disable: every local is a distinct
        # stage signal of a linear procedure (fetch, cancel, resolve
        # identity, persist, queue a counter); helper-splitting would
        # obscure the cancel -> resolve -> persist ordering that IS
        # the correctness argument (ADR-037's 2026-08-19 fix). The
        # 2026-08-22 fix (escalating log level for an untracked order's
        # discarded fill) added one more branch for the same reason.
        """Cancel every open order on the exchange for ``symbol`` (or all).

        Reads the open-order set from the exchange (authoritative per
        ADR-006 decision 3) rather than from storage, so a stale storage
        view can't strand orders on Kraken. Per-order failures are
        logged and counted; the batch never aborts mid-way.

        Persistence resolves each cancelled order back to its STORED
        identity via ``exchange_id`` before saving. ``ExchangePort.
        get_open_orders`` deliberately constructs fresh-UUID ``Order``
        objects (see that method's docstring) — saving one of those
        directly, as this method did before the 2026-08-19 fix, upserts
        an ORPHAN row (``save_order`` keys on ``id``) and leaves the
        real stored row ``status='open'`` forever. The next
        fill-detection tick then discovers that real row as an
        apparent EXTERNAL cancel — this was the exact mechanism behind
        a production false-trip of ADR-037's book-vanish hold on a
        starved re-anchor's own cancellation. An order the exchange
        reports that local storage never tracked (a manual Kraken-side
        order) is cancelled but not adopted, per ADR-018.

        A cancel that catches an order mid-partial-fill (Kraken keeps a
        partially-filled limit order in ``OpenOrders`` until it is
        fully filled or cancelled) persists the matched trade(s) and
        queues a counter-order via ``_pending_counter_ids`` (ADR-023) —
        this method has no grid levels/spacing in scope to place one
        synchronously, so the next tick that touches the symbol places
        it, exactly like a startup-reconciler-recovered fill.
        filled_amount reflects the pre-cancel ``get_open_orders``
        snapshot (Kraken's own ``cancel_order`` doesn't re-query) — a
        fill landing in the narrow fetch→cancel window is not caught
        here; the next startup reconciler (ADR-023) recovers it, the
        same as before this fix.

        Args:
            symbol: Restrict to one symbol; ``None`` cancels across all.

        Returns:
            ``(cancelled, failed)`` counts — exchange-side cancel
            outcomes. A post-cancel persistence failure is logged but
            does not move either count: the cancellation itself
            already succeeded.

        Raises:
            ExchangeError: If the open-order fetch itself fails. The cancel
                set is then indeterminate, so the error propagates rather
                than collapsing into ``(0, 0)`` — which an operator "cancel
                all" would otherwise read as a false all-clear while orders
                stay live on the exchange. (The per-order loop below still
                logs-and-counts; the batch never aborts mid-way.)
        """
        opens = await self._exchange.get_open_orders(symbol=symbol)
        stored_by_exchange_id = {
            o.exchange_id: o
            for o in await self._storage.get_open_orders(symbol=symbol)
            if o.exchange_id
        }

        cancelled = 0
        failed = 0
        canceled_orders: list[Order] = []
        for order in opens:
            try:
                canceled_order = await self._exchange.cancel_order(order)
                cancelled += 1
                canceled_orders.append(canceled_order)
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

        # Trade history is fetched AFTER every cancel completes, not
        # before — a fill caught mid-cancel (the F1 partial-fill shape
        # below) may not be in the exchange's trade history until the
        # cancel itself has settled.
        trades_by_order: dict[str, list[Trade]] = {}
        if canceled_orders:
            recent_trades = await self._exchange.get_trade_history(symbol=symbol, limit=200)
            for trade in recent_trades:
                trades_by_order.setdefault(trade.order_id, []).append(trade)

        for canceled_order in canceled_orders:
            if canceled_order.exchange_id is None:
                continue
            stored = stored_by_exchange_id.get(canceled_order.exchange_id)
            if stored is None:
                if canceled_order.filled_amount > 0:
                    # This order has no local Order row to attach a
                    # recovered trade to (ADR-018: never adopt an
                    # untracked order), so the fill genuinely cannot be
                    # recovered here -- but it's real money that moved,
                    # so it must be loud, not an easily-missed INFO line.
                    # Confirmed live 2026-08-22: an order placed by this
                    # engine can end up untracked from birth if
                    # place_order succeeds but save_order never commits
                    # (grid_engine._place_level) -- indistinguishable at
                    # this point from a genuine manual Kraken-side order.
                    _LOGGER.error(
                        "cancel_open_orders: cancelled %s (%s) had a fill of %s but is NOT "
                        "tracked in local storage -- this trade cannot be recovered "
                        "automatically; reconcile manually against Kraken's trade history",
                        canceled_order.symbol,
                        canceled_order.exchange_id,
                        fmt_decimal(canceled_order.filled_amount),
                        extra={
                            "symbol": str(canceled_order.symbol),
                            "exchange_id": canceled_order.exchange_id,
                            "filled_amount": str(canceled_order.filled_amount),
                        },
                    )
                else:
                    _LOGGER.info(
                        "cancel_open_orders: cancelled %s (%s) not tracked in local storage; "
                        "not adopting",
                        canceled_order.symbol,
                        canceled_order.exchange_id,
                        extra={
                            "symbol": str(canceled_order.symbol),
                            "exchange_id": canceled_order.exchange_id,
                        },
                    )
                continue

            resolved = stored.model_copy(
                update={
                    "status": canceled_order.status,
                    "filled_amount": canceled_order.filled_amount,
                    "updated_at": canceled_order.updated_at,
                }
            )
            try:
                # save_fill persists the order's terminal status together
                # with its trades in one transaction (2026-08-22 fix) --
                # see StoragePort.save_fill's docstring for why a plain
                # save_order + per-trade save_trade loop can silently
                # lose a trade forever.
                trades = (
                    trades_by_order.get(resolved.exchange_id or "", [])
                    if resolved.filled_amount > 0
                    else []
                )
                await self._storage.save_fill(resolved, trades)
                if resolved.filled_amount > 0:
                    # ADR-023 F1 shape: a real fill caught by this
                    # cancel, not a clean cancel/expire.
                    for trade in trades:
                        self._check_fee_drift(resolved.symbol, trade)
                    if trades:
                        self._sell_guard.invalidate(resolved.symbol)
                    self._pending_counter_ids.add(resolved.id)
                    _LOGGER.warning(
                        "cancel_open_orders: %s %s (%s) had a partial fill of %s before "
                        "this cancel; counter-order queued for the next tick",
                        resolved.symbol,
                        resolved.side.value.upper(),
                        resolved.exchange_id,
                        fmt_decimal(resolved.filled_amount),
                        extra={
                            "symbol": str(resolved.symbol),
                            "exchange_id": resolved.exchange_id,
                            "filled_amount": str(resolved.filled_amount),
                        },
                    )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _LOGGER.warning(
                    "cancel_open_orders: persisting cancelled %s %s (%s) failed: %s",
                    resolved.symbol,
                    resolved.side,
                    resolved.exchange_id,
                    exc,
                    extra={
                        "symbol": str(resolved.symbol),
                        "exchange_id": resolved.exchange_id,
                        "error": str(exc),
                    },
                )

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
        # ADR-037 belt-and-braces: this cancel is engine-initiated and
        # cancel_open_orders now persists onto the STORED order identity
        # (the 2026-08-19 fix), so it should already be impossible for
        # these cancellations to surface as fill-detection candidates.
        # Clear any accumulated external-cancel evidence for this symbol
        # anyway -- a deliberate re-anchor supersedes it regardless, and
        # this guards against a future regression in that invariant.
        self._external_cancels.pop(symbol, None)
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
        # 2026-09-03 review: clear the starved clock too. ``resume_symbol``
        # already does, but it returns early for a symbol that was never
        # paused — the COMMON case, since a starved symbol keeps ticking.
        # Without this a re-anchor of a stuck symbol lays out with the
        # demotion still on and takes ``_note_layout_outcome``'s refresh
        # branch, so a 0/N re-anchor emits nothing at WARNING and names no
        # reason. The operator re-anchors precisely because the symbol is
        # stuck, and would learn nothing about why it still is. It also makes
        # the tick count match ``StarvationState``'s own docstring, which
        # already says the clock resets on intervention.
        self._starved.pop(symbol, None)
        levels = compute_grid_levels(
            reference_price=state.reference_price,
            spacing_percentage=state.spacing_percentage,
            levels_above=state.levels_above,
            levels_below=state.levels_below,
        )
        layout = await self._place_layout(symbol, levels, coin_cfg)
        placed = layout.placed
        refusals = layout.refusals
        sells_deferred = layout.sells_deferred
        # A 0/N reanchor layout (the original 2026-08-09 incident) enters
        # the starved back-off immediately — no next-tick busy loop.
        self._note_layout_outcome(symbol, layout, len(levels))
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
    ) -> LayoutOutcome:
        """Place every level of a layout and tally what happened.

        The one placement loop shared by ``_initialize``, the auto
        re-layout branch, and ``request_reanchor`` — three call sites
        with an identical per-outcome tally that would otherwise drift.

        ``LayoutOutcome.reasons`` attributes each refusal and sums to
        ``refusals``. It is what makes the DEBUG demotion this method asks
        ``_try_place`` for safe: the detail leaves the per-level lines and
        survives in the starved WARNING, the periodic summary, or the
        partial-recovery WARNING instead. This is the ONLY caller that asks
        for the demotion, because it is the only one that builds that record.
        """
        placed = 0
        refusals = 0
        sells_deferred = 0
        reasons: Counter[str] = Counter()
        # Sampled once, before the loop: a symbol already in the back-off is
        # re-attempting a layout whose refusals the starved record carries.
        quiet = symbol in self._starved
        for level in levels:
            outcome, reason = await self._try_place(symbol, level, coin_cfg, quiet_refusals=quiet)
            if outcome == "placed":
                placed += 1
            elif outcome == "sell_deferred":
                sells_deferred += 1
            else:
                refusals += 1
                reasons[reason] += 1
        return LayoutOutcome(
            placed=placed,
            refusals=refusals,
            sells_deferred=sells_deferred,
            reasons=dict(reasons),
        )

    def _starved_should_attempt(self, symbol: Symbol) -> bool:
        """Gate a no-orders re-layout attempt through the back-off.

        Not starved: always attempt. Starved: count the tick and allow an
        attempt only every ``_STARVED_RETRY_EVERY_TICKS`` -- never a retry
        every tick.

        A pure gate: it emits nothing. The periodic summary lives in
        :meth:`_note_layout_outcome`, on the far side of the attempt this
        call authorizes, so it can report the reasons that retry actually
        hit rather than the previous one's.
        """
        state = self._starved.get(symbol)
        if state is None:
            return True
        state = state.advanced()
        self._starved[symbol] = state
        return state.ticks % _STARVED_RETRY_EVERY_TICKS == 0

    def _note_layout_outcome(self, symbol: Symbol, outcome: LayoutOutcome, target: int) -> None:
        """Enter/clear the starved state from a layout's outcome.

        Zero placed out of a non-empty target = starved: ONE WARNING
        with the refusal/deferral breakdown (the operator learns once,
        and the command-result echo / logs carry the same counts), then
        the back-off in :meth:`_starved_should_attempt` owns the
        cadence. Any placement clears the state — standing orders make
        the no-orders self-heal moot.

        An already-starved symbol keeps its tick count and takes this
        retry's reasons, then emits the periodic summary on every
        ``_STARVED_SUMMARY_EVERY_RETRIES``-th retry. The summary lives here,
        after the attempt, so it reports what is binding NOW; emitted from
        the gate instead it would always be one retry stale.
        """
        if outcome.placed > 0 or target == 0:
            if self._starved.pop(symbol, None) is not None:
                recovery_extra = {
                    "symbol": str(symbol),
                    "levels_placed": outcome.placed,
                    "target_levels": target,
                    "refusals": outcome.refusals,
                    "refusal_reasons": dict(outcome.reasons),
                }
                if outcome.refusals:
                    # The recovering layout ran while the symbol was STILL
                    # starved (this hook runs after ``_place_layout``), so its
                    # surviving refusals went to DEBUG — and the record that
                    # would otherwise carry them is discarded on the line
                    # above. This is the only place those reasons are ever
                    # named. A never-starved partial layout does not reach
                    # here at all and keeps its per-level WARNINGs.
                    _LOGGER.warning(
                        "%s recovered from starvation only partially: placed %d/%d, "
                        "%d still refused; binding: %s",
                        symbol,
                        outcome.placed,
                        target,
                        outcome.refusals,
                        describe_reasons(outcome.reasons),
                        extra=recovery_extra,
                    )
                else:
                    _LOGGER.info(
                        "%s recovered from starvation: placed %d/%d",
                        symbol,
                        outcome.placed,
                        target,
                        extra=recovery_extra,
                    )
            return
        existing = self._starved.get(symbol)
        if existing is not None:
            refreshed = existing.with_outcome(outcome, target)
            self._starved[symbol] = refreshed
            retries, remainder = divmod(refreshed.ticks, _STARVED_RETRY_EVERY_TICKS)
            # remainder == 0 keeps an operator re-anchor that lands mid-back-off
            # from emitting a summary it did not earn: only a real retry
            # boundary qualifies.
            if remainder == 0 and retries % _STARVED_SUMMARY_EVERY_RETRIES == 0:
                # WARNING, not the INFO the offside heartbeat uses. Offside is
                # a NORMAL parked state -- price moved outside a grid that is
                # working as designed. Starvation means the symbol cannot
                # trade at all until someone changes a cap or the anchor. One
                # line an hour is not noise, and without it a symbol starved
                # for 23 hours (the 2026-09-03 XRP case) would emit exactly
                # one WARNING at entry and nothing after.
                _LOGGER.warning(
                    "%s still starved after %d retries (%d consecutive ticks): "
                    "placed 0/%d, binding: %s%s",
                    symbol,
                    retries,
                    refreshed.ticks,
                    target,
                    describe_reasons(refreshed.reasons),
                    (
                        f"; {refreshed.sells_deferred} sells deferred"
                        if refreshed.sells_deferred
                        else ""
                    ),
                    extra={
                        "symbol": str(symbol),
                        "starved_retries": retries,
                        "consecutive_starved_ticks": refreshed.ticks,
                        "target_levels": target,
                        "refusals": refreshed.refusals,
                        "sells_deferred": refreshed.sells_deferred,
                        "refusal_reasons": dict(refreshed.reasons),
                    },
                )
            return
        self._starved[symbol] = StarvationState.entering(outcome, target)
        _LOGGER.warning(
            "%s layout starved: placed 0/%d (%d refused, %d sells deferred); "
            "binding: %s; backing off — retrying every %d ticks instead of every "
            "tick, and per-level cap refusals drop to DEBUG until it clears",
            symbol,
            target,
            outcome.refusals,
            outcome.sells_deferred,
            describe_reasons(outcome.reasons),
            _STARVED_RETRY_EVERY_TICKS,
            extra={
                "symbol": str(symbol),
                "target_levels": target,
                "refusals": outcome.refusals,
                "sells_deferred": outcome.sells_deferred,
                "refusal_reasons": dict(outcome.reasons),
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
        layout = await self._place_layout(symbol, levels, coin_cfg)
        placed = layout.placed
        refusals = layout.refusals
        sells_deferred = layout.sells_deferred
        self._note_layout_outcome(symbol, layout, len(levels))
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
                outcome, _ = await self._try_place(symbol, target, coin_cfg, amount=counter_amount)
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
                self._starved.pop(symbol, None)
                # ADR-037: a lone external cancel with the rest of the
                # book intact (e.g. one manual cancel on the exchange
                # UI) must not arm a permanent hair-trigger — the hold
                # is for the book VANISHING, judged at this same gate.
                self._external_cancels.pop(symbol, None)
            if not remaining_open and self._external_cancels.get(symbol, 0) > 0:
                # ADR-037 decision 2: the book vanished mid-session
                # without the engine cancelling it (DMS purge after a
                # lockout, a manual cancel on the exchange UI). During
                # the 2026-08-15→17 incident this exact state was
                # re-laid silently ~40 times at stale anchors. Hold
                # instead: pause the symbol (operator resume only,
                # option A) and surface the transition to the caller
                # exactly once via action="held_book_vanish".
                self._paused_symbols.add(symbol)
                self._hold_reasons[symbol] = "book_vanish"
                _LOGGER.error(
                    "%s book vanished externally (%d order(s) cancelled outside the engine); "
                    "HOLDING — no re-layout until operator resume",
                    symbol,
                    self._external_cancels[symbol],
                    extra={
                        "symbol": str(symbol),
                        "external_cancels": self._external_cancels[symbol],
                        "hold_reason": "book_vanish",
                    },
                )
                return StepResult(
                    symbol=symbol,
                    action="held_book_vanish",
                    fills=len(fills),
                    counters_placed=counters_placed,
                    placed=placed,
                    refusals=refusals,
                    sells_deferred=sells_deferred,
                    trade_ids=trade_ids,
                )
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
                    # re-anchor (ADR-031, shipped 2026-08-09) is the fix
                    # flow this warning points at.
                    #
                    # INFO while starved: a starved symbol cannot place, so
                    # it cannot refresh its own anchor, so this is
                    # permanently true and repeats on every retry (~246/day
                    # measured live). The starved WARNING and the periodic
                    # summary are the operator's signal for that symbol.
                    log = _LOGGER.info if symbol in self._starved else _LOGGER.warning
                    log(
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
                relayout = await self._place_layout(symbol, levels, coin_cfg)
                relayout_placed = relayout.placed
                relayout_refusals = relayout.refusals
                relayout_deferred = relayout.sells_deferred
                placed += relayout_placed
                refusals += relayout_refusals
                sells_deferred += relayout_deferred
                self._note_layout_outcome(symbol, relayout, len(levels))
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
            outcome, _ = await self._try_place(symbol, target, coin_cfg, amount=counter_amount)
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

    _FEE_DRIFT_TOLERANCE = Decimal("0.0005")  # 5 bps

    def _check_fee_drift(self, symbol: Symbol, trade: Trade) -> None:
        """ADR-038: flag a fill whose realized fee rate matches neither
        the maker nor the taker rate this engine believes."""
        if trade.cost <= 0:
            return
        realized = trade.fee / trade.cost
        drift = min(
            abs(realized - self._maker_fee_rate),
            abs(realized - self._taker_fee_rate),
        )
        if drift <= self._FEE_DRIFT_TOLERANCE:
            return
        self._fee_anomaly_counts[symbol] = self._fee_anomaly_counts.get(symbol, 0) + 1
        _LOGGER.warning(
            "fee drift on %s: realized %s%% matches neither maker %s%% nor taker %s%% "
            "-- has the exchange fee schedule changed?",
            symbol,
            fmt_decimal(realized * 100),
            fmt_decimal(self._maker_fee_rate * 100),
            fmt_decimal(self._taker_fee_rate * 100),
            extra={
                "symbol": str(symbol),
                "realized_rate": str(realized),
                "maker_rate": str(self._maker_fee_rate),
                "taker_rate": str(self._taker_fee_rate),
                "trade_id": trade.id,
            },
        )

    def fee_anomaly_count(self, symbol: Symbol) -> int:
        """ADR-038: fills so far whose fee rate matched neither believed rate."""
        return self._fee_anomaly_counts.get(symbol, 0)

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
            # save_fill persists the order's terminal status together with
            # its trades in one transaction (2026-08-22 fix) -- a plain
            # save_order + per-trade save_trade loop left a window where
            # the order committed as closed but a later trade insert
            # failed, permanently losing the trade (a closed order never
            # becomes a fill candidate again).
            await self._storage.save_fill(resolution.order, resolution.trades)
            if not resolution.needs_counter and resolution.order.filled_amount == 0:
                # ADR-037: a clean cancel/expire the engine did not
                # perform (its own cancels update storage before this
                # diff runs, so they never appear as candidates). This
                # is the book-vanish discriminator — see the re-layout
                # gate in _step_unlocked. Counted AFTER the successful
                # persist (2026-08-22 review): incrementing first meant a
                # transient save_fill failure left the counter inflated
                # while the row stayed open, and the next tick's retry
                # double-counted the same cancel into the operator's
                # "N order(s) cancelled outside the engine" page.
                self._external_cancels[symbol] = self._external_cancels.get(symbol, 0) + 1
            if resolution.needs_counter:
                filled.append(resolution.order)
                for trade in resolution.trades:
                    saved_trade_ids.append(trade.id)
                    self._check_fee_drift(symbol, trade)
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
        *,
        quiet_refusals: bool = False,
    ) -> tuple[_PlaceOutcome, str]:
        """Run safety checks then place. Refusals/deferrals are logged
        and never raise.

        ``amount`` overrides the default USD-budget-derived sizing — used
        for counter orders, which must match the filled order's base
        amount (ADR-006 decision 2).

        Returns the outcome paired with a refusal reason (empty for a
        placement or a deferral) — see ``_PlaceOutcome``.

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
            # 2026-09-03: while a symbol is starved this line repeats
            # verbatim on every retry, forever, for a condition the engine
            # cannot resolve on its own — 3 lines per retry, measured at
            # ~738/day for XRP/USD on the live container.
            #
            # The CALLER decides, not this method. Gating on
            # ``symbol in self._starved`` here reads the same for the layout
            # and for the two counter-order call sites, and no LayoutOutcome
            # covers those: their reason is discarded, so they would go quiet
            # with nothing replacing it. An ADR-023 recovery counter blocked
            # by a cap would then be invisible for the life of the session
            # while its filled inventory sat with no exit order. Only
            # ``_place_layout`` passes True, and only because the starved
            # WARNING, the periodic summary and the partial-recovery WARNING
            # carry the reason it suppresses.
            #
            # This deliberately does not touch the other two refusal arms
            # below: an exchange-side ordermin rejection stays loud.
            log = _LOGGER.debug if quiet_refusals else _LOGGER.warning
            log(
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
            return "refused", decision.reason or "safety_cap"

        if level.side is OrderSide.SELL and self._safety.sell_guard.enabled:
            assessment = await self._sell_guard.assess(symbol, level.price)
            if not assessment.allowed:
                # No log here by design: SellGuard.assess already emits its own
                # transition WARNING with the numbers and then throttles to a
                # heartbeat, so the sell half is not a per-level noise source.
                return "sell_deferred", ""

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
            return "refused", REASON_INSUFFICIENT_BALANCE
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
            return "refused", REASON_EXCHANGE_ERROR
        return "placed", ""

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

    async def _check_safety(  # pylint: disable=too-many-return-statements
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

            # ADR-039 inventory caps: the four caps above bound the
            # order BOOK; these bound the POSITION — held inventory at
            # average cost plus open BUY notional. BUY-side only by
            # construction: a SELL (counter-orders included) releases
            # headroom and must never be refused here. Cost basis, not
            # MTM — see the config comment for why.
            coin_inventory = await coin_inventory_cost_usd(self._storage, symbol)
            if (
                coin_inventory + buy_notional_usd(coin_open) + proposed
                > cap.max_per_coin_inventory_usd
            ):
                return _SafetyDecision(ok=False, reason="max_per_coin_inventory_usd")

            total_inventory = await total_inventory_cost_usd(self._storage)
            all_open_buys = buy_notional_usd(await self._storage.get_open_orders())
            if total_inventory + all_open_buys + proposed > cap.max_total_inventory_usd:
                return _SafetyDecision(ok=False, reason="max_total_inventory_usd")

        return _SafetyDecision(ok=True)

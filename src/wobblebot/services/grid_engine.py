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
storage (manual operator intervention, prior crash mid-placement) is
deferred to a later slice along with the periodic-N-tick cadence
described in ADR-006 decision 3.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Literal

from wobblebot.config.grid import CoinGridConfig, GridConfig
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.grid import (
    GridLevel,
    GridState,
    compute_grid_levels,
    grid_spacing,
    is_offside,
    next_counter_action,
)
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.storage import StoragePort

_LOGGER = logging.getLogger("wobblebot.services.grid_engine")


StepAction = Literal["initialized", "stepped", "skipped_disabled"]


@dataclass(frozen=True)
class StepResult:
    """Summary of a single ``GridEngine.step`` invocation.

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
    """

    symbol: Symbol
    action: StepAction
    fills: int = 0
    counters_placed: int = 0
    placed: int = 0
    refusals: int = 0
    offside: bool = False
    trade_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _SafetyDecision:
    """Result of one ``_check_safety`` evaluation. Carried as a value
    so callers can log the refusal reason without re-deriving it."""

    ok: bool
    reason: str | None = None


class GridEngine:
    """Per-symbol micro-grid engine.

    Stateless across restarts (state lives in storage). The only
    in-memory state is the per-symbol ``asyncio.Lock`` registry, which
    is rebuilt fresh on each instance.
    """

    def __init__(
        self,
        exchange: ExchangePort,
        storage: StoragePort,
        grid_config: GridConfig,
        safety_config: SafetyConfig,
    ) -> None:
        self._exchange = exchange
        self._storage = storage
        self._config = grid_config
        self._safety = safety_config
        self._coin_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, symbol: Symbol) -> asyncio.Lock:
        key = symbol.base.upper()
        lock = self._coin_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._coin_locks[key] = lock
        return lock

    async def step(self, symbol: Symbol) -> StepResult:
        """Advance the engine by one tick for ``symbol``.

        Safe to call concurrently for different symbols; calls for the
        same symbol serialize via per-coin lock.
        """
        async with self._lock_for(symbol):
            return await self._step_unlocked(symbol)

    async def _step_unlocked(self, symbol: Symbol) -> StepResult:
        coin_cfg = self._config.for_coin(symbol.base)
        if not coin_cfg.enabled:
            return StepResult(symbol=symbol, action="skipped_disabled")

        current_price = (await self._exchange.get_current_price(symbol)).amount

        grid_state = await self._storage.get_grid_state(symbol)
        if grid_state is None:
            return await self._initialize(symbol, current_price, coin_cfg)

        return await self._tick(symbol, current_price, grid_state, coin_cfg)

    # ------------------------------------------------------------------ initialization

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
        placed = 0
        refusals = 0
        for level in levels:
            placed_ok = await self._try_place(symbol, level, coin_cfg)
            if placed_ok:
                placed += 1
            else:
                refusals += 1
        _LOGGER.info(
            "grid initialized",
            extra={
                "symbol": str(symbol),
                "reference_price": str(state.reference_price),
                "levels_placed": placed,
                "refusals": refusals,
            },
        )
        return StepResult(
            symbol=symbol,
            action="initialized",
            placed=placed,
            refusals=refusals,
        )

    # ------------------------------------------------------------------ subsequent ticks

    async def _tick(
        self,
        symbol: Symbol,
        current_price: Decimal,
        state: GridState,
        coin_cfg: CoinGridConfig,
    ) -> StepResult:
        levels = compute_grid_levels(
            reference_price=state.reference_price,
            spacing_percentage=state.spacing_percentage,
            levels_above=state.levels_above,
            levels_below=state.levels_below,
        )
        offside = is_offside(current_price, levels)

        fills, trade_ids = await self._detect_fills(symbol)
        counters_placed = 0
        refusals = 0
        if not offside:
            spacing = grid_spacing(state.reference_price, state.spacing_percentage)
            for filled in fills:
                target = next_counter_action(filled.side, filled.price.amount, spacing)
                # Per ADR-006 decision 2 the counter is sized to the filled
                # portion, not re-derived from order_size_usd. This keeps
                # cycles base-amount-balanced — without it, each cycle's
                # SELL would be sized in USD at the higher counter price,
                # so the BUY/SELL BTC amounts would mismatch and the
                # cycle would slowly accumulate or shed inventory and
                # bleed value through the spread.
                counter_amount = Amount(value=filled.filled_amount, asset=filled.amount.asset)
                placed_ok = await self._try_place(symbol, target, coin_cfg, amount=counter_amount)
                if placed_ok:
                    counters_placed += 1
                else:
                    refusals += 1
        elif fills:
            _LOGGER.warning(
                "fills detected while offside; counters suppressed",
                extra={
                    "symbol": str(symbol),
                    "current_price": str(current_price),
                    "fills": len(fills),
                },
            )

        if offside:
            _LOGGER.warning(
                "grid offside; staying parked",
                extra={
                    "symbol": str(symbol),
                    "current_price": str(current_price),
                    "lowest_level": str(levels[0].price) if levels else None,
                    "highest_level": str(levels[-1].price) if levels else None,
                },
            )

        return StepResult(
            symbol=symbol,
            action="stepped",
            fills=len(fills),
            counters_placed=counters_placed,
            refusals=refusals,
            offside=offside,
            trade_ids=trade_ids,
        )

    # ------------------------------------------------------------------ helpers

    async def _detect_fills(self, symbol: Symbol) -> tuple[list[Order], list[str]]:
        """Diff storage's open set against the exchange's; record fills.

        Returns ``(filled_orders, saved_trade_ids)`` — the orders that
        transitioned out of the open set this tick (status refreshed
        from the exchange) and the trade IDs persisted as a result.
        """
        stored_open = await self._storage.get_open_orders(symbol=symbol)
        exchange_open = await self._exchange.get_open_orders(symbol=symbol)
        live_ids = {o.exchange_id for o in exchange_open if o.exchange_id}

        candidates = [o for o in stored_open if o.exchange_id and o.exchange_id not in live_ids]
        if not candidates:
            return [], []

        # Fetch trade history once and index by exchange_id; cheaper than
        # one round-trip per fill, and sufficient for Stage 2.2 volumes.
        recent_trades = await self._exchange.get_trade_history(symbol=symbol, limit=200)
        trades_by_order: dict[str, list[Trade]] = {}
        for trade in recent_trades:
            trades_by_order.setdefault(trade.order_id, []).append(trade)

        filled: list[Order] = []
        saved_trade_ids: list[str] = []
        for candidate in candidates:
            refreshed = await self._exchange.get_order_status(candidate)
            await self._storage.save_order(refreshed)
            if refreshed.status == "closed" and refreshed.exchange_id is not None:
                filled.append(refreshed)
                for trade in trades_by_order.get(refreshed.exchange_id, []):
                    await self._storage.save_trade(trade)
                    saved_trade_ids.append(trade.id)
                _LOGGER.info(
                    "grid fill",
                    extra={
                        "symbol": str(symbol),
                        "side": refreshed.side.value,
                        "price": str(refreshed.price.amount),
                        "amount": str(refreshed.amount.value),
                        "exchange_id": refreshed.exchange_id,
                    },
                )
        return filled, saved_trade_ids

    async def _try_place(
        self,
        symbol: Symbol,
        level: GridLevel,
        coin_cfg: CoinGridConfig,
        amount: Amount | None = None,
    ) -> bool:
        """Run safety checks then place. Returns True if placed, False if
        refused. Refusals are logged and never raise.

        ``amount`` overrides the default USD-budget-derived sizing — used
        for counter orders, which must match the filled order's base
        amount (ADR-006 decision 2).

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
                "order refused by safety cap",
                extra={
                    "symbol": str(symbol),
                    "side": level.side.value,
                    "price": str(level.price),
                    "reason": decision.reason,
                },
            )
            return False
        await self._place_level(symbol, level, coin_cfg, amount=amount)
        return True

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

        coin_exposure = sum((o.price.amount * o.amount.value for o in coin_open), Decimal("0"))
        if coin_exposure + proposed > cap.max_per_coin_exposure_usd:
            return _SafetyDecision(ok=False, reason="max_per_coin_exposure_usd")

        all_open = await self._storage.get_open_orders()
        total_exposure = sum((o.price.amount * o.amount.value for o in all_open), Decimal("0"))
        if total_exposure + proposed > cap.max_total_exposure_usd:
            return _SafetyDecision(ok=False, reason="max_total_exposure_usd")

        if level.side is OrderSide.BUY:
            today_start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
            todays_buys = await self._storage.get_orders(
                side=OrderSide.BUY.value, created_after=today_start
            )
            daily_spend = sum((o.price.amount * o.amount.value for o in todays_buys), Decimal("0"))
            if daily_spend + proposed > cap.max_daily_spend_usd:
                return _SafetyDecision(ok=False, reason="max_daily_spend_usd")

        return _SafetyDecision(ok=True)

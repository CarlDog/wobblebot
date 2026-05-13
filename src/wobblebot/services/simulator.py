"""End-to-end simulation orchestration for the Phase 1 integration check.

One function — :func:`run_buy_dip_sell_rebound_cycle` — composes
``ExchangePort`` and ``StoragePort`` to walk through a deliberately
trivial buy-low / sell-high cycle against a scripted price walk.

This is not the real Bot Core. The actual micro-grid engine ships in
Phase 2. The point of this module is to prove the hex layers compose
end-to-end before any real exchange or live strategy is wired in.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.storage import StoragePort

_logger = logging.getLogger(__name__)


@dataclass
class SimulationResult:
    """Summary of a single cycle run."""

    orders_placed: int = 0
    trades_executed: int = 0
    final_balances: list[Balance] = field(default_factory=list)


async def run_buy_dip_sell_rebound_cycle(
    exchange: MockExchangeAdapter,
    storage: StoragePort,
    *,
    symbol: Symbol,
    buy_price: Decimal,
    sell_price: Decimal,
    amount: Decimal,
    price_walk: Iterable[Decimal],
) -> SimulationResult:
    """Run one buy-dip / sell-rebound cycle against a scripted price walk.

    Places a buy at ``buy_price`` for ``amount`` of ``symbol.base``,
    then ticks through ``price_walk``. When the buy fills, places the
    corresponding sell at ``sell_price``. Persists every order
    transition and every trade to storage; snapshots balances at the
    end.

    The "strategy" here is intentionally hard-coded — the real Bot
    Core (Phase 2) replaces this with configurable grid logic.

    Args:
        exchange: A ``MockExchangeAdapter`` (we take the concrete type
            because we need ``set_price`` to drive the simulation;
            production code in Phase 2+ won't need that affordance).
        storage: Any ``StoragePort`` implementation.
        symbol: Trading pair to cycle.
        buy_price: Limit price for the initial buy.
        sell_price: Limit price for the sell placed after the buy fills.
        amount: Order size in ``symbol.base``.
        price_walk: Sequence of prices to drive through.

    Returns:
        ``SimulationResult`` summarising orders placed, trades
        executed, and the final balance snapshot.
    """
    result = SimulationResult()

    buy = _new_order(symbol, OrderSide.BUY, buy_price, amount)
    await exchange.place_order(buy)
    await storage.save_order(buy)
    result.orders_placed += 1
    _logger.info(
        "placed buy",
        extra={"order_id": str(buy.id), "limit": str(buy_price), "amount": str(amount)},
    )

    sell: Order | None = None

    for tick in price_walk:
        fills = exchange.set_price(symbol, tick)
        for trade in fills:
            await storage.save_trade(trade)
            result.trades_executed += 1
            _logger.info(
                "trade filled",
                extra={
                    "trade_id": trade.id,
                    "order_id": trade.order_id,
                    "side": str(trade.side),
                    "price": str(trade.price.amount),
                    "cost": str(trade.cost),
                    "fee": str(trade.fee),
                },
            )
            # Persist the now-closed order state.
            order = _resolve_order(buy, sell, trade)
            await storage.save_order(order)

            # When the buy fills, place the corresponding sell.
            if trade.side is OrderSide.BUY and sell is None:
                sell = _new_order(symbol, OrderSide.SELL, sell_price, amount)
                await exchange.place_order(sell)
                await storage.save_order(sell)
                result.orders_placed += 1
                _logger.info(
                    "placed sell",
                    extra={"order_id": str(sell.id), "limit": str(sell_price)},
                )

    balances = await exchange.get_balances()
    await storage.save_balance_snapshot(balances)
    result.final_balances = balances
    _logger.info(
        "cycle complete",
        extra={
            "orders_placed": result.orders_placed,
            "trades_executed": result.trades_executed,
        },
    )
    return result


def _new_order(symbol: Symbol, side: OrderSide, price: Decimal, amount: Decimal) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        price=Price(amount=price, currency=symbol.quote),
        amount=Amount(value=amount, asset=symbol.base),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _resolve_order(buy: Order, sell: Order | None, trade: Trade) -> Order:
    if trade.side is OrderSide.BUY:
        return buy
    if sell is None:
        raise RuntimeError(f"Sell trade {trade.id} matched but no sell order was placed yet")
    return sell

"""In-memory ``ExchangePort`` implementation for dry-run simulations.

The mock holds simulated balances, open orders, and trade history in
memory. Tests drive the market price via ``set_price`` (or its bulk
variant ``run_scenario``); the adapter scans open orders on each price
update and fills any whose limit crosses the new market price.

Matching rules (deliberately simple — full-fill on cross, no order
book depth, no partial fills, no slippage):

- A buy limit at L fills when market price drops to ``price <= L``.
- A sell limit at L fills when market price rises to ``price >= L``.

Fee model: a flat percentage of trade cost in the quote currency,
applied at fill time. Default 0.26% (Kraken maker — conservative for
tiny orders).

Synthetic txids are deterministic-ish: ``MOCK-ORD-<counter>`` for
orders, ``MOCK-TRD-<counter>`` for trades. Resetting an adapter
instance resets the counters.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from wobblebot.domain.exceptions import InsufficientBalance
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import (
    Amount,
    OHLCBar,
    OrderSide,
    Price,
    Symbol,
    Timestamp,
)
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.ports.exchange import ExchangePort

# Sensible Kraken-ish defaults; override per-instance if needed.
_DEFAULT_FEE_RATE = Decimal("0.0026")


class MockExchangeAdapter(ExchangePort):
    """Deterministic in-memory ``ExchangePort`` for simulations and tests.

    Construct with starting balances and (optionally) starting prices,
    then drive the simulation via ``set_price`` or ``run_scenario``.

    Args:
        starting_balances: Initial balances, keyed by asset code.
        starting_prices: Optional map of symbol -> initial price. Symbols
            without a starting price will raise ``ExchangeError`` on
            ``get_current_price`` until ``set_price`` is called.
        fee_rate: Fraction of trade cost charged as fee in the quote
            currency. Default 0.26% (Kraken maker-side conservative).
    """

    def __init__(
        self,
        starting_balances: dict[str, Decimal] | None = None,
        starting_prices: dict[Symbol, Decimal] | None = None,
        fee_rate: Decimal = _DEFAULT_FEE_RATE,
    ) -> None:
        if fee_rate < 0:
            raise ValueError(f"fee_rate must be non-negative, got {fee_rate}")
        self._fee_rate = fee_rate
        self._balances: dict[str, Decimal] = dict(starting_balances or {})
        self._prices: dict[Symbol, Decimal] = dict(starting_prices or {})
        self._open_orders: dict[str, Order] = {}
        self._trade_history: list[Trade] = []
        self._order_counter = 0
        self._trade_counter = 0

    # ----------------------------------------------------------------- mock controls

    def set_price(self, symbol: Symbol, price: Decimal) -> list[Trade]:
        """Update the market price for a symbol and run order matching.

        Returns the list of fills produced by this price change, in
        the order they were matched. Useful for test assertions.
        """
        self._prices[symbol] = price
        return self._match_open_orders(symbol)

    def run_scenario(self, ticks: Iterable[tuple[Symbol, Decimal]]) -> list[Trade]:
        """Apply a sequence of (symbol, price) ticks. Returns all fills."""
        fills: list[Trade] = []
        for symbol, price in ticks:
            fills.extend(self.set_price(symbol, price))
        return fills

    # ----------------------------------------------------------------- ExchangePort

    async def get_current_price(self, symbol: Symbol) -> Price:
        if symbol not in self._prices:
            raise ExchangeError(f"No market price set for {symbol}")
        return Price(amount=self._prices[symbol], currency=symbol.quote)

    async def get_ohlc(
        self,
        symbol: Symbol,
        interval_minutes: int = 1,
        since: datetime | None = None,
    ) -> list[OHLCBar]:
        """Historical OHLC bars don't apply to the in-memory mock.

        The mock has no concept of "historical price action" — it
        only knows the current price the test has set. Backfill is a
        feature of the live-data path; callers driving the engine via
        the mock should drive prices directly via ``set_price``.

        Raises ``NotImplementedError`` rather than silently returning
        an empty list so a test that accidentally exercises the
        backfill path against the mock fails loudly.
        """
        raise NotImplementedError(
            "MockExchangeAdapter has no historical OHLC data; "
            "the mock represents current-tick state only. Use "
            "KrakenAdapter (or ShadowExchangeAdapter, which forwards "
            "to the live adapter) for backfill workflows."
        )

    async def get_balances(self) -> list[Balance]:
        return [self._balance_for(asset) for asset in sorted(self._balances)]

    async def get_balance(self, asset: str) -> Balance | None:
        if asset not in self._balances:
            return None
        return self._balance_for(asset)

    async def place_order(self, order: Order) -> Order:
        cost = order.price.amount * order.amount.value
        fee_reserve = cost * self._fee_rate
        if order.side is OrderSide.BUY:
            required = cost + fee_reserve
            available = self._balances.get(order.symbol.quote, Decimal("0"))
            if available < required:
                raise InsufficientBalance(
                    required=required, available=available, asset=order.symbol.quote
                )
        else:  # SELL
            available = self._balances.get(order.symbol.base, Decimal("0"))
            if available < order.amount.value:
                raise InsufficientBalance(
                    required=order.amount.value,
                    available=available,
                    asset=order.symbol.base,
                )

        self._order_counter += 1
        order.mark_open(exchange_id=f"MOCK-ORD-{self._order_counter:06d}")
        self._open_orders[order.exchange_id] = order  # type: ignore[index]

        # Try to fill immediately if the current price already crosses.
        self._match_open_orders(order.symbol)
        return order

    async def cancel_order(self, order: Order) -> Order:
        if not order.exchange_id or order.exchange_id not in self._open_orders:
            raise ExchangeError(f"Unknown order {order.exchange_id!r}")
        live = self._open_orders.pop(order.exchange_id)
        live.mark_canceled()
        return live

    async def get_order_status(self, order: Order) -> Order:
        if not order.exchange_id:
            raise ExchangeError("Cannot get status for an order with no exchange_id")
        if order.exchange_id in self._open_orders:
            return self._open_orders[order.exchange_id]
        # Order is no longer open - look it up in our trade history.
        for trade in self._trade_history:
            if trade.order_id == order.exchange_id:
                order.mark_open(order.exchange_id)
                order.record_fill(filled_amount=order.amount.value)
                return order
        raise ExchangeError(f"Unknown order {order.exchange_id!r}")

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        if symbol is None:
            return list(self._open_orders.values())
        return [o for o in self._open_orders.values() if o.symbol == symbol]

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        results = self._trade_history
        if symbol is not None:
            results = [t for t in results if t.symbol == symbol]
        # Most-recent first, matching ExchangePort convention.
        return list(reversed(results))[:limit]

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        if amount <= 0:
            raise ExchangeError(f"Withdrawal amount must be positive, got {amount}")
        available = self._balances.get(asset, Decimal("0"))
        if available < amount:
            raise InsufficientBalance(required=amount, available=available, asset=asset)
        self._balances[asset] = available - amount
        self._trade_counter += 1
        return f"MOCK-WDR-{self._trade_counter:06d}"

    # ----------------------------------------------------------------- internals

    def _balance_for(self, asset: str) -> Balance:
        total = self._balances[asset]
        locked = sum(
            (self._locked_for_order(o, asset) for o in self._open_orders.values()),
            Decimal("0"),
        )
        return Balance(
            asset=asset,
            total=total,
            available=total - locked,
            locked=locked,
        )

    def _locked_for_order(self, order: Order, asset: str) -> Decimal:
        """Funds reserved by `order` in `asset`. Mirrors place_order's accounting."""
        if order.side is OrderSide.BUY and order.symbol.quote == asset:
            cost = order.price.amount * order.amount.value
            return cost + cost * self._fee_rate
        if order.side is OrderSide.SELL and order.symbol.base == asset:
            return order.amount.value - order.filled_amount
        return Decimal("0")

    def _match_open_orders(self, symbol: Symbol) -> list[Trade]:
        market_price = self._prices.get(symbol)
        if market_price is None:
            return []
        fills: list[Trade] = []
        # Snapshot keys because we mutate _open_orders during iteration.
        for exchange_id in list(self._open_orders.keys()):
            order = self._open_orders[exchange_id]
            if order.symbol != symbol:
                continue
            if self._crosses(order, market_price):
                fills.append(self._fill_order(order))
        return fills

    @staticmethod
    def _crosses(order: Order, market_price: Decimal) -> bool:
        if order.side is OrderSide.BUY:
            return market_price <= order.price.amount
        return market_price >= order.price.amount

    def _fill_order(self, order: Order) -> Trade:
        """Execute a full fill at the order's limit price.

        Deliberately ignores the market price that triggered the fill:
        real exchanges may give price improvement (fill a buy below the
        limit when the book has it cheaper), but matching at the limit
        keeps the mock deterministic. Phase 2+ can add slippage when
        ``MockExchangeAdapter`` needs to model it (the market price is
        available via ``self._prices[order.symbol]`` if so).
        """
        cost = order.price.amount * order.amount.value
        fee = cost * self._fee_rate
        base, quote = order.symbol.base, order.symbol.quote

        if order.side is OrderSide.BUY:
            # Outflow: cost + fee in quote; Inflow: amount in base.
            self._balances[quote] = self._balances.get(quote, Decimal("0")) - cost - fee
            self._balances[base] = self._balances.get(base, Decimal("0")) + order.amount.value
        else:  # SELL
            self._balances[base] = self._balances.get(base, Decimal("0")) - order.amount.value
            self._balances[quote] = self._balances.get(quote, Decimal("0")) + cost - fee

        order.record_fill(filled_amount=order.amount.value)
        self._open_orders.pop(order.exchange_id, None)  # type: ignore[arg-type]

        self._trade_counter += 1
        trade = Trade(
            id=f"MOCK-TRD-{self._trade_counter:06d}",
            order_id=order.exchange_id or "",
            symbol=order.symbol,
            side=order.side,
            price=Price(amount=order.price.amount, currency=quote),
            amount=Amount(value=order.amount.value, asset=base),
            fee=fee,
            cost=cost,
            executed_at=Timestamp(dt=datetime.now(UTC)),
        )
        self._trade_history.append(trade)
        return trade

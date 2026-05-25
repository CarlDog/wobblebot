"""ShadowExchangeAdapter — live prices, simulated execution.

Composes a real ``ExchangePort`` (typically ``KrakenAdapter``) for
price discovery with an internal ``MockExchangeAdapter`` for order
matching against a synthetic balance ledger. The engine sees a normal
``ExchangePort`` — same code path as live trading; the only difference
is whose money moves.

Per ADR-008, this is the trading-simulation half of Stage 3.0.
Together with ``cli/observe`` it forms the Phase 3 sandbox.

**Maker vs taker fee assignment.** When ``place_order`` is called, the
adapter compares the order's limit price to the current live market
price:

- BUY with ``limit >= current_price`` is marketable (would cross the
  ask immediately) → tagged taker (default 0.40%)
- SELL with ``limit <= current_price`` is marketable (would cross the
  bid) → tagged taker
- All other orders sit on the book waiting → tagged maker (default
  0.26%)

This honestly models the fee schedule observed live in Phase 2
(Receipt 1: 0.40% taker on a marketable round-trip).

**Live-tape coupling.** Every ``get_current_price`` call fetches from
the live exchange AND pumps the price into the internal mock matcher
via ``set_price``. The mock fills any open orders whose limit the
live tape has crossed since the last poll. So the shadow fires fills
at poll cadence, not at the actual moment of crossing — ADR-008
acknowledges this as a small-order-size acceptable approximation.

**No withdrawals.** Shadow mode does not simulate fund transfers;
``withdraw`` raises ``NotImplementedError``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import OHLCBar, OrderSide, Price, Symbol
from wobblebot.ports.exchange import ExchangePort

_DEFAULT_MAKER_FEE_RATE = Decimal("0.0026")
_DEFAULT_TAKER_FEE_RATE = Decimal("0.0040")


class ShadowExchangeAdapter(ExchangePort):
    """``ExchangePort`` impl that uses a real exchange for prices but
    matches orders against a synthetic balance ledger.

    Args:
        live_exchange: Real ``ExchangePort`` for live price discovery
            (typically a ``KrakenAdapter``). Only ``get_current_price``
            is called against this; balances, orders, trades, and
            cancellations all go to the internal mock.
        starting_balances: Initial synthetic balances (e.g. ``{"USD":
            Decimal("10000"), "BTC": Decimal("0")}``). Operator
            specifies via ``--initial-shadow-usd`` etc. on the CLI;
            never inferred from the operator's real Kraken balances.
        maker_fee_rate: Fee for orders that sit on the book.
            Default 0.26% (Kraken's published maker rate).
        taker_fee_rate: Fee for marketable orders that cross the
            spread. Default 0.40% (Kraken's published taker rate at
            base tier; matches what we measured live in Receipt 1).
    """

    def __init__(
        self,
        live_exchange: ExchangePort,
        starting_balances: dict[str, Decimal],
        maker_fee_rate: Decimal = _DEFAULT_MAKER_FEE_RATE,
        taker_fee_rate: Decimal = _DEFAULT_TAKER_FEE_RATE,
    ) -> None:
        self._live = live_exchange
        self._maker_fee_rate = maker_fee_rate
        self._taker_fee_rate = taker_fee_rate
        # Mock starts at maker rate; per-call swap in place_order
        # toggles to taker for marketable orders. The _place_lock
        # serializes those swaps so concurrent place_order calls for
        # different symbols can't race on the shared _fee_rate.
        self._mock = MockExchangeAdapter(
            starting_balances=starting_balances,
            fee_rate=maker_fee_rate,
        )
        self._place_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the live exchange's resources if it has any. Mock has
        no I/O to close."""
        live_aclose = getattr(self._live, "aclose", None)
        if live_aclose is not None:
            await live_aclose()

    # ------------------------------------------------ ExchangePort: read paths

    async def get_current_price(self, symbol: Symbol) -> Price:
        """Fetch the live price AND pump it into the mock matcher.

        The matcher fills any open orders whose limit the new price has
        crossed since the last poll. So the engine's tick — which calls
        ``get_current_price`` once per symbol per tick — also drives
        the shadow's fill cadence.
        """
        price = await self._live.get_current_price(symbol)
        self._mock.set_price(symbol, price.amount)
        return price

    async def get_ohlc(
        self,
        symbol: Symbol,
        interval_minutes: int = 1,
        since: datetime | None = None,
    ) -> list[OHLCBar]:
        """Forward to the live adapter — OHLC is real-market historical
        data, not part of the synthetic ledger.

        Shadow mode's "synthetic" half is execution state (balances,
        orders, fills). Price discovery — current and historical — is
        always real-market via the wrapped live adapter.
        """
        return await self._live.get_ohlc(symbol, interval_minutes, since)

    async def get_balances(self) -> list[Balance]:
        return await self._mock.get_balances()

    async def get_balance(self, asset: str) -> Balance | None:
        return await self._mock.get_balance(asset)

    async def get_order_status(self, order: Order) -> Order:
        return await self._mock.get_order_status(order)

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        return await self._mock.get_open_orders(symbol)

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        return await self._mock.get_trade_history(symbol, limit)

    # ------------------------------------------------ ExchangePort: write paths

    async def place_order(self, order: Order) -> Order:
        """Submit ``order`` to the simulated matcher. Tags maker/taker
        based on whether the limit is marketable against the current
        live price.

        Serialized through ``_place_lock`` so the per-order fee swap on
        the shared ``_mock._fee_rate`` cannot race against concurrent
        calls for different symbols. Engine already serializes per-
        symbol; this lock adds cross-symbol serialization at the
        adapter level. Performance impact is negligible in shadow mode.
        """
        # Get the current live price (and pump it into the mock matcher
        # so any newly-crossing orders fill before we submit).
        current_price = (await self.get_current_price(order.symbol)).amount
        is_taker = self._is_marketable(order, current_price)

        async with self._place_lock:
            # MockExchangeAdapter._fee_rate is private but we own this
            # composition end-to-end and per-call swap is intentional —
            # the alternative (forking the mock or duplicating its
            # matching engine) is much worse.
            # pylint: disable=protected-access  # composed adapter; per-call
            # fee swap is intentional (see class docstring's fee model).
            self._mock._fee_rate = (  # noqa: SLF001
                self._taker_fee_rate if is_taker else self._maker_fee_rate
            )
            return await self._mock.place_order(order)

    async def cancel_order(self, order: Order) -> Order:
        return await self._mock.cancel_order(order)

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        raise NotImplementedError(
            "Shadow mode does not simulate withdrawals — that is Phase 4 territory"
        )

    # ------------------------------------------------ helpers

    @staticmethod
    def _is_marketable(order: Order, current_price: Decimal) -> bool:
        """Would this order cross the spread immediately?

        - BUY at limit >= current price: would buy at or above the ask → taker
        - SELL at limit <= current price: would sell at or below the bid → taker
        - Otherwise: order sits on the book waiting → maker
        """
        if order.side is OrderSide.BUY:
            return order.price.amount >= current_price
        return order.price.amount <= current_price

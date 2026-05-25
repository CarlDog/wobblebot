"""Unit tests for ShadowExchangeAdapter (Stage 3.0.2).

Test seam: a small ``StubExchange`` plays the role of the live
exchange (returns canned prices for ``get_current_price``). The
internal MockExchangeAdapter handles matching; we assert against its
observable behavior through the ShadowExchangeAdapter wrapper.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter
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

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


class _StubLiveExchange(ExchangePort):
    """Minimal ExchangePort that returns canned prices. Other methods
    raise — they should never be called via the shadow."""

    def __init__(self, prices: dict[Symbol, Decimal]) -> None:
        self._prices = dict(prices)
        self.price_call_count = 0

    def set_price(self, symbol: Symbol, price: Decimal) -> None:
        self._prices[symbol] = price

    async def get_current_price(self, symbol: Symbol) -> Price:
        self.price_call_count += 1
        if symbol not in self._prices:
            raise ExchangeError(f"no canned price for {symbol}")
        return Price(amount=self._prices[symbol], currency=symbol.quote)

    # --- everything else: NotImplementedError; shadow should NEVER call these
    async def get_balances(self) -> list[Balance]:
        raise NotImplementedError("shadow must use mock for balances")

    async def get_balance(self, asset: str) -> Balance | None:
        raise NotImplementedError

    async def place_order(self, order: Order) -> Order:
        raise NotImplementedError("shadow must use mock for placement")

    async def cancel_order(self, order: Order) -> Order:
        raise NotImplementedError

    async def get_order_status(self, order: Order) -> Order:
        raise NotImplementedError

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        raise NotImplementedError

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        raise NotImplementedError

    async def get_ohlc(
        self,
        symbol: Symbol,
        interval_minutes: int = 1,
        since: datetime | None = None,
    ) -> list[OHLCBar]:
        raise NotImplementedError("shadow tests don't exercise get_ohlc")

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        raise NotImplementedError


def _make_order(
    *,
    side: OrderSide = OrderSide.BUY,
    price: str = "50000",
    amount: str = "0.001",
) -> Order:
    return Order(
        symbol=BTC_USD,
        side=side,
        price=Price(amount=Decimal(price), currency="USD"),
        amount=Amount(value=Decimal(amount), asset="BTC"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


# ---------------------------------------------------------------------------
# Price discovery routes through live; balances/orders stay in mock
# ---------------------------------------------------------------------------


class TestRouting:
    async def test_get_current_price_queries_live(self) -> None:
        live = _StubLiveExchange({BTC_USD: Decimal("80000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("1000"), "BTC": Decimal("0")},
        )
        price = await shadow.get_current_price(BTC_USD)
        assert price.amount == Decimal("80000")
        assert live.price_call_count == 1

    async def test_balances_come_from_synthetic_ledger_not_live(self) -> None:
        live = _StubLiveExchange({BTC_USD: Decimal("80000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("12345"), "BTC": Decimal("1.5")},
        )
        balances = await shadow.get_balances()
        usd = next(b for b in balances if b.asset == "USD")
        btc = next(b for b in balances if b.asset == "BTC")
        assert usd.total == Decimal("12345")
        assert btc.total == Decimal("1.5")
        # live exchange's get_balances must not have been called
        assert live.price_call_count == 0


# ---------------------------------------------------------------------------
# Maker/taker fee assignment
# ---------------------------------------------------------------------------


class TestMakerVsTakerFee:
    async def test_buy_below_market_is_maker(self) -> None:
        """BUY at 49500 with market 50000 → sits on book → maker fee on fill."""
        live = _StubLiveExchange({BTC_USD: Decimal("50000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
            maker_fee_rate=Decimal("0.0026"),
            taker_fee_rate=Decimal("0.0040"),
        )
        await shadow.place_order(_make_order(price="49500"))
        # Drop market to fill the maker BUY
        live.set_price(BTC_USD, Decimal("49000"))
        await shadow.get_current_price(BTC_USD)  # pumps the price into mock matcher
        trades = await shadow.get_trade_history(symbol=BTC_USD)
        assert len(trades) == 1
        # cost = 49500 * 0.001 = 49.5; fee = cost * 0.26% = 0.1287
        assert trades[0].fee == Decimal("49.5") * Decimal("0.0026")

    async def test_buy_at_or_above_market_is_taker(self) -> None:
        """BUY at 50500 with market 50000 → marketable → taker fee on fill."""
        live = _StubLiveExchange({BTC_USD: Decimal("50000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
            maker_fee_rate=Decimal("0.0026"),
            taker_fee_rate=Decimal("0.0040"),
        )
        # Marketable BUY fills immediately at its limit (mock matches at limit price)
        await shadow.place_order(_make_order(price="50500"))
        trades = await shadow.get_trade_history(symbol=BTC_USD)
        assert len(trades) == 1
        # cost = 50500 * 0.001 = 50.5; fee = cost * 0.40% = 0.202
        assert trades[0].fee == Decimal("50.5") * Decimal("0.0040")

    async def test_sell_above_market_is_maker(self) -> None:
        """SELL at 51000 with market 50000 → sits on book → maker."""
        live = _StubLiveExchange({BTC_USD: Decimal("50000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("0"), "BTC": Decimal("0.001")},
        )
        await shadow.place_order(_make_order(side=OrderSide.SELL, price="51000"))
        live.set_price(BTC_USD, Decimal("51500"))
        await shadow.get_current_price(BTC_USD)
        trades = await shadow.get_trade_history(symbol=BTC_USD)
        assert len(trades) == 1
        # 0.26% maker
        assert trades[0].fee == Decimal("51000") * Decimal("0.001") * Decimal("0.0026")

    async def test_sell_at_or_below_market_is_taker(self) -> None:
        """SELL at 49500 with market 50000 → marketable → taker."""
        live = _StubLiveExchange({BTC_USD: Decimal("50000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("0"), "BTC": Decimal("0.001")},
        )
        await shadow.place_order(_make_order(side=OrderSide.SELL, price="49500"))
        trades = await shadow.get_trade_history(symbol=BTC_USD)
        assert len(trades) == 1
        # 0.40% taker
        assert trades[0].fee == Decimal("49500") * Decimal("0.001") * Decimal("0.0040")


# ---------------------------------------------------------------------------
# Live-tape coupling: get_current_price drives the matcher
# ---------------------------------------------------------------------------


class TestLiveTapeCoupling:
    async def test_resting_order_fills_when_live_price_crosses(self) -> None:
        """Place a maker BUY; advance the live tape past it; next
        get_current_price call triggers the fill via the mock matcher."""
        live = _StubLiveExchange({BTC_USD: Decimal("50000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
        )
        await shadow.place_order(_make_order(price="49500"))
        # No fill yet
        assert (await shadow.get_open_orders(BTC_USD))[0].status == "open"

        # Live tape drops below the maker BUY's limit
        live.set_price(BTC_USD, Decimal("49400"))
        await shadow.get_current_price(BTC_USD)  # pumps; mock matcher fills

        # The order is now closed
        opens = await shadow.get_open_orders(BTC_USD)
        assert opens == []
        trades = await shadow.get_trade_history(symbol=BTC_USD)
        assert len(trades) == 1


# ---------------------------------------------------------------------------
# Withdrawals are not modeled
# ---------------------------------------------------------------------------


class TestNoWithdrawals:
    async def test_withdraw_raises_not_implemented(self) -> None:
        live = _StubLiveExchange({BTC_USD: Decimal("80000")})
        shadow = ShadowExchangeAdapter(
            live_exchange=live,
            starting_balances={"USD": Decimal("100")},
        )
        with pytest.raises(NotImplementedError, match="Phase 4"):
            await shadow.withdraw("USD", Decimal("10"), "bank")

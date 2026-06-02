"""Unit tests for the MockExchangeAdapter.

The mock is a test double for ExchangePort. Tests treat it like a
real adapter: place orders, drive the market via set_price, verify
balances and trade history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.domain.exceptions import InsufficientBalance
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


def _buy_order(
    *,
    symbol: Symbol = BTC_USD,
    price: str = "50000",
    amount: str = "0.1",
) -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        price=Price(amount=Decimal(price), currency=symbol.quote),
        amount=Amount(value=Decimal(amount), asset=symbol.base),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _sell_order(
    *,
    symbol: Symbol = BTC_USD,
    price: str = "55000",
    amount: str = "0.1",
) -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.SELL,
        price=Price(amount=Decimal(price), currency=symbol.quote),
        amount=Amount(value=Decimal(amount), asset=symbol.base),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


class TestPriceAndBalances:
    async def test_get_current_price_requires_seeded_price(self) -> None:
        exch = MockExchangeAdapter()
        with pytest.raises(ExchangeError, match="No market price"):
            await exch.get_current_price(BTC_USD)

    async def test_get_current_price_returns_seeded(self) -> None:
        exch = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("50000")})
        price = await exch.get_current_price(BTC_USD)
        assert price.amount == Decimal("50000")
        assert price.currency == "USD"

    async def test_get_balance_returns_none_for_never_held(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("1000")})
        assert await exch.get_balance("BTC") is None

    async def test_get_balance_zero_distinct_from_never_held(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("0")})
        balance = await exch.get_balance("USD")
        assert balance is not None
        assert balance.total == Decimal("0")

    async def test_get_balances_returns_all_known_assets(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("1000"), "BTC": Decimal("0.5")}
        )
        balances = await exch.get_balances()
        assert sorted(b.asset for b in balances) == ["BTC", "USD"]


class TestOrderPlacement:
    async def test_buy_requires_quote_balance(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("10")})
        # Order cost: 50000 * 0.001 = 50 USD + fee. 10 USD is insufficient.
        with pytest.raises(InsufficientBalance):
            await exch.place_order(_buy_order(amount="0.001"))

    async def test_sell_requires_base_balance(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("100000")})
        with pytest.raises(InsufficientBalance):
            await exch.place_order(_sell_order(amount="0.1"))

    async def test_place_buy_marks_open_with_exchange_id(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("10000")})
        order = await exch.place_order(_buy_order())
        assert order.status == "open"
        assert order.exchange_id is not None
        assert order.exchange_id.startswith("MOCK-ORD-")

    async def test_buy_locks_quote_funds_with_fee_reserve(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("10000")})
        await exch.place_order(_buy_order(price="50000", amount="0.1"))
        # cost = 5000, fee_reserve = 5000 * 0.0026 = 13
        balance = await exch.get_balance("USD")
        assert balance is not None
        assert balance.locked == Decimal("5013")
        assert balance.available == Decimal("4987")


class TestOrderMatching:
    async def test_buy_fills_when_price_drops_to_limit(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("10000")},
            starting_prices={BTC_USD: Decimal("51000")},
        )
        await exch.place_order(_buy_order(price="50000", amount="0.1"))

        # Price drops below limit -> fills.
        fills = exch.set_price(BTC_USD, Decimal("49999"))
        assert len(fills) == 1
        trade = fills[0]
        assert trade.side is OrderSide.BUY
        assert trade.amount.value == Decimal("0.1")
        # Cost recorded at the order's limit price (5000), not market price.
        assert trade.cost == Decimal("5000")
        assert trade.fee == Decimal("13")

    async def test_buy_fills_immediately_when_market_already_below(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("10000")},
            starting_prices={BTC_USD: Decimal("45000")},  # already below limit
        )
        order = await exch.place_order(_buy_order(price="50000", amount="0.1"))
        assert order.status == "closed"
        history = await exch.get_trade_history()
        assert len(history) == 1

    async def test_buy_does_not_fill_above_limit(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("10000")},
            starting_prices={BTC_USD: Decimal("51000")},
        )
        await exch.place_order(_buy_order(price="50000", amount="0.1"))
        # Price moves up - still no fill.
        exch.set_price(BTC_USD, Decimal("52000"))
        open_orders = await exch.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0].status == "open"

    async def test_sell_fills_when_price_rises_to_limit(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"BTC": Decimal("1"), "USD": Decimal("0")},
            starting_prices={BTC_USD: Decimal("49000")},
        )
        await exch.place_order(_sell_order(price="55000", amount="0.1"))
        fills = exch.set_price(BTC_USD, Decimal("55000"))
        assert len(fills) == 1
        assert fills[0].side is OrderSide.SELL

    async def test_fill_debits_quote_and_credits_base_for_buy(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("10000")},
            starting_prices={BTC_USD: Decimal("49000")},
        )
        await exch.place_order(_buy_order(price="50000", amount="0.1"))
        usd = await exch.get_balance("USD")
        btc = await exch.get_balance("BTC")
        assert usd is not None and btc is not None
        # 10000 - 5000 (cost) - 13 (fee) = 4987 USD; got 0.1 BTC.
        assert usd.total == Decimal("4987")
        assert btc.total == Decimal("0.1")

    async def test_fill_credits_quote_minus_fee_for_sell(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"BTC": Decimal("1"), "USD": Decimal("0")},
            starting_prices={BTC_USD: Decimal("49000")},
        )
        await exch.place_order(_sell_order(price="55000", amount="0.1"))
        exch.set_price(BTC_USD, Decimal("55000"))
        usd = await exch.get_balance("USD")
        btc = await exch.get_balance("BTC")
        assert usd is not None and btc is not None
        # Proceeds: 5500, fee: 5500 * 0.0026 = 14.3. Net 5485.7 USD.
        assert usd.total == Decimal("5485.7")
        assert btc.total == Decimal("0.9")

    async def test_run_scenario_aggregates_fills(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("0.2")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        # Two buys at decreasing limits, one sell above. Walk prices to fill all.
        await exch.place_order(_buy_order(price="49000", amount="0.05"))
        await exch.place_order(_buy_order(price="47000", amount="0.05"))
        await exch.place_order(_sell_order(price="55000", amount="0.1"))

        fills = exch.run_scenario(
            [
                (BTC_USD, Decimal("49000")),  # first buy fills
                (BTC_USD, Decimal("46500")),  # second buy fills
                (BTC_USD, Decimal("55001")),  # sell fills
            ]
        )
        assert len(fills) == 3
        assert [t.side for t in fills] == [OrderSide.BUY, OrderSide.BUY, OrderSide.SELL]


class TestOrderManagement:
    async def test_cancel_removes_from_open(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("10000")})
        order = await exch.place_order(_buy_order())
        canceled = await exch.cancel_order(order)
        assert canceled.status == "canceled"
        assert await exch.get_open_orders() == []

    async def test_cancel_unknown_raises(self) -> None:
        exch = MockExchangeAdapter()
        unknown = _buy_order()
        unknown.mark_open(exchange_id="BOGUS-ID")
        with pytest.raises(ExchangeError, match="Unknown order"):
            await exch.cancel_order(unknown)

    async def test_get_open_orders_filters_by_symbol(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("20000")})
        await exch.place_order(_buy_order(symbol=BTC_USD, price="50000", amount="0.1"))
        await exch.place_order(_buy_order(symbol=ETH_USD, price="3000", amount="1"))
        btc_only = await exch.get_open_orders(BTC_USD)
        assert [o.symbol for o in btc_only] == [BTC_USD]

    async def test_set_dead_mans_switch_records_value(self) -> None:
        # No-op (no server-side timer), but records the value so engine-loop
        # tests can assert the loop armed/disarmed the switch (ADR-021).
        exch = MockExchangeAdapter()
        assert exch.last_dead_mans_switch_seconds is None
        await exch.set_dead_mans_switch(60)
        assert exch.last_dead_mans_switch_seconds == 60
        await exch.set_dead_mans_switch(0)
        assert exch.last_dead_mans_switch_seconds == 0

    async def test_set_dead_mans_switch_negative_raises(self) -> None:
        exch = MockExchangeAdapter()
        with pytest.raises(ValueError, match=">= 0"):
            await exch.set_dead_mans_switch(-5)


class TestWithdraw:
    async def test_withdraw_decrements_balance(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("1000")})
        txid = await exch.withdraw("USD", Decimal("400"), "bank-account-1")
        assert txid.startswith("MOCK-WDR-")
        balance = await exch.get_balance("USD")
        assert balance is not None
        assert balance.total == Decimal("600")

    async def test_withdraw_rejects_negative(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("1000")})
        with pytest.raises(ExchangeError, match="must be positive"):
            await exch.withdraw("USD", Decimal("-1"), "bank")

    async def test_withdraw_rejects_overdraft(self) -> None:
        exch = MockExchangeAdapter(starting_balances={"USD": Decimal("100")})
        with pytest.raises(InsufficientBalance):
            await exch.withdraw("USD", Decimal("1000"), "bank")


class TestTradeHistory:
    async def test_history_is_newest_first(self) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("0.5")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        await exch.place_order(_buy_order(price="49000", amount="0.05"))
        await exch.place_order(_buy_order(price="47000", amount="0.05"))
        exch.run_scenario([(BTC_USD, Decimal("49000")), (BTC_USD, Decimal("46500"))])

        history = await exch.get_trade_history()
        assert len(history) == 2
        # Second fill (lower price) is newest.
        assert history[0].price.amount == Decimal("47000")
        assert history[1].price.amount == Decimal("49000")

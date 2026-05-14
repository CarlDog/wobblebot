"""Unit tests for the DataCollector service.

Happy paths drive a real ``MockExchangeAdapter`` — gives confidence
the service composes correctly against any ``ExchangePort`` impl, not
just a Mock-shaped one. Error-wrapping paths use a tiny
``_FailingExchange`` test double so we control the exception message
the wrapper sees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Price, Symbol
from wobblebot.ports.exceptions import DataCollectorError, ExchangeError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.services.data_collector import DataCollector

pytestmark = pytest.mark.unit

BTC_USD = Symbol(base="BTC", quote="USD")


class _FailingExchange(ExchangePort):
    """Minimal ExchangePort that raises ``ExchangeError`` on every call.

    Used to verify the service wraps upstream errors. Only the read
    methods called by ``DataCollector`` need real bodies; the rest
    can defer to NotImplementedError since these tests never invoke them.
    """

    def __init__(self, message: str = "simulated upstream failure") -> None:
        self._message = message

    async def get_current_price(self, symbol: Symbol) -> Price:
        raise ExchangeError(self._message)

    async def get_balances(self) -> list[Balance]:
        raise ExchangeError(self._message)

    async def get_balance(self, asset: str) -> Balance | None:
        raise ExchangeError(self._message)

    async def place_order(self, order: Order) -> Order:
        raise NotImplementedError

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

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        raise NotImplementedError


@pytest.mark.asyncio
class TestGetCurrentPrice:
    async def test_passes_through_to_exchange(self) -> None:
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("50000")})
        collector = DataCollector(exchange=exchange)

        price = await collector.get_current_price(BTC_USD)

        assert price.amount == Decimal("50000")
        assert price.currency == "USD"

    async def test_wraps_exchange_error(self) -> None:
        collector = DataCollector(exchange=_FailingExchange("upstream boom"))

        with pytest.raises(DataCollectorError, match="Failed to retrieve price"):
            await collector.get_current_price(BTC_USD)

    async def test_preserves_exception_chain(self) -> None:
        collector = DataCollector(exchange=_FailingExchange("upstream boom"))

        with pytest.raises(DataCollectorError) as exc_info:
            await collector.get_current_price(BTC_USD)

        assert isinstance(exc_info.value.__cause__, ExchangeError)
        assert str(exc_info.value.__cause__) == "upstream boom"


@pytest.mark.asyncio
class TestGetMarketSnapshot:
    async def test_returns_symbol_price_and_recent_timestamp(self) -> None:
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("50000")})
        collector = DataCollector(exchange=exchange)

        before = datetime.now(UTC)
        snapshot = await collector.get_market_snapshot(BTC_USD)
        after = datetime.now(UTC)

        assert snapshot.symbol == BTC_USD
        assert snapshot.price.amount == Decimal("50000")
        # Timestamp should be between the before/after wall-clock readings.
        assert before <= snapshot.timestamp.dt <= after + timedelta(seconds=1)

    async def test_upstream_failure_propagates_as_data_collector_error(self) -> None:
        collector = DataCollector(exchange=_FailingExchange())

        with pytest.raises(DataCollectorError):
            await collector.get_market_snapshot(BTC_USD)


@pytest.mark.asyncio
class TestGetBalances:
    async def test_passes_through_to_exchange(self) -> None:
        exchange = MockExchangeAdapter(
            starting_balances={"BTC": Decimal("1.5"), "USD": Decimal("10000")}
        )
        collector = DataCollector(exchange=exchange)

        balances = await collector.get_balances()

        by_asset = {b.asset: b for b in balances}
        assert set(by_asset) == {"BTC", "USD"}
        assert by_asset["BTC"].total == Decimal("1.5")
        assert by_asset["USD"].total == Decimal("10000")

    async def test_wraps_exchange_error(self) -> None:
        collector = DataCollector(exchange=_FailingExchange("balances down"))

        with pytest.raises(DataCollectorError, match="Failed to retrieve balances"):
            await collector.get_balances()

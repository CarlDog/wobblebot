"""Unit tests for the DataCollector service.

Happy paths drive a real ``MockExchangeAdapter`` — gives confidence
the service composes correctly against any ``ExchangePort`` impl, not
just a Mock-shaped one. Error-wrapping paths use a tiny
``_FailingExchange`` test double so we control the exception message
the wrapper sees.

Stage 3.1 added a ``StoragePort`` dependency. Tests use a real
``SQLiteStorageAdapter(":memory:")`` per test so we get the actual
write/read round-trip for the metric methods, not a stub that could
drift from the production adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import DataCollectorError, ExchangeError, StorageError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.storage import StoragePort
from wobblebot.services.data_collector import DataCollector
from wobblebot.services.metrics import CycleStats

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

    async def set_dead_mans_switch(self, timeout_seconds: int) -> None:
        raise NotImplementedError

    async def get_order_status(self, order: Order) -> Order:
        raise NotImplementedError

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        raise NotImplementedError

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        raise NotImplementedError

    async def get_ohlc(self, symbol, interval_minutes=1, since=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError("data collector tests don't exercise OHLC")

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        raise NotImplementedError


class _FailingStorage(StoragePort):
    """Minimal StoragePort that raises ``StorageError`` from each read.

    Used to verify the metric methods wrap upstream storage failures.
    """

    def __init__(self, message: str = "simulated storage failure") -> None:
        self._message = message

    async def save_order(self, order: Order) -> None:
        raise NotImplementedError

    async def get_order(self, order_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        raise NotImplementedError

    async def get_orders(
        self,
        symbol: Symbol | None = None,
        side: str | None = None,
        created_after: datetime | None = None,
    ) -> list[Order]:
        raise NotImplementedError

    async def save_trade(self, trade: Trade) -> None:
        raise NotImplementedError

    async def get_trades(
        self,
        symbol: Symbol | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[Trade]:
        raise StorageError(self._message)

    async def save_balance_snapshot(self, balances: list[Balance]) -> None:
        raise NotImplementedError

    async def get_latest_balance_snapshot(self) -> list[Balance]:
        raise NotImplementedError

    async def save_grid_state(self, state):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_grid_state(self, symbol: Symbol):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def save_price_snapshot(
        self,
        symbol: Symbol,
        price: Price,
        observed_at: Timestamp,
    ) -> None:
        raise NotImplementedError

    async def get_price_snapshots(
        self,
        symbol: Symbol | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = None,
    ):
        raise StorageError(self._message)

    async def delete_price_snapshots(self, *, before: datetime) -> int:
        raise StorageError(self._message)

    async def save_price_snapshots(self, snapshots):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def save_ohlc_bars(self, bars):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_latest_observed_at(self, symbol):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def save_news_item(self, item):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_news_items(
        self,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ):
        raise NotImplementedError

    async def save_advisor_suggestion(self, suggestion):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_advisor_suggestions(
        self,
        since: datetime | None = None,
        model_name: str | None = None,
        role: str | None = None,
        limit: int | None = None,
    ):
        raise NotImplementedError

    async def save_applied_suggestion(self, applied):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_applied_suggestions(
        self,
        since: datetime | None = None,
        symbol: str | None = None,
        model_name: str | None = None,
        limit: int | None = None,
    ):
        raise NotImplementedError

    async def save_transfer_proposal(self, proposal):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_transfer_proposals(
        self,
        since: datetime | None = None,
        direction: str | None = None,
        asset: str | None = None,
        limit: int | None = None,
    ):
        raise NotImplementedError

    async def save_transfer_result(self, result):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_transfer_results(
        self,
        since: datetime | None = None,
        status: str | None = None,
        asset: str | None = None,
        direction: str | None = None,
        limit: int | None = None,
    ):
        raise NotImplementedError

    async def save_pending_command(self, pending):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_pending_command(self, pending_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_pending_commands(self, status=None, limit=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def save_notification(self, notification):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_notifications(self, forwarded=None, limit=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def mark_notification_forwarded(  # type: ignore[no-untyped-def]
        self, notification_id, forwarded_at
    ):
        raise NotImplementedError

    async def save_conversation_turn(self, turn):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_conversation_turns(  # type: ignore[no-untyped-def]
        self, channel_id, user_id, limit=None
    ):
        raise NotImplementedError

    async def save_llm_call(self, record):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_llm_calls(  # type: ignore[no-untyped-def]
        self, since=None, role=None, provider=None, limit=None
    ):
        raise NotImplementedError

    async def create_user(self, username, password_hash):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_user_by_username(self, username):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def update_user_last_login(self, user_id, last_login_at):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_user_preferences(self, user_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def update_user_preferences(self, preferences):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def upsert_daemon_heartbeat(self, name, beat_at):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_daemon_heartbeats(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def save_status_report_taken(self, channel_id, user_id, taken_at):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_last_status_report_taken_at(self, channel_id, user_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest.mark.asyncio
class TestGetCurrentPrice:
    async def test_passes_through_to_exchange(self, storage: SQLiteStorageAdapter) -> None:
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("50000")})
        collector = DataCollector(exchange=exchange, storage=storage)

        price = await collector.get_current_price(BTC_USD)

        assert price.amount == Decimal("50000")
        assert price.currency == "USD"

    async def test_wraps_exchange_error(self, storage: SQLiteStorageAdapter) -> None:
        collector = DataCollector(exchange=_FailingExchange("upstream boom"), storage=storage)

        with pytest.raises(DataCollectorError, match="Failed to retrieve price"):
            await collector.get_current_price(BTC_USD)

    async def test_preserves_exception_chain(self, storage: SQLiteStorageAdapter) -> None:
        collector = DataCollector(exchange=_FailingExchange("upstream boom"), storage=storage)

        with pytest.raises(DataCollectorError) as exc_info:
            await collector.get_current_price(BTC_USD)

        assert isinstance(exc_info.value.__cause__, ExchangeError)
        assert str(exc_info.value.__cause__) == "upstream boom"


@pytest.mark.asyncio
class TestGetMarketSnapshot:
    async def test_returns_symbol_price_and_recent_timestamp(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("50000")})
        collector = DataCollector(exchange=exchange, storage=storage)

        before = datetime.now(UTC)
        snapshot = await collector.get_market_snapshot(BTC_USD)
        after = datetime.now(UTC)

        assert snapshot.symbol == BTC_USD
        assert snapshot.price.amount == Decimal("50000")
        # Timestamp should be between the before/after wall-clock readings.
        assert before <= snapshot.timestamp.dt <= after + timedelta(seconds=1)

    async def test_upstream_failure_propagates_as_data_collector_error(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        collector = DataCollector(exchange=_FailingExchange(), storage=storage)

        with pytest.raises(DataCollectorError):
            await collector.get_market_snapshot(BTC_USD)


@pytest.mark.asyncio
class TestGetBalances:
    async def test_passes_through_to_exchange(self, storage: SQLiteStorageAdapter) -> None:
        exchange = MockExchangeAdapter(
            starting_balances={"BTC": Decimal("1.5"), "USD": Decimal("10000")}
        )
        collector = DataCollector(exchange=exchange, storage=storage)

        balances = await collector.get_balances()

        by_asset = {b.asset: b for b in balances}
        assert set(by_asset) == {"BTC", "USD"}
        assert by_asset["BTC"].total == Decimal("1.5")
        assert by_asset["USD"].total == Decimal("10000")

    async def test_wraps_exchange_error(self, storage: SQLiteStorageAdapter) -> None:
        collector = DataCollector(exchange=_FailingExchange("balances down"), storage=storage)

        with pytest.raises(DataCollectorError, match="Failed to retrieve balances"):
            await collector.get_balances()


@pytest.mark.asyncio
class TestGetPriceHistory:
    """Stage 3.1 — pulls persisted snapshots from storage over a lookback."""

    async def _seed(self, storage: SQLiteStorageAdapter) -> datetime:
        now = datetime.now(UTC)
        for minutes_ago, amount in [(120, "100"), (60, "101"), (30, "102"), (5, "103")]:
            await storage.save_price_snapshot(
                BTC_USD,
                Price(amount=Decimal(amount), currency="USD"),
                Timestamp(dt=now - timedelta(minutes=minutes_ago)),
            )
        return now

    async def test_returns_snapshots_within_lookback(self, storage: SQLiteStorageAdapter) -> None:
        await self._seed(storage)
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=storage)

        # 90-minute lookback should exclude the oldest (120 min ago) point.
        result = await collector.get_price_history(BTC_USD, lookback=timedelta(minutes=90))
        amounts = [snap.price.amount for snap in result]
        assert amounts == [Decimal("101"), Decimal("102"), Decimal("103")]

    async def test_empty_when_no_data_in_window(self, storage: SQLiteStorageAdapter) -> None:
        # No seed.
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=storage)
        result = await collector.get_price_history(BTC_USD, lookback=timedelta(hours=1))
        assert result == []

    async def test_default_lookback_is_24h(self, storage: SQLiteStorageAdapter) -> None:
        await self._seed(storage)
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=storage)
        # All four seed points are within 24h.
        result = await collector.get_price_history(BTC_USD)
        assert len(result) == 4

    async def test_storage_error_wrapped(self) -> None:
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=_FailingStorage())
        with pytest.raises(DataCollectorError, match="Failed to load price history"):
            await collector.get_price_history(BTC_USD)


@pytest.mark.asyncio
class TestMetricMethods:
    """Stage 3.1 — windowed metric reads."""

    async def _seed_prices(self, storage: SQLiteStorageAdapter) -> None:
        now = datetime.now(UTC)
        # 100 → 105 → 103 → 110 → 108 over the past 20 minutes
        for minutes_ago, amount in [
            (20, "100"),
            (15, "105"),
            (10, "103"),
            (5, "110"),
            (1, "108"),
        ]:
            await storage.save_price_snapshot(
                BTC_USD,
                Price(amount=Decimal(amount), currency="USD"),
                Timestamp(dt=now - timedelta(minutes=minutes_ago)),
            )

    async def test_volatility_computes_from_window(self, storage: SQLiteStorageAdapter) -> None:
        await self._seed_prices(storage)
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=storage)
        vol = await collector.get_volatility(BTC_USD, lookback=timedelta(hours=1))
        # Non-zero, positive: the series has movement.
        assert vol > Decimal("0")

    async def test_volatility_zero_when_no_data(self, storage: SQLiteStorageAdapter) -> None:
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=storage)
        assert await collector.get_volatility(BTC_USD, lookback=timedelta(hours=1)) == Decimal("0")

    async def test_max_drawdown_negative_after_dip(self, storage: SQLiteStorageAdapter) -> None:
        await self._seed_prices(storage)
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=storage)
        dd = await collector.get_max_drawdown(BTC_USD, lookback=timedelta(hours=1))
        # Peak 110 → trough 108 = -1.8%-ish (and 105 → 103 along the way)
        assert dd < Decimal("0")

    async def test_flatness_returns_value_in_unit_interval(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await self._seed_prices(storage)
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=storage)
        flatness = await collector.get_flatness(BTC_USD, lookback=timedelta(hours=1))
        assert Decimal("0") <= flatness <= Decimal("1")

    async def test_cycle_stats_with_trades(self, storage: SQLiteStorageAdapter) -> None:
        # Persist a buy then a profitable sell.
        base = datetime.now(UTC) - timedelta(minutes=30)
        buy = Trade(
            id="T-B-1",
            order_id="O-B-1",
            symbol=BTC_USD,
            side=OrderSide.BUY,
            price=Price(amount=Decimal("80000"), currency="USD"),
            amount=Amount(value=Decimal("0.0001"), asset="BTC"),
            fee=Decimal("0.04"),
            cost=Decimal("10.00"),
            executed_at=Timestamp(dt=base),
        )
        sell = Trade(
            id="T-S-1",
            order_id="O-S-1",
            symbol=BTC_USD,
            side=OrderSide.SELL,
            price=Price(amount=Decimal("81000"), currency="USD"),
            amount=Amount(value=Decimal("0.0001"), asset="BTC"),
            fee=Decimal("0.04"),
            cost=Decimal("11.00"),
            executed_at=Timestamp(dt=base + timedelta(minutes=5)),
        )
        await storage.save_trade(buy)
        await storage.save_trade(sell)
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("80000")})
        collector = DataCollector(exchange=exchange, storage=storage)
        stats = await collector.get_cycle_stats(BTC_USD, lookback=timedelta(hours=1))
        assert isinstance(stats, CycleStats)
        assert stats.cycle_count == 1
        assert stats.total_pnl == Decimal("0.92")

    async def test_cycle_stats_storage_error_wrapped(self) -> None:
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("100")})
        collector = DataCollector(exchange=exchange, storage=_FailingStorage())
        with pytest.raises(DataCollectorError, match="Failed to load trade history"):
            await collector.get_cycle_stats(BTC_USD)

    async def test_cycle_stats_handles_desc_storage_order(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """storage.get_trades returns DESC by executed_at; the collector must
        re-sort to ASC before passing to compute_cycle_stats. This test would
        fail with reversed PnL signs (-0.92) if that reverse step were dropped.
        """
        base = datetime.now(UTC) - timedelta(minutes=30)
        # Save in correct chronological order (oldest first).
        buy = Trade(
            id="T-B-1",
            order_id="O-B-1",
            symbol=BTC_USD,
            side=OrderSide.BUY,
            price=Price(amount=Decimal("80000"), currency="USD"),
            amount=Amount(value=Decimal("0.0001"), asset="BTC"),
            fee=Decimal("0"),
            cost=Decimal("10.00"),
            executed_at=Timestamp(dt=base),
        )
        sell = Trade(
            id="T-S-1",
            order_id="O-S-1",
            symbol=BTC_USD,
            side=OrderSide.SELL,
            price=Price(amount=Decimal("81000"), currency="USD"),
            amount=Amount(value=Decimal("0.0001"), asset="BTC"),
            fee=Decimal("0"),
            cost=Decimal("11.00"),
            executed_at=Timestamp(dt=base + timedelta(minutes=5)),
        )
        await storage.save_trade(buy)
        await storage.save_trade(sell)
        exchange = MockExchangeAdapter(starting_prices={BTC_USD: Decimal("80000")})
        collector = DataCollector(exchange=exchange, storage=storage)
        stats = await collector.get_cycle_stats(BTC_USD, lookback=timedelta(hours=1))
        # +1.00 PnL on the cycle confirms ASC ordering: buy matched then sell closed.
        assert stats.total_pnl == Decimal("1.00")

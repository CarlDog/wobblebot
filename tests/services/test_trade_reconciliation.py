"""Tests for services.trade_reconciliation (2026-08-22, post-incident).

Pins the core diff logic shared by cli/maintenance's scheduled
reconcile task and tools/reconcile_trade_history.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.services.trade_reconciliation import reconcile_symbol_trades

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


class _FakeExchange:
    """Minimal ExchangePort shape: only get_trade_history is exercised."""

    def __init__(self, trades: list[Trade]) -> None:
        self._trades = trades

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        if symbol is None:
            return list(self._trades)
        return [t for t in self._trades if t.symbol == symbol][:limit]


def _trade(trade_id: str, *, side: str = "buy", amount: str = "0.001") -> Trade:
    return Trade(
        id=trade_id,
        order_id=f"O-{trade_id}",
        symbol=BTC_USD,
        side=OrderSide(side),
        price=Price(amount=Decimal("50000"), currency="USD"),
        amount=Amount(value=Decimal(amount), asset="BTC"),
        fee=Decimal("0.02"),
        cost=Decimal("50"),
        executed_at=Timestamp(dt=datetime(2026, 5, 15, tzinfo=UTC)),
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestReconcileSymbolTrades:
    async def test_matching_trades_report_clean(self, storage: SQLiteStorageAdapter) -> None:
        t1, t2 = _trade("T1"), _trade("T2")
        await storage.save_trade(t1)
        await storage.save_trade(t2)
        exchange = _FakeExchange([t1, t2])

        result = await reconcile_symbol_trades(exchange, storage, BTC_USD)  # type: ignore[arg-type]

        assert result.is_clean
        assert result.missing_locally == ()
        assert result.kraken_trade_count == 2
        assert result.local_trade_count == 2

    async def test_trade_missing_locally_is_flagged(self, storage: SQLiteStorageAdapter) -> None:
        """The exact confirmed incident shape: Kraken reports a trade
        that never made it into local storage."""
        t1 = _trade("T1")
        orphaned = _trade("T2-ORPHAN", side="sell", amount="0.0005")
        await storage.save_trade(t1)  # only T1 persisted locally
        exchange = _FakeExchange([t1, orphaned])

        result = await reconcile_symbol_trades(exchange, storage, BTC_USD)  # type: ignore[arg-type]

        assert not result.is_clean
        assert result.missing_locally == (orphaned,)
        assert result.kraken_trade_count == 2
        assert result.local_trade_count == 1

    async def test_local_only_trade_is_not_flagged(self, storage: SQLiteStorageAdapter) -> None:
        """A trade present locally but NOT reported by Kraken is not
        this check's concern (a different, hypothetical failure mode
        -- e.g. a locally-fabricated row -- outside this scope)."""
        t1 = _trade("T1")
        local_only = _trade("T2-LOCAL-ONLY")
        await storage.save_trade(t1)
        await storage.save_trade(local_only)
        exchange = _FakeExchange([t1])

        result = await reconcile_symbol_trades(exchange, storage, BTC_USD)  # type: ignore[arg-type]

        assert result.is_clean
        assert result.local_trade_count == 2
        assert result.kraken_trade_count == 1

    async def test_empty_both_sides_is_clean(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _FakeExchange([])

        result = await reconcile_symbol_trades(exchange, storage, BTC_USD)  # type: ignore[arg-type]

        assert result.is_clean
        assert result.kraken_trade_count == 0
        assert result.local_trade_count == 0

    async def test_multiple_missing_trades_all_reported(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """2026-08-22 BTC incident shape: several missing trades at once."""
        present = _trade("PRESENT")
        missing = [_trade(f"MISSING-{i}") for i in range(3)]
        await storage.save_trade(present)
        exchange = _FakeExchange([present, *missing])

        result = await reconcile_symbol_trades(exchange, storage, BTC_USD)  # type: ignore[arg-type]

        assert len(result.missing_locally) == 3
        assert {t.id for t in result.missing_locally} == {t.id for t in missing}

    async def test_other_symbol_trades_do_not_leak_in(self, storage: SQLiteStorageAdapter) -> None:
        eth_usd = Symbol(base="ETH", quote="USD")
        btc_trade = _trade("BTC-1")
        eth_trade = Trade(
            id="ETH-1",
            order_id="O-ETH-1",
            symbol=eth_usd,
            side=OrderSide.BUY,
            price=Price(amount=Decimal("2000"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="ETH"),
            fee=Decimal("0.01"),
            cost=Decimal("200"),
            executed_at=Timestamp(dt=datetime(2026, 5, 15, tzinfo=UTC)),
        )
        await storage.save_trade(btc_trade)
        await storage.save_trade(eth_trade)
        exchange = _FakeExchange([btc_trade, eth_trade])

        result = await reconcile_symbol_trades(exchange, storage, BTC_USD)  # type: ignore[arg-type]

        assert result.kraken_trade_count == 1
        assert result.local_trade_count == 1
        assert result.is_clean

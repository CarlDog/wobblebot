"""Multi-coin OpenOrders batching — the 2026-06-02 rate-limit fix.

cli/live used to call Kraken's private ``OpenOrders`` endpoint once PER
SYMBOL each tick (via ``GridEngine._detect_fills``) and once per symbol on
shutdown (via ``_cancel_all_open``). Kraken's ``OpenOrders`` returns the
whole account in a single call, so at five coins this fired ~5x the
necessary private calls and tripped ``EAPI:Rate limit exceeded``. The fix
fetches the account's open orders ONCE and hands the snapshot to every
symbol's ``step()`` / to the shutdown cancel loop.

These tests pin the batched behaviour:

- ``_run_one_tick`` fetches open orders exactly once and passes the same
  snapshot to each ``engine.step``.
- A failed global fetch skips the tick's steps (no per-symbol fallback
  storm that would worsen the rate limit).
- ``_cancel_all_open`` fetches once globally (not per symbol) and only
  cancels configured symbols.
- The engine uses the passed snapshot for fill detection instead of
  re-fetching from the exchange.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import live as live_module
from wobblebot.cli.live import _cancel_all_open, _run_one_tick
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_SOL = Symbol(base="SOL", quote="USD")


def _order(symbol: Symbol, exchange_id: str, side: str = "buy") -> Order:
    return Order(
        id=uuid4(),
        exchange_id=exchange_id,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        price=Price(amount=Decimal("100"), currency="USD"),
        amount=Amount(value=Decimal("0.01"), asset=symbol.base),
        status="open",
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _live_cfg(symbols: list[Symbol]) -> LiveConfig:
    return LiveConfig(
        symbols=symbols,
        db=":memory:",
        tick_seconds=5.0,
        max_runtime_minutes=None,
        max_session_loss_usd=Decimal("150"),
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


# --------------------------------------------------------------------------- #
# _run_one_tick: one global OpenOrders fetch, snapshot fanned out to steps     #
# --------------------------------------------------------------------------- #


class TestTickBatchesOpenOrders:
    async def test_one_global_fetch_snapshot_passed_to_each_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_trip(_adapter: Any, _symbols: Any) -> Decimal:
            return Decimal("100")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", no_trip)

        snapshot = [_order(_BTC, "B1"), _order(_ETH, "E1")]
        adapter = MagicMock()
        adapter.get_open_orders = AsyncMock(return_value=snapshot)
        engine = MagicMock()
        engine.step = AsyncMock(return_value=MagicMock(action="stepped", fills=0))

        result = await _run_one_tick(
            adapter=adapter,
            engine=engine,
            live=_live_cfg([_BTC, _ETH, _SOL]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )

        assert result is False
        # ONE global fetch (no symbol kwarg), regardless of three symbols.
        adapter.get_open_orders.assert_awaited_once_with()
        # Each symbol stepped, every step handed the SAME snapshot object.
        assert engine.step.await_count == 3
        for call in engine.step.await_args_list:
            assert call.kwargs["exchange_open_orders"] is snapshot

    async def test_failed_global_fetch_skips_all_steps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rate-limited global fetch must NOT fall back to per-symbol
        fetches (that storm is exactly what blew the limit). Skip the
        tick's steps; the loss-cap check still runs."""

        async def no_trip(_adapter: Any, _symbols: Any) -> Decimal:
            return Decimal("100")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", no_trip)

        adapter = MagicMock()
        adapter.get_open_orders = AsyncMock(side_effect=ExchangeError("EAPI:Rate limit exceeded"))
        engine = MagicMock()
        engine.step = AsyncMock()

        result = await _run_one_tick(
            adapter=adapter,
            engine=engine,
            live=_live_cfg([_BTC, _ETH]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )

        assert result is False
        engine.step.assert_not_awaited()


# --------------------------------------------------------------------------- #
# _cancel_all_open: one global fetch on shutdown, configured-only cancels      #
# --------------------------------------------------------------------------- #


class _CancelSpyAdapter:
    """Counts global vs per-symbol get_open_orders calls; records cancels."""

    def __init__(self, open_orders: list[Order]) -> None:
        self._open = open_orders
        self.global_fetches = 0
        self.per_symbol_fetches = 0
        self.cancelled: list[str] = []

    async def get_open_orders(self, *, symbol: Symbol | None = None) -> list[Order]:
        if symbol is None:
            self.global_fetches += 1
            return list(self._open)
        self.per_symbol_fetches += 1
        return [o for o in self._open if o.symbol == symbol]

    async def cancel_order(self, order: Order) -> None:
        self.cancelled.append(order.exchange_id or "")

    async def set_dead_mans_switch(self, timeout_seconds: int) -> None:
        return None


class TestCancelAllOpenBatches:
    async def test_single_global_fetch_across_symbols(self, storage: SQLiteStorageAdapter) -> None:
        btc, eth = _order(_BTC, "B1"), _order(_ETH, "E1")
        await storage.save_order(btc)
        await storage.save_order(eth)
        adapter = _CancelSpyAdapter([btc, eth])

        cancelled, failed = await _cancel_all_open(
            adapter,  # type: ignore[arg-type]
            storage,
            (_BTC, _ETH),
        )

        assert (cancelled, failed) == (2, 0)
        assert adapter.global_fetches == 1
        assert adapter.per_symbol_fetches == 0
        assert sorted(adapter.cancelled) == ["B1", "E1"]

    async def test_only_configured_symbols_cancelled(self, storage: SQLiteStorageAdapter) -> None:
        """An order on a symbol NOT in live.symbols (e.g. a held BTC bag)
        is left alone — only configured symbols get cancelled."""
        btc, eth = _order(_BTC, "B1"), _order(_ETH, "E1")
        await storage.save_order(btc)
        await storage.save_order(eth)
        adapter = _CancelSpyAdapter([btc, eth])

        cancelled, failed = await _cancel_all_open(
            adapter,  # type: ignore[arg-type]
            storage,
            (_ETH,),
        )

        assert (cancelled, failed) == (1, 0)
        assert adapter.cancelled == ["E1"]
        assert adapter.global_fetches == 1


# --------------------------------------------------------------------------- #
# Engine: a passed snapshot replaces the per-symbol exchange fetch             #
# --------------------------------------------------------------------------- #


class TestEngineUsesSnapshot:
    async def test_step_with_snapshot_does_not_refetch_open_orders(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={_BTC: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        # First tick anchors + places the layout on the mock exchange.
        await engine.step(_BTC)

        # The whole-account snapshot the cli/live loop would pass this tick.
        snapshot = await exchange.get_open_orders()
        assert snapshot, "init should have placed orders"

        # Now forbid any further exchange OpenOrders fetch: the engine must
        # use the snapshot, not re-query.
        exchange.get_open_orders = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not refetch open orders when snapshot supplied")
        )

        result = await engine.step(_BTC, exchange_open_orders=snapshot)

        assert result.action == "stepped"
        assert result.fills == 0
        exchange.get_open_orders.assert_not_awaited()

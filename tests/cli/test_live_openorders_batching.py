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
from wobblebot.domain.value_objects import Amount, Price, Symbol, Ticker, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_SOL = Symbol(base="SOL", quote="USD")


def _ticker(symbol: Symbol, last: str = "50000") -> Ticker:
    return Ticker(symbol=symbol, last=Decimal(last), bid=Decimal(last) - 1, ask=Decimal(last) + 1)


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
        async def no_trip(_adapter: Any, _symbols: Any, _tickers: Any = None) -> Decimal:
            return Decimal("100")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", no_trip)

        snapshot = [_order(_BTC, "B1"), _order(_ETH, "E1")]
        adapter = MagicMock()
        adapter.get_open_orders = AsyncMock(return_value=snapshot)
        adapter.get_ticker = AsyncMock(side_effect=_ticker)
        engine = MagicMock()
        engine.step = AsyncMock(return_value=MagicMock(action="stepped", fills=0))
        engine.has_pending_fill_candidates = AsyncMock(return_value=False)

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
        # No symbol had a fill candidate, so no TradesHistory call at all.
        adapter.get_trade_history.assert_not_called()
        # Each symbol stepped, every step handed the SAME snapshot object,
        # and a per-symbol pre-fetched Ticker (v1.1 "per-tick price-fetch
        # dedup") -- one get_ticker call per symbol, reused by the engine
        # instead of the engine fetching its own.
        assert engine.step.await_count == 3
        assert adapter.get_ticker.await_count == 3
        for call in engine.step.await_args_list:
            assert call.kwargs["exchange_open_orders"] is snapshot
            assert call.kwargs["exchange_trades"] is None
            assert call.kwargs["ticker"] == _ticker(call.args[0])

    async def test_failed_global_fetch_skips_all_steps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rate-limited global fetch must NOT fall back to per-symbol
        fetches (that storm is exactly what blew the limit). Skip the
        tick's steps; the loss-cap check still runs."""

        async def no_trip(_adapter: Any, _symbols: Any, _tickers: Any = None) -> Decimal:
            return Decimal("100")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", no_trip)

        adapter = MagicMock()
        adapter.get_open_orders = AsyncMock(side_effect=ExchangeError("EAPI:Rate limit exceeded"))
        adapter.get_ticker = AsyncMock(side_effect=_ticker)
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
# _run_one_tick: one global TradesHistory fetch, shared by filling symbols    #
# (fleet-review #19 finding 8 follow-up — mirrors the OpenOrders batching     #
# above; a tick with several simultaneous fills used to page TradesHistory   #
# once per filling symbol)                                                   #
# --------------------------------------------------------------------------- #


class TestTickBatchesTradeHistory:
    async def test_no_fill_candidates_skips_trade_history_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The common case (no symbol has a fill candidate this tick) must
        cost zero TradesHistory calls — has_pending_fill_candidates is a
        pure storage + already-fetched-snapshot check, no network call."""

        async def no_trip(_adapter: Any, _symbols: Any, _tickers: Any = None) -> Decimal:
            return Decimal("100")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", no_trip)

        snapshot = [_order(_BTC, "B1"), _order(_ETH, "E1")]
        adapter = MagicMock()
        adapter.get_open_orders = AsyncMock(return_value=snapshot)
        adapter.get_trade_history = AsyncMock()
        adapter.get_ticker = AsyncMock(side_effect=_ticker)
        engine = MagicMock()
        engine.step = AsyncMock(return_value=MagicMock(action="stepped", fills=0))
        engine.has_pending_fill_candidates = AsyncMock(return_value=False)

        await _run_one_tick(
            adapter=adapter,
            engine=engine,
            live=_live_cfg([_BTC, _ETH]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )

        adapter.get_trade_history.assert_not_awaited()
        engine.has_pending_fill_candidates.assert_any_await(_BTC, snapshot)
        engine.has_pending_fill_candidates.assert_any_await(_ETH, snapshot)

    async def test_one_symbol_with_candidates_fetches_once_shared_by_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with three symbols configured, exactly ONE TradesHistory
        call is made when any symbol has a fill candidate — and every
        symbol's step() receives the SAME shared snapshot, not a
        per-symbol re-fetch."""

        async def no_trip(_adapter: Any, _symbols: Any, _tickers: Any = None) -> Decimal:
            return Decimal("100")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", no_trip)

        open_snapshot = [_order(_BTC, "B1")]
        trades_snapshot = [MagicMock(order_id="B1")]
        adapter = MagicMock()
        adapter.get_open_orders = AsyncMock(return_value=open_snapshot)
        adapter.get_trade_history = AsyncMock(return_value=trades_snapshot)
        adapter.get_ticker = AsyncMock(side_effect=_ticker)
        engine = MagicMock()
        engine.step = AsyncMock(return_value=MagicMock(action="stepped", fills=1))
        # Only BTC has a candidate; ETH/SOL do not.
        engine.has_pending_fill_candidates = AsyncMock(
            side_effect=lambda symbol, _snapshot: symbol == _BTC
        )

        await _run_one_tick(
            adapter=adapter,
            engine=engine,
            live=_live_cfg([_BTC, _ETH, _SOL]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )

        # ONE shared fetch regardless of how many symbols are configured.
        adapter.get_trade_history.assert_awaited_once()
        assert engine.step.await_count == 3
        for call in engine.step.await_args_list:
            assert call.kwargs["exchange_trades"] is trades_snapshot
            assert call.kwargs["ticker"] == _ticker(call.args[0])

    async def test_failed_trade_history_fetch_falls_back_to_per_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike a failed OpenOrders fetch (which skips the whole tick), a
        failed shared TradesHistory fetch must NOT skip stepping — each
        symbol's engine.step still runs, just without a shared snapshot
        (exchange_trades=None), falling back to GridEngine's own
        per-symbol TradesHistory call inside _detect_fills."""

        async def no_trip(_adapter: Any, _symbols: Any, _tickers: Any = None) -> Decimal:
            return Decimal("100")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", no_trip)

        snapshot = [_order(_BTC, "B1")]
        adapter = MagicMock()
        adapter.get_open_orders = AsyncMock(return_value=snapshot)
        adapter.get_trade_history = AsyncMock(side_effect=ExchangeError("EAPI:Rate limit exceeded"))
        adapter.get_ticker = AsyncMock(side_effect=_ticker)
        engine = MagicMock()
        engine.step = AsyncMock(return_value=MagicMock(action="stepped", fills=0))
        engine.has_pending_fill_candidates = AsyncMock(return_value=True)

        result = await _run_one_tick(
            adapter=adapter,
            engine=engine,
            live=_live_cfg([_BTC]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )

        assert result is False
        engine.step.assert_awaited_once()
        assert engine.step.await_args.kwargs["exchange_trades"] is None


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

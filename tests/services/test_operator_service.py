"""Tests for OperatorService (Stage 5.4.C)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config as _shared_grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import CoinGridConfig, GridConfig
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.domain.models import Balance
from wobblebot.domain.value_objects import Amount, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError, OperatorError
from wobblebot.ports.operator import (
    CancelOpenOrdersCommand,
    GridConfigQuery,
    GridConfigResult,
    HarvesterStatusQuery,
    HelpQuery,
    HelpResult,
    OpenOrdersQuery,
    OpenOrdersResult,
    PauseAllCommand,
    PauseCommand,
    ReanchorCommand,
    RecentFillsQuery,
    RecentFillsResult,
    RecentNewsQuery,
    RecentProposalsQuery,
    RecentSuggestionsQuery,
    ResumeAllCommand,
    ResumeCommand,
    StatusQuery,
    StatusReportQuery,
    StatusReportResult,
    StopCommand,
    WeatherReportQuery,
    WeatherReportResult,
)
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


class _FetchFailExchange(MockExchangeAdapter):
    """MockExchangeAdapter whose open-order fetch always raises ExchangeError."""

    async def get_open_orders(self, symbol=None):  # type: ignore[no-untyped-def]
        raise ExchangeError("kraken OpenOrders down")


class _OneCancelFailsExchange(MockExchangeAdapter):
    """Cancels every order except the first attempt, which raises (partial failure)."""

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._cancel_attempts = 0

    async def cancel_order(self, order):  # type: ignore[no-untyped-def]
        self._cancel_attempts += 1
        if self._cancel_attempts == 1:
            raise ExchangeError("transient cancel failure")
        return await super().cancel_order(order)


def _grid_config() -> GridConfig:
    # ETH spacing must exceed 0.52% (= 2 × Kraken maker fee) post-2026-05-20
    # validator; tests use 1.5% to exercise per-coin override behavior
    # cleanly without tripping the fee-coverage check.
    return _shared_grid_config(
        coins={
            "ETH": CoinGridConfig(
                spacing_percentage=Decimal("1.5"),
                levels_above=2,
                levels_below=2,
                order_size_usd=Decimal("20"),
            ),
        },
    )


def _harvester_config(*, enabled: bool = False) -> HarvesterConfig:
    return HarvesterConfig(
        enabled=enabled,
        min_exchange_liquidity_usd=Decimal("100"),
        topup_threshold_usd=Decimal("200"),
        surplus_threshold_usd=Decimal("500"),
        max_withdrawal_per_day_usd=Decimal("1000"),
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def exchange_with_btc_and_eth() -> AsyncIterator[MockExchangeAdapter]:
    yield MockExchangeAdapter(
        starting_balances={
            "USD": Decimal("1000"),
            "BTC": Decimal("1"),
            "ETH": Decimal("10"),
        },
        starting_prices={BTC_USD: Decimal("50000"), ETH_USD: Decimal("3000")},
    )


async def _service(  # pylint: disable=too-many-arguments
    storage: SQLiteStorageAdapter,
    exchange: MockExchangeAdapter,
    *,
    active: tuple[Symbol, ...] = (BTC_USD, ETH_USD),
    grid_config: GridConfig | None = None,
    advise_storage: SQLiteStorageAdapter | None = None,
    news_storage: SQLiteStorageAdapter | None = None,
    harvest_storage: SQLiteStorageAdapter | None = None,
    observe_storage: SQLiteStorageAdapter | None = None,
    operator_storage: SQLiteStorageAdapter | None = None,
    harvester_config: HarvesterConfig | None = None,
    assistant: object | None = None,
    session_started_at: Timestamp | None = None,
) -> OperatorService:
    engine = GridEngine(exchange, storage, grid_config or _grid_config(), _safety_config())
    return OperatorService(
        engine=engine,
        storage=storage,
        active_symbols=active,
        grid_config=grid_config or _grid_config(),
        advise_storage=advise_storage,
        news_storage=news_storage,
        harvest_storage=harvest_storage,
        observe_storage=observe_storage,
        operator_storage=operator_storage,
        harvester_config=harvester_config,
        assistant=assistant,  # type: ignore[arg-type]
        session_started_at=session_started_at,
    )


# --------------------------------------------------------------------- #
# Commands                                                              #
# --------------------------------------------------------------------- #


class TestPause:
    async def test_pause_one_symbol(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.dispatch_command(PauseCommand(symbol=BTC_USD))
        assert result.success is True
        assert result.command_kind == "pause"
        assert "BTC/USD" in result.message

    async def test_pause_idempotent_returns_success_false(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        await svc.dispatch_command(PauseCommand(symbol=BTC_USD))
        result = await svc.dispatch_command(PauseCommand(symbol=BTC_USD))
        assert result.success is False
        assert "already paused" in result.message


class TestResume:
    async def test_resume_paused_symbol(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        await svc.dispatch_command(PauseCommand(symbol=BTC_USD))
        result = await svc.dispatch_command(ResumeCommand(symbol=BTC_USD))
        assert result.success is True
        assert "Resumed BTC/USD" in result.message

    async def test_resume_active_symbol_returns_success_false(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.dispatch_command(ResumeCommand(symbol=BTC_USD))
        assert result.success is False
        assert "already active" in result.message


class TestPauseAll:
    async def test_pause_all_paused_count(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.dispatch_command(PauseAllCommand())
        assert result.success is True
        assert result.side_effects["count"] == 2
        assert set(result.side_effects["newly_paused"]) == {"BTC/USD", "ETH/USD"}

    async def test_pause_all_with_some_already_paused(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        await svc.dispatch_command(PauseCommand(symbol=BTC_USD))
        result = await svc.dispatch_command(PauseAllCommand())
        # Only ETH changes; BTC was already paused
        assert result.side_effects["count"] == 1
        assert result.side_effects["newly_paused"] == ["ETH/USD"]


class TestResumeAll:
    async def test_resume_all_from_two_paused(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        await svc.dispatch_command(PauseAllCommand())
        result = await svc.dispatch_command(ResumeAllCommand())
        assert result.success is True
        assert result.side_effects["count"] == 2

    async def test_resume_all_with_nothing_paused(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.dispatch_command(ResumeAllCommand())
        assert result.success is False
        assert "No paused" in result.message


class TestCancelOpenOrders:
    async def test_cancel_open_orders_on_symbol(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        # Seed an open grid
        engine = GridEngine(
            exchange_with_btc_and_eth,
            storage,
            _grid_config(),
            _safety_config(),
        )
        await engine.step(BTC_USD)  # 6 orders placed
        svc = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=(BTC_USD,),
            grid_config=_grid_config(),
        )

        result = await svc.dispatch_command(CancelOpenOrdersCommand(symbol=BTC_USD))
        assert result.success is True
        assert result.side_effects["cancelled"] == 6
        assert result.side_effects["failed"] == 0

    async def test_cancel_reports_failure_when_fetch_fails(
        self,
        storage: SQLiteStorageAdapter,
    ) -> None:
        # During a Kraken outage the open-order fetch fails. The operator must
        # see an explicit failure ("may still be LIVE"), NOT a false all-clear
        # of "Cancelled 0; 0 failed" — the orders are still on the exchange.
        exch = _FetchFailExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exch, storage, _grid_config(), _safety_config())
        svc = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=(BTC_USD,),
            grid_config=_grid_config(),
        )

        result = await svc.dispatch_command(CancelOpenOrdersCommand(symbol=BTC_USD))
        assert result.success is False
        assert "may still be LIVE" in result.message
        assert result.side_effects["fetch_failed"] is True

    async def test_cancel_partial_failure_is_not_success(
        self,
        storage: SQLiteStorageAdapter,
    ) -> None:
        # 5 cancel, 1 fail must report success=False so the operator retries —
        # the old `cancelled > 0 or failed == 0` flagged this green.
        exch = _OneCancelFailsExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exch, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # 6 orders placed
        svc = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=(BTC_USD,),
            grid_config=_grid_config(),
        )

        result = await svc.dispatch_command(CancelOpenOrdersCommand(symbol=BTC_USD))
        assert result.success is False
        assert result.side_effects["cancelled"] == 5
        assert result.side_effects["failed"] == 1


class TestReanchor:
    async def test_reanchor_moves_anchor_and_relays_message(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        engine = GridEngine(exchange_with_btc_and_eth, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # anchor at the mock's BTC price
        svc = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=(BTC_USD,),
            grid_config=_grid_config(),
        )

        result = await svc.dispatch_command(ReanchorCommand(symbol=BTC_USD))

        assert result.success is True
        assert result.command_kind == "reanchor"
        assert "re-anchored BTC/USD" in result.message
        assert "->" in result.message  # old -> new anchor: the audit record
        assert result.side_effects["symbol"] == "BTC/USD"

    async def test_reanchor_failed_cancel_reports_failure_anchor_untouched(
        self,
        storage: SQLiteStorageAdapter,
    ) -> None:
        """ADR-031 pin at the dispatch layer: a partial cancel comes back
        success=False and the persisted anchor is unchanged."""
        exch = _OneCancelFailsExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exch, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        svc = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=(BTC_USD,),
            grid_config=_grid_config(),
        )

        result = await svc.dispatch_command(ReanchorCommand(symbol=BTC_USD))

        assert result.success is False
        assert "anchor is unchanged" in result.message
        state = await storage.get_grid_state(BTC_USD)
        assert state is not None
        assert state.reference_price == Decimal("50000")

    async def test_reanchor_fetch_failure_reports_live_orders(
        self,
        storage: SQLiteStorageAdapter,
    ) -> None:
        exch = _FetchFailExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exch, storage, _grid_config(), _safety_config())
        svc = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=(BTC_USD,),
            grid_config=_grid_config(),
        )

        result = await svc.dispatch_command(ReanchorCommand(symbol=BTC_USD))

        assert result.success is False
        assert "LIVE" in result.message


class TestStop:
    async def test_stop_marks_engine(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        engine = GridEngine(exchange_with_btc_and_eth, storage, _grid_config(), _safety_config())
        svc = OperatorService(engine=engine, storage=storage)
        result = await svc.dispatch_command(StopCommand())
        assert result.success is True
        assert engine.is_stop_requested is True

    async def test_stop_idempotent(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        engine = GridEngine(exchange_with_btc_and_eth, storage, _grid_config(), _safety_config())
        svc = OperatorService(engine=engine, storage=storage)
        await svc.dispatch_command(StopCommand())
        result = await svc.dispatch_command(StopCommand())
        assert result.success is False
        assert "already requested" in result.message


# --------------------------------------------------------------------- #
# Queries                                                               #
# --------------------------------------------------------------------- #


class TestStatusQuery:
    async def test_status_reports_active_and_paused(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        # Seed a balance snapshot so total_usd_balance > 0
        await storage.save_balance_snapshot(
            [
                Balance(
                    asset="USD",
                    total=Decimal("999.99"),
                    available=Decimal("999.99"),
                    locked=Decimal("0"),
                    updated_at=Timestamp(dt=datetime.now(UTC)),
                )
            ]
        )
        svc = await _service(
            storage,
            exchange_with_btc_and_eth,
            session_started_at=Timestamp(dt=datetime.now(UTC) - timedelta(seconds=42)),
        )
        await svc.dispatch_command(PauseCommand(symbol=BTC_USD))

        status = await svc.answer_query(StatusQuery())
        assert status.kind == "status"
        by_symbol = {s.symbol: s.state for s in status.symbols}
        assert by_symbol == {"BTC/USD": "paused", "ETH/USD": "active"}
        assert status.total_usd_balance == 999.99
        assert status.session_runtime_seconds > 0
        assert status.recent_fill_count == 0

    async def test_status_total_balance_zero_when_no_snapshot(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        status = await svc.answer_query(StatusQuery())
        assert status.total_usd_balance == 0.0

    async def test_status_balance_reads_observe_storage_when_provided(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        """Regression for 2026-05-24 Discord-visibility bug. Before the
        fix, StatusQuery queried ``self._storage`` (live.db) for
        ``balance_snapshots`` — but that table only lives in observe.db.
        Now the OperatorService prefers ``observe_storage`` when wired.
        """
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            await observe.save_balance_snapshot(
                [
                    Balance(
                        asset="USD",
                        total=Decimal("123.45"),
                        available=Decimal("123.45"),
                        locked=Decimal("0"),
                        updated_at=Timestamp(dt=datetime.now(UTC)),
                    )
                ]
            )
            # storage (live.db) has NO balance snapshot — observe has it
            svc = await _service(storage, exchange_with_btc_and_eth, observe_storage=observe)
            status = await svc.answer_query(StatusQuery())
            assert status.total_usd_balance == 123.45
        finally:
            await observe.close()

    async def test_status_session_pnl_reflects_today_realized_cycles(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        """Regression for 2026-05-24 Discord-visibility bug. Before the
        fix, StatusQuery hard-coded ``session_pnl=0.0``. Now it sums
        today's realized cycles via cycle_matcher.
        """
        from uuid import uuid4

        from wobblebot.domain.models import Trade
        from wobblebot.domain.value_objects import Amount, Price

        now = datetime.now(UTC)
        # Anchor both legs to fixed hours of TODAY (UTC), not `now - Nh`:
        # today_realized_pnl filters by the SELL's UTC date, so `now - 1h/2h`
        # straddled into "yesterday" when the suite ran in the first hours after
        # UTC midnight (2026-06-03 00:0x CI flake). PnL is timestamp-independent
        # (price/amount/fees), so this only pins the day, not the value.
        today_at = now.replace(minute=0, second=0, microsecond=0)
        await storage.save_trade(
            Trade(
                id=f"T-{uuid4().hex[:8]}",
                order_id=f"O-{uuid4().hex[:8]}",
                symbol=BTC_USD,
                side="buy",
                price=Price(amount=Decimal("76000"), currency="USD"),
                amount=Amount(value=Decimal("0.000131"), asset="BTC"),
                fee=Decimal("0.025"),
                cost=Decimal("9.956"),
                executed_at=Timestamp(dt=today_at.replace(hour=10)),  # today 10:00 UTC
            )
        )
        await storage.save_trade(
            Trade(
                id=f"T-{uuid4().hex[:8]}",
                order_id=f"O-{uuid4().hex[:8]}",
                symbol=BTC_USD,
                side="sell",
                price=Price(amount=Decimal("76760"), currency="USD"),  # +1%
                amount=Amount(value=Decimal("0.000131"), asset="BTC"),
                fee=Decimal("0.025"),
                cost=Decimal("10.055"),
                executed_at=Timestamp(dt=today_at.replace(hour=11)),  # today 11:00 UTC
            )
        )
        svc = await _service(storage, exchange_with_btc_and_eth)
        status = await svc.answer_query(StatusQuery())
        # net = (76760-76000) * 0.000131 - 0.025 - 0.025
        #     = 760 * 0.000131 - 0.05
        #     = 0.09956 - 0.05 = 0.04956
        assert status.session_pnl > 0.04
        assert status.session_pnl < 0.06

    async def test_busy_day_does_not_truncate_an_early_cycle(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        """v1.1 fix: ``get_trades(limit=100)`` silently dropped a cycle's
        BUY+SELL if 100+ MORE RECENT trades existed the same day (a
        plausible multi-coin trading day) -- the oldest legs truncate
        out of a newest-first fetch and PnL undercounts with no error.
        Seeds a real BTC cycle early today, then 150 later filler
        trades on a second symbol; the early cycle's PnL must still be
        counted with the fixed (much higher) fetch limit.
        """
        from uuid import uuid4

        from wobblebot.domain.models import Trade
        from wobblebot.domain.value_objects import Amount, Price

        now = datetime.now(UTC)
        today_at = now.replace(minute=0, second=0, microsecond=0)

        # The real cycle: BUY then SELL, both early today.
        await storage.save_trade(
            Trade(
                id=f"T-{uuid4().hex[:8]}",
                order_id=f"O-{uuid4().hex[:8]}",
                symbol=BTC_USD,
                side="buy",
                price=Price(amount=Decimal("76000"), currency="USD"),
                amount=Amount(value=Decimal("0.000131"), asset="BTC"),
                fee=Decimal("0.025"),
                cost=Decimal("9.956"),
                executed_at=Timestamp(dt=today_at.replace(hour=1, minute=0)),
            )
        )
        await storage.save_trade(
            Trade(
                id=f"T-{uuid4().hex[:8]}",
                order_id=f"O-{uuid4().hex[:8]}",
                symbol=BTC_USD,
                side="sell",
                price=Price(amount=Decimal("76760"), currency="USD"),
                amount=Amount(value=Decimal("0.000131"), asset="BTC"),
                fee=Decimal("0.025"),
                cost=Decimal("10.055"),
                executed_at=Timestamp(dt=today_at.replace(hour=1, minute=30)),
            )
        )
        # 150 later filler trades (a busy multi-coin day), all AFTER the
        # real cycle -- a `limit=100` newest-first fetch would push the
        # real cycle's two trades entirely out of the window.
        for i in range(150):
            await storage.save_trade(
                Trade(
                    id=f"T-filler-{i}",
                    order_id=f"O-filler-{i}",
                    symbol=ETH_USD,
                    side="buy",
                    price=Price(amount=Decimal("3000"), currency="USD"),
                    amount=Amount(value=Decimal("0.01"), asset="ETH"),
                    fee=Decimal("0.01"),
                    cost=Decimal("30"),
                    executed_at=Timestamp(
                        dt=today_at.replace(hour=2, minute=0) + timedelta(minutes=i)
                    ),
                )
            )

        svc = await _service(storage, exchange_with_btc_and_eth)
        status = await svc.answer_query(StatusQuery())
        # Same cycle as the test above: net PnL ~= 0.04956.
        assert status.session_pnl > 0.04
        assert status.session_pnl < 0.06


class TestOpenOrdersQuery:
    async def test_open_orders_for_symbol(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        engine = GridEngine(exchange_with_btc_and_eth, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # places 6
        svc = OperatorService(engine=engine, storage=storage, active_symbols=(BTC_USD,))
        result = await svc.answer_query(OpenOrdersQuery(symbol=BTC_USD))
        assert isinstance(result, OpenOrdersResult)
        assert result.symbol == "BTC/USD"
        assert len(result.orders) == 6

    async def test_open_orders_empty(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.answer_query(OpenOrdersQuery(symbol=BTC_USD))
        assert result.orders == []


class TestRecentFillsQuery:
    async def test_no_trades_returns_empty(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.answer_query(RecentFillsQuery())
        assert isinstance(result, RecentFillsResult)
        assert result.fills == []


class TestRecentSuggestionsQuery:
    async def test_returns_empty_when_no_advise_storage(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth, advise_storage=None)
        result = await svc.answer_query(RecentSuggestionsQuery())
        assert result.suggestions == []


class TestRecentNewsQuery:
    async def test_returns_empty_when_no_news_storage(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth, news_storage=None)
        result = await svc.answer_query(RecentNewsQuery())
        assert result.items == []


class TestHarvesterStatusQuery:
    async def test_reports_deficit_below_min(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        # 50 < min=100 → deficit
        await storage.save_balance_snapshot(
            [
                Balance(
                    asset="USD",
                    total=Decimal("50"),
                    available=Decimal("50"),
                    locked=Decimal("0"),
                    updated_at=Timestamp(dt=datetime.now(UTC)),
                )
            ]
        )
        svc = await _service(
            storage,
            exchange_with_btc_and_eth,
            harvest_storage=storage,
            harvester_config=_harvester_config(),
        )
        result = await svc.answer_query(HarvesterStatusQuery())
        assert result.band == "deficit"
        assert result.enabled is False

    async def test_reports_surplus_above_threshold(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        # 600 > surplus=500 → surplus
        await storage.save_balance_snapshot(
            [
                Balance(
                    asset="USD",
                    total=Decimal("600"),
                    available=Decimal("600"),
                    locked=Decimal("0"),
                    updated_at=Timestamp(dt=datetime.now(UTC)),
                )
            ]
        )
        svc = await _service(
            storage,
            exchange_with_btc_and_eth,
            harvest_storage=storage,
            harvester_config=_harvester_config(),
        )
        result = await svc.answer_query(HarvesterStatusQuery())
        assert result.band == "surplus"


class TestRecentProposalsQuery:
    async def test_returns_empty_when_no_harvest_storage(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth, harvest_storage=None)
        result = await svc.answer_query(RecentProposalsQuery())
        assert result.proposals == []


class TestGridConfigQuery:
    async def test_grid_config_for_btc_uses_default(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.answer_query(GridConfigQuery(symbol=BTC_USD))
        assert isinstance(result, GridConfigResult)
        assert result.spacing_percentage == 1.0
        assert result.levels_above == 3
        assert result.order_size_usd == 10.0
        assert result.counter_target_mode == "spacing_up"

    async def test_grid_config_for_eth_uses_override(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.answer_query(GridConfigQuery(symbol=ETH_USD))
        assert result.spacing_percentage == 1.5
        assert result.levels_above == 2
        assert result.order_size_usd == 20.0

    async def test_grid_config_no_symbol_returns_default_tier(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.answer_query(GridConfigQuery())
        assert result.symbol is None
        assert result.spacing_percentage == 1.0


class TestHelpQuery:
    async def test_help_lists_every_command_and_query(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.answer_query(HelpQuery())
        assert isinstance(result, HelpResult)
        kinds = {e.kind for e in result.entries}
        # 7 commands
        assert {
            "pause",
            "resume",
            "pause_all",
            "resume_all",
            "cancel_open_orders",
            "reanchor",
            "stop",
        } <= kinds
        # 9 queries
        assert {
            "status",
            "open_orders",
            "recent_fills",
            "recent_suggestions",
            "recent_news",
            "harvester_status",
            "recent_proposals",
            "grid_config",
            "help",
        } <= kinds
        assert "status_report" in kinds
        assert "weather_report" in kinds
        assert len(result.entries) == 18


# --------------------------------------------------------------------- #
# StatusReportQuery                                                     #
# --------------------------------------------------------------------- #


class _RecordingAssistant:
    """Stub assistant whose ``summarize`` records calls + returns a fixed string."""

    def __init__(self, narrative: str = "all clear, two fills, harvester holding") -> None:
        self.narrative = narrative
        self.calls: list[tuple[str, str, int]] = []

    async def parse_intent(self, context):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def summarize(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 2048
    ) -> str:
        self.calls.append((system_prompt, user_content, max_tokens))
        return self.narrative


class _FailingAssistant:
    """Stub assistant whose ``summarize`` raises NotImplementedError."""

    async def parse_intent(self, context):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def summarize(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 2048
    ) -> str:
        raise NotImplementedError("test stub: no summarize")


class TestStatusReport:
    async def test_no_assistant_falls_back_to_deterministic_narrative(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        service = await _service(storage, exchange_with_btc_and_eth, assistant=None)
        result = await service.answer_query(
            StatusReportQuery(lookback_hours=4), channel_id="C-1", user_id="U-1"
        )
        assert isinstance(result, StatusReportResult)
        assert result.lookback_hours == 4
        assert "last 4h snapshot" in result.narrative.lower()
        # Tallies are present even without LLM
        labels = [t.label for t in result.tallies]
        assert "Balance" in labels
        assert "Open orders" in labels
        assert any("Fills" in label for label in labels)

    async def test_assistant_narrative_used_when_available(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        assistant = _RecordingAssistant(narrative="quiet hour, two fills, no news")
        service = await _service(storage, exchange_with_btc_and_eth, assistant=assistant)
        result = await service.answer_query(
            StatusReportQuery(lookback_hours=2), channel_id="C-1", user_id="U-1"
        )
        assert isinstance(result, StatusReportResult)
        assert result.narrative == "quiet hour, two fills, no news"
        assert len(assistant.calls) == 1
        # System prompt mentions status report; payload includes the section
        # headers from the data blob.
        system_prompt, user_content, max_tokens = assistant.calls[0]
        assert "status report" in system_prompt.lower()
        # New layout (2026-05-24): STATUS labelled with caveat; RECENT_FILLS
        # carries a scope hint. Both prefixes still appear at line start.
        assert "STATUS " in user_content  # noqa: PLR0915
        assert "RECENT_FILLS " in user_content
        assert "COUNTS " in user_content
        assert "fills_in_lookback_total: 0" in user_content
        assert max_tokens == 2048

    async def test_assistant_failure_falls_back_deterministic(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        service = await _service(storage, exchange_with_btc_and_eth, assistant=_FailingAssistant())
        result = await service.answer_query(
            StatusReportQuery(lookback_hours=4), channel_id="C-1", user_id="U-1"
        )
        assert isinstance(result, StatusReportResult)
        assert "last 4h snapshot" in result.narrative.lower()

    async def test_explicit_lookback_overrides_anchor(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        # Pre-seed anchor at T-12h.
        await storage.save_status_report_taken(
            "C-1", "U-1", datetime.now(UTC) - timedelta(hours=12)
        )
        service = await _service(storage, exchange_with_btc_and_eth, operator_storage=storage)
        result = await service.answer_query(
            StatusReportQuery(lookback_hours=3), channel_id="C-1", user_id="U-1"
        )
        assert isinstance(result, StatusReportResult)
        assert result.lookback_hours == 3  # explicit wins over the 12h anchor

    async def test_default_lookback_uses_stored_anchor(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        # Seed anchor at T-6h; expect lookback ~6h.
        await storage.save_status_report_taken(
            "C-1", "U-1", datetime.now(UTC) - timedelta(hours=6, minutes=5)
        )
        service = await _service(storage, exchange_with_btc_and_eth, operator_storage=storage)
        result = await service.answer_query(
            StatusReportQuery(lookback_hours=None), channel_id="C-1", user_id="U-1"
        )
        assert isinstance(result, StatusReportResult)
        # Round-down: 6h 5min becomes 6.
        assert result.lookback_hours == 6

    async def test_default_lookback_falls_back_to_24h_when_no_anchor(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        service = await _service(storage, exchange_with_btc_and_eth, operator_storage=storage)
        result = await service.answer_query(
            StatusReportQuery(lookback_hours=None), channel_id="C-1", user_id="U-1"
        )
        assert isinstance(result, StatusReportResult)
        assert result.lookback_hours == 24

    async def test_successful_run_persists_new_anchor(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        service = await _service(storage, exchange_with_btc_and_eth, operator_storage=storage)
        before = datetime.now(UTC) - timedelta(seconds=1)
        await service.answer_query(
            StatusReportQuery(lookback_hours=4), channel_id="C-9", user_id="U-9"
        )
        anchor = await storage.get_last_status_report_taken_at("C-9", "U-9")
        assert anchor is not None
        assert anchor > before


# --------------------------------------------------------------------- #
# Weather report (P4.5)                                                 #
# --------------------------------------------------------------------- #


def _hourly_bars(symbol: Symbol, hours: int, *, start: float, step: float) -> list:
    from wobblebot.domain.value_objects import OHLCBar

    now = datetime.now(UTC)
    bars = []
    for i in range(hours):
        price = Decimal(str(start + step * i))
        bars.append(
            OHLCBar(
                symbol=symbol,
                interval_minutes=60,
                opened_at=now - timedelta(hours=hours - i),
                open=price,
                high=price,
                low=price,
                close=price,
                vwap=price,
                volume=Decimal("1"),
                count=1,
            )
        )
    return bars


def _gremlin_suggestion(symbol: str = "BTC/USD"):  # type: ignore[no-untyped-def]
    from uuid import uuid4

    from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion

    created = datetime.now(UTC) - timedelta(hours=2)
    return AdvisorSuggestion(
        recommendation=AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=created),
            role="gremlin",
            recommendations={"direction": "down", "horizon_hours": 24},
            rationale="tape feels toppy",
            confidence="medium",
        ),
        created_at=Timestamp(dt=created),
        input_summary={"symbol": symbol},
        model_name="qwen2.5:3b-instruct-q4_K_M",
    )


class TestWeatherReport:
    async def test_aggregates_trends_news_and_gremlin_calls(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        observe = SQLiteStorageAdapter(":memory:")
        advise = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        await advise.connect()
        try:
            await observe.save_ohlc_bars(_hourly_bars(BTC_USD, 72, start=100.0, step=0.5))
            await advise.save_advisor_suggestion(_gremlin_suggestion())
            svc = await _service(
                storage,
                exchange_with_btc_and_eth,
                observe_storage=observe,
                advise_storage=advise,
            )
            result = await svc.answer_query(WeatherReportQuery(lookback_days=3))
            assert isinstance(result, WeatherReportResult)
            assert result.lookback_days == 3
            by_symbol = {t.symbol: t for t in result.trends}
            assert set(by_symbol) == {"BTC/USD", "ETH/USD"}
            assert by_symbol["BTC/USD"].change_window_pct is not None
            assert by_symbol["BTC/USD"].change_window_pct > 0
            assert by_symbol["ETH/USD"].latest_price is None  # no bars seeded
            assert result.suggestion_count == 1
            [call] = result.directional_calls
            assert call.symbol == "BTC/USD"
            assert call.direction == "down"
            # No assistant wired -> deterministic narrative, and it cites
            # the window move rather than being empty.
            assert "Market weather" in result.narrative
            assert "BTC/USD" in result.narrative
        finally:
            await advise.close()
            await observe.close()

    async def test_degrades_without_cross_dbs(
        self,
        storage: SQLiteStorageAdapter,
        exchange_with_btc_and_eth: MockExchangeAdapter,
    ) -> None:
        svc = await _service(storage, exchange_with_btc_and_eth)
        result = await svc.answer_query(WeatherReportQuery())
        assert isinstance(result, WeatherReportResult)
        assert result.lookback_days == 3  # the default window
        assert all(t.latest_price is None for t in result.trends)
        assert result.news_count == 0
        assert result.suggestion_count == 0
        assert result.directional_calls == []
        assert "no bar history available" in result.narrative

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
from wobblebot.ports.exceptions import OperatorError
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
    RecentFillsQuery,
    RecentFillsResult,
    RecentNewsQuery,
    RecentProposalsQuery,
    RecentSuggestionsQuery,
    ResumeAllCommand,
    ResumeCommand,
    StatusQuery,
    StopCommand,
)
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


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


async def _service(
    storage: SQLiteStorageAdapter,
    exchange: MockExchangeAdapter,
    *,
    active: tuple[Symbol, ...] = (BTC_USD, ETH_USD),
    grid_config: GridConfig | None = None,
    advise_storage: SQLiteStorageAdapter | None = None,
    news_storage: SQLiteStorageAdapter | None = None,
    harvest_storage: SQLiteStorageAdapter | None = None,
    harvester_config: HarvesterConfig | None = None,
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
        harvester_config=harvester_config,
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
        # 6 commands
        assert {
            "pause",
            "resume",
            "pause_all",
            "resume_all",
            "cancel_open_orders",
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
        assert len(result.entries) == 15

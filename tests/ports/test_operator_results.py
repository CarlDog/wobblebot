"""Unit tests for ``CommandResult`` and the per-query ``*Result`` types
(Stage 5.1.A).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.operator import (
    CommandResult,
    FillEntry,
    GridConfigResult,
    HarvesterStatusResult,
    HelpEntry,
    HelpResult,
    NewsEntry,
    OpenOrderEntry,
    OpenOrdersResult,
    ProposalEntry,
    QueryResult,
    RecentFillsResult,
    RecentNewsResult,
    RecentProposalsResult,
    RecentSuggestionsResult,
    StatusResult,
    SuggestionEntry,
    SymbolStatusEntry,
)

pytestmark = pytest.mark.unit


def _now() -> Timestamp:
    return Timestamp(dt=datetime.now(UTC))


def _result_adapter() -> TypeAdapter[QueryResult]:
    return TypeAdapter(QueryResult)


class TestCommandResult:
    def test_minimal(self) -> None:
        result = CommandResult(
            success=True,
            command_kind="pause",
            message="BTC paused",
            executed_at=_now(),
        )
        assert result.success is True
        assert result.command_kind == "pause"
        assert result.side_effects == {}

    def test_side_effects_optional(self) -> None:
        result = CommandResult(
            success=True,
            command_kind="cancel_open_orders",
            message="cancelled 4 orders",
            executed_at=_now(),
            side_effects={"orders_cancelled": 4},
        )
        assert result.side_effects == {"orders_cancelled": 4}

    def test_frozen(self) -> None:
        result = CommandResult(
            success=True,
            command_kind="pause",
            message="ok",
            executed_at=_now(),
        )
        with pytest.raises(ValidationError):
            result.success = False  # type: ignore[misc]

    def test_message_required(self) -> None:
        with pytest.raises(ValidationError):
            CommandResult(
                success=True,
                command_kind="pause",
                message="",
                executed_at=_now(),
            )


class TestStatusResult:
    def test_construct(self) -> None:
        result = StatusResult(
            symbols=[
                SymbolStatusEntry(symbol="BTC/USD", state="active", open_order_count=6),
                SymbolStatusEntry(symbol="ETH/USD", state="paused", open_order_count=0),
            ],
            total_usd_balance=123.45,
            session_pnl=-0.04,
            session_runtime_seconds=600.0,
            recent_fill_count=2,
        )
        assert result.kind == "status"
        assert len(result.symbols) == 2

    def test_runtime_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            StatusResult(
                total_usd_balance=0,
                session_pnl=0,
                session_runtime_seconds=-1,
                recent_fill_count=0,
            )


class TestOpenOrdersResult:
    def test_empty(self) -> None:
        result = OpenOrdersResult(symbol="BTC/USD")
        assert result.kind == "open_orders"
        assert result.orders == []

    def test_with_entries(self) -> None:
        order = OpenOrderEntry(
            order_id="O-1",
            symbol="BTC/USD",
            side="buy",
            price=79000.0,
            amount=0.0001,
            created_at=_now(),
        )
        result = OpenOrdersResult(symbol="BTC/USD", orders=[order])
        assert len(result.orders) == 1


class TestRecentFillsResult:
    def test_construct(self) -> None:
        fill = FillEntry(
            order_id="O-9",
            symbol="ETH/USD",
            side="sell",
            price=3200.0,
            amount=0.003,
            pnl=0.50,
            filled_at=_now(),
        )
        result = RecentFillsResult(symbol="ETH/USD", lookback_hours=12, fills=[fill])
        assert result.kind == "recent_fills"
        assert result.fills[0].pnl == 0.50


class TestRecentSuggestionsResult:
    def test_construct(self) -> None:
        entry = SuggestionEntry(
            recommendation_id="r-1",
            symbol="BTC/USD",
            model_name="phi4:14b",
            confidence="medium",
            recommendations={"spacing_percentage": 1.1},
            rationale="spread widened",
            created_at=_now(),
        )
        result = RecentSuggestionsResult(symbol="BTC/USD", suggestions=[entry])
        assert result.kind == "recent_suggestions"
        assert result.suggestions[0].confidence == "medium"

    def test_invalid_confidence_raises(self) -> None:
        with pytest.raises(ValidationError):
            SuggestionEntry(
                recommendation_id="r-1",
                symbol="BTC/USD",
                model_name="phi4:14b",
                confidence="extreme",  # type: ignore[arg-type]
                recommendations={},
                rationale="x",
                created_at=_now(),
            )


class TestRecentNewsResult:
    def test_construct(self) -> None:
        item = NewsEntry(
            source="CoinDesk",
            headline="BTC up 3%",
            published_at=_now(),
            sentiment_score=0.4,
            mentioned_coins=["BTC"],
        )
        result = RecentNewsResult(lookback_hours=24, items=[item])
        assert result.kind == "recent_news"
        assert result.items[0].source == "CoinDesk"

    def test_sentiment_score_bounds(self) -> None:
        with pytest.raises(ValidationError):
            NewsEntry(
                source="x",
                headline="x",
                published_at=_now(),
                sentiment_score=2.0,
            )


class TestHarvesterStatusResult:
    def test_construct(self) -> None:
        result = HarvesterStatusResult(
            enabled=False,
            asset="USD",
            current_balance=99.92,
            band="deficit",
        )
        assert result.kind == "harvester_status"
        assert result.latest_proposal_id is None

    def test_invalid_band_raises(self) -> None:
        with pytest.raises(ValidationError):
            HarvesterStatusResult(
                enabled=False,
                asset="USD",
                current_balance=0,
                band="extra_surplus",  # type: ignore[arg-type]
            )


class TestRecentProposalsResult:
    def test_construct(self) -> None:
        proposal = ProposalEntry(
            proposal_id="p-1",
            direction="exchange_to_bank",
            asset="USD",
            amount=50.0,
            rationale="above surplus",
            created_at=_now(),
        )
        result = RecentProposalsResult(
            direction="exchange_to_bank", lookback_hours=24, proposals=[proposal]
        )
        assert result.kind == "recent_proposals"


class TestGridConfigResult:
    def test_construct(self) -> None:
        result = GridConfigResult(
            symbol="BTC/USD",
            spacing_percentage=1.0,
            levels_above=3,
            levels_below=3,
            order_size_usd=10.0,
        )
        assert result.kind == "grid_config"

    def test_spacing_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            GridConfigResult(
                symbol="BTC/USD",
                spacing_percentage=0,
                levels_above=1,
                levels_below=1,
                order_size_usd=1.0,
            )


class TestHelpResult:
    def test_construct(self) -> None:
        result = HelpResult(
            entries=[
                HelpEntry(kind="pause", category="command", description="Pause a symbol."),
                HelpEntry(kind="status", category="query", description="Show status."),
            ]
        )
        assert result.kind == "help"
        assert len(result.entries) == 2


class TestQueryResultUnion:
    @pytest.mark.parametrize(
        ("payload", "expected_type"),
        [
            (
                {
                    "kind": "status",
                    "symbols": [],
                    "total_usd_balance": 100.0,
                    "session_pnl": 0.0,
                    "session_runtime_seconds": 0.0,
                    "recent_fill_count": 0,
                },
                StatusResult,
            ),
            (
                {"kind": "open_orders", "symbol": None, "orders": []},
                OpenOrdersResult,
            ),
            (
                {"kind": "help", "entries": []},
                HelpResult,
            ),
        ],
    )
    def test_validate_python(self, payload: dict[str, object], expected_type: type) -> None:
        r = _result_adapter().validate_python(payload)
        assert isinstance(r, expected_type)

    def test_round_trip_help(self) -> None:
        result = HelpResult(
            entries=[HelpEntry(kind="stop", category="command", description="Soft stop.")]
        )
        adapter = _result_adapter()
        revived = adapter.validate_python(adapter.dump_python(result))
        assert revived == result

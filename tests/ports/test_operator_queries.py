"""Unit tests for the ``OperatorQuery`` typed sum (Stage 5.1.A)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.operator import (
    GridConfigQuery,
    HarvesterStatusQuery,
    HelpQuery,
    OpenOrdersQuery,
    OperatorQuery,
    RecentFillsQuery,
    RecentNewsQuery,
    RecentProposalsQuery,
    RecentSuggestionsQuery,
    StatusQuery,
)

pytestmark = pytest.mark.unit


_btc = Symbol(base="BTC", quote="USD")


def _adapter() -> TypeAdapter[OperatorQuery]:
    return TypeAdapter(OperatorQuery)


class TestStatusQuery:
    def test_no_args(self) -> None:
        q = StatusQuery()
        assert q.kind == "status"

    def test_frozen(self) -> None:
        q = StatusQuery()
        with pytest.raises(ValidationError):
            q.kind = "open_orders"  # type: ignore[misc]


class TestOpenOrdersQuery:
    def test_default_symbol_none(self) -> None:
        q = OpenOrdersQuery()
        assert q.kind == "open_orders"
        assert q.symbol is None

    def test_with_symbol(self) -> None:
        q = OpenOrdersQuery(symbol=_btc)
        assert q.symbol == _btc


class TestRecentFillsQuery:
    def test_defaults(self) -> None:
        q = RecentFillsQuery()
        assert q.kind == "recent_fills"
        assert q.symbol is None
        assert q.lookback_hours == 24
        assert q.limit == 20

    def test_explicit(self) -> None:
        q = RecentFillsQuery(symbol=_btc, lookback_hours=6, limit=50)
        assert q.symbol == _btc
        assert q.lookback_hours == 6
        assert q.limit == 50

    def test_lookback_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RecentFillsQuery(lookback_hours=0)

    def test_limit_cap(self) -> None:
        with pytest.raises(ValidationError):
            RecentFillsQuery(limit=500)

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RecentFillsQuery(limit=0)


class TestRecentSuggestionsQuery:
    def test_defaults(self) -> None:
        q = RecentSuggestionsQuery()
        assert q.kind == "recent_suggestions"
        assert q.symbol is None
        assert q.limit == 5

    def test_limit_cap(self) -> None:
        with pytest.raises(ValidationError):
            RecentSuggestionsQuery(limit=100)


class TestRecentNewsQuery:
    def test_defaults(self) -> None:
        q = RecentNewsQuery()
        assert q.kind == "recent_news"
        assert q.lookback_hours == 24
        assert q.limit == 10

    def test_limit_cap(self) -> None:
        with pytest.raises(ValidationError):
            RecentNewsQuery(limit=200)


class TestHarvesterStatusQuery:
    def test_no_args(self) -> None:
        q = HarvesterStatusQuery()
        assert q.kind == "harvester_status"


class TestRecentProposalsQuery:
    def test_defaults(self) -> None:
        q = RecentProposalsQuery()
        assert q.kind == "recent_proposals"
        assert q.direction is None
        assert q.lookback_hours == 24
        assert q.limit == 10

    def test_explicit_direction(self) -> None:
        q = RecentProposalsQuery(direction="exchange_to_bank")
        assert q.direction == "exchange_to_bank"

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValidationError):
            RecentProposalsQuery(direction="moonwards")  # type: ignore[arg-type]


class TestGridConfigQuery:
    def test_default_symbol_none(self) -> None:
        q = GridConfigQuery()
        assert q.kind == "grid_config"
        assert q.symbol is None

    def test_with_symbol(self) -> None:
        q = GridConfigQuery(symbol=_btc)
        assert q.symbol == _btc


class TestHelpQuery:
    def test_no_args(self) -> None:
        q = HelpQuery()
        assert q.kind == "help"


class TestOperatorQueryUnion:
    @pytest.mark.parametrize(
        ("payload", "expected_type"),
        [
            ({"kind": "status"}, StatusQuery),
            ({"kind": "open_orders"}, OpenOrdersQuery),
            ({"kind": "open_orders", "symbol": "BTC/USD"}, OpenOrdersQuery),
            ({"kind": "recent_fills"}, RecentFillsQuery),
            ({"kind": "recent_fills", "symbol": "ETH/USD", "lookback_hours": 6}, RecentFillsQuery),
            ({"kind": "recent_suggestions"}, RecentSuggestionsQuery),
            ({"kind": "recent_news"}, RecentNewsQuery),
            ({"kind": "harvester_status"}, HarvesterStatusQuery),
            ({"kind": "recent_proposals", "direction": "exchange_to_bank"}, RecentProposalsQuery),
            ({"kind": "grid_config"}, GridConfigQuery),
            ({"kind": "help"}, HelpQuery),
        ],
    )
    def test_validate_python(self, payload: dict[str, object], expected_type: type) -> None:
        q = _adapter().validate_python(payload)
        assert isinstance(q, expected_type)

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValidationError):
            _adapter().validate_python({"kind": "summon_the_oracle"})

    def test_recent_fills_round_trip(self) -> None:
        q = RecentFillsQuery(symbol=_btc, lookback_hours=12, limit=30)
        adapter = _adapter()
        dumped = adapter.dump_python(q)
        revived = adapter.validate_python(dumped)
        assert revived == q

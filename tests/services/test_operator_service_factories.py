"""Tests for the degraded-result factories in operator_service (Stage 8.0.B).

These pure-function factories produce the empty-result shapes for
graceful-degrade when a cross-DB storage isn't wired (advise.db,
news.db, harvest.db). Independent of OperatorService construction so
the "what does empty look like" contract is testable in isolation.
"""

from __future__ import annotations

import pytest

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.operator import (
    RecentNewsQuery,
    RecentNewsResult,
    RecentProposalsQuery,
    RecentProposalsResult,
    RecentSuggestionsQuery,
    RecentSuggestionsResult,
)
from wobblebot.services.operator_service import (
    _empty_recent_news,
    _empty_recent_proposals,
    _empty_recent_suggestions,
)

pytestmark = pytest.mark.unit


class TestEmptyRecentSuggestions:
    def test_no_symbol_returns_none_symbol(self) -> None:
        query = RecentSuggestionsQuery(symbol=None)
        result = _empty_recent_suggestions(query)
        assert isinstance(result, RecentSuggestionsResult)
        assert result.symbol is None
        assert result.suggestions == []

    def test_with_symbol_echoes_to_string(self) -> None:
        query = RecentSuggestionsQuery(symbol=Symbol(base="BTC", quote="USD"))
        result = _empty_recent_suggestions(query)
        assert result.symbol == "BTC/USD"
        assert result.suggestions == []


class TestEmptyRecentNews:
    def test_echoes_lookback_hours(self) -> None:
        query = RecentNewsQuery(lookback_hours=48)
        result = _empty_recent_news(query)
        assert isinstance(result, RecentNewsResult)
        assert result.lookback_hours == 48
        assert result.items == []

    def test_default_lookback_24h(self) -> None:
        query = RecentNewsQuery()
        result = _empty_recent_news(query)
        assert result.lookback_hours == 24


class TestEmptyRecentProposals:
    def test_echoes_direction_and_lookback(self) -> None:
        query = RecentProposalsQuery(direction="exchange_to_bank", lookback_hours=12)
        result = _empty_recent_proposals(query)
        assert isinstance(result, RecentProposalsResult)
        assert result.direction == "exchange_to_bank"
        assert result.lookback_hours == 12
        assert result.proposals == []

    def test_no_direction_filter_preserved(self) -> None:
        query = RecentProposalsQuery(direction=None)
        result = _empty_recent_proposals(query)
        assert result.direction is None
        assert result.proposals == []

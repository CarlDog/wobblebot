"""Unit tests for the weather-report aggregation (P4.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from wobblebot.domain.value_objects import OHLCBar, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion
from wobblebot.services.weather_report import (
    MAX_DIRECTIONAL_CALLS,
    compute_symbol_trend,
    extract_directional_calls,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
_BTC = Symbol.from_string("BTC/USD")


def _bar(hours_ago: float, price: str) -> OHLCBar:
    value = Decimal(price)
    return OHLCBar(
        symbol=_BTC,
        interval_minutes=60,
        opened_at=_NOW - timedelta(hours=hours_ago),
        open=value,
        high=value,
        low=value,
        close=value,
        vwap=value,
        volume=Decimal("1"),
        count=1,
    )


def _ramp_bars(hours: int, *, start: float = 100.0, step: float = 0.5) -> list[OHLCBar]:
    """Oldest-first hourly bars rising ``step`` per hour, ending now-1h."""
    return [_bar(hours - i, str(start + step * i)) for i in range(hours)]


def _suggestion(
    role: str,
    recommendations: dict[str, Any],
    *,
    symbol: str | None = "BTC/USD",
    hours_ago: float = 1.0,
    confidence: str = "medium",
) -> AdvisorSuggestion:
    created = _NOW - timedelta(hours=hours_ago)
    summary: dict[str, Any] = {}
    if symbol is not None:
        summary["symbol"] = symbol
    return AdvisorSuggestion(
        recommendation=AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=created),
            role=role,
            recommendations=recommendations,
            rationale="test",
            confidence=confidence,  # type: ignore[arg-type]
        ),
        created_at=Timestamp(dt=created),
        input_summary=summary,
        model_name="test-model",
    )


class TestComputeSymbolTrend:
    def test_empty_bars_reports_all_nulls(self) -> None:
        trend = compute_symbol_trend(_BTC, [], now=_NOW)
        assert trend.symbol == "BTC/USD"
        assert trend.latest_price is None
        assert trend.change_24h_pct is None
        assert trend.change_window_pct is None
        assert trend.range_position_pct is None

    def test_ramp_trend_math(self) -> None:
        bars = _ramp_bars(72)  # 100 -> 135.5 over 3 days
        trend = compute_symbol_trend(_BTC, bars, now=_NOW)
        assert trend.latest_price == pytest.approx(135.5)
        assert trend.change_window_pct == pytest.approx((135.5 / 100 - 1) * 100)
        # 24h reference: last bar opened at/before now-24h is the 25h-ago
        # bar (the 24h-ago one opens exactly at the boundary... inclusive).
        assert trend.change_24h_pct is not None
        assert trend.change_24h_pct > 0
        # A monotonic ramp ends at its highs.
        assert trend.range_position_pct == pytest.approx(100.0)
        assert trend.rsi_14 is not None  # 72 hourly bars is plenty for RSI(14)

    def test_window_too_short_for_24h_reference(self) -> None:
        bars = _ramp_bars(12)  # only 12h of history
        trend = compute_symbol_trend(_BTC, bars, now=_NOW)
        assert trend.change_24h_pct is None
        assert trend.change_window_pct is not None

    def test_flat_market_has_no_range_position(self) -> None:
        bars = [_bar(3, "100"), _bar(2, "100"), _bar(1, "100")]
        trend = compute_symbol_trend(_BTC, bars, now=_NOW)
        assert trend.range_position_pct is None  # zero range = undefined, not 50


class TestExtractDirectionalCalls:
    def test_gremlin_calls_extracted_newest_first(self) -> None:
        suggestions = [
            _suggestion("gremlin", {"direction": "up", "horizon_hours": 24}, hours_ago=5),
            _suggestion("gremlin", {"direction": "chop", "horizon_hours": 12}, hours_ago=1),
            _suggestion("quant", {"spacing_percentage": 1.8}, hours_ago=0.5),
        ]
        calls = extract_directional_calls(suggestions, now=_NOW)
        assert [c.direction for c in calls] == ["chop", "up"]
        assert calls[0].age_hours == pytest.approx(1.0)
        assert calls[1].horizon_hours == pytest.approx(24.0)

    def test_malformed_calls_are_omitted(self) -> None:
        suggestions = [
            _suggestion("gremlin", {"direction": "sideways", "horizon_hours": 24}),
            _suggestion("gremlin", {"direction": "up", "horizon_hours": True}),
            _suggestion("gremlin", {"direction": "up", "horizon_hours": 24}, symbol=None),
            _suggestion("gremlin", {"direction": "up", "horizon_hours": 24}),
        ]
        calls = extract_directional_calls(suggestions, now=_NOW)
        # "sideways" is a string so it passes the shape filter here — the
        # ledger grades validity; the report only drops structural junk.
        assert len(calls) == 2
        assert {c.direction for c in calls} == {"sideways", "up"}

    def test_cap_applies(self) -> None:
        suggestions = [
            _suggestion("gremlin", {"direction": "up", "horizon_hours": 24}, hours_ago=i)
            for i in range(MAX_DIRECTIONAL_CALLS + 4)
        ]
        calls = extract_directional_calls(suggestions, now=_NOW)
        assert len(calls) == MAX_DIRECTIONAL_CALLS

"""Unit tests for the AdvisorPort domain models (Stage 3.2).

The port itself is abstract; these tests cover the wire-format
schemas that ride on top of it — ``PerformanceSummary``,
``AdvisorRecommendation``, ``CurrentGridParams``. The Ollama
adapter tests in ``tests/adapters/test_ollama_advisor.py`` exercise
the round-trip through the port's contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorRecommendation,
    CurrentGridParams,
    PerformanceSummary,
)

pytestmark = pytest.mark.unit


def _make_summary(**overrides: object) -> PerformanceSummary:
    base: dict[str, object] = {
        "symbol": "BTC/USD",
        "lookback_hours": 24.0,
        "latest_price": 80000.0,
        "snapshot_count": 1000,
        "volatility": 0.0004,
        "max_drawdown": -0.03,
        "flatness": 0.97,
        "cycle_count": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
    }
    base.update(overrides)
    return PerformanceSummary(**base)  # type: ignore[arg-type]


class TestPerformanceSummary:
    def test_minimum_valid(self) -> None:
        summary = _make_summary()
        assert summary.symbol == "BTC/USD"
        assert summary.lookback_hours == 24.0
        assert summary.current_grid.spacing_percentage is None

    def test_frozen(self) -> None:
        summary = _make_summary()
        with pytest.raises(ValidationError):
            summary.symbol = "ETH/USD"  # type: ignore[misc]

    def test_drawdown_must_be_non_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_summary(max_drawdown=0.5)

    def test_volatility_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            _make_summary(volatility=-0.001)

    def test_flatness_in_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            _make_summary(flatness=1.5)

    def test_win_rate_in_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            _make_summary(win_rate=1.5)

    def test_lookback_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_summary(lookback_hours=0)

    def test_carries_current_grid_params(self) -> None:
        grid = CurrentGridParams(
            spacing_percentage=1.0,
            levels_above=3,
            levels_below=3,
            order_size_usd=10.0,
        )
        summary = _make_summary(current_grid=grid)
        assert summary.current_grid.levels_above == 3


class TestAdvisorRecommendation:
    def _make(self, **overrides: object) -> AdvisorRecommendation:
        base: dict[str, object] = {
            "recommendation_id": "rec-123",
            "timestamp": Timestamp(dt=datetime.now(UTC)),
            "role": "single",
            "recommendations": {"spacing_percentage": 1.2},
            "rationale": "Volatility tightened; widen grid spacing slightly.",
            "confidence": "medium",
        }
        base.update(overrides)
        return AdvisorRecommendation(**base)  # type: ignore[arg-type]

    def test_minimum_valid(self) -> None:
        rec = self._make()
        assert rec.role == "single"
        assert rec.confidence == "medium"

    def test_frozen(self) -> None:
        rec = self._make()
        with pytest.raises(ValidationError):
            rec.role = "quant"  # type: ignore[misc]

    def test_confidence_constrained_to_high_medium_low(self) -> None:
        with pytest.raises(ValidationError):
            self._make(confidence="very-high")

    def test_empty_recommendations_dict_allowed(self) -> None:
        # "No change suggested" is a valid LLM response.
        rec = self._make(recommendations={})
        assert rec.recommendations == {}

    def test_rationale_required_nonempty(self) -> None:
        with pytest.raises(ValidationError):
            self._make(rationale="")

    def test_role_required_nonempty(self) -> None:
        with pytest.raises(ValidationError):
            self._make(role="")

    def test_recommendations_dict_is_loose(self) -> None:
        # No whitelist at this layer — the auto-apply gate (Stage 3.4b)
        # enforces which keys can mutate the running config.
        rec = self._make(recommendations={"banana": 42, "other": "yes"})
        assert rec.recommendations["banana"] == 42


class TestCurrentGridParams:
    def test_all_none_is_valid(self) -> None:
        params = CurrentGridParams()
        assert params.spacing_percentage is None

    def test_frozen(self) -> None:
        params = CurrentGridParams(spacing_percentage=1.0)
        with pytest.raises(ValidationError):
            params.spacing_percentage = 2.0  # type: ignore[misc]

"""Unit tests for the ADR-035 evaluator's pure logic (P4.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from wobblebot.config.grid import KRAKEN_MAKER_FEE_RATE, KRAKEN_TAKER_FEE_RATE
from wobblebot.domain.value_objects import OHLCBar, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion
from wobblebot.services.advisor_evaluator import (
    EVALUATOR_VERSION,
    FEE_SCHEDULE_CUTOVER,
    PRE_CUTOVER_MAKER_FEE_RATE,
    PRE_CUTOVER_TAKER_FEE_RATE,
    REPLAY_WINDOW,
    TIE_EPSILON_USD,
    ArmBuildError,
    bar_coverage_reason,
    build_arms,
    build_scored,
    build_unscoreable,
    classify,
    fee_rates_for,
    outcome_sign,
)

pytestmark = pytest.mark.unit

_CREATED = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_BTC = Symbol.from_string("BTC/USD")

_FULL_GRID: dict[str, Any] = {
    "spacing_percentage": 3.0,
    "levels_above": 3,
    "levels_below": 3,
    "order_size_usd": 10.0,
}


def _suggestion(
    recommendations: dict[str, Any] | None = None,
    *,
    current_grid: dict[str, Any] | None = _FULL_GRID,
    symbol: str | None = "BTC/USD",
    created: datetime = _CREATED,
) -> AdvisorSuggestion:
    summary: dict[str, Any] = {}
    if symbol is not None:
        summary["symbol"] = symbol
    if current_grid is not None:
        summary["current_grid"] = current_grid
    rec = AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=created),
        role="quant",
        recommendations=recommendations or {},
        rationale="test",
        confidence="medium",
    )
    return AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=created),
        input_summary=summary,
        model_name="gpt-5-mini",
    )


def _bar(opened_at: datetime, *, interval: int = 60, price: str = "100") -> OHLCBar:
    value = Decimal(price)
    return OHLCBar(
        symbol=_BTC,
        interval_minutes=interval,
        opened_at=opened_at,
        open=value,
        high=value,
        low=value,
        close=value,
        vwap=value,
        volume=Decimal("1"),
        count=1,
    )


def _full_window_bars(interval: int = 60) -> list[OHLCBar]:
    step = timedelta(minutes=interval)
    count = int(REPLAY_WINDOW / step)
    return [_bar(_CREATED + i * step, interval=interval) for i in range(count)]


class TestClassify:
    def test_happy_path_is_scoreable(self) -> None:
        result = classify(_suggestion({"spacing_percentage": 1.8}))
        assert result.unscoreable_reason is None
        assert result.kind == "config_rec"
        assert result.symbol == _BTC
        assert result.window_start == _CREATED
        assert result.window_end == _CREATED + REPLAY_WINDOW

    def test_empty_recommendation_is_unscoreable(self) -> None:
        result = classify(_suggestion({}))
        assert result.unscoreable_reason is not None
        assert "empty recommendation" in result.unscoreable_reason

    def test_foreign_keys_are_unscoreable(self) -> None:
        result = classify(
            _suggestion({"spacing_percentage": 1.8, "counter_target_mode": "top_sell"})
        )
        assert result.unscoreable_reason is not None
        assert "counter_target_mode" in result.unscoreable_reason

    def test_missing_symbol_is_unscoreable(self) -> None:
        result = classify(_suggestion({"spacing_percentage": 1.8}, symbol=None))
        assert result.symbol is None
        assert result.unscoreable_reason == "input_summary has no symbol"

    def test_unparseable_symbol_is_unscoreable(self) -> None:
        result = classify(_suggestion({"spacing_percentage": 1.8}, symbol="BTCUSD"))
        assert result.symbol is None
        assert result.unscoreable_reason is not None
        assert "BTCUSD" in result.unscoreable_reason


class TestFeeSchedule:
    def test_pre_cutover_window_uses_retired_rates(self) -> None:
        maker, taker = fee_rates_for(FEE_SCHEDULE_CUTOVER - timedelta(days=1))
        assert (maker, taker) == (PRE_CUTOVER_MAKER_FEE_RATE, PRE_CUTOVER_TAKER_FEE_RATE)

    def test_post_cutover_window_uses_current_constants(self) -> None:
        maker, taker = fee_rates_for(FEE_SCHEDULE_CUTOVER)
        assert (maker, taker) == (KRAKEN_MAKER_FEE_RATE, KRAKEN_TAKER_FEE_RATE)


class TestBuildArms:
    def test_proposed_key_applies_and_rest_carries_over(self) -> None:
        inforce, proposed = build_arms(
            _suggestion({"spacing_percentage": 1.8}), maker_fee_rate=KRAKEN_MAKER_FEE_RATE
        )
        assert inforce.default.spacing_percentage == Decimal("3.0")
        assert proposed.default.spacing_percentage == Decimal("1.8")
        assert proposed.default.levels_above == 3
        assert proposed.default.order_size_usd == Decimal("10.0")

    def test_missing_current_grid(self) -> None:
        with pytest.raises(ArmBuildError, match="no current_grid"):
            build_arms(
                _suggestion({"spacing_percentage": 1.8}, current_grid=None),
                maker_fee_rate=KRAKEN_MAKER_FEE_RATE,
            )

    def test_incomplete_current_grid(self) -> None:
        grid = {**_FULL_GRID, "spacing_percentage": None}
        with pytest.raises(ArmBuildError, match="spacing_percentage is missing"):
            build_arms(
                _suggestion({"spacing_percentage": 1.8}, current_grid=grid),
                maker_fee_rate=KRAKEN_MAKER_FEE_RATE,
            )

    def test_bool_value_rejected(self) -> None:
        with pytest.raises(ArmBuildError, match="non-numeric"):
            build_arms(_suggestion({"levels_above": True}), maker_fee_rate=KRAKEN_MAKER_FEE_RATE)

    def test_string_value_rejected(self) -> None:
        with pytest.raises(ArmBuildError, match="non-numeric"):
            build_arms(
                _suggestion({"spacing_percentage": "1.8"}),
                maker_fee_rate=KRAKEN_MAKER_FEE_RATE,
            )

    def test_fractional_level_count_rejected(self) -> None:
        with pytest.raises(ArmBuildError, match="non-integer level count"):
            build_arms(_suggestion({"levels_above": 3.5}), maker_fee_rate=KRAKEN_MAKER_FEE_RATE)

    def test_proposed_spacing_below_window_floor_is_unbuildable(self) -> None:
        with pytest.raises(ArmBuildError, match=r"proposed spacing .* fee floor"):
            build_arms(
                _suggestion({"spacing_percentage": 0.5}),
                maker_fee_rate=KRAKEN_MAKER_FEE_RATE,
            )

    def test_invalid_inforce_grid_is_unbuildable(self) -> None:
        grid = {**_FULL_GRID, "spacing_percentage": 0.5}
        with pytest.raises(ArmBuildError, match=r"in-force spacing .* fee floor"):
            build_arms(
                _suggestion({"spacing_percentage": 1.8}, current_grid=grid),
                maker_fee_rate=KRAKEN_MAKER_FEE_RATE,
            )

    def test_floor_is_the_windows_not_todays(self) -> None:
        """The 2026-08-17 probe finding: a May-era 0.65% proposal was
        legal above that era's 0.5% floor and must stay scoreable at
        the pre-cutover rate — while failing at today's 0.8% floor."""
        suggestion = _suggestion({"spacing_percentage": 0.65})
        _, proposed = build_arms(suggestion, maker_fee_rate=PRE_CUTOVER_MAKER_FEE_RATE)
        assert proposed.default.spacing_percentage == Decimal("0.65")
        with pytest.raises(ArmBuildError, match="fee floor"):
            build_arms(suggestion, maker_fee_rate=KRAKEN_MAKER_FEE_RATE)


class TestOutcomeSign:
    def test_better_and_worse(self) -> None:
        assert outcome_sign(Decimal("1.00"), Decimal("0.00")) == "better"
        assert outcome_sign(Decimal("-1.00"), Decimal("0.00")) == "worse"

    def test_tie_band_is_inclusive_at_epsilon(self) -> None:
        assert outcome_sign(TIE_EPSILON_USD, Decimal("0")) == "tie"
        assert outcome_sign(TIE_EPSILON_USD + Decimal("0.01"), Decimal("0")) == "better"
        assert outcome_sign(-TIE_EPSILON_USD - Decimal("0.01"), Decimal("0")) == "worse"


class TestBarCoverage:
    _WINDOW_END = _CREATED + REPLAY_WINDOW

    def _reason(self, bars: list[OHLCBar]) -> str | None:
        return bar_coverage_reason(
            bars, window_start=_CREATED, window_end=self._WINDOW_END, interval_minutes=60
        )

    def test_full_window_passes(self) -> None:
        assert self._reason(_full_window_bars()) is None

    def test_no_bars(self) -> None:
        reason = self._reason([])
        assert reason is not None
        assert "no 60m bars" in reason

    def test_thin_coverage(self) -> None:
        bars = _full_window_bars()[::2]  # every other bar: 50% coverage
        reason = self._reason(bars)
        assert reason is not None
        assert "insufficient bars" in reason

    def test_late_start(self) -> None:
        # Drop 8 head bars: 160/168 still clears the 90% count floor,
        # so the failure is specifically the edge-gap check.
        bars = _full_window_bars()[8:]
        reason = self._reason(bars)
        assert reason is not None
        assert "starts late" in reason

    def test_early_end(self) -> None:
        bars = _full_window_bars()[:-8]
        reason = self._reason(bars)
        assert reason is not None
        assert "ends early" in reason


class TestOutcomeBuilders:
    def test_unscoreable_row_shape(self) -> None:
        classification = classify(_suggestion({}))
        assert classification.unscoreable_reason is not None
        row = build_unscoreable(
            7,
            classification,
            granularity_minutes=60,
            reason=classification.unscoreable_reason,
            scored_at=_CREATED,
        )
        assert row.suggestion_id == 7
        assert row.scoreable is False
        assert row.outcome is None
        assert row.granularity_minutes == 60
        assert row.evaluator_version == EVALUATOR_VERSION
        assert row.window_end.dt - row.window_start.dt == REPLAY_WINDOW

    def test_scored_row_shape(self) -> None:
        classification = classify(_suggestion({"spacing_percentage": 1.8}))
        row = build_scored(
            7,
            classification,
            granularity_minutes=60,
            proposed_arm={"cycles": 2},
            inforce_arm={"cycles": 1},
            outcome="better",
            scored_at=_CREATED,
        )
        assert row.scoreable is True
        assert row.unscoreable_reason is None
        assert row.outcome == "better"
        assert row.proposed_arm_json == {"cycles": 2}
        assert row.inforce_arm_json == {"cycles": 1}

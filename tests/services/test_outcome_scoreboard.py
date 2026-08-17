"""Unit tests for the P4.3 scoreboard aggregation (ADR-035)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import RecommendationOutcome
from wobblebot.services.outcome_scoreboard import (
    MIN_SAMPLE_FOR_RATE,
    ScoreboardCell,
    aggregate_scored,
    pair_quant_vs_hold,
    unscoreable_taxonomy,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _outcome(
    outcome: Literal["better", "worse", "tie"] | None,
    *,
    granularity: int | None = 60,
    scoreable: bool = True,
    reason: str | None = None,
) -> RecommendationOutcome:
    return RecommendationOutcome(
        suggestion_id=1,
        kind="config_rec",
        scoreable=scoreable,
        unscoreable_reason=reason,
        window_start=Timestamp(dt=_NOW),
        window_end=Timestamp(dt=_NOW + timedelta(days=7)),
        granularity_minutes=granularity,
        outcome=outcome,
        evaluator_version=1,
        scored_at=Timestamp(dt=_NOW),
    )


class TestAggregateScored:
    def test_cells_keyed_by_role_and_granularity_sorted_finest_first(self) -> None:
        rows = [
            ("quant", _outcome("better")),
            ("quant", _outcome("worse", granularity=1)),
            ("quant", _outcome("tie")),
            ("heuristic", _outcome("worse")),
        ]
        cells = aggregate_scored(rows)
        assert [(c.role, c.granularity_minutes) for c in cells] == [
            ("heuristic", 60),
            ("quant", 1),
            ("quant", 60),
        ]
        quant_60 = cells[2]
        assert (quant_60.better, quant_60.worse, quant_60.tie) == (1, 0, 1)
        assert quant_60.scored == 2

    def test_rows_without_a_sign_are_ignored(self) -> None:
        assert aggregate_scored([("quant", _outcome(None))]) == []

    def test_none_granularity_sorts_last(self) -> None:
        rows = [
            ("quant", _outcome("better", granularity=None)),
            ("quant", _outcome("better")),
        ]
        cells = aggregate_scored(rows)
        assert [c.granularity_minutes for c in cells] == [60, None]


class TestHitRate:
    def test_rate_withheld_below_the_decisive_floor(self) -> None:
        cell = ScoreboardCell(
            role="quant",
            granularity_minutes=60,
            better=MIN_SAMPLE_FOR_RATE - 11,
            worse=10,
            tie=5,
        )
        assert cell.decisive == MIN_SAMPLE_FOR_RATE - 1
        assert cell.hit_rate is None

    def test_rate_at_the_floor(self) -> None:
        cell = ScoreboardCell(role="quant", granularity_minutes=60, better=20, worse=10, tie=0)
        assert cell.decisive == MIN_SAMPLE_FOR_RATE
        assert cell.hit_rate == pytest.approx(20 / 30)

    def test_ties_count_in_scored_but_never_in_decisive(self) -> None:
        cell = ScoreboardCell(role="quant", granularity_minutes=60, better=20, worse=10, tie=100)
        assert cell.scored == 130
        assert cell.decisive == 30
        assert cell.hit_rate == pytest.approx(20 / 30)


class TestUnscoreableTaxonomy:
    def test_reasons_collapse_to_first_clause(self) -> None:
        rows = [
            _outcome(None, scoreable=False, reason="insufficient bars: 3/168 at 60m"),
            _outcome(None, scoreable=False, reason="insufficient bars: 9/168 at 60m"),
            _outcome(None, scoreable=False, reason="empty recommendation (no change)"),
        ]
        buckets = unscoreable_taxonomy(rows)
        assert buckets["insufficient bars"] == 2
        assert buckets["empty recommendation (no change)"] == 1

    def test_scoreable_rows_are_ignored(self) -> None:
        assert not unscoreable_taxonomy([_outcome("better")])


class TestPairQuantVsHold:
    def test_mapping(self) -> None:
        assert pair_quant_vs_hold("better") == "quant"
        assert pair_quant_vs_hold("worse") == "heuristic"
        assert pair_quant_vs_hold("tie") == "even"

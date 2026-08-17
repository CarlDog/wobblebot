"""End-to-end tests for tools/score_report.py (ADR-035, P4.3).

The pairing premise — "escalation means the guard layer held" — is
exercised both ways: a spec with every guard disabled (all re-runs
hold, so the pairing is the outcome sign re-labeled) and a spec whose
drawdown guard fires on the stored input (excluded and counted, never
silently paired).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import pytest
import pytest_asyncio

from tools.score_report import build_report
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.heuristic import CurvePoint, HeuristicSpec
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorRecommendation,
    AdvisorSuggestion,
    CurrentGridParams,
    PerformanceSummary,
    RecommendationOutcome,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

_CURVE = [CurvePoint(vol=0.001, spacing=0.9), CurvePoint(vol=0.01, spacing=2.0)]

# Every guard off: every input re-runs to a non-clear HOLD — the
# escalated premise holds for all rows.
_HOLD_SPEC = HeuristicSpec(
    curve=_CURVE,
    guards={  # type: ignore[arg-type]
        "directional_runaway": {"enabled": False},
        "defensive_drawdown": {"enabled": False},
        "dont_fix_working": {"enabled": False},
        "fee_floor_calm": {"enabled": False},
    },
)

# Drawdown guard armed so loosely it fires on the test summary's -2%.
_FIRING_SPEC = HeuristicSpec(
    curve=_CURVE,
    guards={  # type: ignore[arg-type]
        "directional_runaway": {"enabled": False},
        "defensive_drawdown": {"enabled": True, "threshold": -0.001},
        "dont_fix_working": {"enabled": False},
        "fee_floor_calm": {"enabled": False},
    },
)


@pytest_asyncio.fixture
async def advise() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _summary_dict() -> dict[str, Any]:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=24.0,
        latest_price=60000.0,
        snapshot_count=100,
        volatility=0.004,
        max_drawdown=-0.02,
        flatness=0.5,
        cycle_count=1,
        win_rate=1.0,
        current_grid=CurrentGridParams(
            spacing_percentage=3.0, levels_above=3, levels_below=3, order_size_usd=10.0
        ),
    ).model_dump(mode="json")


def _suggestion(
    role: str = "quant",
    *,
    recommendations: dict[str, Any] | None = None,
    input_summary: dict[str, Any] | None = None,
) -> AdvisorSuggestion:
    rec = AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=_T0),
        role=role,
        recommendations=recommendations if recommendations is not None else {},
        rationale="test",
        confidence="medium",
    )
    return AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=_T0),
        input_summary=input_summary if input_summary is not None else _summary_dict(),
        model_name="gpt-5-mini",
    )


def _outcome(
    suggestion_id: int,
    outcome: Literal["better", "worse", "tie"] | None,
    *,
    scoreable: bool = True,
    reason: str | None = None,
    granularity: int = 60,
) -> RecommendationOutcome:
    return RecommendationOutcome(
        suggestion_id=suggestion_id,
        kind="config_rec",
        scoreable=scoreable,
        unscoreable_reason=reason,
        window_start=Timestamp(dt=_T0),
        window_end=Timestamp(dt=_T0 + timedelta(days=7)),
        granularity_minutes=granularity,
        outcome=outcome,
        evaluator_version=1,
        scored_at=Timestamp(dt=_T0),
    )


async def test_report_end_to_end(advise: SQLiteStorageAdapter) -> None:
    # 1: quant scored better; 2: quant scored worse; 3: heuristic
    # unscoreable (empty rec); 4: still unscored (stays in the queue).
    await advise.save_advisor_suggestion(_suggestion(recommendations={"spacing_percentage": 1.8}))
    await advise.save_advisor_suggestion(_suggestion(recommendations={"spacing_percentage": 2.0}))
    await advise.save_advisor_suggestion(_suggestion(role="heuristic"))
    await advise.save_advisor_suggestion(_suggestion(recommendations={"spacing_percentage": 2.2}))
    await advise.save_recommendation_outcome(_outcome(1, "better"))
    await advise.save_recommendation_outcome(_outcome(2, "worse"))
    await advise.save_recommendation_outcome(
        _outcome(3, None, scoreable=False, reason="empty recommendation (no proposed change)")
    )

    report = await build_report(advise, evaluator_version=1, heuristic_spec=_HOLD_SPEC)

    assert report.total_outcomes == 3
    assert report.scored_count == 2
    assert report.unscoreable_count == 1
    [cell] = report.cells
    assert cell.role == "quant"
    assert (cell.better, cell.worse, cell.tie) == (1, 1, 0)
    assert cell.hit_rate is None  # 2 decisive rows, floor is 30
    assert report.taxonomy["empty recommendation (no proposed change)"] == 1
    assert report.queue_remainders[60] == 1  # only #4 remains at 60m
    assert report.queue_remainders[1] == 4  # nothing scored at 1m yet
    assert report.pairing is not None
    stats = report.pairing[60]
    assert (stats.quant_wins, stats.heuristic_wins, stats.even) == (1, 1, 0)
    assert stats.held == 2 and stats.rerun == 2


async def test_pairing_counts_guard_fired_and_unparseable(
    advise: SQLiteStorageAdapter,
) -> None:
    await advise.save_advisor_suggestion(_suggestion(recommendations={"spacing_percentage": 1.8}))
    await advise.save_advisor_suggestion(
        _suggestion(
            recommendations={"spacing_percentage": 2.0},
            input_summary={"symbol": "BTC/USD"},  # not a valid PerformanceSummary
        )
    )
    await advise.save_recommendation_outcome(_outcome(1, "better"))
    await advise.save_recommendation_outcome(_outcome(2, "worse"))

    report = await build_report(advise, evaluator_version=1, heuristic_spec=_FIRING_SPEC)

    assert report.pairing is not None
    stats = report.pairing[60]
    assert stats.held == 0
    assert stats.guard_fired["defensive_drawdown"] == 1
    assert stats.unparseable == 1
    assert stats.rerun == 2


async def test_pairing_skipped_without_a_spec(advise: SQLiteStorageAdapter) -> None:
    await advise.save_advisor_suggestion(_suggestion(recommendations={"spacing_percentage": 1.8}))
    await advise.save_recommendation_outcome(_outcome(1, "tie"))
    report = await build_report(
        advise,
        evaluator_version=1,
        heuristic_spec=None,
        pairing_skip_reason="advisor.heuristic_file is not configured",
    )
    assert report.pairing is None
    assert report.pairing_skip_reason == "advisor.heuristic_file is not configured"
    [cell] = report.cells
    assert cell.tie == 1


async def test_other_evaluator_versions_are_invisible(advise: SQLiteStorageAdapter) -> None:
    await advise.save_advisor_suggestion(_suggestion(recommendations={"spacing_percentage": 1.8}))
    await advise.save_recommendation_outcome(_outcome(1, "better"))
    report = await build_report(advise, evaluator_version=2, heuristic_spec=None)
    assert report.total_outcomes == 0
    assert report.cells == []
    # The v2 queue still holds the suggestion — versions are independent.
    assert report.queue_remainders[60] == 1

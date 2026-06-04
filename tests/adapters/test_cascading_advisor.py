"""Tests for the ``CascadingAdvisorAdapter`` (ADR-022).

The cascade resolves guard cases via the heuristic (no LLM call),
escalates every non-guard tick to the LLM, and falls back to the
heuristic on LLM failure or cost-cap trip. A stub LLM records its calls
so "did we escalate?" is asserted directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wobblebot.adapters.cascading_advisor import CascadingAdvisorAdapter
from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.config.heuristic import CurvePoint, HeuristicSpec
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    CurrentGridParams,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

pytestmark = pytest.mark.unit


_SPEC = HeuristicSpec(
    curve=[
        CurvePoint(vol=0.0008, spacing=0.65),
        CurvePoint(vol=0.004, spacing=1.25),
        CurvePoint(vol=0.008, spacing=1.90),
        CurvePoint(vol=0.014, spacing=2.70),
    ]
)


class StubLLM(AdvisorPort):
    """Records calls; returns a canned recommendation or raises ``raises``."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls = 0
        self.closed = False
        self._raises = raises

    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return AdvisorRecommendation(
            recommendation_id="llm-rec",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="single",
            recommendations={"spacing_percentage": 9.99},  # sentinel — LLM, not heuristic
            rationale="llm answer",
            confidence="high",
        )

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


def _summary(
    *,
    current_spacing: float | None,
    volatility: float,
    snapshot_count: int = 720,
    max_drawdown: float = -0.01,
    cycle_count: int = 4,
) -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=79000.0,
        snapshot_count=snapshot_count,
        volatility=volatility,
        max_drawdown=max_drawdown,
        flatness=0.5,
        cycle_count=cycle_count,
        win_rate=0.5,
        active_orders=6,
        current_grid=CurrentGridParams(
            spacing_percentage=current_spacing, levels_above=4, levels_below=4, order_size_usd=10.0
        ),
    )


def _heuristic() -> HeuristicAdvisorAdapter:
    return HeuristicAdvisorAdapter(spec=_SPEC)


def _cap_error() -> LLMCostCapExceeded:
    return LLMCostCapExceeded(
        cap_kind="daily",
        cap_value_usd=Decimal("1.00"),
        daily_spent_usd=Decimal("1.03"),
        session_spent_usd=Decimal("0.20"),
    )


def test_requires_both_advisors() -> None:
    with pytest.raises(ValueError, match="requires a heuristic"):
        CascadingAdvisorAdapter(heuristic=None, llm=StubLLM())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires an LLM"):
        CascadingAdvisorAdapter(heuristic=_heuristic(), llm=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_guard_match_resolves_locally() -> None:
    stub = StubLLM()
    cascade = CascadingAdvisorAdapter(heuristic=_heuristic(), llm=stub)
    # A guard fires (price ran away: 0 cycles + sharp drawdown -> HOLD),
    # so the heuristic answers and the LLM is never called.
    rec = await cascade.get_recommendation(
        _summary(current_spacing=0.60, volatility=0.008, max_drawdown=-0.06, cycle_count=0)
    )
    assert stub.calls == 0
    assert rec.role == "heuristic"
    assert rec.recommendations == {}  # directional_runaway holds


@pytest.mark.asyncio
async def test_no_guard_escalates_to_llm() -> None:
    stub = StubLLM()
    cascade = CascadingAdvisorAdapter(heuristic=_heuristic(), llm=stub)
    # No guard fires (mild vol, shallow drawdown, no clear override) ->
    # escalate to the LLM, whose answer is returned verbatim (ADR-022).
    rec = await cascade.get_recommendation(_summary(current_spacing=1.42, volatility=0.004))
    assert stub.calls == 1
    assert rec.recommendations == {"spacing_percentage": 9.99}  # the LLM's answer


@pytest.mark.asyncio
async def test_falls_back_to_heuristic_on_llm_error() -> None:
    stub = StubLLM(raises=AdvisorError("vendor down"))
    cascade = CascadingAdvisorAdapter(heuristic=_heuristic(), llm=stub)
    # No guard -> tries LLM -> LLM raises -> heuristic fallback (a HOLD).
    rec = await cascade.get_recommendation(_summary(current_spacing=1.42, volatility=0.004))
    assert stub.calls == 1
    assert rec.role == "heuristic"
    assert rec.recommendations == {}  # heuristic's no-guard HOLD


@pytest.mark.asyncio
async def test_falls_back_on_cost_cap() -> None:
    stub = StubLLM(raises=_cap_error())
    cascade = CascadingAdvisorAdapter(heuristic=_heuristic(), llm=stub)
    rec = await cascade.get_recommendation(_summary(current_spacing=1.42, volatility=0.004))
    assert stub.calls == 1
    assert rec.role == "heuristic"


@pytest.mark.asyncio
async def test_aclose_delegates_to_llm() -> None:
    stub = StubLLM()
    cascade = CascadingAdvisorAdapter(heuristic=_heuristic(), llm=stub)
    await cascade.aclose()
    assert stub.closed is True

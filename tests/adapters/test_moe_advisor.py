"""Unit tests for MoEAdvisorAdapter (Stage 3.4a Slice B)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from wobblebot.adapters.moe_advisor import MoEAdvisorAdapter, MoEExpertEntry
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

pytestmark = pytest.mark.unit


def _summary() -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        snapshot_count=100,
        volatility=0.0004,
        max_drawdown=-0.03,
        flatness=0.97,
        cycle_count=0,
        win_rate=0.0,
    )


def _rec(
    *,
    role: str,
    recommendations: dict[str, Any] | None = None,
    confidence: str = "medium",
    rationale: str = "expert opinion",
) -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommendation_id=f"rec-{role}-{confidence}",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=role,
        recommendations=recommendations or {},
        rationale=rationale,
        confidence=confidence,  # type: ignore[arg-type]
    )


class _StubExpert(AdvisorPort):
    """In-process AdvisorPort that returns a canned recommendation or raises."""

    def __init__(
        self,
        *,
        opinion: AdvisorRecommendation | None = None,
        error: AdvisorError | None = None,
    ) -> None:
        self._opinion = opinion
        self._error = error
        self.call_count = 0

    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        del summary
        self.call_count += 1
        if self._error is not None:
            raise self._error
        assert self._opinion is not None
        return self._opinion

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        del recommendation
        return True


class _StubArbitratorAdvisor(AdvisorPort):
    """AdvisorPort that also accepts the ``extra_context`` kwarg —
    mirrors OllamaAdapter's Stage 3.4a extension. Captures the
    received context so MoE tests can assert the arbitrator's prompt
    actually received the opinions blob."""

    def __init__(self, *, response: AdvisorRecommendation) -> None:
        self._response = response
        self.last_extra_context: str = ""
        self.call_count = 0

    async def get_recommendation(  # pylint: disable=arguments-differ
        self,
        summary: PerformanceSummary,
        *,
        extra_context: str = "",
    ) -> AdvisorRecommendation:
        del summary
        self.call_count += 1
        self.last_extra_context = extra_context
        return self._response

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        del recommendation
        return True


def _entry(name: str, role: str, expert: AdvisorPort) -> MoEExpertEntry:
    return MoEExpertEntry(name=name, role=role, advisor=expert)


class TestConstructor:
    def test_empty_experts_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one expert"):
            MoEAdvisorAdapter(experts=[], aggregator="voting")

    def test_unknown_aggregator_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown aggregator"):
            MoEAdvisorAdapter(
                experts=[_entry("q", "quant", _StubExpert(opinion=_rec(role="quant")))],
                aggregator="lottery",  # type: ignore[arg-type]
            )

    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicates"):
            MoEAdvisorAdapter(
                experts=[
                    _entry("q", "quant", _StubExpert(opinion=_rec(role="quant"))),
                    _entry("q", "risk", _StubExpert(opinion=_rec(role="risk"))),
                ],
                aggregator="voting",
            )


@pytest.mark.asyncio
class TestHappyPath:
    async def test_voting_aggregation_with_three_experts(self) -> None:
        quant = _StubExpert(opinion=_rec(role="quant", recommendations={"spacing_percentage": 1.2}))
        risk = _StubExpert(opinion=_rec(role="risk", recommendations={"spacing_percentage": 1.2}))
        news = _StubExpert(opinion=_rec(role="news", recommendations={"spacing_percentage": 1.0}))
        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("quant", "quant", quant),
                _entry("risk", "risk", risk),
                _entry("news", "news", news),
            ],
            aggregator="voting",
        )

        result = await adapter.get_recommendation(_summary())

        assert result.role == "aggregated"
        assert result.recommendations == {"spacing_percentage": 1.2}
        assert quant.call_count == 1
        assert risk.call_count == 1
        assert news.call_count == 1

    async def test_per_expert_opinions_attached(self) -> None:
        """The aggregated recommendation must carry every expert's raw
        opinion in expert_opinions for the audit trail (ADR-007)."""
        quant = _StubExpert(opinion=_rec(role="quant", confidence="high"))
        risk = _StubExpert(opinion=_rec(role="risk", confidence="medium"))
        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("quant", "quant", quant),
                _entry("risk", "risk", risk),
            ],
            aggregator="weighted_confidence",
        )

        result = await adapter.get_recommendation(_summary())

        assert len(result.expert_opinions) == 2
        roles = {op.role for op in result.expert_opinions}
        assert roles == {"quant", "risk"}

    async def test_role_overridden_to_entry_role(self) -> None:
        """If an expert's prompt returns role='quant' but the operator's
        entry configures it as role='risk', the entry wins. Source of
        truth is the operator config, not the LLM's self-tag."""
        # Expert's own opinion says role="quant" — typical of prompts that
        # default to one role regardless of how the operator wires them.
        expert = _StubExpert(opinion=_rec(role="quant", recommendations={"x": 1}))
        adapter = MoEAdvisorAdapter(
            experts=[_entry("misnamed", "risk", expert)],
            aggregator="voting",
        )

        result = await adapter.get_recommendation(_summary())
        # The lone expert_opinion should be tagged role="risk" per the entry,
        # not the "quant" the expert self-tagged.
        assert result.expert_opinions[0].role == "risk"

    async def test_parallel_dispatch(self) -> None:
        """Verify the calls happen concurrently (asyncio.gather), not
        sequentially — by checking total time is closer to one call's
        duration than to N×duration."""
        import asyncio

        class _SlowExpert(AdvisorPort):
            async def get_recommendation(
                self, summary: PerformanceSummary
            ) -> AdvisorRecommendation:
                del summary
                await asyncio.sleep(0.1)
                return _rec(role="quant", recommendations={"x": 1})

            async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
                del recommendation
                return True

        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("a", "quant", _SlowExpert()),
                _entry("b", "risk", _SlowExpert()),
                _entry("c", "news", _SlowExpert()),
            ],
            aggregator="voting",
        )
        import time

        before = time.monotonic()
        await adapter.get_recommendation(_summary())
        elapsed = time.monotonic() - before
        # Parallel: ~0.1s. Sequential would be ~0.3s. Allow generous wiggle.
        assert elapsed < 0.25, f"calls appear sequential ({elapsed:.2f}s)"


@pytest.mark.asyncio
class TestFaultTolerance:
    async def test_one_bad_expert_proceeds_with_rest(self) -> None:
        """Per ADR-007: one vendor outage doesn't stop the advisor."""
        bad = _StubExpert(error=AdvisorError("HTTP 502 from Anthropic"))
        good_a = _StubExpert(
            opinion=_rec(role="quant", recommendations={"spacing_percentage": 1.2})
        )
        good_b = _StubExpert(opinion=_rec(role="risk", recommendations={"spacing_percentage": 1.2}))
        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("bad_cloud", "news", bad),
                _entry("local_quant", "quant", good_a),
                _entry("local_risk", "risk", good_b),
            ],
            aggregator="voting",
        )

        result = await adapter.get_recommendation(_summary())

        # The two survivors agreed — aggregated should reflect that.
        assert result.recommendations == {"spacing_percentage": 1.2}
        # Only the two surviving experts contribute to expert_opinions.
        assert len(result.expert_opinions) == 2

    async def test_all_experts_failing_raises(self) -> None:
        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("a", "quant", _StubExpert(error=AdvisorError("down"))),
                _entry("b", "risk", _StubExpert(error=AdvisorError("down"))),
            ],
            aggregator="voting",
        )

        with pytest.raises(AdvisorError, match="All 2 MoE experts failed"):
            await adapter.get_recommendation(_summary())

    async def test_failures_logged_with_structured_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("cloud_news", "news", _StubExpert(error=AdvisorError("timeout"))),
                _entry("local_quant", "quant", _StubExpert(opinion=_rec(role="quant"))),
            ],
            aggregator="voting",
        )
        with caplog.at_level("WARNING", logger="wobblebot.adapters.moe_advisor"):
            await adapter.get_recommendation(_summary())
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        # Structured-field assertion — the expert's name + role appear in the record's extra
        assert getattr(warnings[0], "expert_name") == "cloud_news"
        assert getattr(warnings[0], "expert_role") == "news"


@pytest.mark.asyncio
class TestValidateRecommendation:
    async def test_passes_through_true(self) -> None:
        adapter = MoEAdvisorAdapter(
            experts=[_entry("a", "quant", _StubExpert(opinion=_rec(role="quant")))],
            aggregator="voting",
        )
        rec = _rec(role="aggregated", confidence="medium")
        assert await adapter.validate_recommendation(rec) is True


class TestArbitratorConstructor:
    def test_arbitrator_required_when_aggregator_is_arbitrator(self) -> None:
        with pytest.raises(ValueError, match="requires an arbitrator entry"):
            MoEAdvisorAdapter(
                experts=[
                    _entry("q", "quant", _StubExpert(opinion=_rec(role="quant"))),
                    _entry("r", "risk", _StubExpert(opinion=_rec(role="risk"))),
                ],
                aggregator="arbitrator",
            )

    def test_arbitrator_forbidden_for_pure_aggregators(self) -> None:
        arbitrator = _StubArbitratorAdvisor(response=_rec(role="arbitrator"))
        with pytest.raises(ValueError, match="cannot accept an arbitrator entry"):
            MoEAdvisorAdapter(
                experts=[_entry("q", "quant", _StubExpert(opinion=_rec(role="quant")))],
                aggregator="voting",
                arbitrator=_entry("arb", "arbitrator", arbitrator),
            )

    def test_arbitrator_name_must_be_unique(self) -> None:
        """Arbitrator's name shares the same namespace as experts —
        no duplicates allowed (audit logs use ``expert_name`` for both)."""
        arbitrator = _StubArbitratorAdvisor(response=_rec(role="arbitrator"))
        with pytest.raises(ValueError, match="duplicates"):
            MoEAdvisorAdapter(
                experts=[_entry("clash", "quant", _StubExpert(opinion=_rec(role="quant")))],
                aggregator="arbitrator",
                arbitrator=_entry("clash", "arbitrator", arbitrator),
            )


@pytest.mark.asyncio
class TestArbitratorDispatch:
    async def test_arbitrator_receives_opinions_context(self) -> None:
        """Verify the MoE wires opinions into the arbitrator's
        extra_context — proves the end-to-end channel works."""
        quant = _StubExpert(opinion=_rec(role="quant", recommendations={"x": 1}))
        risk = _StubExpert(opinion=_rec(role="risk", recommendations={"x": 2}))
        arb = _StubArbitratorAdvisor(
            response=_rec(
                role="single",  # arbitrator self-tags; MoE overrides
                recommendations={"x": 3},
                confidence="high",
                rationale="synthesized",
            )
        )
        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("q", "quant", quant),
                _entry("r", "risk", risk),
            ],
            aggregator="arbitrator",
            arbitrator=_entry("arb", "arbitrator", arb),
        )

        result = await adapter.get_recommendation(_summary())

        assert arb.call_count == 1
        # The two expert opinions reached the arbitrator's prompt.
        assert "quant" in arb.last_extra_context
        assert "risk" in arb.last_extra_context
        # MoE stamps the final role to "aggregated" regardless of the
        # arbitrator's self-tag.
        assert result.role == "aggregated"
        assert result.recommendations == {"x": 3}
        # Per-expert audit trail still populated.
        assert len(result.expert_opinions) == 2

    async def test_arbitrator_only_called_after_experts(self) -> None:
        """If every expert fails, MoE raises before invoking the
        arbitrator — there's nothing meaningful to synthesize."""
        arb = _StubArbitratorAdvisor(response=_rec(role="arbitrator"))
        adapter = MoEAdvisorAdapter(
            experts=[
                _entry("a", "quant", _StubExpert(error=AdvisorError("down"))),
                _entry("b", "risk", _StubExpert(error=AdvisorError("down"))),
            ],
            aggregator="arbitrator",
            arbitrator=_entry("arb", "arbitrator", arb),
        )
        with pytest.raises(AdvisorError, match="All 2 MoE experts failed"):
            await adapter.get_recommendation(_summary())
        assert arb.call_count == 0

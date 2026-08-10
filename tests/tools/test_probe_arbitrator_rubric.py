"""Lock the arbitrator-battery rubric + the deterministic baselines.

Sister to ``test_probe_advisor_scoring.py``. That file pins the quant
battery's anti-degenerate properties; this one pins the *arbitration*
rubric and — more importantly — the measured behaviour of the two free
aggregators on it.

Why pin the baselines rather than just the rubric: the battery exists to
answer "does an LLM arbitrator earn its per-tick cost over mechanical
aggregation?" That answer is only meaningful if the baselines' scores are
stable. If someone changes ``aggregate_voting`` or
``aggregate_weighted_confidence``, these tests fail and force a
deliberate re-baseline rather than silently moving the comparison.

Measured 2026-08-10: voting 5/8, weighted_confidence 1/8,
gpt-5-mini 8/8 (the LLM run is live and not asserted here).
"""

from __future__ import annotations

import pytest

from tools.probe_arbitrator import _fixtures, _grade
from wobblebot.services.aggregators import aggregate_voting, aggregate_weighted_confidence

pytestmark = pytest.mark.unit


def _score(fn) -> tuple[int, dict[str, bool]]:  # type: ignore[no-untyped-def]
    per: dict[str, bool] = {}
    for fx in _fixtures():
        verdict = _grade(fx, fn(fx.opinions))
        per[fx.name] = verdict.ok
    return sum(per.values()), per


class TestRubricShape:
    def test_every_arbitration_rule_has_a_fixture(self) -> None:
        """All five numbered rules in arbitrator.md, plus both hard
        constraints, must be exercised — otherwise the battery grades a
        subset of the contract while looking complete."""
        rules = {fx.rule.split()[0] for fx in _fixtures()}
        assert {"1", "2", "3", "4", "5"} <= rules
        constraint_fixtures = [fx for fx in _fixtures() if fx.rule.startswith("constraint")]
        assert len(constraint_fixtures) >= 2  # fee floor + never-tighten

    def test_expected_values_are_always_expert_proposed(self) -> None:
        """Rule 4 forbids inventing a value no expert proposed. A fixture
        whose *expected answer* were an invented midpoint would grade the
        opposite of the rule."""
        for fx in _fixtures():
            if fx.expect_spacing is None:
                continue
            proposed = {
                op.recommendations.get("spacing_percentage")
                for op in fx.opinions
                if "spacing_percentage" in op.recommendations
            }
            assert (
                fx.expect_spacing in proposed
            ), f"{fx.name}: expected {fx.expect_spacing} was proposed by no expert"

    def test_no_fixture_expects_a_tighten_or_a_sub_floor_value(self) -> None:
        """The constraints are absolute, so no fixture may require
        violating them."""
        from tools.probe_arbitrator import _CURRENT_SPACING, _FEE_FLOOR

        for fx in _fixtures():
            if fx.expect_spacing is not None:
                assert fx.expect_spacing >= _FEE_FLOOR
                assert fx.expect_spacing >= _CURRENT_SPACING


class TestDeterministicBaselines:
    """The free aggregators, pinned. These are the comparison an LLM
    arbitrator has to beat to justify its cost."""

    def test_voting_scores_five_of_eight(self) -> None:
        score, per = _score(aggregate_voting)
        assert score == 5, per

    def test_weighted_confidence_scores_one_of_eight(self) -> None:
        score, per = _score(aggregate_weighted_confidence)
        assert score == 1, per

    def test_voting_cannot_pick_a_conservative_value_on_disagreement(self) -> None:
        """Voting's whole failure mode: no majority → omit the key. It can
        HOLD safely but can never take the conservative ACTION rule 1 and
        rule 4 require."""
        _, per = _score(aggregate_voting)
        assert per["risk_overrides_quant"] is False
        assert per["no_invented_midpoint"] is False
        assert per["concord_widen"] is False

    def test_weighted_confidence_breaches_the_fee_floor(self) -> None:
        """A real safety finding, not a scoring nicety: given experts who
        propose sub-floor spacing, weighted_confidence averages them and
        emits a value below the maker+taker break-even. The auto-apply
        floor is what actually stops this reaching the engine — this is
        defence-in-depth documentation, so if the aggregator is ever
        fixed, this test should be updated deliberately."""
        from tools.probe_arbitrator import _FEE_FLOOR

        fx = next(f for f in _fixtures() if f.name == "never_below_fee_floor")
        result = aggregate_weighted_confidence(fx.opinions)
        spacing = result.recommendations.get("spacing_percentage")
        assert spacing is not None and float(spacing) < _FEE_FLOOR

    def test_weighted_confidence_lets_news_drive_a_number(self) -> None:
        """ADR-007 says news must never be the sole driver of a numeric
        value. weighted_confidence has no concept of role, so it cannot
        enforce that — worth pinning so the limitation stays visible."""
        fx = next(f for f in _fixtures() if f.name == "news_cannot_drive_a_number")
        result = aggregate_weighted_confidence(fx.opinions)
        assert result.recommendations.get("spacing_percentage") == pytest.approx(5.0)


class TestGarbageExpertContainment:
    """The 2026-08-10 live failure, as a fixture: one expert emits
    schema-valid nonsense while two agree."""

    def test_both_deterministic_aggregators_contain_the_garbage(self) -> None:
        fx = next(f for f in _fixtures() if f.name == "garbage_expert_ignored")
        for fn in (aggregate_voting, aggregate_weighted_confidence):
            result = fn(fx.opinions)
            assert result.recommendations.get("spacing_percentage") == pytest.approx(3.5)

"""Lock the news-battery rubric — especially the echo-vs-tighten fix.

Third in the set after ``test_probe_advisor_scoring.py`` (quant) and
``test_probe_arbitrator_rubric.py`` (arbitrator).

**The bug this exists to prevent (2026-08-10).** The first draft graded
any ``spacing <= current`` as TIGHTEN. But a model that restates the
CURRENT spacing is expressing a HOLD, not narrowing the grid — and
"never tighten" is the single most dangerous news-role failure per
``news.md``. The bug scored ``qwen3.6:35b-a3b`` at 5/12 and made it look
like a degenerate constant-widen model; the corrected rubric scores the
same run **10/12**, a genuinely discriminating reasoner. A grader that
calls the right answer the worst failure is worse than no grader.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tools.probe_news import (
    _CURRENT_SPACING,
    _fixtures,
    _grade,
)
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation

pytestmark = pytest.mark.unit


def _rec(**recs: object) -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommendation_id="t",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="news",
        recommendations=dict(recs),
        rationale="test",
        confidence="low",
    )


def _fx(name: str):  # type: ignore[no-untyped-def]
    return next(f for f in _fixtures() if f.name == name)


class TestEchoIsAHoldNotATighten:
    def test_echoing_current_spacing_on_a_hold_fixture_passes(self) -> None:
        v = _grade(_fx("single_price_clickbait"), _rec(spacing_percentage=_CURRENT_SPACING))
        assert v.ok, v.detail
        assert "echo" in v.detail.lower()

    def test_strictly_below_current_is_still_a_tighten(self) -> None:
        v = _grade(_fx("single_price_clickbait"), _rec(spacing_percentage=_CURRENT_SPACING - 1.0))
        assert not v.ok
        assert v.verdict == "TIGHTEN"

    def test_echo_on_a_widen_fixture_is_a_MISS_not_a_pass(self) -> None:
        """Restating current when a hack just happened is failing to act —
        the echo allowance must not leak into the widen fixtures."""
        v = _grade(_fx("major_exchange_hack"), _rec(spacing_percentage=_CURRENT_SPACING))
        assert not v.ok
        assert v.verdict == "MISS"

    def test_omission_is_still_the_cleanest_hold(self) -> None:
        v = _grade(_fx("single_price_clickbait"), _rec())
        assert v.ok and "omitted" in v.detail


class TestRoleBoundary:
    def test_touching_risk_levers_fails_even_with_a_correct_spacing_call(self) -> None:
        """news.md: 'Don't touch order_size_usd or level counts.' A model
        out of its lane has failed regardless of the spacing verdict."""
        v = _grade(_fx("major_exchange_hack"), _rec(spacing_percentage=4.0, order_size_usd=3.0))
        assert not v.ok
        assert v.verdict == "OUT_OF_LANE"


class TestDegenerateStrategyDefence:
    def test_constant_widen_and_constant_hold_both_score_poorly(self) -> None:
        """The whole point of the 7-hold / 5-widen imbalance: neither
        degenerate strategy may look competent."""
        fixtures = _fixtures()
        widen_always = sum(
            _grade(f, _rec(spacing_percentage=_CURRENT_SPACING + 0.6)).ok for f in fixtures
        )
        hold_always = sum(_grade(f, _rec()).ok for f in fixtures)
        assert widen_always == 5, widen_always
        assert hold_always == 7, hold_always
        assert max(widen_always, hold_always) < len(fixtures) * 0.75

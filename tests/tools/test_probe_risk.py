"""Pin the risk battery's design (2026-08-11).

The risk seat had NO battery until now. `risk.md`'s answer space is
narrower than quant's — two conservative levers, WIDER spacing and
SMALLER order_size_usd — so the balanced-classes trick that gives the
quant sets a 33% constant floor cannot work here: two classes floor at
50% no matter how the fixtures are arranged.

The discriminating axis is therefore SEVERITY, not direction. These
tests pin that, plus the constant baselines, so the set cannot drift into
the failure modes its three predecessors did.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "probe_risk.py"
    spec = importlib.util.spec_from_file_location("probe_risk", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_risk"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def _rec(**recs: float) -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommendation_id="test",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="risk",
        recommendations=dict(recs),
        rationale="test",
        confidence="medium",
    )


def _score(strategy) -> Counter:  # type: ignore[no-untyped-def]
    return Counter(mod.grade(fx, _rec(**strategy(fx))).verdict for fx in mod.FIXTURES)


_HOLD = lambda fx: {}  # noqa: E731
_SMALLER = lambda fx: {"order_size_usd": fx.summary.current_grid.order_size_usd * 0.7}  # noqa: E731
_WIDER = lambda fx: {
    "spacing_percentage": fx.summary.current_grid.spacing_percentage * 1.3
}  # noqa: E731
_LARGER = lambda fx: {"order_size_usd": fx.summary.current_grid.order_size_usd * 1.3}  # noqa: E731
_TIGHTER = lambda fx: {
    "spacing_percentage": fx.summary.current_grid.spacing_percentage * 0.7
}  # noqa: E731


class TestSeverityGrading:
    """The severity axis is what makes a two-class battery discriminating."""

    def test_holding_a_severe_fixture_is_unsafe(self) -> None:
        """Failing to de-risk under compounding pressure is the exact
        failure the risk seat exists to prevent — not merely suboptimal."""
        severe = [fx for fx in mod.FIXTURES if fx.severity == "severe"]
        assert severe
        for fx in severe:
            assert mod.grade(fx, _rec()).verdict == "UNSAFE"

    def test_holding_a_moderate_fixture_is_only_suboptimal(self) -> None:
        for fx in (f for f in mod.FIXTURES if f.severity == "moderate"):
            assert mod.grade(fx, _rec()).verdict == "SUBOPTIMAL"

    def test_holding_a_comfortable_fixture_is_correct(self) -> None:
        for fx in (f for f in mod.FIXTURES if f.severity == "comfortable"):
            assert mod.grade(fx, _rec()).verdict == "OK"


class TestForbiddenLevers:
    def test_tighter_spacing_is_unsafe_everywhere(self) -> None:
        """risk.md states this without qualification: tightening is never
        a de-risk lever, on any fixture, at any severity."""
        for fx in mod.FIXTURES:
            assert mod.grade(fx, _rec(**_TIGHTER(fx))).verdict == "UNSAFE"

    def test_tighten_is_reported_even_when_paired_with_a_size_cut(self) -> None:
        """A model must not be able to hide a forbidden tighten behind a
        simultaneous de-risk."""
        fx = mod.FIXTURES[0]
        grid = fx.summary.current_grid
        result = mod.grade(
            fx,
            _rec(
                spacing_percentage=grid.spacing_percentage * 0.7,
                order_size_usd=grid.order_size_usd * 0.7,
            ),
        )
        assert result.verdict == "UNSAFE" and result.direction == "tighten"

    def test_loosening_into_pressure_is_unsafe(self) -> None:
        for fx in (f for f in mod.FIXTURES if f.expect == "de_risk"):
            assert mod.grade(fx, _rec(**_LARGER(fx))).verdict == "UNSAFE"


class TestConstantBaselines:
    """The check that demolished freejudge-v1, applied here BEFORE any
    model runs rather than months later."""

    def test_no_constant_strategy_earns_a_clean_sheet(self) -> None:
        """50% on direction is inherent to a two-lever prompt and is
        disclosed. What no constant may do is score well AND stay safe."""
        for name, strat in [("HOLD", _HOLD), ("SMALLER", _SMALLER), ("WIDER", _WIDER)]:
            c = _score(strat)
            ok_frac = c["OK"] / len(mod.FIXTURES)
            assert ok_frac <= 0.55, f"constant-{name} scores {ok_frac:.0%}"

    def test_constant_hold_accumulates_unsafe_calls(self) -> None:
        """The severity axis doing its job: doing nothing cannot look safe."""
        assert _score(_HOLD)["UNSAFE"] == 5

    def test_constant_derisk_is_safe_but_mediocre(self) -> None:
        """Known weak spot, asserted so it stays visible: always de-risking
        earns zero unsafe calls. risk.md does not call it dangerous, so
        grading it UNSAFE would invent a rule. OK% is the discriminator."""
        c = _score(_SMALLER)
        assert c["UNSAFE"] == 0
        assert c["OK"] == 9

    def test_a_perfect_responder_scores_100(self) -> None:
        """Headroom check — the battery must not ceiling a good model at
        anything less than full marks, and must be achievable at all."""
        perfect = lambda fx: {} if fx.expect == "hold" else _SMALLER(fx)  # noqa: E731
        c = _score(perfect)
        assert c["OK"] == len(mod.FIXTURES)
        assert c["UNSAFE"] == 0


class TestFixtureSetShape:
    def test_split_is_nine_four_five(self) -> None:
        """Chosen so BOTH constants land at ~50%; an even 6/6/6 would hand
        constant-DE-RISK 67%."""
        counts = Counter(fx.severity for fx in mod.FIXTURES)
        assert counts == {"comfortable": 9, "moderate": 4, "severe": 5}

    def test_expectations_are_balanced(self) -> None:
        assert Counter(fx.expect for fx in mod.FIXTURES) == {"hold": 9, "de_risk": 9}

    def test_caps_are_identical_so_only_ratios_vary(self) -> None:
        """The model must react to headroom, not memorise cap values."""
        for fx in mod.FIXTURES:
            s = fx.summary
            assert s.max_total_exposure_usd == 150.0
            assert s.max_per_coin_exposure_usd == 40.0
            assert s.max_daily_spend_usd == 120.0

    def test_every_fixture_carries_the_exposure_fields(self) -> None:
        """Without these the seat is back to confabulating (PR #83)."""
        for fx in mod.FIXTURES:
            s = fx.summary
            assert s.total_exposure_usd is not None
            assert s.coin_exposure_usd is not None
            assert s.daily_spend_usd is not None

    def test_every_label_is_argued(self) -> None:
        for fx in mod.FIXTURES:
            assert len(fx.note) > 80, f"{fx.name}: note too thin to audit"

    def test_no_severe_fixture_trips_the_engine_drawdown_guard(self) -> None:
        """Guard-free equivalent for this battery: the engine's
        defensive_drawdown guard fires at -5% and would resolve the tick
        before the LLM ever saw it."""
        for fx in mod.FIXTURES:
            assert fx.summary.max_drawdown > -0.05, fx.name

"""Lock the no-guard free-judge battery (ADR-022 follow-up).

``tools/probe_freejudge.py`` grades an LLM free judge on the *no-guard*
ambiguous middle — the ticks the heuristic escalates in production. The
battery is only meaningful if two invariants hold:

1. **Every fixture is genuinely guard-free.** Run through the real shipped
   ``HeuristicAdvisorAdapter``, none may fire a guard — otherwise the case
   never reaches the LLM in production and doesn't belong here. This is the
   load-bearing check; a future guard-threshold retune that swallows a
   fixture fails loudly here.
2. **The risk-model rubric is non-vacuous.** The forbidden call scores
   UNSAFE, a sub-fee-floor spacing scores UNSAFE regardless of direction, an
   acceptable direction scores OK, and a defensible-but-not-ideal one scores
   SUBOPTIMAL.

These run with NO network / LLM calls — pure fixture + scoring checks.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation

pytestmark = pytest.mark.unit

_VALID_DIRECTIONS = {"widen", "hold", "tighten"}


def _load_module() -> ModuleType:
    """Load ``tools/probe_freejudge.py`` by path (repo tool-test convention)."""
    path = Path(__file__).resolve().parents[2] / "tools" / "probe_freejudge.py"
    spec = importlib.util.spec_from_file_location("probe_freejudge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_freejudge"] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _rec(spacing: float | None) -> AdvisorRecommendation:
    """A recommendation proposing ``spacing`` (or HOLD when None)."""
    recs: dict[str, float] = {} if spacing is None else {"spacing_percentage": spacing}
    return AdvisorRecommendation(
        recommendation_id="test-rec",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="quant",
        recommendations=recs,
        rationale="test",
        confidence="medium",
    )


def _by_name(name: str):  # type: ignore[no-untyped-def]
    return next(fx for fx in mod.FIXTURES if fx.name == name)


# ---------------------------------------------------------------------------
# Invariant 1 — every fixture is genuinely guard-free
# ---------------------------------------------------------------------------


def test_every_fixture_is_guard_free() -> None:
    offenders = mod.verify_no_guard()
    assert offenders == [], f"fixtures that wrongly fire a guard: {offenders}"


# ---------------------------------------------------------------------------
# Label well-formedness + coverage
# ---------------------------------------------------------------------------


def test_labels_are_well_formed() -> None:
    for fx in mod.FIXTURES:
        assert fx.acceptable, f"{fx.name}: acceptable set is empty"
        assert fx.acceptable <= _VALID_DIRECTIONS, f"{fx.name}: bad acceptable {fx.acceptable}"
        assert fx.forbidden is None or fx.forbidden in _VALID_DIRECTIONS
        # A direction can't be both acceptable and forbidden.
        assert fx.forbidden not in fx.acceptable, f"{fx.name}: {fx.forbidden} both ok and forbidden"
        assert fx.note.strip(), f"{fx.name}: missing regime note"


def test_battery_has_meaningful_coverage() -> None:
    assert len(mod.FIXTURES) >= 12
    accept_sets = [fx.acceptable for fx in mod.FIXTURES]
    # Discriminating cases exist: must-widen, must-hold, and tighten-allowed.
    assert frozenset({"widen"}) in accept_sets, "no must-widen fixture"
    assert frozenset({"hold"}) in accept_sets, "no must-hold fixture"
    assert any("tighten" in a for a in accept_sets), "no fixture where tighten is acceptable"
    # Over-tightening is the bot's cardinal risk, so most fixtures forbid it,
    # but at least one forbids widen and at least one forbids nothing.
    forbids = [fx.forbidden for fx in mod.FIXTURES]
    assert forbids.count("tighten") >= 6, "expected many tighten-forbidden fixtures"
    assert "widen" in forbids, "no widen-forbidden fixture"
    assert None in forbids, "no genuinely-open (no-forbidden) fixture"


# ---------------------------------------------------------------------------
# Invariant 2 — the rubric is non-vacuous
# ---------------------------------------------------------------------------


def test_forbidden_call_scores_unsafe() -> None:
    fx = _by_name("developing_downtrend_mild")  # current 1.3, forbidden=tighten
    verdict, direction, _ = mod.score_fixture(_rec(1.0), fx)  # 1.0 < 1.3 -> tighten
    assert direction == "tighten"
    assert verdict == "UNSAFE"


def test_acceptable_call_scores_ok() -> None:
    fx = _by_name("developing_downtrend_mild")  # acceptable {hold, widen}
    assert mod.score_fixture(_rec(1.6), fx)[0] == "OK"  # widen
    assert mod.score_fixture(_rec(None), fx)[0] == "OK"  # hold (omitted spacing)


def test_below_fee_floor_is_unsafe_even_when_direction_is_acceptable() -> None:
    fx = _by_name("too_wide_calm_starved")  # acceptable {tighten, hold}, forbidden widen
    # 0.4% IS a tighten (an acceptable direction here) but below the fee floor.
    verdict, direction, why = mod.score_fixture(_rec(0.4), fx)
    assert direction == "tighten"
    assert verdict == "UNSAFE"
    assert "fee floor" in why


def test_defensible_but_not_ideal_scores_suboptimal() -> None:
    fx = _by_name("well_matched_ranging")  # acceptable {hold}, forbidden None
    verdict, direction, _ = mod.score_fixture(_rec(1.6), fx)  # widen on a hold-only case
    assert direction == "widen"
    assert verdict == "SUBOPTIMAL"


def test_classify_direction_boundaries() -> None:
    assert mod.classify_direction(2.0, 1.0) == "widen"
    assert mod.classify_direction(0.5, 1.0) == "tighten"
    assert mod.classify_direction(1.02, 1.0) == "hold"  # within ±5% deadband
    assert mod.classify_direction(None, 1.0) == "hold"  # omitted -> hold
    assert mod.classify_direction(1.5, None) == "hold"  # no current -> hold


# ---------------------------------------------------------------------------
# HARD set (2026-08-11) — the anti-saturation invariants
#
# v1 saturated: a model that always answers HOLD scores 12/14 OK (86%) with
# ZERO UNSAFE, beating the champion's 83% and nine of eleven models in the
# 2026-08-11 sweep. `hold` was acceptable in 12 of 14 fixtures. These tests
# pin the property that makes `hard` different, so it cannot silently drift
# back into rewarding a do-nothing model.
# ---------------------------------------------------------------------------


def _constant_score(fixtures, spacing_fn) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Score a degenerate always-the-same-answer strategy."""
    counts = {"OK": 0, "SUBOPTIMAL": 0, "UNSAFE": 0, "ERROR": 0}
    for fx in fixtures:
        current = fx.summary.current_grid.spacing_percentage
        verdict, _, _ = mod.score_fixture(
            _rec(None if spacing_fn is None else spacing_fn(current)), fx
        )
        counts[verdict] += 1
    return counts


_CONSTANTS = {
    "hold": None,
    "widen": lambda c: c * 1.3,
    "tighten": lambda c: c * 0.7,
}
_CONSTANT_CEILING = 0.40  # no degenerate strategy may clear 40% OK on `hard`


class TestHardSetIsBalanced:
    def test_every_hard_fixture_is_guard_free(self) -> None:
        """Same load-bearing check as v1: a fixture whose guard fires is not
        a case the LLM ever sees in production."""
        offenders = mod.verify_no_guard(mod.HARD_FIXTURES)
        assert offenders == [], f"hard fixtures that wrongly fire a guard: {offenders}"

    def test_no_constant_strategy_beats_the_ceiling(self) -> None:
        """THE point of this set. On v1, constant-HOLD scores 86% — better
        than the real champion. A battery a rock can pass measures nothing."""
        for name, fn in _CONSTANTS.items():
            counts = _constant_score(mod.HARD_FIXTURES, fn)
            ok_frac = counts["OK"] / len(mod.HARD_FIXTURES)
            assert ok_frac <= _CONSTANT_CEILING, (
                f"constant-{name.upper()} scores {ok_frac:.0%} on the hard set "
                f"(ceiling {_CONSTANT_CEILING:.0%}) — the set has drifted back "
                f"toward rewarding a degenerate strategy"
            )

    def test_v1_constant_hold_advantage_is_documented_not_fixed(self) -> None:
        """v1 is deliberately LEFT saturated so historical scores stay
        comparable. This asserts the known value rather than 'fixing' v1,
        which would invalidate every recorded bake-off."""
        counts = _constant_score(mod.FIXTURES, _CONSTANTS["hold"])
        assert counts["OK"] == 12 and counts["UNSAFE"] == 0

    def test_each_direction_is_the_sole_answer_equally_often(self) -> None:
        """Balance is what caps the constants. Any direction over-represented
        as the sole answer hands a constant an edge."""
        sole = {"widen": 0, "hold": 0, "tighten": 0}
        for fx in mod.HARD_FIXTURES:
            assert len(fx.acceptable) == 1, (
                f"{fx.name} accepts {sorted(fx.acceptable)} — a 2-of-3 set lets a "
                f"coin flip pass, which is how v1 saturated"
            )
            sole[next(iter(fx.acceptable))] += 1
        assert max(sole.values()) - min(sole.values()) <= 1, f"unbalanced: {sole}"

    def test_hard_set_is_registered_and_v1_is_the_default(self) -> None:
        assert mod.FIXTURE_SETS["hard"] is mod.HARD_FIXTURES
        assert mod.FIXTURE_SETS["v1"] is mod.FIXTURES

    def test_every_hard_label_is_argued(self) -> None:
        """A fixture label is a claim; the note is its justification. Labels
        must be defensible BEFORE a model runs, never adjusted after."""
        for fx in mod.HARD_FIXTURES:
            assert len(fx.note) > 60, f"{fx.name}: note too thin to audit"
            assert fx.forbidden not in fx.acceptable


# ---------------------------------------------------------------------------
# Calibration axis (2026-08-11) — scored as ASSOCIATION, not per-fixture
# correctness.
#
# quant.md: "if the metrics are thin or ambiguous, say so with confidence:
# low." Scoring that per-fixture is degenerate — a model answering `low` to
# everything passes every thin fixture, the constant-HOLD problem in a new
# axis. The prompt's demand only means something if confidence VARIES: the
# point of "say so" is to DISTINGUISH. So the axis scores whether
# confidence TRACKS evidence quality, which is non-degenerate by
# construction and needs no rule the prompt does not state.
# ---------------------------------------------------------------------------


def _rows(pairs):  # type: ignore[no-untyped-def]
    """(evidence, confidence) pairs as scorer input rows."""
    return [{"evidence": e, "confidence": c} for e, c in pairs]


class TestCalibrationIsNonDegenerate:
    def test_constant_confidence_scores_undefined_not_high(self) -> None:
        """THE property that made correlation the right choice. A model
        answering the same confidence everywhere carries no signal, and
        must not be able to score well by parroting `low` at thin
        fixtures."""
        for level in ("low", "medium", "high"):
            rows = _rows([(1, level), (3, level), (5, level), (4, level)])
            assert mod.calibration_tau(rows) is None, f"constant-{level} scored a number"

    def test_perfect_tracking_scores_positive(self) -> None:
        rows = _rows([(1, "low"), (2, "low"), (4, "high"), (5, "high")])
        tau = mod.calibration_tau(rows)
        assert tau is not None and tau > 0.8

    def test_inverted_confidence_scores_negative(self) -> None:
        """Confident on thin evidence, hedging on strong — the actively
        wrong shape, which must score BELOW zero rather than merely low."""
        rows = _rows([(1, "high"), (2, "high"), (4, "low"), (5, "low")])
        tau = mod.calibration_tau(rows)
        assert tau is not None and tau < -0.8

    def test_no_evidence_spread_is_undefined(self) -> None:
        """A fixture set where every scenario has identical evidence
        cannot measure calibration — distinct from a model with no
        signal, and reported differently."""
        rows = _rows([(3, "low"), (3, "medium"), (3, "high")])
        assert mod.calibration_tau(rows) is None


class TestEvidenceQualityIsMechanical:
    def test_derived_from_snapshots_and_cycles_only(self) -> None:
        """Evidence must never be a per-fixture judgement call — that is
        where an author's own inference re-enters, which is how two hard
        fixtures ended up encoding rules quant.md never states."""
        for fx in mod.HARD_FIXTURES + mod.FIXTURES:
            expected = (
                1
                + (
                    2
                    if fx.summary.snapshot_count >= 300
                    else (1 if fx.summary.snapshot_count >= 100 else 0)
                )
                + (2 if fx.summary.cycle_count >= 6 else (1 if fx.summary.cycle_count >= 2 else 0))
            )
            assert mod.evidence_quality(fx) == expected

    def test_both_sets_have_enough_spread_to_score(self) -> None:
        """Without spread the axis is undefined — a silent n/a would look
        like a passing run."""
        for name, fixtures in mod.FIXTURE_SETS.items():
            levels = {mod.evidence_quality(fx) for fx in fixtures}
            assert len(levels) >= 3, f"{name} has only {levels} evidence levels"


class TestKendallTauB:
    def test_handles_ties_without_dividing_by_zero(self) -> None:
        assert mod.kendall_tau_b([1, 1, 1], [1, 2, 3]) is None
        assert mod.kendall_tau_b([1, 2, 3], [2, 2, 2]) is None

    def test_too_few_points_is_undefined(self) -> None:
        assert mod.kendall_tau_b([1], [2]) is None


# ---------------------------------------------------------------------------
# GEN3 (2026-08-11) — built because `hard` ceilinged at 119/120.
#
# Pins the three design answers so the set cannot drift back into the
# failure modes of its two predecessors: v1 rewarded a do-nothing model
# (constant-HOLD 86%), and `hard` compressed the calibration axis to
# three evidence levels with 8 of 15 at the top.
# ---------------------------------------------------------------------------


class TestGen3Design:
    def test_every_fixture_is_guard_free(self) -> None:
        offenders = mod.verify_no_guard(mod.GEN3_FIXTURES)
        assert offenders == [], f"gen3 fixtures firing a guard: {offenders}"

    def test_no_constant_strategy_beats_the_ceiling(self) -> None:
        for name, fn in _CONSTANTS.items():
            counts = _constant_score(mod.GEN3_FIXTURES, fn)
            ok_frac = counts["OK"] / len(mod.GEN3_FIXTURES)
            assert ok_frac <= _CONSTANT_CEILING, (
                f"constant-{name.upper()} scores {ok_frac:.0%} on gen3 "
                f"(ceiling {_CONSTANT_CEILING:.0%})"
            )

    def test_directions_are_exactly_balanced(self) -> None:
        sole: dict[str, int] = {"widen": 0, "hold": 0, "tighten": 0}
        for fx in mod.GEN3_FIXTURES:
            assert len(fx.acceptable) == 1, f"{fx.name} accepts {sorted(fx.acceptable)}"
            sole[next(iter(fx.acceptable))] += 1
        assert len(set(sole.values())) == 1, f"unbalanced: {sole}"

    def test_evidence_spans_every_level(self) -> None:
        """The reason gen3 exists for the calibration axis: `hard` spans
        only 3 levels with 8 of 15 at the top, which compresses tau_b."""
        levels = {mod.evidence_quality(fx) for fx in mod.GEN3_FIXTURES}
        assert levels == {1, 2, 3, 4, 5}, f"gen3 evidence levels: {sorted(levels)}"

    def test_calibration_axis_has_headroom(self) -> None:
        """A perfect tracker must score near +1 and any constant must be
        undefined — otherwise the axis cannot rank models on this set."""
        ladder = ["low", "low", "medium", "medium", "high"]
        perfect = [
            {
                "evidence": mod.evidence_quality(fx),
                "confidence": ladder[mod.evidence_quality(fx) - 1],
            }
            for fx in mod.GEN3_FIXTURES
        ]
        assert (mod.calibration_tau(perfect) or 0) > 0.9
        constant = [
            {"evidence": mod.evidence_quality(fx), "confidence": "high"} for fx in mod.GEN3_FIXTURES
        ]
        assert mod.calibration_tau(constant) is None

    def test_every_label_is_argued(self) -> None:
        for fx in mod.GEN3_FIXTURES:
            assert len(fx.note) > 80, f"{fx.name}: note too thin to audit"
            assert fx.forbidden not in fx.acceptable

    def test_registered_and_v1_still_default(self) -> None:
        assert mod.FIXTURE_SETS["gen3"] is mod.GEN3_FIXTURES
        assert len(mod.GEN3_FIXTURES) == 21

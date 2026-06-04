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

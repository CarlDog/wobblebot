"""Tests for the deterministic ``HeuristicAdvisorAdapter`` (Stage 8.5).

The headline tests run the adapter — loaded from the SHIPPED
``config/heuristic/quant.yml`` — through the same fixture batteries and
scoring rubric that ``tools/probe_advisor.py`` uses to grade LLM
candidates. The heuristic codifies the maintainer's grid judgment, so it
should score a perfect 36/36 on the core battery and resolve all 8
held-out cases (the 4 conflict discriminators that every local LLM
failed) in the right direction. If an operator edits the curve in the
YAML and breaks a fixture, these fail loudly.

The remaining tests pin behaviour the battery doesn't exercise:
curve interpolation, the fee-floor clamp, per-guard on/off toggles, and
the cascade ``clear_match`` / escalation signal.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.config.heuristic import (
    CurvePoint,
    HeuristicGuards,
    HeuristicSpec,
    load_heuristic_spec,
)
from wobblebot.ports.advisor import CurrentGridParams, PerformanceSummary

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_SPEC = _REPO_ROOT / "config" / "heuristic" / "quant.yml"


def _load_probe_advisor() -> ModuleType:
    """Load ``tools/probe_advisor.py`` by path (the repo convention for
    testing one-shot operator scripts; see test_probe_advisor_scoring)."""
    script_path = _REPO_ROOT / "tools" / "probe_advisor.py"
    spec = importlib.util.spec_from_file_location("probe_advisor", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_advisor"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe_advisor()


@pytest.fixture
def adapter() -> HeuristicAdvisorAdapter:
    """Adapter backed by the committed production spec."""
    return HeuristicAdvisorAdapter(spec=load_heuristic_spec(_SHIPPED_SPEC))


def _summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    current_spacing: float | None,
    volatility: float,
    max_drawdown: float = -0.01,
    win_rate: float = 0.5,
    cycle_count: int = 4,
    snapshot_count: int = 720,
    flatness: float = 0.5,
) -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=79000.0,
        snapshot_count=snapshot_count,
        volatility=volatility,
        max_drawdown=max_drawdown,
        flatness=flatness,
        cycle_count=cycle_count,
        win_rate=win_rate,
        active_orders=6,
        current_grid=CurrentGridParams(
            spacing_percentage=current_spacing,
            levels_above=4,
            levels_below=4,
            order_size_usd=10.0,
        ),
    )


# ---------------------------------------------------------------------------
# Battery reproduction — the SHIPPED spec must reproduce the validated judgment
# ---------------------------------------------------------------------------


def _score_battery(adapter: HeuristicAdvisorAdapter, fixtures: tuple) -> tuple[int, list[str]]:
    """Score the heuristic across a probe fixture battery; collect mismatches."""
    total = 0
    mismatches: list[str] = []
    for fx in fixtures:
        rec = adapter.evaluate(fx.summary).recommendation
        actual = probe._classify_direction(rec, fx.summary.current_grid)
        magnitude_ok = probe._magnitude_ok(rec, fx.ideal_spacing)
        score, verdict = probe._score_row(fx.expected, actual, magnitude_ok)
        total += score
        if actual != fx.expected:
            mismatches.append(f"{fx.name}: expected {fx.expected}, got {actual} ({verdict})")
    return total, mismatches


def test_core_battery_scores_perfect(adapter: HeuristicAdvisorAdapter) -> None:
    total, mismatches = _score_battery(adapter, probe.FIXTURES)
    assert not mismatches, f"core direction mismatches: {mismatches}"
    assert total == probe._MAX_PER_FIXTURE * len(probe.FIXTURES)  # 36/36


def test_heldout_battery_all_directions_correct(adapter: HeuristicAdvisorAdapter) -> None:
    # The 4 discriminators are conflict cases every local LLM failed; the
    # heuristic must resolve all 8 via its guards in the right direction.
    total, mismatches = _score_battery(adapter, probe.HELDOUT_FIXTURES)
    assert not mismatches, f"held-out direction mismatches: {mismatches}"
    assert total == probe._MAX_PER_FIXTURE * len(probe.HELDOUT_FIXTURES)  # 24/24


def test_each_guard_path_is_exercised_by_the_heldout_battery(
    adapter: HeuristicAdvisorAdapter,
) -> None:
    # The 4 discriminators each exercise a distinct guard; assert the
    # reason tokens so a refactor can't silently route them elsewhere.
    by_name = {fx.name: adapter.evaluate(fx.summary) for fx in probe.HELDOUT_FIXTURES}
    assert by_name["heldout_directional_downtrend"].reason == "directional_runaway"
    assert by_name["heldout_drawdown_overrides_calm"].reason == "defensive_drawdown"
    assert by_name["heldout_working_well"].reason == "dont_fix_working"
    assert by_name["heldout_tight_but_scalping"].reason == "dont_fix_working"
    assert by_name["heldout_fee_floor"].reason == "fee_floor_calm"


def test_all_battery_verdicts_are_clear_matches(adapter: HeuristicAdvisorAdapter) -> None:
    # Every validated fixture is inside the heuristic's competence zone,
    # so a cascade resolves all 20 without ever calling the LLM.
    for fx in (*probe.FIXTURES, *probe.HELDOUT_FIXTURES):
        verdict = adapter.evaluate(fx.summary)
        assert verdict.clear_match, f"{fx.name} unexpectedly flagged for escalation"


# ---------------------------------------------------------------------------
# Curve interpolation + fee-floor clamp
# ---------------------------------------------------------------------------


def test_curve_interpolates_between_points(adapter: HeuristicAdvisorAdapter) -> None:
    # vol 0.005 sits halfway between (0.004, 1.25) and (0.006, 1.60) -> 1.425
    assert adapter._ideal(0.005) == pytest.approx(1.425)


def test_curve_flat_clamps_outside_the_range(adapter: HeuristicAdvisorAdapter) -> None:
    assert adapter._ideal(0.00001) == pytest.approx(0.65)  # below first point
    assert adapter._ideal(0.5) == pytest.approx(2.70)  # above last point


def test_fee_floor_clamps_the_target() -> None:
    # A custom curve whose low end dips under the floor must be clamped up.
    spec = HeuristicSpec(
        curve=[CurvePoint(vol=0.0, spacing=0.10), CurvePoint(vol=0.02, spacing=3.0)],
        fee_floor=0.52,
    )
    adapter = HeuristicAdvisorAdapter(spec=spec)
    assert adapter._ideal(0.0) == pytest.approx(0.52)  # 0.10 floored to 0.52


# ---------------------------------------------------------------------------
# Guard on/off toggles (the operator-configurable behaviour)
# ---------------------------------------------------------------------------


def test_defensive_drawdown_toggle_flips_widen_to_hold() -> None:
    # Calm market, sharp drawdown, still cycling: guard ON -> WIDEN.
    summary = _summary(
        current_spacing=0.90, volatility=0.0015, max_drawdown=-0.085, cycle_count=2, win_rate=0.45
    )
    on = HeuristicAdvisorAdapter(spec=load_heuristic_spec(_SHIPPED_SPEC))
    assert on.evaluate(summary).direction == "widen"

    spec_off = load_heuristic_spec(_SHIPPED_SPEC)
    spec_off = spec_off.model_copy(
        update={
            "guards": spec_off.guards.model_copy(
                update={
                    "defensive_drawdown": spec_off.guards.defensive_drawdown.model_copy(
                        update={"enabled": False}
                    )
                }
            )
        }
    )
    off = HeuristicAdvisorAdapter(spec=spec_off)
    # Without the guard, the calm-market first-order call holds (gap < deadband).
    assert off.evaluate(summary).direction == "hold"


def test_dont_fix_working_toggle_flips_hold_to_widen() -> None:
    # Tight-for-vol but printing fills: guard ON -> HOLD; OFF -> WIDEN.
    summary = _summary(
        current_spacing=1.00, volatility=0.007, max_drawdown=-0.004, cycle_count=14, win_rate=0.92
    )
    on = HeuristicAdvisorAdapter(spec=load_heuristic_spec(_SHIPPED_SPEC))
    assert on.evaluate(summary).direction == "hold"

    spec = load_heuristic_spec(_SHIPPED_SPEC)
    spec = spec.model_copy(
        update={
            "guards": spec.guards.model_copy(
                update={
                    "dont_fix_working": spec.guards.dont_fix_working.model_copy(
                        update={"enabled": False}
                    )
                }
            )
        }
    )
    off = HeuristicAdvisorAdapter(spec=spec)
    assert off.evaluate(summary).direction == "widen"


# ---------------------------------------------------------------------------
# clear_match / escalation signal (the cascade reads this)
# ---------------------------------------------------------------------------


def test_no_current_grid_holds_and_is_not_clear(adapter: HeuristicAdvisorAdapter) -> None:
    verdict = adapter.evaluate(_summary(current_spacing=None, volatility=0.004))
    assert verdict.direction == "hold"
    assert verdict.clear_match is False
    assert verdict.reason == "no_current_grid"
    assert verdict.recommendation.confidence == "low"


def test_gap_in_ambiguous_band_is_not_clear(adapter: HeuristicAdvisorAdapter) -> None:
    # ideal(0.004)=1.25; pick current so the gap ~12% lands in the
    # ambiguous band [0.075, 0.225] with no guard firing.
    summary = _summary(
        current_spacing=1.42, volatility=0.004, max_drawdown=-0.01, cycle_count=4, win_rate=0.5
    )
    verdict = adapter.evaluate(summary)
    assert verdict.direction == "hold"  # |gap| ~0.12 < 0.15 deadband
    assert verdict.clear_match is False  # ...but inside the ambiguous band
    assert verdict.recommendation.confidence == "low"


def test_thin_metrics_window_is_not_clear(adapter: HeuristicAdvisorAdapter) -> None:
    # A clearly-matched hold, but too few snapshots to trust the vol estimate.
    summary = _summary(
        current_spacing=1.25, volatility=0.004, cycle_count=4, win_rate=0.5, snapshot_count=5
    )
    verdict = adapter.evaluate(summary)
    assert verdict.direction == "hold"
    assert verdict.clear_match is False


def test_clear_action_emits_target_spacing(adapter: HeuristicAdvisorAdapter) -> None:
    # Tight grid, active market -> a confident widen to the vol-ideal.
    summary = _summary(current_spacing=0.60, volatility=0.008, cycle_count=4, win_rate=0.4)
    verdict = adapter.evaluate(summary)
    assert verdict.direction == "widen"
    assert verdict.clear_match is True
    assert verdict.recommendation.recommendations == {"spacing_percentage": 1.90}


@pytest.mark.asyncio
async def test_get_recommendation_returns_the_verdict_recommendation(
    adapter: HeuristicAdvisorAdapter,
) -> None:
    summary = _summary(current_spacing=0.60, volatility=0.008, cycle_count=4, win_rate=0.4)
    rec = await adapter.get_recommendation(summary)
    assert rec.recommendations == {"spacing_percentage": 1.90}
    assert rec.role == "heuristic"
    assert await adapter.validate_recommendation(rec) is True

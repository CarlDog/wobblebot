"""Tests for the deterministic ``HeuristicAdvisorAdapter`` (ADR-022).

The heuristic is a guard layer: it makes only the four clear override
calls and escalates every other tick to the LLM (``clear_match=False``,
reason ``no_guard_fired``). These tests pin that the guards still fire on
their held-out discriminators, that the former first-order cases now
escalate, that the ``_ideal`` curve still floors the drawdown guard's
widen target, and the per-guard on/off toggles.
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
# Guards still fire; former first-order cases now escalate (ADR-022)
# ---------------------------------------------------------------------------


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


def test_guard_cases_are_clear_matches(adapter: HeuristicAdvisorAdapter) -> None:
    # The 5 held-out guard cases resolve locally (clear_match=True) — the
    # cascade answers them for $0, no LLM call.
    guard_cases = (
        "heldout_directional_downtrend",
        "heldout_drawdown_overrides_calm",
        "heldout_working_well",
        "heldout_tight_but_scalping",
        "heldout_fee_floor",
    )
    by_name = {fx.name: adapter.evaluate(fx.summary) for fx in probe.HELDOUT_FIXTURES}
    for name in guard_cases:
        assert by_name[name].clear_match is True, f"{name} should resolve via a guard"


def test_former_first_order_cases_now_escalate(adapter: HeuristicAdvisorAdapter) -> None:
    # The 3 held-out cases the retired first-order logic used to resolve
    # (a clear widen + two matched grids) now have no guard, so they
    # escalate to the LLM (clear_match=False, reason no_guard_fired).
    escalators = ("heldout_clear_widen", "heldout_matched", "heldout_matched_whipsaw")
    by_name = {fx.name: adapter.evaluate(fx.summary) for fx in probe.HELDOUT_FIXTURES}
    for name in escalators:
        verdict = by_name[name]
        assert verdict.clear_match is False, f"{name} should escalate post-ADR-022"
        assert verdict.reason == "no_guard_fired"
        assert verdict.direction == "hold"  # escalation default is a HOLD


def test_no_guard_fired_returns_low_confidence_hold(adapter: HeuristicAdvisorAdapter) -> None:
    # A vanilla tick with no guard condition: hold, non-clear, low confidence.
    verdict = adapter.evaluate(
        _summary(current_spacing=1.20, volatility=0.005, max_drawdown=-0.01, win_rate=0.5)
    )
    assert verdict.direction == "hold"
    assert verdict.clear_match is False
    assert verdict.reason == "no_guard_fired"
    assert verdict.recommendation.confidence == "low"
    assert verdict.recommendation.recommendations == {}


# ---------------------------------------------------------------------------
# _ideal curve helper (now used only by the defensive_drawdown guard floor)
# ---------------------------------------------------------------------------


def test_ideal_interpolates_between_points(adapter: HeuristicAdvisorAdapter) -> None:
    # vol 0.005 sits halfway between (0.004, 1.25) and (0.006, 1.60) -> 1.425
    assert adapter._ideal(0.005) == pytest.approx(1.425)


def test_ideal_flat_clamps_outside_the_range(adapter: HeuristicAdvisorAdapter) -> None:
    assert adapter._ideal(0.00001) == pytest.approx(0.65)  # below first point
    assert adapter._ideal(0.5) == pytest.approx(2.70)  # above last point


def test_ideal_floors_the_drawdown_guard_widen_target(adapter: HeuristicAdvisorAdapter) -> None:
    # defensive_drawdown widens to max(current * widen_factor, _ideal(vol)).
    # In a high-vol drawdown the _ideal floor exceeds current*1.5 and sets
    # the target — proving the curve still feeds the guard.
    summary = _summary(
        current_spacing=1.00, volatility=0.014, max_drawdown=-0.085, cycle_count=3, win_rate=0.4
    )
    verdict = adapter.evaluate(summary)
    assert verdict.reason == "defensive_drawdown"
    assert verdict.direction == "widen"
    # current*1.5 = 1.50, _ideal(0.014) = 2.70 -> floor wins -> 2.70
    assert verdict.recommendation.recommendations == {"spacing_percentage": 2.70}


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
    # Without the guard, no guard fires -> escalate-HOLD (no first-order curve).
    off_verdict = off.evaluate(summary)
    assert off_verdict.direction == "hold"
    assert off_verdict.clear_match is False
    assert off_verdict.reason == "no_guard_fired"


def test_dont_fix_working_toggle_flips_clear_match() -> None:
    # Tight-for-vol but printing fills: guard ON -> a clear HOLD resolved
    # locally; OFF -> no guard fires, so the tick escalates to the LLM.
    summary = _summary(
        current_spacing=1.00, volatility=0.007, max_drawdown=-0.004, cycle_count=14, win_rate=0.92
    )
    on = HeuristicAdvisorAdapter(spec=load_heuristic_spec(_SHIPPED_SPEC))
    on_verdict = on.evaluate(summary)
    assert on_verdict.direction == "hold"
    assert on_verdict.clear_match is True
    assert on_verdict.reason == "dont_fix_working"

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
    off_verdict = off.evaluate(summary)
    assert off_verdict.clear_match is False
    assert off_verdict.reason == "no_guard_fired"


# ---------------------------------------------------------------------------
# clear_match / escalation signal (the cascade reads this)
# ---------------------------------------------------------------------------


def test_no_current_grid_holds_and_is_not_clear(adapter: HeuristicAdvisorAdapter) -> None:
    verdict = adapter.evaluate(_summary(current_spacing=None, volatility=0.004))
    assert verdict.direction == "hold"
    assert verdict.clear_match is False
    assert verdict.reason == "no_current_grid"
    assert verdict.recommendation.confidence == "low"


@pytest.mark.asyncio
async def test_get_recommendation_wraps_a_guard_verdict(
    adapter: HeuristicAdvisorAdapter,
) -> None:
    # The async port wrapper returns the guard's recommendation verbatim.
    # A sharp drawdown fires defensive_drawdown -> a widen with a target.
    summary = _summary(
        current_spacing=1.00, volatility=0.014, max_drawdown=-0.085, cycle_count=3, win_rate=0.4
    )
    rec = await adapter.get_recommendation(summary)
    assert rec.recommendations == {"spacing_percentage": 2.70}
    assert rec.role == "heuristic"
    assert await adapter.validate_recommendation(rec) is True

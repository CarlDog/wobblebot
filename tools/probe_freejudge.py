"""Grade an LLM free judge on the NO-GUARD battery (ADR-022 follow-up).

Post-ADR-022 the advisor is *guards + LLM free judge*: the deterministic
heuristic makes only the four clear guard calls and escalates every other
tick to the LLM. The model bake-off (`docs/reference/advisor-llm-models.md`,
2026-06-04) picked `gpt-5-mini` — but it scored the candidates on the
`heldout` battery, of which only 3 fixtures actually escalate. This battery
is the gold-standard follow-up that ADR-022 promised: a purpose-built set of
**no-guard** scenarios — the ambiguous middle the free judge actually owns
in production.

**The oracle problem.** A no-guard tick has no mechanical "right answer"
(that's *why* it reaches the LLM and gets tracked against real outcomes over
the soak). So this battery does NOT key fixtures to a curve. Instead each
fixture is scored against the bot's **risk model**:

- ``acceptable`` — the set of directions a sound free judge could defend
  given the regime (e.g. ``{hold, widen}``).
- ``forbidden`` — the actively *unsafe* call for that regime, if any. For a
  grid whose dominant failure is over-tightening, this is almost always
  ``tighten`` (into a developing trend, toward/below the fee floor, or over a
  grid that's working).

Verdicts: **OK** (acceptable), **SUBOPTIMAL** (defensible but not ideal —
e.g. an over-trade on a hold-only fixture), **UNSAFE** (the forbidden call,
or a spacing below the ~0.52% fee floor — always unsafe regardless of label).

**Self-check (load-bearing).** Every fixture, run through the *real*
``HeuristicAdvisorAdapter`` loaded from the shipped ``quant.yml``, MUST
report ``clear_match=False`` (no guard fires). If a guard fires, the fixture
isn't a no-guard case and doesn't belong here — `main()` refuses to run and
``tests/tools/test_freejudge_battery.py`` fails loudly. This keeps the
battery honest if a guard threshold is ever retuned.

Run: ``python tools/probe_freejudge.py --provider openai --model gpt-5-mini
--max-tokens 4000``. Spends real money via the provider API under the
ADR-014 cost gate (isolated ``data/probe_llm_cost.db``). ``--json`` appends a
machine-readable ``JSON_RESULT:`` line.
"""

# pylint: disable=too-many-lines
# One battery: fixture data + its scoring rubric + the runner. The file is
# ~60% fixture literals, and they must stay beside the rubric that grades
# them -- a label is only auditable next to the scoring rule it feeds.
# NOT split into a sibling module because tools/ is not a package and both
# this file and probe_advisor.py are path-loaded (spec_from_file_location)
# by tests and by each other, so a sibling import would need sys.path
# manipulation to resolve. Revisit if tools/ ever becomes a package.

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import load_operator_env
from wobblebot.config.heuristic import load_heuristic_spec
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    CurrentGridParams,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHIPPED_SPEC = _REPO_ROOT / "config" / "heuristic" / "quant.yml"
_FEE_FLOOR = 0.52  # 2x the 0.26% maker fee — a spacing below this can't clear fees
_HOLD_DEADBAND = 0.05  # |Δspacing|/current below this reads as "no meaningful change"

Direction = Literal["widen", "hold", "tighten"]
Verdict = Literal["OK", "SUBOPTIMAL", "UNSAFE", "ERROR"]


@dataclass(frozen=True)
class NoGuardFixture:
    """One no-guard scenario + its risk-model labels.

    ``acceptable`` is the set of directions a sound free judge could defend;
    ``forbidden`` is the actively-unsafe call (or None). ``note`` records the
    regime read so the label is auditable.
    """

    name: str
    summary: PerformanceSummary
    acceptable: frozenset[Direction]
    forbidden: Direction | None
    note: str


def _summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    current_spacing: float,
    volatility: float,
    max_drawdown: float,
    win_rate: float,
    cycle_count: int,
    flatness: float,
    total_pnl: float = 0.0,
    latest_price: float = 79000.0,
    snapshot_count: int = 720,
    active_orders: int = 6,
) -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=latest_price,
        snapshot_count=snapshot_count,
        volatility=volatility,
        max_drawdown=max_drawdown,
        flatness=flatness,
        cycle_count=cycle_count,
        win_rate=win_rate,
        total_pnl=total_pnl,
        active_orders=active_orders,
        current_grid=CurrentGridParams(
            spacing_percentage=current_spacing,
            levels_above=4,
            levels_below=4,
            order_size_usd=10.0,
        ),
    )


# 14 no-guard fixtures spanning the ambiguous middle. Each is verified
# guard-free by `verify_no_guard` (and the test). Labels follow the bot's
# risk model: over-tightening (into a trend, toward the fee floor, or over a
# working grid) is the cardinal sin, so `forbidden` is usually `tighten`.
FIXTURES: tuple[NoGuardFixture, ...] = (
    NoGuardFixture(
        "well_matched_ranging",
        _summary(
            current_spacing=1.2,
            volatility=0.004,
            max_drawdown=-0.008,
            win_rate=0.62,
            cycle_count=6,
            flatness=0.78,
        ),
        acceptable=frozenset({"hold"}),
        forbidden=None,
        note="Spacing proportionate to vol, healthy fills, shallow dd, ranging — leave it.",
    ),
    NoGuardFixture(
        "too_tight_churning_active",
        _summary(
            current_spacing=0.6,
            volatility=0.009,
            max_drawdown=-0.015,
            win_rate=0.30,
            cycle_count=9,
            flatness=0.40,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note="Active market, lots of fills but low win — fees eat a too-tight grid; widen.",
    ),
    NoGuardFixture(
        "too_wide_calm_starved",
        _summary(
            current_spacing=2.2,
            volatility=0.0025,
            max_drawdown=-0.005,
            win_rate=0.70,
            cycle_count=1,
            flatness=0.85,
        ),
        acceptable=frozenset({"tighten", "hold"}),
        forbidden="widen",
        note="Wide grid in a calm range barely filling — tighten toward fills; widening is wrong.",
    ),
    NoGuardFixture(
        "developing_downtrend_mild",
        _summary(
            current_spacing=1.3,
            volatility=0.005,
            max_drawdown=-0.035,
            win_rate=0.35,
            cycle_count=2,
            flatness=0.35,
            total_pnl=-22.0,
            latest_price=74000.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="Price drifting down (dd -3.5%, trending), few cycles — never tighten into a trend.",
    ),
    NoGuardFixture(
        "developing_uptrend_mild",
        _summary(
            current_spacing=1.3,
            volatility=0.005,
            max_drawdown=-0.010,
            win_rate=0.55,
            cycle_count=4,
            flatness=0.40,
            total_pnl=18.0,
            latest_price=83000.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden=None,
        note=(
            "Mild uptrend, grid catching some — hold/widen; tightening to chase "
            "over-trades (SUBOPTIMAL, not dangerous — unlike tightening into a downtrend)."
        ),
    ),
    NoGuardFixture(
        "whipsaw_midspacing",
        _summary(
            current_spacing=1.8,
            volatility=0.012,
            max_drawdown=-0.020,
            win_rate=0.60,
            cycle_count=6,
            flatness=0.30,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="High-vol chop at mid spacing, decent fills — tightening into whipsaw raises churn.",
    ),
    NoGuardFixture(
        "near_floor_active_not_calm",
        _summary(
            current_spacing=0.65,
            volatility=0.006,
            max_drawdown=-0.010,
            win_rate=0.45,
            cycle_count=7,
            flatness=0.50,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="Near the fee floor but NOT calm (so no guard) — can't tighten below the floor.",
    ),
    NoGuardFixture(
        "slightly_tight_but_healthy",
        _summary(
            current_spacing=0.9,
            volatility=0.006,
            max_drawdown=-0.012,
            win_rate=0.78,
            cycle_count=7,
            flatness=0.50,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="A touch tight for vol but fills are healthy (just below dont_fix) — don't disrupt.",
    ),
    NoGuardFixture(
        "slightly_wide_moderate",
        _summary(
            current_spacing=1.8,
            volatility=0.006,
            max_drawdown=-0.008,
            win_rate=0.68,
            cycle_count=3,
            flatness=0.60,
        ),
        acceptable=frozenset({"tighten", "hold"}),
        forbidden=None,
        note="A bit wide for moderate vol, modest fills — tighten toward ideal or hold; both fine.",
    ),
    NoGuardFixture(
        "ambiguous_thin_mixed",
        _summary(
            current_spacing=1.2,
            volatility=0.005,
            max_drawdown=-0.020,
            win_rate=0.50,
            cycle_count=3,
            flatness=0.55,
            snapshot_count=40,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden=None,
        note=(
            "Mixed signal on a thin window — an honest low-confidence HOLD or a "
            "defensive widen on the -2% drawdown are both defensible."
        ),
    ),
    NoGuardFixture(
        "moderate_drawdown_below_guard",
        _summary(
            current_spacing=1.1,
            volatility=0.004,
            max_drawdown=-0.045,
            win_rate=0.40,
            cycle_count=3,
            flatness=0.45,
            total_pnl=-12.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="dd -4.5%, just shy of the -5% defensive guard — still lean defensive, never tighten.",
    ),
    NoGuardFixture(
        "high_vol_tight_low_win",
        _summary(
            current_spacing=0.8,
            volatility=0.011,
            max_drawdown=-0.025,
            win_rate=0.32,
            cycle_count=6,
            flatness=0.30,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note="Tight grid in high-vol whipsaw, low win — clearly too tight for the vol; widen.",
    ),
    NoGuardFixture(
        "calm_well_matched_lowcycle",
        _summary(
            current_spacing=0.9,
            volatility=0.002,
            max_drawdown=-0.004,
            win_rate=0.70,
            cycle_count=2,
            flatness=0.90,
        ),
        acceptable=frozenset({"hold"}),
        forbidden="widen",
        note=(
            "Calm, ranging, spacing at the vol-ideal — hold; a tighten just chases the "
            "fee floor (SUBOPTIMAL), widening is wrong."
        ),
    ),
    NoGuardFixture(
        "recovering_after_dip",
        _summary(
            current_spacing=1.4,
            volatility=0.007,
            max_drawdown=-0.030,
            win_rate=0.60,
            cycle_count=8,
            flatness=0.45,
            total_pnl=6.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="Volatile then recovering, decent cycles — work through it; don't tighten into risk.",
    ),
)


# ---------------------------------------------------------------------------
# HARD set (2026-08-11) — built because v1 SATURATED.
#
# Measured on v1: a model that always answers HOLD scores **12/14 OK (86%)
# with ZERO UNSAFE** — beating the champion's 83% and nine of the eleven
# models in the 2026-08-11 sweep. `hold` is in the acceptable set of 12 of
# 14 fixtures, and 10 of 14 accept two of three directions, so a coin flip
# passes. That is why the whole field scored 0 UNSAFE: doing nothing scores
# 0 UNSAFE too. v1 measures instruction-following, not judgment.
#
# The fix is BALANCE, not subtlety: five fixtures for each direction as the
# ONLY defensible answer, so no constant strategy clears ~33%.
#
# Labels are argued from the metrics BEFORE any model ran — a fixture
# change must be justified in advance, never after seeing a score.
# ``tests/tools/test_freejudge_battery.py`` pins the guard-free property
# AND the constant-baseline ceiling, so this set cannot drift back into
# hold-bias unnoticed.
#
# KNOWN GAP (filed 2026-08-11, not built): this battery scores DIRECTION
# only. quant.md also makes an explicit, testable demand about calibration —
# "if the metrics are thin or ambiguous, say so with confidence: low" — and
# nothing here checks it. That gap is what made the original
# hard_thin_window_mixed_signals invalid: it tried to test thin-data
# handling through the direction axis, encoding "thin => hold", which the
# prompt never says. Testing calibration properly means scoring the
# `confidence` field against fixture ambiguity, which is a new axis rather
# than a new fixture.
#
# Domain reasoning follows quant.md's own asymmetry: WIDEN is the defensive
# lever, and a TIGHTEN is defensible "only when the metrics show genuine
# ranging that the current spacing is too wide to capture."
# ---------------------------------------------------------------------------
HARD_FIXTURES: tuple[NoGuardFixture, ...] = (
    # --- WIDEN only (5) ---
    NoGuardFixture(
        "hard_fee_bleed_churn",
        _summary(
            current_spacing=0.75,
            volatility=0.0065,
            max_drawdown=-0.015,
            win_rate=0.38,
            cycle_count=14,
            flatness=0.55,
            total_pnl=-1.85,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note=(
            "14 cycles at 38% win with NEGATIVE pnl: round-tripping constantly "
            "and losing to fees. Vol implies swings far wider than 0.75%. "
            "Holding keeps bleeding; tightening accelerates it."
        ),
    ),
    NoGuardFixture(
        "hard_vol_spike_grid_far_too_tight",
        _summary(
            current_spacing=0.85,
            volatility=0.0125,
            max_drawdown=-0.030,
            win_rate=0.45,
            cycle_count=9,
            flatness=0.35,
            total_pnl=-0.90,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note="Whipsaw vol against a 0.85% grid, ~3x too tight; low flatness = real movement.",
    ),
    NoGuardFixture(
        "hard_trend_onset_still_cycling",
        _summary(
            current_spacing=1.1,
            volatility=0.0075,
            max_drawdown=-0.042,
            win_rate=0.42,
            cycle_count=7,
            flatness=0.28,
            total_pnl=-1.20,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note=(
            "Flatness 0.28 = directional, drawdown -4.2% (just inside the -5% "
            "guard), still cycling. Widen is the documented defensive lever."
        ),
    ),
    NoGuardFixture(
        "hard_whipsaw_underspaced",
        _summary(
            current_spacing=1.0,
            volatility=0.0135,
            max_drawdown=-0.025,
            win_rate=0.50,
            cycle_count=11,
            flatness=0.40,
            total_pnl=-0.35,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note="Extreme whipsaw vs a 1.0% grid; many cycles at break-even = churn without capture.",
    ),
    NoGuardFixture(
        "hard_active_market_tight_grid_losing",
        _summary(
            current_spacing=0.9,
            volatility=0.009,
            max_drawdown=-0.028,
            win_rate=0.36,
            cycle_count=10,
            flatness=0.50,
            total_pnl=-1.40,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note=(
            "Active vol (0.009) against a 0.9% grid: ten round-trips at 36% win "
            "and clearly NEGATIVE pnl. The grid is trading hard and losing to "
            "fees, which is the widen case. CORRECTED 2026-08-11 — the original "
            "carried win 0.55 and pnl +0.05, i.e. a grid 'completing round-trips "
            "and staying green', which quant.md says to LEAVE ALONE. All four "
            "models answered hold on all 12 runs and were right; the label "
            "contradicted the prompt. Fixed by making the metrics match the "
            "note's stated intent, not by relabelling to match the answers."
        ),
    ),
    # --- TIGHTEN only (5) ---
    NoGuardFixture(
        "hard_starved_tight_range",
        _summary(
            current_spacing=2.6,
            volatility=0.0016,
            max_drawdown=-0.004,
            win_rate=0.0,
            cycle_count=0,
            flatness=0.95,
            total_pnl=0.0,
        ),
        acceptable=frozenset({"tighten"}),
        forbidden="widen",
        note=(
            "Flatness 0.95 at vol 0.0016 = a tight genuine range; a 2.6% grid is "
            "several times the swing and has completed ZERO cycles. Holding means "
            "continuing to do nothing — exactly what quant.md says a TIGHTEN is for."
        ),
    ),
    NoGuardFixture(
        "hard_starved_calm_very_wide",
        _summary(
            current_spacing=3.0,
            volatility=0.0021,
            max_drawdown=-0.006,
            win_rate=0.0,
            cycle_count=0,
            flatness=0.92,
            total_pnl=0.0,
        ),
        acceptable=frozenset({"tighten"}),
        forbidden="widen",
        note=(
            "Starvation one tier wider: no fills at all; widening guarantees "
            "permanent inactivity."
        ),
    ),
    NoGuardFixture(
        "hard_overwide_one_lucky_cycle",
        _summary(
            current_spacing=2.8,
            volatility=0.0026,
            max_drawdown=-0.005,
            win_rate=1.0,
            cycle_count=1,
            flatness=0.89,
            total_pnl=0.21,
        ),
        acceptable=frozenset({"tighten"}),
        forbidden="widen",
        note=(
            "A 100% win rate on ONE cycle is evidence the grid barely trades, not "
            "that it works. The trap is reading win_rate without cycle_count."
        ),
    ),
    NoGuardFixture(
        "hard_range_bound_overwide",
        _summary(
            current_spacing=2.2,
            volatility=0.0031,
            max_drawdown=-0.007,
            win_rate=0.50,
            cycle_count=2,
            flatness=0.90,
            total_pnl=0.06,
        ),
        acceptable=frozenset({"tighten"}),
        forbidden="widen",
        note="Strong ranging, vol implies ~1.05% swings vs a 2.2% grid; 2 cycles/6h is starvation.",
    ),
    NoGuardFixture(
        "hard_quiet_but_above_floor_wide",
        _summary(
            current_spacing=1.9,
            volatility=0.0013,
            max_drawdown=-0.003,
            win_rate=0.0,
            cycle_count=0,
            flatness=0.96,
            total_pnl=0.0,
        ),
        acceptable=frozenset({"tighten"}),
        forbidden="widen",
        note=(
            "Quiet and range-bound but NOT guard territory: vol 0.0013 exceeds "
            "fee_floor_calm's 0.001 and spacing 1.9% far exceeds its 0.68%, so it "
            "escalates. Long way to tighten before the fee floor bites."
        ),
    ),
    # --- HOLD only (5) ---
    NoGuardFixture(
        "hard_matched_and_earning",
        _summary(
            current_spacing=1.25,
            volatility=0.0041,
            max_drawdown=-0.009,
            win_rate=0.71,
            cycle_count=7,
            flatness=0.76,
            total_pnl=1.35,
        ),
        acceptable=frozenset({"hold"}),
        forbidden="tighten",
        note=(
            "Spacing sits on the vol-implied swing; 7 cycles at 71% win, clearly "
            "positive. Nothing is broken. win<0.85 keeps dont_fix_working dormant, "
            "so the LLM must reach this itself."
        ),
    ),
    NoGuardFixture(
        "hard_marginal_gap_not_worth_churn",
        _summary(
            current_spacing=1.35,
            volatility=0.0043,
            max_drawdown=-0.011,
            win_rate=0.66,
            cycle_count=6,
            flatness=0.73,
            total_pnl=0.82,
        ),
        acceptable=frozenset({"hold"}),
        forbidden=None,
        note=(
            "Spacing ~8% off the vol-implied ideal — inside noise, and profitable. "
            "Acting on a gap this small is the over-trading failure; neither "
            "direction is dangerous, both are unjustified."
        ),
        # NOT ambiguous: snapshot_count is the default 720 and the metrics
        # are clear (profitable, cycling, matched). Its "ambiguity" is that
        # the DECISION is marginal, which is not what quant.md means by
        # "thin or ambiguous" -- that is about the EVIDENCE. Marked True on
        # 2026-08-11 and corrected the same day when all three models
        # answered `high` here and were right.
    ),
    NoGuardFixture(
        "hard_ran_away_spacing_is_wrong_lever",
        _summary(
            current_spacing=1.3,
            volatility=0.0062,
            max_drawdown=-0.038,
            win_rate=0.0,
            cycle_count=0,
            flatness=0.22,
            total_pnl=0.0,
            latest_price=71500.0,
        ),
        acceptable=frozenset({"hold"}),
        forbidden=None,
        note=(
            "Price ran away directionally (flatness 0.22) and round-trips have "
            "STOPPED (0 cycles), with drawdown -3.8% — inside the -5% "
            "directional_runaway guard, so it escalates. quant.md states the "
            "rule verbatim: 'If price has run away directionally and round-trips "
            "have stopped, spacing is the wrong lever — that needs re-anchoring, "
            "not retuning, so HOLD spacing.' Hard because it LOOKS like it "
            "demands action. REPLACES hard_thin_window_mixed_signals (2026-08-11), "
            "which labelled a thin 38-snapshot window hold-only — a rule quant.md "
            "does NOT state: it says thin data means confidence: low, not "
            "inaction. 9 of 12 model runs tightened and were defensible."
        ),
    ),
    NoGuardFixture(
        "hard_recovering_and_working",
        _summary(
            current_spacing=1.6,
            volatility=0.0058,
            max_drawdown=-0.031,
            win_rate=0.68,
            cycle_count=6,
            flatness=0.66,
            total_pnl=0.44,
        ),
        acceptable=frozenset({"hold"}),
        forbidden="tighten",
        note=(
            "Drawdown -3.1% but recovering: still cycling, still net positive, "
            "spacing near the swing. Widening sacrifices a working grid to a move "
            "that already passed."
        ),
    ),
    NoGuardFixture(
        "hard_mild_uptrend_grid_keeping_up",
        _summary(
            current_spacing=1.9,
            volatility=0.0079,
            max_drawdown=-0.012,
            win_rate=0.63,
            cycle_count=5,
            flatness=0.58,
            total_pnl=0.67,
        ),
        acceptable=frozenset({"hold"}),
        forbidden="tighten",
        note=(
            "Vol implies ~1.9% swings and the grid IS 1.9% — matched, mildly "
            "trending, profitable. The trap is reading 'trend' as an automatic "
            "widen when the spacing already fits."
        ),
    ),
)


FIXTURE_SETS: dict[str, tuple[NoGuardFixture, ...]] = {
    "v1": FIXTURES,
    "hard": HARD_FIXTURES,
}


# ---------------------------------------------------------------------------
# Calibration axis (2026-08-11): does the model's confidence TRACK the
# quality of the evidence?
#
# quant.md: "If the metrics are thin or ambiguous, say so with
# confidence: low." Scoring that as a per-fixture rule is degenerate — a
# model that answers `low` to everything passes every thin fixture. The
# prompt's demand only means something if confidence VARIES: the purpose
# of "say so" is to DISTINGUISH, and a signal that never changes cannot.
#
# So the axis scores ASSOCIATION, not per-fixture correctness. That is
# non-degenerate by construction (any constant confidence has zero
# variance and scores 0.0) and needs no rule the prompt does not state —
# it tests the instruction as written rather than an inference layered on
# top, which is what produced two bad fixtures earlier today.
#
# Evidence quality is DERIVED MECHANICALLY, never hand-assigned: a
# subjective per-fixture "ambiguity" rating is precisely where an author's
# own inference re-enters. Both inputs are things the prompt itself names
# as evidence weight — snapshot_count ("how much data backs the window —
# thin => trust it less") and completed cycles (realized round-trips).
# ---------------------------------------------------------------------------
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def evidence_quality(fixture: NoGuardFixture) -> int:
    """How much evidence backs this fixture, 1 (thinnest) to 5 (strongest).

    Mechanical and auditable: no per-fixture judgement call, so it cannot
    be tuned to flatter a model after the fact.
    """
    snapshots = fixture.summary.snapshot_count
    cycles = fixture.summary.cycle_count
    snap_pts = 2 if snapshots >= 300 else (1 if snapshots >= 100 else 0)
    cycle_pts = 2 if cycles >= 6 else (1 if cycles >= 2 else 0)
    return 1 + snap_pts + cycle_pts


def kendall_tau_b(xs: list[int], ys: list[int]) -> float | None:
    """Kendall's tau-b, or ``None`` when it is undefined.

    tau-b (not Spearman) because confidence has only three levels, so
    ties dominate and tau-b is the tie-corrected form. ``None`` means one
    side had zero variance — every fixture rated the same, or every
    answer the same confidence — which is the degenerate case the axis
    exists to expose, not a score of zero to be averaged in.
    """
    n = len(xs)
    if n < 2:
        return None
    concordant = discordant = 0
    tie_x = tie_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            product = dx * dy
            if product > 0:
                concordant += 1
            elif product < 0:
                discordant += 1
            else:
                if dx == 0:
                    tie_x += 1
                if dy == 0:
                    tie_y += 1
    n0 = n * (n - 1) / 2
    denominator = ((n0 - tie_x) * (n0 - tie_y)) ** 0.5
    if denominator == 0:
        return None
    return (concordant - discordant) / denominator


def calibration_tau(rows: list[dict[str, object]]) -> float | None:
    """Association between evidence quality and reported confidence."""
    xs: list[int] = []
    ys: list[int] = []
    for row in rows:
        rank = _CONFIDENCE_RANK.get(str(row.get("confidence")))
        evidence = row.get("evidence")
        if rank is None or not isinstance(evidence, int):
            continue
        xs.append(evidence)
        ys.append(rank)
    return kendall_tau_b(xs, ys)


def classify_direction(proposed: object, current: float | None) -> Direction:
    """Map a proposed spacing to widen/hold/tighten vs the current grid.

    An omitted / non-numeric / current-less proposal is a deliberate HOLD
    (the prompt tells the model to omit fields it won't change). Within
    ±``_HOLD_DEADBAND`` of current reads as no meaningful change → hold.
    """
    pf = _as_float(proposed)
    if pf is None or current is None or current == 0:
        return "hold"
    delta = (pf - current) / current
    if abs(delta) < _HOLD_DEADBAND:
        return "hold"
    return "widen" if delta > 0 else "tighten"


def score_fixture(rec: AdvisorRecommendation, fx: NoGuardFixture) -> tuple[Verdict, Direction, str]:
    """Grade one recommendation against a fixture's risk-model labels."""
    proposed = rec.recommendations.get("spacing_percentage")
    direction = classify_direction(proposed, fx.summary.current_grid.spacing_percentage)
    pf = _as_float(proposed)
    if pf is not None and pf < _FEE_FLOOR:
        return "UNSAFE", direction, f"proposed {pf}% is below the {_FEE_FLOOR}% fee floor"
    if fx.forbidden is not None and direction == fx.forbidden:
        return "UNSAFE", direction, f"made the forbidden call ({fx.forbidden}) for this regime"
    if direction in fx.acceptable:
        return "OK", direction, "acceptable"
    return "SUBOPTIMAL", direction, f"defensible-but-not-ideal (acceptable={sorted(fx.acceptable)})"


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def verify_no_guard(
    fixtures: tuple[NoGuardFixture, ...] = FIXTURES,
    spec_path: Path = _SHIPPED_SPEC,
) -> list[tuple[str, str]]:
    """Return ``(name, guard_reason)`` for any fixture that WRONGLY fires a
    guard under the shipped heuristic. Empty list = the battery is valid."""
    adapter = HeuristicAdvisorAdapter(spec=load_heuristic_spec(spec_path))
    offenders: list[tuple[str, str]] = []
    for fx in fixtures:
        verdict = adapter.evaluate(fx.summary)
        if verdict.clear_match:
            offenders.append((fx.name, verdict.reason))
    return offenders


def _load_cloud_builder() -> object:
    """Reuse ``probe_advisor._build_cloud_advisor`` by path-load (the repo's
    tool/test interop pattern) — no duplication of the cost-gated wiring."""
    path = _REPO_ROOT / "tools" / "probe_advisor.py"
    spec = importlib.util.spec_from_file_location("probe_advisor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_advisor"] = module
    spec.loader.exec_module(module)
    # Deliberate reuse of the sibling tool's cost-gated builder (no duplication).
    # pylint: disable-next=protected-access
    return module._build_cloud_advisor  # type: ignore[attr-defined]


def _calibration_line(rows: list[dict[str, object]]) -> str:
    """One-line calibration verdict for the run summary.

    Extracted from ``main_async`` to keep it under pylint's statement cap
    -- the same R0915 that reached CI once already this week.
    """
    conf_counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("confidence"))
        conf_counts[key] = conf_counts.get(key, 0) + 1
    tau = calibration_tau(rows)
    if tau is None:
        distinct_evidence = len({row.get("evidence") for row in rows})
        verdict = (
            "[UNDEFINED: the FIXTURE SET has no evidence spread]"
            if distinct_evidence <= 1
            else "[DEGENERATE: one confidence level for every fixture -- no signal]"
        )
        tau_text = "n/a"
    else:
        tau_text = f"{tau:+.2f}"
        if tau >= 0.4:
            verdict = "[tracks evidence]"
        elif tau > 0.0:
            verdict = "[weak]"
        else:
            verdict = "[INVERTED: confident when the evidence is thin]"
    counts = "  ".join(f"{k}={v}" for k, v in sorted(conf_counts.items()))
    return f"CALIBRATION tau_b={tau_text} {verdict}  {counts}"


async def main_async(args: argparse.Namespace) -> int:  # pylint: disable=too-many-locals
    fixtures = FIXTURE_SETS[args.fixture_set]
    offenders = verify_no_guard(fixtures)
    if offenders:
        print("error: battery integrity check FAILED — these fixtures fire a guard:")
        for name, reason in offenders:
            print(f"  {name}: {reason}")
        return 3

    prompt = load_prompt(Path(args.prompt_file))
    build = _load_cloud_builder()
    storage = SQLiteStorageAdapter("data/probe_llm_cost.db")
    Path("data").mkdir(exist_ok=True)
    await storage.connect()
    adapter: AdvisorPort = build(  # type: ignore[operator]
        provider=args.provider,
        model=args.model,
        prompt=prompt,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout_seconds=args.timeout_seconds,
        storage=storage,
        session_cap=args.session_cap,
        daily_cap=args.daily_cap,
    )

    print(
        f"# free-judge battery [{args.fixture_set}]: {len(fixtures)} no-guard "
        f"fixtures (all verified guard-free)"
    )
    print(f"# model: {args.provider}/{args.model}  prompt: {args.prompt_file}")
    counts = {"OK": 0, "SUBOPTIMAL": 0, "UNSAFE": 0, "ERROR": 0}
    rows: list[dict[str, object]] = []
    try:
        for fx in fixtures:
            t0 = time.monotonic()
            try:
                rec = await adapter.get_recommendation(fx.summary)
                verdict, direction, why = score_fixture(rec, fx)
                spacing = rec.recommendations.get("spacing_percentage", "—")
                confidence = rec.confidence
            except (AdvisorError, LLMCostCapExceeded) as exc:
                verdict, direction, why, spacing = "ERROR", "hold", str(exc)[:60], "—"
                confidence = None
            elapsed = time.monotonic() - t0
            counts[verdict] = counts.get(verdict, 0) + 1
            rows.append(
                {
                    "name": fx.name,
                    "verdict": verdict,
                    "direction": direction,
                    "spacing": str(spacing),
                    "forbidden": fx.forbidden,
                    "acceptable": sorted(fx.acceptable),
                    "why": why,
                    "confidence": confidence,
                    "evidence": evidence_quality(fx),
                    "elapsed_s": round(elapsed, 1),
                }
            )
            print(
                f"  {fx.name:32s} {verdict:10s} dir={direction:7s} "
                f"spacing={str(spacing):7s} ({elapsed:.1f}s)  {why}"
            )
    finally:
        aclose = getattr(adapter, "aclose", None)
        if aclose is not None:
            await aclose()
        await storage.close()

    safe = counts["OK"] + counts["SUBOPTIMAL"]
    print(
        f"\nSUMMARY  OK={counts['OK']}  SUBOPTIMAL={counts['SUBOPTIMAL']}  "
        f"UNSAFE={counts['UNSAFE']}  ERROR={counts['ERROR']}  "
        f"(non-unsafe {safe}/{len(fixtures)})"
    )
    # --- calibration: MEASURED, NOT SCORED (2026-08-11) ---
    # quant.md demands "if the metrics are thin or ambiguous, say so with
    # confidence: low". Nothing has ever recorded the confidence field, so
    # there is no evidence models vary it at all. Report the distribution
    # first; design a rubric from real data second. Building the rubric
    # blind is what produced the news-battery and fixture defects earlier
    # in this arc.
    print(_calibration_line(rows))

    if args.json:
        print(
            "JSON_RESULT: "
            + json.dumps(
                {
                    "model": args.model,
                    "provider": args.provider,
                    "fixture_set": args.fixture_set,
                    "counts": counts,
                    "calibration_tau_b": calibration_tau(rows),
                    "rows": rows,
                }
            )
        )
    return 0


def main() -> int:
    # Windows pipes/redirects default stdout to cp1252, which crashes on any
    # non-ASCII output char. Force UTF-8 so redirected runs never die on a glyph.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    load_operator_env()
    p = argparse.ArgumentParser(
        prog="tools.probe_freejudge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--provider",
        choices=("openai", "anthropic", "google", "atlas"),
        default="openai",
    )
    p.add_argument("--model", default="gpt-5-mini")
    p.add_argument(
        "--fixture-set",
        choices=tuple(FIXTURE_SETS),
        default="v1",
        help=(
            "Which battery. 'v1' (default) is the 14-fixture set every recorded "
            "score uses -- keep it for comparability. 'hard' is the 15-fixture "
            "balanced set built 2026-08-11 after v1 saturated (constant-HOLD "
            "scores 86%% there vs 33%% on hard)."
        ),
    )
    p.add_argument("--prompt-file", default="config/prompts/quant.md")
    p.add_argument("--max-tokens", type=int, default=4000)
    p.add_argument("--temperature", type=float, default=0.5)
    p.add_argument("--timeout-seconds", type=float, default=120.0)
    p.add_argument("--session-cap", type=float, default=2.0)
    p.add_argument("--daily-cap", type=float, default=5.0)
    p.add_argument("--json", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

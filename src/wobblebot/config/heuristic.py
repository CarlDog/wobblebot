"""Heuristic-advisor spec loader (Stage 8.5).

The deterministic ``HeuristicAdvisorAdapter`` reads its decision logic
from an operator-editable YAML file — the same ownership model as the
prompt files in ``config/prompts/`` (committed default, edited freely,
bind-mounted on the NAS). Pointing ``advisor.heuristic_file`` at a file
lets an operator retune the heuristic *without a code change*.

What lives in the file (DATA) vs the adapter (LOGIC):

- **File:** the ideal-spacing-vs-volatility ``curve`` (the load-bearing
  judgment), the ``fee_floor`` and ``hold_deadband`` scalars, each
  guard's thresholds, per-guard on/off ``enabled`` toggles, and the
  cascade ``escalation`` band.
- **Adapter (code):** the guard *algorithm*, their *priority order*,
  and how they compose. New guard behaviour is a code change with a
  fixture-battery test — by design (see ``adapters/heuristic_advisor.py``).

File format (``config/heuristic/quant.yml``)::

    curve:
      - {vol: 0.0008, spacing: 0.65}
      - {vol: 0.002,  spacing: 0.90}
      ...
    fee_floor: 0.52
    hold_deadband: 0.15
    guards:
      directional_runaway: {enabled: true, threshold: -0.05}
      defensive_drawdown:  {enabled: true, threshold: -0.05, widen_factor: 1.5}
      dont_fix_working:    {enabled: true, win_rate_min: 0.85, cycles_min: 8, drawdown_max: -0.02}
      fee_floor_calm:      {enabled: true, calm_vol: 0.001, near_floor_spacing: 0.68}
    escalation: {enabled: true, margin: 0.5, min_snapshots: 30}

``curve`` is the only required field — it is the heuristic's core
judgment and has no sensible default. Every scalar / guard threshold
defaults in code (guard *semantics* are part of the algorithm), so a
minimal file need only declare the curve; the committed default lists
everything for operator transparency.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class CurvePoint(BaseModel):
    """One (volatility, ideal-spacing) breakpoint on the curve.

    ``vol`` is realized per-tick volatility (sample stdev of simple
    returns, matching ``PerformanceSummary.volatility``); ``spacing`` is
    the ideal grid spacing in PERCENT units (``0.90`` == 0.90%).
    """

    vol: float = Field(ge=0)
    spacing: float = Field(gt=0)

    class Config:
        frozen = True


class DirectionalRunawayGuard(BaseModel):
    """Guard 4 — price ran away directionally; spacing can't fix it.

    Fires when there are zero completed cycles AND the drawdown is at
    least as deep as ``threshold`` (price moved hard one way without a
    single round-trip). The fix is re-anchoring, not a spacing change,
    so the verdict is HOLD.
    """

    enabled: bool = True
    threshold: float = Field(default=-0.05, le=0)

    class Config:
        frozen = True


class DefensiveDrawdownGuard(BaseModel):
    """Guard 3 — capital preservation overrides the calm-tighten instinct.

    Fires on a sharp drawdown (``max_drawdown <= threshold``) when the
    grid is still cycling (Guard 4 claims the zero-cycle case first).
    Verdict WIDEN, target ``current * widen_factor`` (but never below
    the vol-ideal).
    """

    enabled: bool = True
    threshold: float = Field(default=-0.05, le=0)
    widen_factor: float = Field(default=1.5, gt=1)

    class Config:
        frozen = True


class DontFixWorkingGuard(BaseModel):
    """Guard 2 — don't disrupt a configuration that's actively printing fills.

    Fires when win rate, cycle count, and drawdown all clear their
    thresholds (high win AND high cycles AND shallow drawdown). Verdict
    HOLD even if the vol/spacing pairing looks theoretically mismatched.
    ``drawdown_max`` is an upper bound on depth: ``max_drawdown`` must be
    shallower than it (``>=``).
    """

    enabled: bool = True
    win_rate_min: float = Field(default=0.85, ge=0, le=1)
    cycles_min: int = Field(default=8, ge=0)
    drawdown_max: float = Field(default=-0.02, le=0)

    class Config:
        frozen = True


class FeeFloorCalmGuard(BaseModel):
    """Guard 1 — near the fee floor in a dead-calm market, don't churn.

    Fires when current spacing is already near the fee floor
    (``<= near_floor_spacing``) and the market is calm
    (``volatility <= calm_vol``). Tightening below ~2x the maker fee is
    unprofitable and a tiny widen in dead calm just churns orders, so
    the verdict is HOLD.
    """

    enabled: bool = True
    calm_vol: float = Field(default=0.001, ge=0)
    near_floor_spacing: float = Field(default=0.68, gt=0)

    class Config:
        frozen = True


class HeuristicGuards(BaseModel):
    """The four override guards, each individually toggleable."""

    directional_runaway: DirectionalRunawayGuard = Field(default_factory=DirectionalRunawayGuard)
    defensive_drawdown: DefensiveDrawdownGuard = Field(default_factory=DefensiveDrawdownGuard)
    dont_fix_working: DontFixWorkingGuard = Field(default_factory=DontFixWorkingGuard)
    fee_floor_calm: FeeFloorCalmGuard = Field(default_factory=FeeFloorCalmGuard)

    class Config:
        frozen = True


class EscalationConfig(BaseModel):
    """Cascade escalation tuning (only consulted in ``engine: cascade``).

    The heuristic flags a first-order call as a "non-clear match" — the
    signal the cascade reads to defer to the LLM — when the gap between
    current spacing and the vol-ideal sits in an ambiguous band straddling
    the hold deadband, OR when the metrics window is too thin to trust
    the volatility estimate.

    - ``margin``: half-width of the ambiguous band as a fraction of
      ``hold_deadband``. With deadband 0.15 and margin 0.5, gaps whose
      magnitude lies in ``[0.075, 0.225]`` escalate; clearly-inside
      (confident HOLD) and clearly-outside (confident action) do not.
    - ``min_snapshots``: first-order calls computed from fewer than this
      many price snapshots are treated as low-confidence (escalate).
      Guards key off realized drawdown / cycles / win rate, not the vol
      estimate, so they stay clear regardless.
    - ``enabled``: when false the heuristic never flags ambiguity, so a
      cascade resolves every case via the heuristic (the LLM fires only
      on hard failures / cost-cap fallback).
    """

    enabled: bool = True
    margin: float = Field(default=0.5, ge=0, le=1)
    min_snapshots: int = Field(default=30, ge=0)

    class Config:
        frozen = True


class HeuristicSpec(BaseModel):
    """Operator-tunable spec for the deterministic advisor.

    ``curve`` is required (≥2 points) — it is the heuristic's core
    judgment with no sensible default. Everything else defaults in code.
    """

    curve: list[CurvePoint] = Field(min_length=2)
    fee_floor: float = Field(default=0.52, gt=0)
    hold_deadband: float = Field(default=0.15, gt=0, lt=1)
    guards: HeuristicGuards = Field(default_factory=HeuristicGuards)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)

    class Config:
        frozen = True

    @model_validator(mode="after")
    def _validate_curve_monotonic(self) -> HeuristicSpec:
        """Curve volatilities must be strictly increasing.

        The interpolator assumes a sorted, de-duplicated curve;
        out-of-order or duplicate ``vol`` values are an operator typo
        that would make ``ideal(vol)`` ambiguous, so reject at load
        time rather than silently mis-interpolate.
        """
        vols = [p.vol for p in self.curve]
        if vols != sorted(vols) or len(set(vols)) != len(vols):
            raise ValueError(
                "heuristic curve points must be sorted by strictly increasing `vol`; " f"got {vols}"
            )
        return self


def load_heuristic_spec(path: Path) -> HeuristicSpec:
    """Read and validate a heuristic spec file at ``path``.

    Args:
        path: Filesystem path to a YAML heuristic spec.

    Returns:
        Validated :class:`HeuristicSpec`.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: File is empty, not a YAML mapping, or fails the
            curve-monotonicity / schema checks.
        pydantic.ValidationError: Mapping present but fails the schema.
    """
    if not path.exists():
        raise FileNotFoundError(f"Heuristic spec file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"Heuristic spec file {path} is empty")
    if not isinstance(raw, dict):
        raise ValueError(
            f"Heuristic spec file {path} must be a YAML mapping at the top level; "
            f"got {type(raw).__name__}"
        )
    return HeuristicSpec.model_validate(raw)


__all__ = [
    "CurvePoint",
    "DefensiveDrawdownGuard",
    "DirectionalRunawayGuard",
    "DontFixWorkingGuard",
    "EscalationConfig",
    "FeeFloorCalmGuard",
    "HeuristicGuards",
    "HeuristicSpec",
    "load_heuristic_spec",
]

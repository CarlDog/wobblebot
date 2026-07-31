"""SafetyConfig — non-negotiable trading caps and emergency-stop thresholds.

Field-level validation only: positive caps, non-negative balance floors,
percentages bounded 0-100. Cross-field invariants (e.g. per-coin cap must
not exceed total cap) and runtime enforcement live in the grid engine
(slice 2.2.4), not here.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class SellGuardConfig(BaseModel):
    """ADR-032 cost-basis sell guard.

    Defers a SELL whose net-of-fees proceeds fall more than
    ``max_loss_percentage`` below the symbol's replayed average cost
    basis, while every other placement continues. Replaces
    ``EmergencyStopConfig`` (retired by ADR-032): that knob's documented
    halt-all-trading role was already served by the enforced
    ``live.max_session_loss_usd`` mark-to-market cap and was parsed by
    nobody. Full defaults — absent config loads clean.
    """

    enabled: bool = True
    max_loss_percentage: Decimal = Field(default=Decimal("1.0"), gt=Decimal("0"), le=Decimal("100"))

    class Config:
        frozen = True


class SafetyConfig(BaseModel):
    """Trading caps. Enforcement happens inside Bot Core (per ADR-006 and
    the financial-power-fragmentation invariant in CLAUDE.md), never in an
    adapter."""

    max_total_exposure_usd: Decimal = Field(gt=Decimal("0"))
    max_daily_spend_usd: Decimal = Field(gt=Decimal("0"))
    max_per_coin_exposure_usd: Decimal = Field(gt=Decimal("0"))
    max_orders_per_coin: int = Field(gt=0)
    sell_guard: SellGuardConfig = Field(default_factory=SellGuardConfig)

    class Config:
        frozen = True

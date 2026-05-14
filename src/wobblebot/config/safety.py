"""SafetyConfig — non-negotiable trading caps and emergency-stop thresholds.

Field-level validation only: positive caps, non-negative balance floors,
percentages bounded 0-100. Cross-field invariants (e.g. per-coin cap must
not exceed total cap) and runtime enforcement live in the grid engine
(slice 2.2.4), not here.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class EmergencyStopConfig(BaseModel):
    """Conditions under which the engine halts all trading."""

    enabled: bool = True
    max_loss_percentage: Decimal = Field(gt=Decimal("0"), le=Decimal("100"))
    min_exchange_balance_usd: Decimal = Field(ge=Decimal("0"))

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
    emergency_stop: EmergencyStopConfig

    class Config:
        frozen = True

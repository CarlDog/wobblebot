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

    # ADR-039 inventory caps (2026-08-17). The four caps above bound the
    # ORDER BOOK; these bound the POSITION — held inventory at average
    # COST basis plus open BUY notional. Cost, not mark-to-market: an
    # MTM figure shrinks as price falls and would re-open buying into a
    # falling market. Gates BUY placement only — a SELL releases
    # headroom and is never blocked by these. Born from the 2026-08
    # reader-key incident's churn-buys ($55 of BTC the commitment caps
    # could not see) plus the restart-ratchet (each deploy's boot
    # re-layout buys ~$5 the sell guard then defers below basis).
    max_per_coin_inventory_usd: Decimal = Field(default=Decimal("40"), gt=Decimal("0"))
    max_total_inventory_usd: Decimal = Field(default=Decimal("300"), gt=Decimal("0"))
    sell_guard: SellGuardConfig = Field(default_factory=SellGuardConfig)
    # ADR-025 pre-placement spread guard: skip the whole tick (not a
    # per-order safety-cap arm) when the symbol's bid-ask spread is too
    # wide -- a market-quality signal, not a per-order invariant. Default
    # 1.0% never fires on healthy BTC/ETH (~0.01-0.05%); None disables.
    max_spread_percentage: Decimal | None = Field(
        default=Decimal("1.0"), gt=Decimal("0"), le=Decimal("100")
    )

    class Config:
        frozen = True

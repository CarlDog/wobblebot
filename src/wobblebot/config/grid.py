"""GridConfig — per-coin grid trading parameters with default + override layering.

A grid is defined by spacing (as percentage of base price), the number of
levels above and below the reference price, and the per-level order size in
USD. Coins with no entry in ``coins`` use ``default``; coins with an entry
override every field and carry their own ``enabled`` flag.

Per ADR-006, the grid is a "stay parked" mean-reversion structure — these
parameters define the layout, not behavior under trend regimes.

Per-coin override semantics are deliberately strict (every field must be
specified) rather than partial-merge. The example YAML
(``config/settings.example.yml``) follows this pattern.

Spacing-vs-fees validation (soak-surfaced finding 2026-05-20). Each grid
cycle pays the maker fee twice — once on the buy leg and once on the
sell leg. If the configured spacing is less than ``2 × maker_fee_rate``,
the cycle cannot profit even if both legs fill as maker. The validator
on ``GridConfig`` rejects such configurations for enabled coins at
config-load time, surfacing in a clear error message instead of silently
losing money on every completed cycle. Disabled coins skip the check —
operator may be holding a stale spacing they plan to fix before enabling.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Kraken Tier-1 published rates. DOUBLED by Kraken's cross-platform fee
# tiers, effective 2026-07-09 (accounts auto-migrated; discovered
# 2026-08-17 from the trades table's own fee buckets, confirmed against
# the published schedule AND the account's live TradeVolume response —
# the drift hid five weeks because a new-schedule maker fill bills
# exactly what an old-schedule taker did). Same source used by
# ``shadow.maker_fee_rate`` / ``shadow.taker_fee_rate`` in ShadowConfig;
# update both together. Per ADR-038 these are the FALLBACK — cli/live
# fetches the account's actual per-pair rates from TradeVolume at
# session start and these apply only when that fetch fails.
KRAKEN_MAKER_FEE_RATE = Decimal("0.0040")  # 0.40% maker — limit orders that sit
KRAKEN_TAKER_FEE_RATE = Decimal("0.0080")  # 0.80% taker — marketable orders


class GridLevels(BaseModel):
    """Geometry of a single grid (default or per-coin)."""

    spacing_percentage: Decimal = Field(gt=Decimal("0"))
    levels_above: int = Field(gt=0)
    levels_below: int = Field(gt=0)
    order_size_usd: Decimal = Field(gt=Decimal("0"))
    # ADR-029: where a filled BUY's counter SELL goes. ``spacing_up`` = one
    # spacing step above the fill (classic grid); ``top_sell`` = the top of
    # the configured band (sell into strength; carries inventory-accumulation
    # risk in a grinding downtrend). SELL-fill counters are unaffected. Read
    # live each tick — never snapshotted into GridState. Non-numeric, so the
    # auto-apply gate rejects it by construction (operator-approval-only).
    counter_target_mode: Literal["spacing_up", "top_sell"] = "spacing_up"

    class Config:
        frozen = True


class CoinGridConfig(GridLevels):
    """Per-coin grid entry. Adds an ``enabled`` flag — a coin defined in
    the config but ``enabled: false`` is parsed but skipped at engine
    wiring time."""

    enabled: bool = True


class GridConfig(BaseModel):
    """Top-level grid section: default geometry plus per-coin overrides."""

    default: GridLevels
    coins: dict[str, CoinGridConfig] = Field(default_factory=dict)

    class Config:
        frozen = True

    @model_validator(mode="after")
    def _validate_spacing_covers_fees(self) -> "GridConfig":
        """Refuse configurations where a grid cycle cannot profit.

        For each enabled coin (plus the always-active ``default``), the
        spacing must strictly exceed ``2 × maker_fee_rate``. At-or-below
        that threshold, the round-trip cycle loses money or breaks even
        even in the best case (both legs filling as maker).

        Disabled per-coin entries skip the check — they're parsed but
        not traded, so their spacing is operator scratchpad space.
        """
        min_profitable_pct = KRAKEN_MAKER_FEE_RATE * Decimal("2") * Decimal("100")

        def _check(label: str, spacing_percentage: Decimal) -> None:
            if spacing_percentage <= min_profitable_pct:
                raise ValueError(
                    f"{label} spacing_percentage={spacing_percentage}% is at or "
                    f"below minimum profitable spacing {min_profitable_pct}% "
                    f"(= 2 × Kraken maker fee {KRAKEN_MAKER_FEE_RATE * 100}%). "
                    f"Every completed grid cycle would lose money or break even "
                    f"even with both legs filling as maker. Increase "
                    f"spacing_percentage above {min_profitable_pct}% or "
                    f"set enabled=false on the per-coin entry."
                )

        _check("grid.default", self.default.spacing_percentage)
        for coin_name, coin_cfg in self.coins.items():
            if coin_cfg.enabled:
                _check(f"grid.coins.{coin_name} (enabled)", coin_cfg.spacing_percentage)
        return self

    def for_coin(self, symbol: str) -> CoinGridConfig:
        """Return effective config for a coin.

        If the coin has a per-coin entry, that entry is returned verbatim.
        Otherwise the default geometry is returned with ``enabled=True``
        (a coin not explicitly disabled is enabled).

        Symbol matching is case-insensitive — ``DOGE`` and ``doge`` both
        find an entry keyed ``"DOGE"``.
        """
        override = self.coins.get(symbol.upper())
        if override is not None:
            return override
        # Splat, not per-field enumeration: a hand-copied field list here
        # silently reverts any newly-added GridLevels field to its class
        # default for every coin without an override (the ADR-029
        # counter_target_mode implementation note).
        return CoinGridConfig(**self.default.model_dump(), enabled=True)

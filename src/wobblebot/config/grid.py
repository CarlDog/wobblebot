"""GridConfig — per-coin grid trading parameters with default + override layering.

A grid is defined by spacing (as percentage of base price), the number of
levels above and below the reference price, and the per-level order size in
USD. Coins with no entry in ``coins`` use ``default``; coins with an entry
override every field and carry their own ``enabled`` flag.

Per ADR-006, the grid is a "stay parked" mean-reversion structure — these
parameters define the layout, not behavior under trend regimes.

Per-coin override semantics are deliberately strict (every field must be
specified) rather than partial-merge. The example YAML
(``config/wobblebot.example.yml``) follows this pattern.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class GridLevels(BaseModel):
    """Geometry of a single grid (default or per-coin)."""

    spacing_percentage: Decimal = Field(gt=Decimal("0"))
    levels_above: int = Field(gt=0)
    levels_below: int = Field(gt=0)
    order_size_usd: Decimal = Field(gt=Decimal("0"))

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
        return CoinGridConfig(
            spacing_percentage=self.default.spacing_percentage,
            levels_above=self.default.levels_above,
            levels_below=self.default.levels_below,
            order_size_usd=self.default.order_size_usd,
            enabled=True,
        )

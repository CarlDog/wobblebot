"""Shared test helper builders for config-heavy unit + integration tests.

Eight test modules were hand-rolling ``_grid_config()`` / ``_safety_config()``
builders with subtly different signatures (default vs explicit ``enabled=True``,
different cap magnitudes, different emergency-stop knobs). They've been
consolidated into the two public builders below.

These are plain functions, not pytest fixtures — call them directly in test
bodies. The signatures are the union of every variant that existed; every
prior call site can be expressed by overriding kwargs against the defaults.

Permissive defaults make the "I just want a valid config object" case a
single zero-arg call:

    grid = grid_config()
    safety = safety_config()

Tighter caps for cap-trip tests override the relevant kwargs:

    safety = safety_config(max_total="100", max_orders=10, max_loss_pct="5")
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from wobblebot.config.grid import CoinGridConfig, GridConfig, GridLevels
from wobblebot.config.safety import SafetyConfig, SellGuardConfig


def grid_config(
    *,
    spacing_pct: str = "1.0",
    above: int = 3,
    below: int = 3,
    order_size: str = "10",
    counter_target_mode: Literal["spacing_up", "top_sell"] = "spacing_up",
    coins: dict[str, CoinGridConfig] | None = None,
) -> GridConfig:
    """Build a ``GridConfig`` with default-only or with explicit per-coin overrides."""
    return GridConfig(
        default=GridLevels(
            spacing_percentage=Decimal(spacing_pct),
            levels_above=above,
            levels_below=below,
            order_size_usd=Decimal(order_size),
            counter_target_mode=counter_target_mode,
        ),
        coins=coins or {},
    )


def safety_config(
    *,
    max_total: str = "100000",
    max_daily: str = "100000",
    max_per_coin: str = "100000",
    max_orders: int = 100,
    sell_guard_enabled: bool = True,
    max_loss_pct: str = "1.0",
) -> SafetyConfig:
    """Permissive default — individual tests tighten one cap to test it."""
    return SafetyConfig(
        max_total_exposure_usd=Decimal(max_total),
        max_daily_spend_usd=Decimal(max_daily),
        max_per_coin_exposure_usd=Decimal(max_per_coin),
        max_orders_per_coin=max_orders,
        sell_guard=SellGuardConfig(
            enabled=sell_guard_enabled,
            max_loss_percentage=Decimal(max_loss_pct),
        ),
    )

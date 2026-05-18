"""Tests for services.calibrator — pure-function recalibration (Stage 7.6.A)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.config.cli import LiveConfig
from wobblebot.config.grid import CoinGridConfig, GridConfig, GridLevels
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.calibrator import (
    RecalibrationProposal,
    recalibrate,
)

pytestmark = pytest.mark.unit


def _full_config(
    *,
    grid: GridConfig | None = None,
    safety: SafetyConfig | None = None,
    live: LiveConfig | None = None,
    harvester: HarvesterConfig | None = None,
) -> WobbleBotConfig:
    """Build a WobbleBotConfig with optional explicit blocks.

    Defaults: a permissive grid + safety, no live/harvester. Tests
    override individual sections.
    """
    return WobbleBotConfig(
        grid=grid or _grid_config(),
        safety=safety or _safety_config(),
        live=live,
        harvester=harvester,
    )


def _safety(
    *,
    max_total: str = "100",
    max_daily: str = "100",
    max_per_coin: str = "50",
    max_orders: int = 20,
    max_loss_pct: str = "20",
    min_balance: str = "0",
) -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=Decimal(max_total),
        max_daily_spend_usd=Decimal(max_daily),
        max_per_coin_exposure_usd=Decimal(max_per_coin),
        max_orders_per_coin=max_orders,
        emergency_stop=EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal(max_loss_pct),
            min_exchange_balance_usd=Decimal(min_balance),
        ),
    )


def _live(*, max_session_loss: str = "5") -> LiveConfig:
    return LiveConfig(
        symbols=[Symbol(base="BTC", quote="USD")],
        max_session_loss_usd=Decimal(max_session_loss),
    )


def _harvester(
    *,
    min_liq: str = "200",
    topup: str = "300",
    surplus: str = "500",
    day_cap: str = "1000",
) -> HarvesterConfig:
    return HarvesterConfig(
        enabled=False,
        min_exchange_liquidity_usd=Decimal(min_liq),
        topup_threshold_usd=Decimal(topup),
        surplus_threshold_usd=Decimal(surplus),
        max_withdrawal_per_day_usd=Decimal(day_cap),
    )


# --------------------------------------------------------------------- #
# Input validation                                                      #
# --------------------------------------------------------------------- #


class TestInputValidation:
    def test_zero_current_balance_raises(self) -> None:
        cfg = _full_config()
        with pytest.raises(ValueError, match="current_balance must be positive"):
            recalibrate(
                current_balance=Decimal("0"),
                target_balance=Decimal("10"),
                current_config=cfg,
            )

    def test_negative_current_balance_raises(self) -> None:
        cfg = _full_config()
        with pytest.raises(ValueError, match="current_balance must be positive"):
            recalibrate(
                current_balance=Decimal("-50"),
                target_balance=Decimal("10"),
                current_config=cfg,
            )

    def test_zero_target_balance_raises(self) -> None:
        cfg = _full_config()
        with pytest.raises(ValueError, match="target_balance must be positive"):
            recalibrate(
                current_balance=Decimal("100"),
                target_balance=Decimal("0"),
                current_config=cfg,
            )

    def test_negative_target_balance_raises(self) -> None:
        cfg = _full_config()
        with pytest.raises(ValueError, match="target_balance must be positive"):
            recalibrate(
                current_balance=Decimal("100"),
                target_balance=Decimal("-50"),
                current_config=cfg,
            )


# --------------------------------------------------------------------- #
# Scale factor                                                          #
# --------------------------------------------------------------------- #


class TestScaleFactor:
    def test_scale_down_to_one_tenth(self) -> None:
        cfg = _full_config()
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("10"),
            current_config=cfg,
        )
        assert prop.scale_factor == Decimal("0.1")

    def test_scale_up_to_double(self) -> None:
        cfg = _full_config()
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("200"),
            current_config=cfg,
        )
        assert prop.scale_factor == Decimal("2")

    def test_identity_scale_produces_no_changes(self) -> None:
        cfg = _full_config()
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("100"),
            current_config=cfg,
        )
        assert prop.scale_factor == Decimal("1")
        assert prop.changes == ()


# --------------------------------------------------------------------- #
# Grid scaling                                                          #
# --------------------------------------------------------------------- #


class TestGridScaling:
    def test_scales_default_order_size(self) -> None:
        cfg = _full_config(grid=_grid_config(order_size="10"))
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path for c in prop.changes}
        assert "grid.default.order_size_usd" in paths
        change = next(c for c in prop.changes if c.yaml_path == "grid.default.order_size_usd")
        assert change.current_value == Decimal("10")
        assert change.proposed_value == Decimal("5.00")

    def test_scales_per_coin_overrides(self) -> None:
        cfg = _full_config(
            grid=GridConfig(
                default=GridLevels(
                    spacing_percentage=Decimal("1"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("10"),
                ),
                coins={
                    "DOGE": CoinGridConfig(
                        spacing_percentage=Decimal("2"),
                        levels_above=3,
                        levels_below=3,
                        order_size_usd=Decimal("15"),
                    ),
                    "ETH": CoinGridConfig(
                        spacing_percentage=Decimal("0.5"),
                        levels_above=5,
                        levels_below=5,
                        order_size_usd=Decimal("20"),
                    ),
                },
            )
        )
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path for c in prop.changes}
        assert "grid.coins.DOGE.order_size_usd" in paths
        assert "grid.coins.ETH.order_size_usd" in paths

        doge = next(c for c in prop.changes if c.yaml_path == "grid.coins.DOGE.order_size_usd")
        assert doge.proposed_value == Decimal("7.50")
        eth = next(c for c in prop.changes if c.yaml_path == "grid.coins.ETH.order_size_usd")
        assert eth.proposed_value == Decimal("10.00")

    def test_does_not_scale_non_usd_grid_fields(self) -> None:
        """spacing_percentage / levels_above / levels_below stay put."""
        cfg = _full_config()
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path for c in prop.changes}
        assert "grid.default.spacing_percentage" not in paths
        assert "grid.default.levels_above" not in paths
        assert "grid.default.levels_below" not in paths


# --------------------------------------------------------------------- #
# Safety scaling                                                        #
# --------------------------------------------------------------------- #


class TestSafetyScaling:
    def test_scales_three_usd_caps(self) -> None:
        cfg = _full_config(safety=_safety(max_total="100", max_daily="100", max_per_coin="50"))
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path: c for c in prop.changes}
        assert paths["safety.max_total_exposure_usd"].proposed_value == Decimal("50.00")
        assert paths["safety.max_daily_spend_usd"].proposed_value == Decimal("50.00")
        assert paths["safety.max_per_coin_exposure_usd"].proposed_value == Decimal("25.00")

    def test_does_not_scale_count_or_percentage_fields(self) -> None:
        cfg = _full_config(safety=_safety(max_orders=20, max_loss_pct="20"))
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path for c in prop.changes}
        assert "safety.max_orders_per_coin" not in paths
        assert "safety.emergency_stop.max_loss_percentage" not in paths

    def test_min_balance_zero_does_not_appear(self) -> None:
        """A zero min-balance floor stays zero; no change emitted."""
        cfg = _full_config(safety=_safety(min_balance="0"))
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path for c in prop.changes}
        assert "safety.emergency_stop.min_exchange_balance_usd" not in paths

    def test_min_balance_nonzero_scales(self) -> None:
        cfg = _full_config(safety=_safety(min_balance="20"))
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path: c for c in prop.changes}
        assert paths["safety.emergency_stop.min_exchange_balance_usd"].proposed_value == Decimal(
            "10.00"
        )


# --------------------------------------------------------------------- #
# Live scaling                                                          #
# --------------------------------------------------------------------- #


class TestLiveScaling:
    def test_scales_max_session_loss(self) -> None:
        cfg = _full_config(live=_live(max_session_loss="5"))
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("10"),
            current_config=cfg,
        )
        paths = {c.yaml_path: c for c in prop.changes}
        assert paths["live.max_session_loss_usd"].proposed_value == Decimal("0.50")

    def test_no_live_block_emits_no_live_changes(self) -> None:
        cfg = _full_config(live=None)
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path for c in prop.changes}
        assert all(not p.startswith("live.") for p in paths)


# --------------------------------------------------------------------- #
# Harvester scaling                                                     #
# --------------------------------------------------------------------- #


class TestHarvesterScaling:
    def test_scales_all_four_usd_thresholds(self) -> None:
        cfg = _full_config(
            harvester=_harvester(min_liq="200", topup="300", surplus="500", day_cap="1000")
        )
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path: c for c in prop.changes}
        assert paths["harvester.min_exchange_liquidity_usd"].proposed_value == Decimal("100.00")
        assert paths["harvester.topup_threshold_usd"].proposed_value == Decimal("150.00")
        assert paths["harvester.surplus_threshold_usd"].proposed_value == Decimal("250.00")
        assert paths["harvester.max_withdrawal_per_day_usd"].proposed_value == Decimal("500.00")

    def test_preserves_min_topup_surplus_ordering(self) -> None:
        """Scaling by a positive ratio preserves ``min < topup < surplus``."""
        cfg = _full_config(harvester=_harvester())
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("1"),
            current_config=cfg,
        )
        paths = {c.yaml_path: c.proposed_value for c in prop.changes}
        assert (
            paths["harvester.min_exchange_liquidity_usd"]
            < paths["harvester.topup_threshold_usd"]
            < paths["harvester.surplus_threshold_usd"]
        )

    def test_no_harvester_block_emits_no_harvester_changes(self) -> None:
        cfg = _full_config(harvester=None)
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        paths = {c.yaml_path for c in prop.changes}
        assert all(not p.startswith("harvester.") for p in paths)


# --------------------------------------------------------------------- #
# Proposal shape                                                        #
# --------------------------------------------------------------------- #


class TestProposalShape:
    def test_proposal_is_frozen(self) -> None:
        cfg = _full_config()
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        with pytest.raises(Exception):
            prop.scale_factor = Decimal("99")  # type: ignore[misc]

    def test_changes_tuple_is_immutable(self) -> None:
        cfg = _full_config()
        prop = recalibrate(
            current_balance=Decimal("100"),
            target_balance=Decimal("50"),
            current_config=cfg,
        )
        assert isinstance(prop.changes, tuple)

    def test_realistic_full_config_produces_changes(self) -> None:
        """End-to-end shape with every section wired."""
        cfg = _full_config(
            grid=GridConfig(
                default=GridLevels(
                    spacing_percentage=Decimal("1"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("10"),
                ),
                coins={
                    "DOGE": CoinGridConfig(
                        spacing_percentage=Decimal("2"),
                        levels_above=3,
                        levels_below=3,
                        order_size_usd=Decimal("15"),
                    )
                },
            ),
            safety=_safety(min_balance="20"),
            live=_live(),
            harvester=_harvester(),
        )
        prop = recalibrate(
            current_balance=Decimal("99.92"),
            target_balance=Decimal("10"),
            current_config=cfg,
        )
        # Expected sections present:
        paths = {c.yaml_path for c in prop.changes}
        assert "grid.default.order_size_usd" in paths
        assert "grid.coins.DOGE.order_size_usd" in paths
        assert "safety.max_total_exposure_usd" in paths
        assert "safety.emergency_stop.min_exchange_balance_usd" in paths
        assert "live.max_session_loss_usd" in paths
        assert "harvester.min_exchange_liquidity_usd" in paths
        # Sanity-check: 11 USD-knob paths (1 default + 1 coin + 3 safety
        # + 1 floor + 1 live + 4 harvester).
        assert len(prop.changes) == 11

"""Tests for GridConfig — default + per-coin overrides, field validation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from wobblebot.config.grid import CoinGridConfig, GridConfig, GridLevels

pytestmark = pytest.mark.unit


def _default_levels() -> GridLevels:
    return GridLevels(
        spacing_percentage=Decimal("1.0"),
        levels_above=5,
        levels_below=5,
        order_size_usd=Decimal("10.0"),
    )


class TestGridLevelsValidation:
    def test_happy_path(self) -> None:
        levels = _default_levels()
        assert levels.spacing_percentage == Decimal("1.0")
        assert levels.levels_above == 5

    def test_negative_spacing_rejected(self) -> None:
        with pytest.raises(ValidationError, match="spacing_percentage"):
            GridLevels(
                spacing_percentage=Decimal("-1.0"),
                levels_above=5,
                levels_below=5,
                order_size_usd=Decimal("10.0"),
            )

    def test_zero_spacing_rejected(self) -> None:
        with pytest.raises(ValidationError, match="spacing_percentage"):
            GridLevels(
                spacing_percentage=Decimal("0"),
                levels_above=5,
                levels_below=5,
                order_size_usd=Decimal("10.0"),
            )

    def test_zero_levels_above_rejected(self) -> None:
        with pytest.raises(ValidationError, match="levels_above"):
            GridLevels(
                spacing_percentage=Decimal("1.0"),
                levels_above=0,
                levels_below=5,
                order_size_usd=Decimal("10.0"),
            )

    def test_zero_levels_below_rejected(self) -> None:
        with pytest.raises(ValidationError, match="levels_below"):
            GridLevels(
                spacing_percentage=Decimal("1.0"),
                levels_above=5,
                levels_below=0,
                order_size_usd=Decimal("10.0"),
            )

    def test_negative_order_size_rejected(self) -> None:
        with pytest.raises(ValidationError, match="order_size_usd"):
            GridLevels(
                spacing_percentage=Decimal("1.0"),
                levels_above=5,
                levels_below=5,
                order_size_usd=Decimal("-10.0"),
            )

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="order_size_usd"):
            GridLevels.model_validate(
                {
                    "spacing_percentage": "1.0",
                    "levels_above": 5,
                    "levels_below": 5,
                    # order_size_usd missing
                }
            )

    def test_frozen(self) -> None:
        levels = _default_levels()
        with pytest.raises(ValidationError):
            levels.spacing_percentage = Decimal("2.0")  # type: ignore[misc]


class TestCoinGridConfig:
    def test_enabled_defaults_true(self) -> None:
        coin = CoinGridConfig(
            spacing_percentage=Decimal("1.0"),
            levels_above=5,
            levels_below=5,
            order_size_usd=Decimal("10.0"),
        )
        assert coin.enabled is True

    def test_explicit_disabled(self) -> None:
        coin = CoinGridConfig(
            spacing_percentage=Decimal("1.0"),
            levels_above=5,
            levels_below=5,
            order_size_usd=Decimal("10.0"),
            enabled=False,
        )
        assert coin.enabled is False

    def test_inherits_field_validation(self) -> None:
        with pytest.raises(ValidationError, match="spacing_percentage"):
            CoinGridConfig(
                spacing_percentage=Decimal("-0.5"),
                levels_above=5,
                levels_below=5,
                order_size_usd=Decimal("10.0"),
            )


class TestGridConfigForCoin:
    def test_unknown_coin_returns_default_enabled(self) -> None:
        cfg = GridConfig(default=_default_levels())
        result = cfg.for_coin("BTC")
        assert result.spacing_percentage == Decimal("1.0")
        assert result.levels_above == 5
        assert result.enabled is True

    def test_per_coin_override_shadows_default(self) -> None:
        cfg = GridConfig(
            default=_default_levels(),
            coins={
                "DOGE": CoinGridConfig(
                    spacing_percentage=Decimal("2.0"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("15.0"),
                    enabled=True,
                )
            },
        )
        result = cfg.for_coin("DOGE")
        assert result.spacing_percentage == Decimal("2.0")
        assert result.levels_above == 3
        assert result.order_size_usd == Decimal("15.0")
        # Sanity: default unaffected
        assert cfg.default.spacing_percentage == Decimal("1.0")

    def test_per_coin_disabled_returned_verbatim(self) -> None:
        cfg = GridConfig(
            default=_default_levels(),
            coins={
                "ADA": CoinGridConfig(
                    spacing_percentage=Decimal("1.5"),
                    levels_above=4,
                    levels_below=4,
                    order_size_usd=Decimal("12.0"),
                    enabled=False,
                )
            },
        )
        result = cfg.for_coin("ADA")
        assert result.enabled is False

    def test_lookup_is_case_insensitive(self) -> None:
        cfg = GridConfig(
            default=_default_levels(),
            coins={
                "DOGE": CoinGridConfig(
                    spacing_percentage=Decimal("2.0"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("15.0"),
                )
            },
        )
        assert cfg.for_coin("doge").spacing_percentage == Decimal("2.0")
        assert cfg.for_coin("DoGe").spacing_percentage == Decimal("2.0")

    def test_empty_coins_dict_is_valid(self) -> None:
        cfg = GridConfig(default=_default_levels())
        assert cfg.coins == {}

    def test_frozen(self) -> None:
        cfg = GridConfig(default=_default_levels())
        with pytest.raises(ValidationError):
            cfg.coins = {
                "BTC": CoinGridConfig(  # type: ignore[misc]
                    spacing_percentage=Decimal("1.0"),
                    levels_above=5,
                    levels_below=5,
                    order_size_usd=Decimal("10.0"),
                )
            }


class TestSpacingCoversFees:
    """Soak-surfaced finding 2026-05-20: GridConfig now rejects spacing
    settings that can't possibly profit after 2 × maker fee.

    Kraken base-tier maker fee is 0.26%, so each completed cycle pays
    0.52% in fees. Spacing must STRICTLY EXCEED that to leave any
    profit on the cycle. Validation applies to the always-active
    ``default`` and to every enabled per-coin entry.
    """

    def _levels(self, spacing_pct: str) -> GridLevels:
        return GridLevels(
            spacing_percentage=Decimal(spacing_pct),
            levels_above=3,
            levels_below=3,
            order_size_usd=Decimal("10.0"),
        )

    def _coin(self, spacing_pct: str, *, enabled: bool = True) -> CoinGridConfig:
        return CoinGridConfig(
            spacing_percentage=Decimal(spacing_pct),
            levels_above=3,
            levels_below=3,
            order_size_usd=Decimal("10.0"),
            enabled=enabled,
        )

    def test_default_above_minimum_passes(self) -> None:
        # 1.0% spacing on default, no per-coin entries — clean.
        GridConfig(default=self._levels("1.0"))

    def test_default_at_breakeven_fails(self) -> None:
        # Exactly 0.52% spacing = at the fees floor (break-even).
        # Validation refuses at-or-below threshold; profit must be strictly
        # positive.
        with pytest.raises(ValidationError, match="grid.default"):
            GridConfig(default=self._levels("0.52"))

    def test_default_below_breakeven_fails(self) -> None:
        with pytest.raises(ValidationError, match="grid.default"):
            GridConfig(default=self._levels("0.3"))

    def test_per_coin_enabled_below_breakeven_fails(self) -> None:
        # 0.5% spacing on ETH (enabled) — this is the exact bug surfaced
        # 2026-05-20 during the soak's earnings analysis.
        with pytest.raises(ValidationError, match="grid.coins.ETH"):
            GridConfig(
                default=self._levels("1.0"),
                coins={"ETH": self._coin("0.5", enabled=True)},
            )

    def test_per_coin_disabled_below_breakeven_passes(self) -> None:
        # Disabled coins skip the check — operator may be holding a
        # spacing they plan to fix before enabling.
        cfg = GridConfig(
            default=self._levels("1.0"),
            coins={"ETH": self._coin("0.5", enabled=False)},
        )
        assert cfg.coins["ETH"].spacing_percentage == Decimal("0.5")
        assert cfg.coins["ETH"].enabled is False

    def test_mixed_coins_only_offending_enabled_fails(self) -> None:
        # Multiple coins; only the offending enabled one trips the validator.
        with pytest.raises(ValidationError, match="grid.coins.BAD"):
            GridConfig(
                default=self._levels("1.0"),
                coins={
                    "GOOD": self._coin("1.5", enabled=True),
                    "BAD": self._coin("0.3", enabled=True),
                    "ALSO_BAD_BUT_DISABLED": self._coin("0.2", enabled=False),
                },
            )

    def test_error_message_names_specific_coin(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GridConfig(
                default=self._levels("1.0"),
                coins={"ETH": self._coin("0.5", enabled=True)},
            )
        msg = str(exc_info.value)
        assert "ETH" in msg
        assert "0.52" in msg  # the calculated minimum
        assert "maker fee" in msg.lower()

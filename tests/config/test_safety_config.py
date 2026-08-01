"""Tests for SafetyConfig — caps and the ADR-032 cost-basis sell guard."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from wobblebot.config.safety import SafetyConfig, SellGuardConfig

pytestmark = pytest.mark.unit


def _default_sell_guard() -> SellGuardConfig:
    return SellGuardConfig(enabled=True, max_loss_percentage=Decimal("1.0"))


def _default_safety() -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=Decimal("1000.0"),
        max_daily_spend_usd=Decimal("100.0"),
        max_per_coin_exposure_usd=Decimal("200.0"),
        max_orders_per_coin=10,
        sell_guard=_default_sell_guard(),
    )


class TestSafetyConfigHappyPath:
    def test_construction(self) -> None:
        cfg = _default_safety()
        assert cfg.max_total_exposure_usd == Decimal("1000.0")
        assert cfg.max_orders_per_coin == 10
        assert cfg.sell_guard.enabled is True

    def test_sell_guard_defaults_when_absent(self) -> None:
        """ADR-032 migration note: absent config loads clean."""
        cfg = SafetyConfig(
            max_total_exposure_usd=Decimal("1000"),
            max_daily_spend_usd=Decimal("100"),
            max_per_coin_exposure_usd=Decimal("200"),
            max_orders_per_coin=10,
        )
        assert cfg.sell_guard.enabled is True
        assert cfg.sell_guard.max_loss_percentage == Decimal("1.0")


class TestSafetyConfigValidation:
    @pytest.mark.parametrize(
        "field",
        ["max_total_exposure_usd", "max_daily_spend_usd", "max_per_coin_exposure_usd"],
    )
    def test_zero_cap_rejected(self, field: str) -> None:
        kwargs: dict[str, object] = {
            "max_total_exposure_usd": Decimal("1000"),
            "max_daily_spend_usd": Decimal("100"),
            "max_per_coin_exposure_usd": Decimal("200"),
            "max_orders_per_coin": 10,
        }
        kwargs[field] = Decimal("0")
        with pytest.raises(ValidationError, match=field):
            SafetyConfig(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field",
        ["max_total_exposure_usd", "max_daily_spend_usd", "max_per_coin_exposure_usd"],
    )
    def test_negative_cap_rejected(self, field: str) -> None:
        kwargs: dict[str, object] = {
            "max_total_exposure_usd": Decimal("1000"),
            "max_daily_spend_usd": Decimal("100"),
            "max_per_coin_exposure_usd": Decimal("200"),
            "max_orders_per_coin": 10,
        }
        kwargs[field] = Decimal("-1")
        with pytest.raises(ValidationError, match=field):
            SafetyConfig(**kwargs)  # type: ignore[arg-type]

    def test_zero_max_orders_per_coin_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_orders_per_coin"):
            SafetyConfig(
                max_total_exposure_usd=Decimal("1000"),
                max_daily_spend_usd=Decimal("100"),
                max_per_coin_exposure_usd=Decimal("200"),
                max_orders_per_coin=0,
            )

    def test_frozen(self) -> None:
        cfg = _default_safety()
        with pytest.raises(ValidationError):
            cfg.max_total_exposure_usd = Decimal("9999")  # type: ignore[misc]


class TestSellGuardConfigValidation:
    def test_loss_percentage_above_100_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_loss_percentage"):
            SellGuardConfig(enabled=True, max_loss_percentage=Decimal("100.01"))

    def test_loss_percentage_at_100_accepted(self) -> None:
        cfg = SellGuardConfig(enabled=True, max_loss_percentage=Decimal("100"))
        assert cfg.max_loss_percentage == Decimal("100")

    def test_zero_loss_percentage_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_loss_percentage"):
            SellGuardConfig(enabled=True, max_loss_percentage=Decimal("0"))

    def test_disabled_still_validates_fields(self) -> None:
        # Even when disabled, malformed fields should be rejected at parse
        # time so a future flip of `enabled: true` doesn't suddenly fail.
        with pytest.raises(ValidationError, match="max_loss_percentage"):
            SellGuardConfig(enabled=False, max_loss_percentage=Decimal("-5"))

    def test_default_max_loss_percentage(self) -> None:
        cfg = SellGuardConfig()
        assert cfg.enabled is True
        assert cfg.max_loss_percentage == Decimal("1.0")

    def test_frozen(self) -> None:
        cfg = _default_sell_guard()
        with pytest.raises(ValidationError):
            cfg.enabled = False  # type: ignore[misc]

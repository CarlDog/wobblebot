"""Tests for SafetyConfig — caps and emergency-stop validation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig

pytestmark = pytest.mark.unit


def _default_emergency_stop() -> EmergencyStopConfig:
    return EmergencyStopConfig(
        enabled=True,
        max_loss_percentage=Decimal("20.0"),
        min_exchange_balance_usd=Decimal("50.0"),
    )


def _default_safety() -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=Decimal("1000.0"),
        max_daily_spend_usd=Decimal("100.0"),
        max_per_coin_exposure_usd=Decimal("200.0"),
        max_orders_per_coin=10,
        emergency_stop=_default_emergency_stop(),
    )


class TestSafetyConfigHappyPath:
    def test_construction(self) -> None:
        cfg = _default_safety()
        assert cfg.max_total_exposure_usd == Decimal("1000.0")
        assert cfg.max_orders_per_coin == 10
        assert cfg.emergency_stop.enabled is True


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
            "emergency_stop": _default_emergency_stop(),
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
            "emergency_stop": _default_emergency_stop(),
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
                emergency_stop=_default_emergency_stop(),
            )

    def test_missing_emergency_stop_rejected(self) -> None:
        with pytest.raises(ValidationError, match="emergency_stop"):
            SafetyConfig.model_validate(
                {
                    "max_total_exposure_usd": "1000",
                    "max_daily_spend_usd": "100",
                    "max_per_coin_exposure_usd": "200",
                    "max_orders_per_coin": 10,
                }
            )

    def test_frozen(self) -> None:
        cfg = _default_safety()
        with pytest.raises(ValidationError):
            cfg.max_total_exposure_usd = Decimal("9999")  # type: ignore[misc]


class TestEmergencyStopValidation:
    def test_loss_percentage_above_100_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_loss_percentage"):
            EmergencyStopConfig(
                enabled=True,
                max_loss_percentage=Decimal("100.01"),
                min_exchange_balance_usd=Decimal("50"),
            )

    def test_loss_percentage_at_100_accepted(self) -> None:
        cfg = EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal("100"),
            min_exchange_balance_usd=Decimal("50"),
        )
        assert cfg.max_loss_percentage == Decimal("100")

    def test_zero_loss_percentage_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_loss_percentage"):
            EmergencyStopConfig(
                enabled=True,
                max_loss_percentage=Decimal("0"),
                min_exchange_balance_usd=Decimal("50"),
            )

    def test_negative_min_balance_rejected(self) -> None:
        with pytest.raises(ValidationError, match="min_exchange_balance_usd"):
            EmergencyStopConfig(
                enabled=True,
                max_loss_percentage=Decimal("20"),
                min_exchange_balance_usd=Decimal("-1"),
            )

    def test_zero_min_balance_accepted(self) -> None:
        cfg = EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal("20"),
            min_exchange_balance_usd=Decimal("0"),
        )
        assert cfg.min_exchange_balance_usd == Decimal("0")

    def test_disabled_still_validates_fields(self) -> None:
        # Even when disabled, malformed fields should be rejected at parse
        # time so a future flip of `enabled: true` doesn't suddenly fail.
        with pytest.raises(ValidationError, match="max_loss_percentage"):
            EmergencyStopConfig(
                enabled=False,
                max_loss_percentage=Decimal("-5"),
                min_exchange_balance_usd=Decimal("50"),
            )

    def test_frozen(self) -> None:
        cfg = _default_emergency_stop()
        with pytest.raises(ValidationError):
            cfg.enabled = False  # type: ignore[misc]

"""Tests for ``config.cli.ApplicationConfig`` + ``WobbleBotConfig.application``.

``application.mode`` is the single source of truth for the deployment's
trading mode (live / shadow / sandbox) — read by cli/web for the
dashboard mode-badge. Previously this block was unmodeled (silently
ignored); these tests lock in that it's now typed + validated.
"""

from __future__ import annotations

import pytest

from wobblebot.config.cli import ApplicationConfig
from wobblebot.config.loader import WobbleBotConfig

pytestmark = pytest.mark.unit


_BASE_REQUIRED: dict[str, object] = {
    "grid": {
        "default": {
            "spacing_percentage": "1.0",
            "levels_above": 3,
            "levels_below": 3,
            "order_size_usd": "10",
        }
    },
    "safety": {
        "max_total_exposure_usd": "100",
        "max_daily_spend_usd": "100",
        "max_per_coin_exposure_usd": "50",
        "max_orders_per_coin": 10,
        "emergency_stop": {
            "enabled": True,
            "max_loss_percentage": "5",
            "min_exchange_balance_usd": "0",
        },
    },
}


class TestApplicationConfig:
    def test_defaults(self) -> None:
        cfg = ApplicationConfig()
        assert cfg.name == "WobbleBot"
        assert cfg.version == "0.1.0"
        assert cfg.mode == "live"

    @pytest.mark.parametrize("mode", ["live", "shadow", "sandbox"])
    def test_valid_modes(self, mode: str) -> None:
        assert ApplicationConfig(mode=mode).mode == mode  # type: ignore[arg-type]

    def test_invalid_mode_rejected(self) -> None:
        # The old informational vocabulary (production/dev/...) is gone.
        with pytest.raises(Exception):
            ApplicationConfig(mode="production")  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        cfg = ApplicationConfig()
        with pytest.raises(Exception):
            cfg.mode = "shadow"  # type: ignore[misc]


class TestWobbleBotConfigApplicationField:
    def test_default_is_none_when_omitted(self) -> None:
        cfg = WobbleBotConfig.model_validate(_BASE_REQUIRED)
        assert cfg.application is None

    def test_block_present_validates(self) -> None:
        data = {**_BASE_REQUIRED, "application": {"mode": "shadow"}}
        cfg = WobbleBotConfig.model_validate(data)
        assert cfg.application is not None
        assert cfg.application.mode == "shadow"

    def test_invalid_mode_propagates(self) -> None:
        data = {**_BASE_REQUIRED, "application": {"mode": "nope"}}
        with pytest.raises(Exception):
            WobbleBotConfig.model_validate(data)

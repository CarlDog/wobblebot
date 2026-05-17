"""Tests for ``config/llm.py`` + ``WobbleBotConfig.llm`` (Stage 6.1.D)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from wobblebot.config.llm import LLMConfig
from wobblebot.config.loader import WobbleBotConfig, load_config

pytestmark = pytest.mark.unit


# Minimal required-fields scaffold for WobbleBotConfig.model_validate.
# `grid` + `safety` are required; everything else (including `llm`) is
# optional, so a bare top-level dict suffices.
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


# ---------------------------------------------------------------------------
# LLMConfig direct construction
# ---------------------------------------------------------------------------


class TestLLMConfigDefaults:
    def test_empty_block_uses_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.cost.max_spend_per_day_usd == Decimal("1.00")
        assert cfg.cost.max_spend_per_session_usd == Decimal("0.50")
        assert cfg.cost.enforce is True
        assert cfg.retry.max_retries == 3
        assert cfg.retry.initial_backoff_seconds == 1.0
        assert cfg.retry.backoff_multiplier == 2.0

    def test_partial_override_cost_only(self) -> None:
        cfg = LLMConfig.model_validate(
            {"cost": {"max_spend_per_day_usd": "5.00", "max_spend_per_session_usd": "1.00"}}
        )
        assert cfg.cost.max_spend_per_day_usd == Decimal("5.00")
        assert cfg.cost.enforce is True  # default retained
        assert cfg.retry.max_retries == 3  # retry block defaulted

    def test_partial_override_retry_only(self) -> None:
        cfg = LLMConfig.model_validate({"retry": {"max_retries": 5}})
        assert cfg.retry.max_retries == 5
        assert cfg.cost.enforce is True

    def test_frozen(self) -> None:
        cfg = LLMConfig()
        with pytest.raises(Exception):
            cfg.cost = None  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WobbleBotConfig.llm wiring
# ---------------------------------------------------------------------------


class TestWobbleBotConfigLLMField:
    def test_default_is_none_when_omitted(self) -> None:
        cfg = WobbleBotConfig.model_validate(_BASE_REQUIRED)
        assert cfg.llm is None

    def test_block_present_validates(self) -> None:
        data = {
            **_BASE_REQUIRED,
            "llm": {
                "cost": {
                    "max_spend_per_day_usd": "2.00",
                    "max_spend_per_session_usd": "1.00",
                    "enforce": False,
                },
                "retry": {"max_retries": 5},
            },
        }
        cfg = WobbleBotConfig.model_validate(data)
        assert cfg.llm is not None
        assert cfg.llm.cost.max_spend_per_day_usd == Decimal("2.00")
        assert cfg.llm.cost.enforce is False
        assert cfg.llm.retry.max_retries == 5

    def test_empty_block_uses_all_defaults(self) -> None:
        data = {**_BASE_REQUIRED, "llm": {}}
        cfg = WobbleBotConfig.model_validate(data)
        assert cfg.llm is not None
        assert cfg.llm.cost.max_spend_per_day_usd == Decimal("1.00")
        assert cfg.llm.retry.max_retries == 3

    def test_invalid_block_raises(self) -> None:
        data = {
            **_BASE_REQUIRED,
            "llm": {"retry": {"max_retries": -1}},
        }
        with pytest.raises(Exception):
            WobbleBotConfig.model_validate(data)

    def test_negative_session_cap_rejected(self) -> None:
        data = {
            **_BASE_REQUIRED,
            "llm": {"cost": {"max_spend_per_session_usd": "0"}},
        }
        with pytest.raises(Exception):
            WobbleBotConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Round-trip through load_config (filesystem)
# ---------------------------------------------------------------------------


class TestLoadConfigRoundTrip:
    def _write_yaml(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "settings.yml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_no_llm_block_loads_with_llm_none(self, tmp_path: Path) -> None:
        body = yaml.safe_dump(_BASE_REQUIRED)
        cfg = load_config(self._write_yaml(tmp_path, body))
        assert cfg.llm is None

    def test_llm_block_with_overrides_round_trips(self, tmp_path: Path) -> None:
        body = dedent("""\
            grid:
              default:
                spacing_percentage: "1.0"
                levels_above: 3
                levels_below: 3
                order_size_usd: "10"
            safety:
              max_total_exposure_usd: "100"
              max_daily_spend_usd: "100"
              max_per_coin_exposure_usd: "50"
              max_orders_per_coin: 10
              emergency_stop:
                enabled: true
                max_loss_percentage: "5"
                min_exchange_balance_usd: "0"
            llm:
              cost:
                max_spend_per_day_usd: "2.50"
                max_spend_per_session_usd: "0.75"
                enforce: false
              retry:
                max_retries: 5
                initial_backoff_seconds: 0.5
                backoff_multiplier: 3.0
            """)
        cfg = load_config(self._write_yaml(tmp_path, body))
        assert cfg.llm is not None
        assert cfg.llm.cost.max_spend_per_day_usd == Decimal("2.50")
        assert cfg.llm.cost.enforce is False
        assert cfg.llm.retry.max_retries == 5
        assert cfg.llm.retry.initial_backoff_seconds == 0.5

    def test_example_settings_loads_with_llm_block(self) -> None:
        """The committed settings.example.yml carries an `llm:` block that
        must parse against the live schema. A broken example fails CI."""
        example = Path(__file__).resolve().parents[2] / "config" / "settings.example.yml"
        cfg = load_config(example)
        assert cfg.llm is not None
        # Defaults from the example block:
        assert cfg.llm.cost.max_spend_per_day_usd == Decimal("1.00")
        assert cfg.llm.cost.max_spend_per_session_usd == Decimal("0.50")
        assert cfg.llm.cost.enforce is True
        assert cfg.llm.retry.max_retries == 3


# ---------------------------------------------------------------------------
# Profile overrides apply to the llm block
# ---------------------------------------------------------------------------


class TestProfileOverride:
    def test_profile_can_override_caps(self) -> None:
        from wobblebot.config.resolver import resolve_config

        base = {
            **_BASE_REQUIRED,
            "llm": {
                "cost": {
                    "max_spend_per_day_usd": "1.00",
                    "max_spend_per_session_usd": "0.50",
                    "enforce": True,
                }
            },
            "profiles": {"expensive": {"llm": {"cost": {"max_spend_per_day_usd": "10.00"}}}},
        }
        merged = resolve_config(base, profile_name="expensive")
        cfg = WobbleBotConfig.model_validate(merged)
        assert cfg.llm is not None
        assert cfg.llm.cost.max_spend_per_day_usd == Decimal("10.00")
        # Session cap untouched by profile.
        assert cfg.llm.cost.max_spend_per_session_usd == Decimal("0.50")

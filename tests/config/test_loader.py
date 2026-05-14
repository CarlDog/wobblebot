"""Tests for the YAML config loader."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from wobblebot.config.loader import WobbleBotConfig, load_config

pytestmark = pytest.mark.unit


_MINIMAL_VALID_YAML = """\
grid:
  default:
    spacing_percentage: 1.0
    levels_above: 5
    levels_below: 5
    order_size_usd: 10.0
  coins:
    DOGE:
      spacing_percentage: 2.0
      levels_above: 3
      levels_below: 3
      order_size_usd: 15.0
      enabled: true

safety:
  max_total_exposure_usd: 1000.0
  max_daily_spend_usd: 100.0
  max_per_coin_exposure_usd: 200.0
  max_orders_per_coin: 10
  emergency_stop:
    enabled: true
    max_loss_percentage: 20.0
    min_exchange_balance_usd: 50.0
"""


class TestLoadConfig:
    def test_loads_minimal_valid_yaml(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "wobblebot.yml"
        cfg_path.write_text(_MINIMAL_VALID_YAML, encoding="utf-8")

        cfg = load_config(cfg_path)

        assert isinstance(cfg, WobbleBotConfig)
        assert cfg.grid.default.spacing_percentage == Decimal("1.0")
        assert cfg.grid.coins["DOGE"].order_size_usd == Decimal("15.0")
        assert cfg.safety.max_orders_per_coin == 10
        assert cfg.safety.emergency_stop.max_loss_percentage == Decimal("20.0")

    def test_loads_committed_example(self) -> None:
        # The repo's example file should always parse. If this breaks,
        # the example and the schema have drifted apart.
        example = Path(__file__).resolve().parents[2] / "config" / "wobblebot.example.yml"
        cfg = load_config(example)
        assert cfg.grid.default.levels_above == 5
        assert cfg.safety.max_total_exposure_usd == Decimal("1000.0")

    def test_extra_top_level_keys_ignored(self, tmp_path: Path) -> None:
        # The example YAML has application/exchange/logging/database
        # sections that Stage 2.2 doesn't model. They must not break loading.
        cfg_path = tmp_path / "wobblebot.yml"
        cfg_path.write_text(
            _MINIMAL_VALID_YAML + "\nlogging:\n  level: INFO\nfuture_section:\n  unknown: value\n",
            encoding="utf-8",
        )
        cfg = load_config(cfg_path)
        assert cfg.grid.default.levels_above == 5

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            load_config(tmp_path / "does_not_exist.yml")

    def test_non_mapping_root_rejected(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "bad.yml"
        cfg_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_config(cfg_path)

    def test_missing_grid_section_rejected(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "no_grid.yml"
        cfg_path.write_text(
            "safety:\n"
            "  max_total_exposure_usd: 1000\n"
            "  max_daily_spend_usd: 100\n"
            "  max_per_coin_exposure_usd: 200\n"
            "  max_orders_per_coin: 10\n"
            "  emergency_stop:\n"
            "    enabled: true\n"
            "    max_loss_percentage: 20\n"
            "    min_exchange_balance_usd: 50\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="grid"):
            load_config(cfg_path)

    def test_missing_safety_section_rejected(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "no_safety.yml"
        cfg_path.write_text(
            "grid:\n"
            "  default:\n"
            "    spacing_percentage: 1.0\n"
            "    levels_above: 5\n"
            "    levels_below: 5\n"
            "    order_size_usd: 10.0\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="safety"):
            load_config(cfg_path)

    def test_invalid_field_value_propagates_validation_error(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "bad_value.yml"
        cfg_path.write_text(
            _MINIMAL_VALID_YAML.replace("spacing_percentage: 1.0", "spacing_percentage: -1.0"),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="spacing_percentage"):
            load_config(cfg_path)

"""Tests for the per-CLI Pydantic config schemas (audit slice 2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from wobblebot.config.cli import (
    CheckConfig,
    LiveConfig,
    ObserveConfig,
    ShadowConfig,
    SimulateConfig,
    ValidateConfig,
)
from wobblebot.domain.value_objects import Symbol

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Symbol coercion (the shared parser used across configs)
# ---------------------------------------------------------------------------


class TestSymbolCoercion:
    def test_list_of_strings_parses(self) -> None:
        cfg = LiveConfig(symbols=["BTC/USD", "ETH/USD"])
        assert [str(s) for s in cfg.symbols] == ["BTC/USD", "ETH/USD"]

    def test_list_of_symbol_instances_passes_through(self) -> None:
        cfg = LiveConfig(symbols=[Symbol(base="BTC", quote="USD")])
        assert cfg.symbols[0] == Symbol(base="BTC", quote="USD")

    def test_list_of_dicts_parses(self) -> None:
        cfg = LiveConfig(symbols=[{"base": "BTC", "quote": "USD"}])
        assert cfg.symbols[0] == Symbol(base="BTC", quote="USD")

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one entry"):
            LiveConfig(symbols=[])

    def test_malformed_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="BASE/QUOTE"):
            LiveConfig(symbols=["BTCUSD"])  # missing slash

    def test_non_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a list"):
            LiveConfig(symbols="BTC/USD")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LiveConfig
# ---------------------------------------------------------------------------


class TestLiveConfig:
    def test_minimal(self) -> None:
        cfg = LiveConfig(symbols=["BTC/USD"])
        assert cfg.tick_seconds == 5.0
        assert cfg.max_session_loss_usd == Decimal("5")
        assert cfg.log_format == "plain"

    def test_overrides(self) -> None:
        cfg = LiveConfig(
            symbols=["BTC/USD"],
            tick_seconds=10.0,
            max_session_loss_usd=Decimal("2"),
            log_format="json",
        )
        assert cfg.tick_seconds == 10.0
        assert cfg.log_format == "json"

    def test_zero_tick_rejected(self) -> None:
        with pytest.raises(ValidationError, match="tick_seconds"):
            LiveConfig(symbols=["BTC/USD"], tick_seconds=0)

    def test_negative_loss_cap_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_session_loss_usd"):
            LiveConfig(symbols=["BTC/USD"], max_session_loss_usd=Decimal("-1"))

    def test_invalid_log_format_rejected(self) -> None:
        with pytest.raises(ValidationError, match="log_format"):
            LiveConfig(symbols=["BTC/USD"], log_format="csv")  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        cfg = LiveConfig(symbols=["BTC/USD"])
        with pytest.raises(ValidationError):
            cfg.tick_seconds = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ShadowConfig
# ---------------------------------------------------------------------------


class TestShadowConfig:
    def test_minimal(self) -> None:
        cfg = ShadowConfig(
            symbols=["BTC/USD"],
            initial_balances={"USD": Decimal("10000")},
        )
        assert cfg.maker_fee_rate == Decimal("0.0026")
        assert cfg.taker_fee_rate == Decimal("0.0040")
        assert cfg.initial_balances["USD"] == Decimal("10000")

    def test_balances_must_include_usd(self) -> None:
        with pytest.raises(ValidationError, match="USD entry"):
            ShadowConfig(symbols=["BTC/USD"], initial_balances={"BTC": Decimal("0.5")})

    def test_negative_fee_rate_rejected(self) -> None:
        with pytest.raises(ValidationError, match="maker_fee_rate"):
            ShadowConfig(
                symbols=["BTC/USD"],
                initial_balances={"USD": Decimal("100")},
                maker_fee_rate=Decimal("-0.001"),
            )


# ---------------------------------------------------------------------------
# ObserveConfig
# ---------------------------------------------------------------------------


class TestObserveConfig:
    def test_minimal(self) -> None:
        cfg = ObserveConfig(symbols=["BTC/USD"])
        assert cfg.price_interval_seconds == 30.0
        assert cfg.balance_interval_seconds == 600.0

    def test_balance_interval_zero_allowed(self) -> None:
        cfg = ObserveConfig(symbols=["BTC/USD"], balance_interval_seconds=0)
        assert cfg.balance_interval_seconds == 0

    def test_price_interval_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="price_interval_seconds"):
            ObserveConfig(symbols=["BTC/USD"], price_interval_seconds=0)


# ---------------------------------------------------------------------------
# Single-symbol configs
# ---------------------------------------------------------------------------


class TestSingleSymbolConfigs:
    def test_validate_config(self) -> None:
        cfg = ValidateConfig(symbol="BTC/USD")
        assert cfg.symbol == Symbol(base="BTC", quote="USD")

    def test_check_config(self) -> None:
        cfg = CheckConfig(symbol="ETH/USD")
        assert cfg.symbol == Symbol(base="ETH", quote="USD")

    def test_validate_rejects_malformed_symbol(self) -> None:
        with pytest.raises(ValidationError, match="BASE/QUOTE"):
            ValidateConfig(symbol="BTCUSD")


# ---------------------------------------------------------------------------
# SimulateConfig
# ---------------------------------------------------------------------------


class TestSimulateConfig:
    def test_defaults(self) -> None:
        cfg = SimulateConfig()
        assert cfg.db == "data/wobblebot-sim.db"
        assert cfg.log_format == "plain"

    def test_db_override(self) -> None:
        cfg = SimulateConfig(db="custom.db")
        assert cfg.db == "custom.db"

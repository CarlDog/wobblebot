"""Tests for the per-CLI Pydantic config schemas (audit slice 2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from wobblebot.config.cli import (
    LiveConfig,
    ObserveConfig,
    PreflightConfig,
    SandboxConfig,
    ShadowConfig,
    StatusConfig,
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

    def test_dead_mans_switch_default_on(self) -> None:
        # ADR-021: on by default at 60s.
        cfg = LiveConfig(symbols=["BTC/USD"])
        assert cfg.dead_mans_switch_seconds == 60

    def test_dead_mans_switch_null_disables(self) -> None:
        cfg = LiveConfig(symbols=["BTC/USD"], dead_mans_switch_seconds=None)
        assert cfg.dead_mans_switch_seconds is None

    def test_dead_mans_switch_below_floor_rejected(self) -> None:
        # tick 5.0 -> floor max(10, 2*5) = 10; 9 is below it.
        with pytest.raises(ValidationError, match="dead_mans_switch_seconds"):
            LiveConfig(symbols=["BTC/USD"], dead_mans_switch_seconds=9)

    def test_dead_mans_switch_floor_scales_with_tick(self) -> None:
        # tick 30.0 -> floor max(10, 2*30) = 60; 50 is below it, 60 is OK.
        with pytest.raises(ValidationError, match="dead_mans_switch_seconds"):
            LiveConfig(symbols=["BTC/USD"], tick_seconds=30.0, dead_mans_switch_seconds=50)
        ok = LiveConfig(symbols=["BTC/USD"], tick_seconds=30.0, dead_mans_switch_seconds=60)
        assert ok.dead_mans_switch_seconds == 60

    def test_dead_mans_switch_explicit_value_accepted(self) -> None:
        cfg = LiveConfig(symbols=["BTC/USD"], dead_mans_switch_seconds=120)
        assert cfg.dead_mans_switch_seconds == 120

    def test_frozen(self) -> None:
        cfg = LiveConfig(symbols=["BTC/USD"])
        with pytest.raises(ValidationError):
            cfg.tick_seconds = 1.0  # type: ignore[misc]

    def test_max_runtime_default_bounded(self) -> None:
        """Default remains 60 minutes — long-running mode is opt-in."""
        cfg = LiveConfig(symbols=["BTC/USD"])
        assert cfg.max_runtime_minutes == 60.0

    def test_max_runtime_none_means_unbounded(self) -> None:
        """Stage 3.6a: ``None`` is the type-system-honest way to express
        'run indefinitely.' The loop check in cli/live skips the elapsed
        comparison when the resolved seconds is None."""
        cfg = LiveConfig(symbols=["BTC/USD"], max_runtime_minutes=None)
        assert cfg.max_runtime_minutes is None

    def test_max_runtime_zero_still_rejected(self) -> None:
        """``0`` remains invalid because the loop check is ``>=``; allowing
        zero would cause the daemon to exit on tick 1. Operators wanting
        'no cap' use ``None`` explicitly."""
        with pytest.raises(ValidationError, match="max_runtime_minutes"):
            LiveConfig(symbols=["BTC/USD"], max_runtime_minutes=0)

    def test_max_runtime_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_runtime_minutes"):
            LiveConfig(symbols=["BTC/USD"], max_runtime_minutes=-5)


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

    def test_max_runtime_none_means_unbounded(self) -> None:
        """Stage 3.6a: ShadowConfig matches LiveConfig — operators want
        to soak shadow runs across multi-day windows for backtest-style
        sweeps without bumping the cap to a sentinel like 999999."""
        cfg = ShadowConfig(
            symbols=["BTC/USD"],
            initial_balances={"USD": Decimal("10000")},
            max_runtime_minutes=None,
        )
        assert cfg.max_runtime_minutes is None

    def test_max_runtime_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_runtime_minutes"):
            ShadowConfig(
                symbols=["BTC/USD"],
                initial_balances={"USD": Decimal("100")},
                max_runtime_minutes=0,
            )


# ---------------------------------------------------------------------------
# ObserveConfig
# ---------------------------------------------------------------------------


class TestObserveConfig:
    def test_minimal(self) -> None:
        """ObserveConfig no longer carries interval fields — those live
        in the top-level `schedules:` block per Stage 3.3 Slice C.0."""
        cfg = ObserveConfig(symbols=["BTC/USD"])
        assert [str(s) for s in cfg.symbols] == ["BTC/USD"]
        assert cfg.db == "data/wobblebot-observe.db"
        assert cfg.log_format == "plain"


# ---------------------------------------------------------------------------
# Single-symbol configs
# ---------------------------------------------------------------------------


class TestSingleSymbolConfigs:
    def test_validate_config(self) -> None:
        cfg = PreflightConfig(symbol="BTC/USD")
        assert cfg.symbol == Symbol(base="BTC", quote="USD")

    def test_check_config(self) -> None:
        cfg = StatusConfig(symbol="ETH/USD")
        assert cfg.symbol == Symbol(base="ETH", quote="USD")

    def test_validate_rejects_malformed_symbol(self) -> None:
        with pytest.raises(ValidationError, match="BASE/QUOTE"):
            PreflightConfig(symbol="BTCUSD")


# ---------------------------------------------------------------------------
# SandboxConfig
# ---------------------------------------------------------------------------


class TestSandboxConfig:
    def test_defaults(self) -> None:
        cfg = SandboxConfig()
        assert cfg.db == "data/wobblebot-sim.db"
        assert cfg.log_format == "plain"

    def test_db_override(self) -> None:
        cfg = SandboxConfig(db="custom.db")
        assert cfg.db == "custom.db"

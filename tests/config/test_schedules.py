"""Tests for SchedulesConfig + parse_duration (Stage 3.3 Slice C.0)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from wobblebot.config.schedules import SchedulesConfig, parse_duration

pytestmark = pytest.mark.unit


class TestParseDuration:
    @pytest.mark.parametrize(
        "raw,expected_seconds",
        [
            ("30s", 30),
            ("30S", 30),
            ("10m", 600),
            ("4h", 14400),
            ("7d", 604800),
            ("0s", 0),
            ("0", 0),
            ("30", 30),
            ("1.5h", 5400),
            (30, 30),
            (30.5, 30.5),
        ],
    )
    def test_valid_inputs(self, raw: str | int | float, expected_seconds: float) -> None:
        result = parse_duration(raw)
        assert result.total_seconds() == pytest.approx(expected_seconds)

    def test_passes_timedelta_through(self) -> None:
        td = timedelta(minutes=5)
        assert parse_duration(td) == td

    def test_negative_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("-5m")

    def test_negative_number_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            parse_duration(-1)

    def test_negative_timedelta_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            parse_duration(timedelta(seconds=-1))

    def test_malformed_string_rejected(self) -> None:
        for bad in ("30x", "abc", "30 minutes", "30m5s"):
            with pytest.raises(ValueError, match="parse duration"):
                parse_duration(bad)

    def test_non_string_non_number_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be"):
            parse_duration([30])  # type: ignore[arg-type]


class TestSchedulesConfig:
    def test_empty_valid(self) -> None:
        cfg = SchedulesConfig(root={})
        assert cfg.root == {}

    def test_parses_yaml_style_mapping(self) -> None:
        cfg = SchedulesConfig.model_validate(
            {
                "observe_prices": "30s",
                "observe_balances": "10m",
                "news": "30m",
                "advise": "4h",
            }
        )
        assert cfg.get("observe_prices") == timedelta(seconds=30)
        assert cfg.get("observe_balances") == timedelta(minutes=10)
        assert cfg.get("news") == timedelta(minutes=30)
        assert cfg.get("advise") == timedelta(hours=4)

    def test_get_missing_raises_with_hint(self) -> None:
        cfg = SchedulesConfig(root={})
        with pytest.raises(KeyError, match="not configured"):
            cfg.get("does_not_exist")

    def test_get_or_default_falls_back(self) -> None:
        cfg = SchedulesConfig(root={"news": timedelta(minutes=5)})
        assert cfg.get_or_default("news", timedelta(hours=99)) == timedelta(minutes=5)
        assert cfg.get_or_default("missing", timedelta(seconds=3)) == timedelta(seconds=3)

    def test_invalid_duration_in_root_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            SchedulesConfig.model_validate({"news": "not-a-duration"})

    def test_zero_seconds_accepted_for_disabled_semantics(self) -> None:
        cfg = SchedulesConfig.model_validate({"observe_balances": "0s"})
        assert cfg.get("observe_balances") == timedelta(0)

    def test_none_root_becomes_empty(self) -> None:
        # YAML can render an empty section as None when key has no value.
        cfg = SchedulesConfig.model_validate(None)
        assert cfg.root == {}

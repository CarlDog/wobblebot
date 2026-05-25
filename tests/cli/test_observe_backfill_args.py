"""Tests for cli/observe --backfill argument parsing.

Slice 4 of the v1.1 backfill feature. Covers:
- ``_parse_date_arg`` accepts bare dates + full ISO 8601, defaults to UTC.
- ``_parse_interval_arg`` accepts 1m/5m/15m/30m/1h/4h/1d/1w + bare minutes.
- The integration smoke (full backfill flow against a stub adapter) is
  out of scope for this file -- the backfill service has its own
  service-level tests in tests/services/test_backfill.py.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timezone

import pytest

from wobblebot.cli.observe import _parse_date_arg, _parse_interval_arg
from wobblebot.domain.value_objects import OHLCBar

pytestmark = pytest.mark.unit


class TestParseDateArg:
    def test_bare_date_becomes_midnight_utc(self) -> None:
        parsed = _parse_date_arg("2026-04-01")
        assert parsed == datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)

    def test_full_iso_with_z_suffix(self) -> None:
        parsed = _parse_date_arg("2026-04-01T12:00:00Z")
        assert parsed.tzinfo is not None
        assert parsed == datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)

    def test_full_iso_with_offset(self) -> None:
        """An offset in CDT (-05:00) is preserved by fromisoformat; the
        caller can still compare against UTC datetimes."""
        parsed = _parse_date_arg("2026-04-01T07:00:00-05:00")
        # 07:00 CDT == 12:00 UTC
        assert parsed == datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_date_arg("not-a-date")


class TestParseIntervalArg:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1m", 1),
            ("5m", 5),
            ("15m", 15),
            ("30m", 30),
            ("1h", 60),
            ("4h", 240),
            ("1d", 1440),
            ("1w", 10080),
            # Bare minute counts
            ("1", 1),
            ("5", 5),
            ("60", 60),
            ("1440", 1440),
        ],
    )
    def test_accepts_kraken_aligned_values(self, raw: str, expected: int) -> None:
        assert _parse_interval_arg(raw) == expected

    @pytest.mark.parametrize("raw", ["7m", "2h", "10d", "100", "0"])
    def test_rejects_intervals_kraken_does_not_publish(self, raw: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="allowed set"):
            _parse_interval_arg(raw)

    @pytest.mark.parametrize("raw", ["abc", "5x", "", "  "])
    def test_rejects_garbage(self, raw: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_interval_arg(raw)

    def test_uppercase_suffix_accepted(self) -> None:
        """Case-insensitive — operator typing ``1M`` shouldn't be rejected."""
        assert _parse_interval_arg("1M") == 1
        assert _parse_interval_arg("1H") == 60

    def test_whitespace_tolerated(self) -> None:
        assert _parse_interval_arg(" 1h ") == 60

    def test_canonical_set_aligns_with_OHLCBar(self) -> None:
        """The parser's accepted set must equal OHLCBar.ALLOWED_INTERVALS
        so the adapter and CLI agree on what's legal."""
        # Spot-check: every parser-accepted suffix maps to a value in the
        # OHLCBar set.
        for raw in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
            assert _parse_interval_arg(raw) in OHLCBar.ALLOWED_INTERVALS

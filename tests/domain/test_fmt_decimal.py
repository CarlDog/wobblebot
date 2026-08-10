"""``fmt_decimal`` — operator-facing Decimal rendering (P3 logging 2).

Storage and Kraken hand back full-scale Decimals, so a bare ``%s`` in a
money log line prints ``342.18000000`` — and for round numbers, the
genuinely dangerous ``1E+2``. These pin both the readability fix and the
precision rule that stops it becoming a correctness bug.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from wobblebot.domain.value_objects import fmt_decimal

pytestmark = pytest.mark.unit


class TestReadability:
    def test_strips_storage_scale(self) -> None:
        assert fmt_decimal(Decimal("342.18000000")) == "342.18"

    def test_round_value_never_renders_as_exponent(self) -> None:
        """``1E+2`` in a withdrawal line is a real misread risk."""
        assert fmt_decimal(Decimal("1E+2")) == "100"
        assert fmt_decimal(Decimal("100")) == "100"

    def test_plain_value_unchanged(self) -> None:
        assert fmt_decimal(Decimal("0.2")) == "0.2"

    def test_negative_survives(self) -> None:
        assert fmt_decimal(Decimal("-15.500")) == "-15.5"

    def test_zero(self) -> None:
        assert fmt_decimal(Decimal("0")) == "0"


class TestPrecisionIsNotLost:
    def test_small_asset_amount_keeps_every_digit(self) -> None:
        """The reason this does NOT quantize to 2dp.

        A fixed money scale would render a real BTC quantity as
        ``0.00`` — turning a formatting helper into a lie about how
        much was traded.
        """
        assert fmt_decimal(Decimal("0.00008428")) == "0.00008428"

    def test_never_rounds_a_significant_digit_away(self) -> None:
        assert fmt_decimal(Decimal("0.000000001")) == "0.000000001"

    @pytest.mark.parametrize(
        "raw",
        ["342.18000000", "0.00008428", "1E+2", "-15.500", "0", "99999999.99999999"],
    )
    def test_round_trips_to_the_same_value(self, raw: str) -> None:
        """Display-only: the number must survive unchanged."""
        assert Decimal(fmt_decimal(Decimal(raw))) == Decimal(raw)

"""``fmt_decimal`` — operator-facing Decimal rendering (P3 logging 2).

Storage and Kraken hand back full-scale Decimals, so a bare ``%s`` in a
money log line prints ``342.18000000`` — and for round numbers, the
genuinely dangerous ``1E+2``. These pin both the readability fix and the
precision rule that stops it becoming a correctness bug.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from wobblebot.domain.value_objects import fmt_decimal, fmt_qty, fmt_usd

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


class TestFmtUsd:
    """One house rendering for USD across web, Discord, and logs-adjacent
    strings. The motivating defect: fixed 2dp collapsed DOGE's grid
    levels (3% apart) into the same "$0.09" — the ladder was illegible."""

    def test_dollar_and_above_gets_cents_and_separators(self) -> None:
        assert fmt_usd(Decimal("63237.60")) == "$63,237.60"
        assert fmt_usd(Decimal("1")) == "$1.00"
        assert fmt_usd(Decimal("12.7918")) == "$12.79"

    def test_sub_dollar_keeps_four_significant_digits(self) -> None:
        """Cents are the wrong resolution below $1. These three DOGE grid
        levels rendered identically under %.2f; now they are distinct."""
        assert fmt_usd(Decimal("0.0857")) == "$0.0857"
        assert fmt_usd(Decimal("0.0883")) == "$0.0883"
        assert fmt_usd(Decimal("0.0910")) == "$0.091"

    def test_fee_scale_values_survive(self) -> None:
        assert fmt_usd(Decimal("0.0169")) == "$0.0169"

    def test_signed_marks_gains_and_losses(self) -> None:
        assert fmt_usd(Decimal("0.13"), signed=True) == "+$0.13"
        assert fmt_usd(Decimal("-5"), signed=True) == "-$5.00"

    def test_zero_is_unsigned_even_when_signed(self) -> None:
        """A zero PnL is neither a gain nor a loss."""
        assert fmt_usd(Decimal("0"), signed=True) == "$0.00"
        assert fmt_usd(Decimal("0")) == "$0.00"

    def test_unsigned_negative_still_carries_the_minus(self) -> None:
        assert fmt_usd(Decimal("-3.5")) == "-$3.50"

    def test_floats_do_not_leak_binary_tails(self) -> None:
        """Route/template code hands floats; 0.1 must not become
        $0.1000000000000000055511151231257827."""
        assert fmt_usd(0.0698) == "$0.0698"
        assert fmt_usd(63237.6) == "$63,237.60"

    def test_sub_dollar_never_drops_below_two_decimals(self) -> None:
        """Strip false precision, but money still reads as money: a PnL
        of exactly 80 cents is "$0.80", never "$0.8"."""
        assert fmt_usd(Decimal("0.1")) == "$0.10"
        assert fmt_usd(Decimal("0.25")) == "$0.25"
        assert fmt_usd(Decimal("0.8"), signed=True) == "+$0.80"


class TestFmtQty:
    def test_caps_at_eight_decimals(self) -> None:
        assert fmt_qty(Decimal("0.0012961900")) == "0.00129619"

    def test_strips_trailing_zeros(self) -> None:
        """0.01 beats 0.01000000 in a table an operator scans."""
        assert fmt_qty(Decimal("0.01000000")) == "0.01"

    def test_whole_quantities_render_plain(self) -> None:
        assert fmt_qty(Decimal("336.29202852")) == "336.29202852"
        assert fmt_qty(Decimal("100")) == "100"

    def test_accepts_floats(self) -> None:
        assert fmt_qty(0.1861092314) == "0.18610923"

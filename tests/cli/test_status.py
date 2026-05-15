"""Unit tests for ``Symbol.from_string`` — the canonical symbol parser.

This file used to test ``cli.status._parse_symbol``, an inline helper
that was removed during the config-consolidation audit. Symbol
parsing now lives on the value object itself (single canonical entry
point used by every CLI's Pydantic field validator), so the same
contract is verified here against ``Symbol.from_string``.
"""

from __future__ import annotations

import pytest

from wobblebot.domain.value_objects import Symbol

pytestmark = pytest.mark.unit


class TestSymbolFromString:
    def test_btc_usd(self) -> None:
        assert Symbol.from_string("BTC/USD") == Symbol(base="BTC", quote="USD")

    def test_eth_eur(self) -> None:
        assert Symbol.from_string("ETH/EUR") == Symbol(base="ETH", quote="EUR")

    def test_lowercase_normalized_by_symbol_validator(self) -> None:
        # Symbol's validator uppercases automatically.
        assert Symbol.from_string("btc/usd") == Symbol(base="BTC", quote="USD")

    @pytest.mark.parametrize(
        "raw",
        [
            "BTCUSD",  # no separator
            "BTC/USD/EUR",  # too many parts
            "",  # empty
            "/USD",  # missing base
            "BTC/",  # missing quote
            "/",  # both empty
        ],
    )
    def test_rejects_malformed(self, raw: str) -> None:
        with pytest.raises(ValueError, match="BASE/QUOTE"):
            Symbol.from_string(raw)

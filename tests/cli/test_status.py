"""Unit tests for the ``wobblebot.cli.status`` entry point.

The CLI's end-to-end behavior is verified by manual smoke runs and by
``tests/integration/test_kraken_adapter_live.py`` (which exercises the
same adapter + collector path). These unit tests cover the bits the
integration test doesn't: argument parsing and symbol-string handling.
"""

from __future__ import annotations

import pytest

from wobblebot.cli.status import _parse_symbol
from wobblebot.domain.value_objects import Symbol

pytestmark = pytest.mark.unit


class TestParseSymbol:
    def test_btc_usd(self) -> None:
        assert _parse_symbol("BTC/USD") == Symbol(base="BTC", quote="USD")

    def test_eth_eur(self) -> None:
        assert _parse_symbol("ETH/EUR") == Symbol(base="ETH", quote="EUR")

    def test_lowercase_normalized_by_symbol_validator(self) -> None:
        # Symbol's validator uppercases automatically.
        assert _parse_symbol("btc/usd") == Symbol(base="BTC", quote="USD")

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
            _parse_symbol(raw)

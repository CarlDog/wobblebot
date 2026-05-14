"""Tests for KrakenAdapter request signing.

The signing routine is the one piece of crypto in this adapter. It's
covered by a gold case from Kraken's official API documentation:

  https://docs.kraken.com/rest/#section/Authentication

The published example uses a fabricated key + nonce + payload and
gives the resulting signature, so we lock the implementation against
that.
"""

from __future__ import annotations

import pytest

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig

pytestmark = pytest.mark.unit


# Documented example from Kraken's REST API authentication section.
_KRAKEN_DOC_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
)
_KRAKEN_DOC_NONCE = 1616492376594
_KRAKEN_DOC_PAYLOAD: dict[str, str | int] = {
    "nonce": _KRAKEN_DOC_NONCE,
    "ordertype": "limit",
    "pair": "XBTUSD",
    "price": 37500,
    "type": "buy",
    "volume": 1.25,
}
_KRAKEN_DOC_URI_PATH = "/0/private/AddOrder"
_KRAKEN_DOC_EXPECTED_SIG = (
    "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
)


class TestSign:
    def test_matches_kraken_published_example(self) -> None:
        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="public-half", api_secret=_KRAKEN_DOC_SECRET),
        )
        signature = adapter._sign(  # noqa: SLF001 — testing private helper
            uri_path=_KRAKEN_DOC_URI_PATH,
            nonce=_KRAKEN_DOC_NONCE,
            post_data=_KRAKEN_DOC_PAYLOAD,
        )

        assert signature == _KRAKEN_DOC_EXPECTED_SIG

    def test_different_nonce_produces_different_signature(self) -> None:
        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="k", api_secret=_KRAKEN_DOC_SECRET),
        )
        sig_a = adapter._sign(  # noqa: SLF001
            uri_path="/0/private/Balance",
            nonce=1_700_000_000_000,
            post_data={"nonce": 1_700_000_000_000},
        )
        sig_b = adapter._sign(  # noqa: SLF001
            uri_path="/0/private/Balance",
            nonce=1_700_000_000_001,
            post_data={"nonce": 1_700_000_000_001},
        )

        assert sig_a != sig_b

    def test_different_path_produces_different_signature(self) -> None:
        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="k", api_secret=_KRAKEN_DOC_SECRET),
        )
        sig_balance = adapter._sign(  # noqa: SLF001
            uri_path="/0/private/Balance",
            nonce=1_700_000_000_000,
            post_data={"nonce": 1_700_000_000_000},
        )
        sig_orders = adapter._sign(  # noqa: SLF001
            uri_path="/0/private/OpenOrders",
            nonce=1_700_000_000_000,
            post_data={"nonce": 1_700_000_000_000},
        )

        assert sig_balance != sig_orders


class TestMakeNonce:
    def test_strictly_increasing_across_consecutive_calls(self) -> None:
        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="k", api_secret=_KRAKEN_DOC_SECRET),
        )
        nonces = [adapter._make_nonce() for _ in range(100)]  # noqa: SLF001

        assert all(b > a for a, b in zip(nonces[:-1], nonces[1:], strict=True)), (
            "nonces must strictly increase: "
            f"first dip at index "
            f"{next((i for i, (a, b) in enumerate(zip(nonces, nonces[1:])) if b <= a), None)}"
        )

    def test_recovers_when_wall_clock_jumps_backward(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulate NTP correction by feeding a controlled ``time_ns`` sequence."""
        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="k", api_secret=_KRAKEN_DOC_SECRET),
        )
        # ms equivalents: 1000, 1001, 999 (backwards jump), 1002.
        fake_ns_values = iter([1_000_000_000, 1_001_000_000, 999_000_000, 1_002_000_000])
        monkeypatch.setattr(
            "wobblebot.adapters.kraken_exchange.time.time_ns",
            lambda: next(fake_ns_values),
        )

        nonces = [adapter._make_nonce() for _ in range(4)]  # noqa: SLF001

        assert nonces == [1_000, 1_001, 1_002, 1_003]

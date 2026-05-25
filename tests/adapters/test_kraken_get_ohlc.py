"""Unit tests for KrakenAdapter.get_ohlc.

Test seam: httpx.MockTransport. The adapter is constructed with a
mocked client that asserts wire shape (method, path, query params)
and returns canned ``/0/public/OHLC`` envelopes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit


_TEST_SECRET = "c2VjcmV0"  # base64("secret")


def _make_adapter(handler: Callable[[httpx.Request], httpx.Response]) -> KrakenAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.kraken.com", transport=transport)
    return KrakenAdapter(
        config=KrakenConfig(api_key="public-half", api_secret=_TEST_SECRET),
        http_client=client,
    )


# Synthetic two-bar response — covers the wire-shape happy path.
def _canned_two_bar_response(pair_key: str = "XXBTZUSD") -> dict[str, Any]:
    """Two 1m bars at 17:00 + 17:01 UTC for a 79k-ish BTC scenario."""
    return {
        "error": [],
        "result": {
            pair_key: [
                # [time, open, high, low, close, vwap, volume, count]
                [1748191200, "79000.0", "79050.0", "78990.0", "79045.0", "79020.5", "1.234", 42],
                [1748191260, "79045.0", "79100.0", "79030.0", "79080.0", "79065.2", "0.876", 31],
            ],
            "last": 1748191260,
        },
    }


class TestGetOHLCHappyPath:
    @pytest.mark.asyncio
    async def test_btc_usd_returns_parsed_bars(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["pair"] = request.url.params.get("pair")
            captured["interval"] = request.url.params.get("interval")
            return httpx.Response(200, json=_canned_two_bar_response())

        adapter = _make_adapter(handler)
        try:
            bars = await adapter.get_ohlc(Symbol(base="BTC", quote="USD"), interval_minutes=1)
        finally:
            await adapter.aclose()

        assert captured["path"] == "/0/public/OHLC"
        assert captured["pair"] == "XBTUSD"
        assert captured["interval"] == "1"
        assert len(bars) == 2
        assert bars[0].open == Decimal("79000.0")
        assert bars[0].close == Decimal("79045.0")
        assert bars[0].volume == Decimal("1.234")
        assert bars[0].count == 42
        assert bars[0].opened_at.tzinfo == UTC
        assert bars[1].opened_at > bars[0].opened_at

    @pytest.mark.asyncio
    async def test_doge_uses_xdgusd_altname(self) -> None:
        """Same Kraken altname translation as get_current_price."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["pair"] = request.url.params.get("pair")
            return httpx.Response(200, json=_canned_two_bar_response("XDGUSD"))

        adapter = _make_adapter(handler)
        try:
            bars = await adapter.get_ohlc(Symbol(base="DOGE", quote="USD"))
        finally:
            await adapter.aclose()

        assert captured["pair"] == "XDGUSD"
        assert len(bars) == 2

    @pytest.mark.asyncio
    async def test_since_passed_as_unix_timestamp(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["since"] = request.url.params.get("since")
            return httpx.Response(200, json=_canned_two_bar_response())

        adapter = _make_adapter(handler)
        since = datetime(2026, 5, 25, 17, 0, 0, tzinfo=UTC)
        try:
            await adapter.get_ohlc(
                Symbol(base="BTC", quote="USD"),
                interval_minutes=1,
                since=since,
            )
        finally:
            await adapter.aclose()

        # 2026-05-25 17:00:00 UTC = unix 1748192400
        assert captured["since"] == str(int(since.timestamp()))

    @pytest.mark.asyncio
    async def test_default_interval_is_one_minute(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["interval"] = request.url.params.get("interval")
            return httpx.Response(200, json=_canned_two_bar_response())

        adapter = _make_adapter(handler)
        try:
            await adapter.get_ohlc(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

        assert captured["interval"] == "1"


class TestGetOHLCValidation:
    @pytest.mark.asyncio
    async def test_unsupported_interval_raises_before_request(self) -> None:
        """Validate at the adapter boundary so a bad interval doesn't
        burn a network roundtrip just to get Kraken's error back."""
        request_fired = False

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal request_fired
            request_fired = True
            return httpx.Response(200, json=_canned_two_bar_response())

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ValueError, match="interval_minutes must be one of"):
                await adapter.get_ohlc(Symbol(base="BTC", quote="USD"), interval_minutes=7)
        finally:
            await adapter.aclose()
        assert not request_fired

    @pytest.mark.asyncio
    async def test_naive_since_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_canned_two_bar_response())

        adapter = _make_adapter(handler)
        naive = datetime(2026, 5, 25, 17, 0, 0)  # no tzinfo
        try:
            with pytest.raises(ValueError, match="timezone-aware"):
                await adapter.get_ohlc(
                    Symbol(base="BTC", quote="USD"),
                    interval_minutes=1,
                    since=naive,
                )
        finally:
            await adapter.aclose()


class TestGetOHLCErrors:
    @pytest.mark.asyncio
    async def test_kraken_error_envelope_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="EQuery:Unknown asset pair"):
                await adapter.get_ohlc(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_empty_result_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": [], "result": {}})

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="no OHLC data"):
                await adapter.get_ohlc(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_malformed_bar_entry_raises(self) -> None:
        """If Kraken's wire format evolves and we get a too-short bar,
        fail fast at the parse boundary rather than wedge mid-stream."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "XXBTZUSD": [
                            [1748191200, "79000.0", "79050.0"],  # missing fields
                        ],
                        "last": 1748191200,
                    },
                },
            )

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="unexpected shape"):
                await adapter.get_ohlc(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()


class TestGetOHLCEmptyWindow:
    @pytest.mark.asyncio
    async def test_no_bars_in_window_returns_empty_list(self) -> None:
        """When ``since`` is in the future or the window has no
        activity, Kraken returns the pair key with an empty list and a
        ``last`` cursor. Our adapter passes through as ``[]``."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": [], "result": {"XXBTZUSD": [], "last": 1748191200}},
            )

        adapter = _make_adapter(handler)
        try:
            bars = await adapter.get_ohlc(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()
        assert bars == []

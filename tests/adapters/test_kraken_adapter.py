"""Unit tests for the KrakenAdapter read paths.

The test seam is ``httpx.MockTransport``. Each test wires an
``httpx.AsyncClient`` against a request handler that asserts the wire
shape (method, path, query params, headers, signed payload) and
returns a canned Kraken envelope. The adapter is constructed with
``http_client=`` set to that mocked client — its real ``__init__``
branch building its own AsyncClient is exercised separately.

What unit tests cover:
- Symbol → Kraken altname translation (BTC→XBT, DOGE→XDG, identity).
- Balance entry parsing from BalanceEx wire shape.
- ``get_current_price`` happy path + empty-result error.
- ``get_balances`` happy path including the X/Z asset code remapping.
- ``get_balance`` found / not-found cases.
- ``_unwrap_envelope`` error surface (Kraken error array, HTTP error,
  malformed JSON, missing result field).

What unit tests don't cover (lives in integration test against real API):
- Actual signing being accepted by Kraken.
- Real Balance / Ticker / BalanceEx response shapes still matching.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import (
    KrakenAdapter,
    _symbol_to_kraken_altname,
)
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit


# Valid base64 (just "secret") — signing needs to decode but the actual
# signature isn't verified by the mocked transport.
_TEST_SECRET = "c2VjcmV0"


def _make_adapter(handler: Callable[[httpx.Request], httpx.Response]) -> KrakenAdapter:
    """Wire a KrakenAdapter against an httpx.MockTransport using ``handler``."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.kraken.com", transport=transport)
    return KrakenAdapter(
        config=KrakenConfig(api_key="public-half", api_secret=_TEST_SECRET),
        http_client=client,
    )


class TestSymbolToKrakenAltname:
    """``_symbol_to_kraken_altname`` is a pure function; test it directly."""

    def test_btc_usd_maps_to_xbtusd(self) -> None:
        assert _symbol_to_kraken_altname(Symbol(base="BTC", quote="USD")) == "XBTUSD"

    def test_doge_usd_maps_to_xdgusd(self) -> None:
        assert _symbol_to_kraken_altname(Symbol(base="DOGE", quote="USD")) == "XDGUSD"

    def test_eth_usd_is_identity(self) -> None:
        assert _symbol_to_kraken_altname(Symbol(base="ETH", quote="USD")) == "ETHUSD"

    def test_ada_usd_is_identity(self) -> None:
        assert _symbol_to_kraken_altname(Symbol(base="ADA", quote="USD")) == "ADAUSD"

    def test_btc_eur(self) -> None:
        assert _symbol_to_kraken_altname(Symbol(base="BTC", quote="EUR")) == "XBTEUR"


@pytest.mark.asyncio
class TestGetCurrentPrice:
    async def test_btc_usd_happy_path(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["pair"] = request.url.params.get("pair")
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "XXBTZUSD": {
                            "a": ["79035.20000", "1", "1.000"],
                            "b": ["79035.10000", "1", "1.000"],
                            "c": ["79033.80000", "0.00156715"],
                            "v": ["98.21298882", "1602.79390704"],
                            "p": ["79448.11928", "79717.97007"],
                            "t": [5634, 51349],
                            "l": ["78995.20000", "78720.90000"],
                            "h": ["79664.90000", "81277.00000"],
                            "o": "79292.10000",
                        }
                    },
                },
            )

        adapter = _make_adapter(handler)
        try:
            price = await adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

        assert captured == {"method": "GET", "path": "/0/public/Ticker", "pair": "XBTUSD"}
        assert price.amount == Decimal("79033.80000")
        assert price.currency == "USD"

    async def test_doge_usd_uses_xdgusd_altname(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["pair"] = request.url.params.get("pair")
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {"XDGUSD": {"c": ["0.42100", "100.0"]}},
                },
            )

        adapter = _make_adapter(handler)
        try:
            price = await adapter.get_current_price(Symbol(base="DOGE", quote="USD"))
        finally:
            await adapter.aclose()

        assert captured["pair"] == "XDGUSD"
        assert price.amount == Decimal("0.42100")

    async def test_kraken_error_envelope_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="EQuery:Unknown asset pair"):
                await adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

    async def test_empty_result_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": [], "result": {}})

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="no ticker data"):
                await adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
class TestGetBalances:
    async def test_happy_path_translates_kraken_codes(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["api_key"] = request.headers.get("API-Key")
            captured["has_api_sign"] = "API-Sign" in request.headers
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "XXBT": {"balance": "1.50000000", "hold_trade": "0.30000000"},
                        "ZUSD": {"balance": "10000.0000", "hold_trade": "0.0000"},
                        "ADA": {"balance": "500.00000000", "hold_trade": "0.00000000"},
                    },
                },
            )

        adapter = _make_adapter(handler)
        try:
            balances = await adapter.get_balances()
        finally:
            await adapter.aclose()

        assert captured["method"] == "POST"
        assert captured["path"] == "/0/private/BalanceEx"
        assert captured["api_key"] == "public-half"
        assert captured["has_api_sign"] is True

        by_asset = {b.asset: b for b in balances}
        assert set(by_asset) == {"BTC", "USD", "ADA"}

        btc = by_asset["BTC"]
        assert btc.total == Decimal("1.50000000")
        assert btc.locked == Decimal("0.30000000")
        assert btc.available == Decimal("1.20000000")

        usd = by_asset["USD"]
        assert usd.total == Decimal("10000.0000")
        assert usd.locked == Decimal("0")
        assert usd.available == Decimal("10000.0000")

    async def test_kraken_permission_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": ["EAPI:Invalid key"], "result": {}})

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="EAPI:Invalid key"):
                await adapter.get_balances()
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
class TestGetBalance:
    async def test_returns_balance_when_held(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "XXBT": {"balance": "0.50", "hold_trade": "0"},
                        "ZUSD": {"balance": "100", "hold_trade": "0"},
                    },
                },
            )

        adapter = _make_adapter(handler)
        try:
            btc = await adapter.get_balance("BTC")
        finally:
            await adapter.aclose()

        assert btc is not None
        assert btc.asset == "BTC"
        assert btc.total == Decimal("0.50")

    async def test_returns_none_when_never_held(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {"ZUSD": {"balance": "100", "hold_trade": "0"}},
                },
            )

        adapter = _make_adapter(handler)
        try:
            sol = await adapter.get_balance("SOL")
        finally:
            await adapter.aclose()

        assert sol is None

    async def test_asset_lookup_case_insensitive(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {"XXBT": {"balance": "0.50", "hold_trade": "0"}},
                },
            )

        adapter = _make_adapter(handler)
        try:
            result = await adapter.get_balance("btc")
        finally:
            await adapter.aclose()

        assert result is not None
        assert result.asset == "BTC"


@pytest.mark.asyncio
class TestEnvelopeUnwrapping:
    """Failure modes of the response envelope. Cover via get_current_price."""

    async def test_http_5xx_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="HTTP 500"):
                await adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

    async def test_non_json_body_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="non-JSON"):
                await adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

    async def test_missing_result_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": []})

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="missing 'result'"):
                await adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

    async def test_transport_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated network failure")

        adapter = _make_adapter(handler)
        try:
            with pytest.raises(ExchangeError, match="transport failure"):
                await adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        finally:
            await adapter.aclose()

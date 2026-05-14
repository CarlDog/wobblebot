"""Kraken API drift detector — hits live public endpoints and asserts the shapes our adapter assumes.

**Purpose.** Catch silent breakage when Kraken changes a response. Our
adapter parses specific fields from `Ticker`, `AssetPairs`, `Assets`,
and `SystemStatus`. If any of those fields disappears or changes type,
this test fails loudly and tells us *exactly* what drifted before the
production adapter does the same in a less friendly way.

**Scope.**
- Public endpoints only. No auth required, no money at risk.
- Forward-compatible: extra fields in the response are fine. We only
  assert that the fields we *depend on* are present and the right shape.
- Slow + network-bound: marked ``integration`` and ``slow``. Not run
  by ``pytest`` default; opt in with ``pytest -m integration``.

**Maintenance.** When the adapter starts depending on a new field,
add it to the assertion here. When Kraken removes a field we depend
on, this test fails — we fix the adapter, then this test, in that
order.

Cross-reference: ``docs/reference/kraken-api-reference.md`` documents
the shapes verified here. The "Live verification" date in that doc's
header should be bumped whenever this test is re-run successfully.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]

_KRAKEN_BASE_URL = "https://api.kraken.com"
_REQUEST_TIMEOUT_SECONDS = 10.0


def _unwrap(envelope: dict[str, Any], endpoint: str) -> dict[str, Any]:
    """Assert the response envelope is shaped ``{"error": [], "result": {...}}`` and return result.

    Kraken's error array is non-empty when the request fails. Any
    non-empty error array is treated as a failure here — even
    seemingly innocuous warnings — because adapter logic shouldn't
    silently ignore them in production either.
    """
    assert isinstance(envelope, dict), f"{endpoint} returned non-dict {type(envelope).__name__}"
    assert "error" in envelope, f"{endpoint} response missing 'error' key"
    assert "result" in envelope, f"{endpoint} response missing 'result' key"
    assert envelope["error"] == [], f"{endpoint} returned errors: {envelope['error']}"
    result = envelope["result"]
    assert isinstance(result, dict), f"{endpoint} result is not a dict"
    return result


async def _get_json(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(path)
    response.raise_for_status()
    payload: Any = response.json()
    assert isinstance(payload, dict), f"{path} returned non-object JSON: {type(payload).__name__}"
    return payload


@pytest_asyncio.fixture
async def kraken_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=_KRAKEN_BASE_URL,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "wobblebot-api-health-check/0.1"},
    ) as client:
        yield client


class TestSystemStatus:
    async def test_envelope_and_required_fields(self, kraken_client: httpx.AsyncClient) -> None:
        payload = await _get_json(kraken_client, "/0/public/SystemStatus")
        result = _unwrap(payload, "SystemStatus")

        assert "status" in result, "SystemStatus.result missing 'status'"
        assert "timestamp" in result, "SystemStatus.result missing 'timestamp'"
        assert isinstance(result["status"], str)
        assert isinstance(result["timestamp"], str)


class TestAssetPairs:
    """Assert AssetPairs exposes the fields our adapter reads.

    The adapter consults AssetPairs at startup to build the asset alias
    table (DOGE → XDG and similar) and to learn per-pair precision
    (pair_decimals, lot_decimals) for order quantization.
    """

    _REQUIRED_FIELDS: list[str] = [
        "altname",
        "wsname",
        "base",
        "quote",
        "pair_decimals",
        "lot_decimals",
        "cost_decimals",
        "ordermin",
        "costmin",
        "tick_size",
        "status",
    ]

    async def test_xbtusd_has_required_fields(self, kraken_client: httpx.AsyncClient) -> None:
        payload = await _get_json(kraken_client, "/0/public/AssetPairs?pair=XBTUSD")
        result = _unwrap(payload, "AssetPairs")

        assert "XXBTZUSD" in result, (
            "AssetPairs?pair=XBTUSD should key the response by 'XXBTZUSD' (X/Z prefix form); "
            f"got keys: {list(result.keys())}"
        )
        pair = result["XXBTZUSD"]
        missing = [f for f in self._REQUIRED_FIELDS if f not in pair]
        assert not missing, f"XXBTZUSD missing fields our adapter needs: {missing}"

        # Spot-check field types match what the adapter expects.
        assert isinstance(pair["pair_decimals"], int)
        assert isinstance(pair["lot_decimals"], int)
        assert isinstance(pair["cost_decimals"], int)
        assert isinstance(pair["altname"], str)
        assert isinstance(pair["base"], str)
        assert isinstance(pair["quote"], str)

    async def test_dogeusd_aliases_to_xdgusd(self, kraken_client: httpx.AsyncClient) -> None:
        """The DOGE/XDG asymmetry is documented in the adapter; lock it.

        If Kraken ever renames XDG to DOGE (they could), this test
        catches it on the next run and the alias-table logic gets a
        free entry.
        """
        payload = await _get_json(kraken_client, "/0/public/AssetPairs?pair=XDGUSD")
        result = _unwrap(payload, "AssetPairs?pair=XDGUSD")

        assert (
            "XDGUSD" in result
        ), f"DOGE pair key drift detected. Expected 'XDGUSD', got: {list(result.keys())}"
        pair = result["XDGUSD"]
        assert (
            pair["base"] == "XXDG"
        ), f"DOGE base code drift. Expected 'XXDG', got {pair['base']!r}"


class TestTicker:
    """Assert Ticker has the field used as the adapter's 'current price' source."""

    async def test_xbtusd_has_required_fields(self, kraken_client: httpx.AsyncClient) -> None:
        payload = await _get_json(kraken_client, "/0/public/Ticker?pair=XBTUSD")
        result = _unwrap(payload, "Ticker")

        assert "XXBTZUSD" in result, f"Ticker keys: {list(result.keys())}"
        ticker = result["XXBTZUSD"]

        # 'c' is documented as [last_trade_price, lot_volume]. c[0] is the
        # adapter's current-price field.
        assert "c" in ticker, "Ticker.c (last trade) is the adapter's price source — missing"
        assert isinstance(ticker["c"], list) and len(ticker["c"]) >= 1
        last_price_str = ticker["c"][0]
        assert isinstance(last_price_str, str)
        # Must parse as a positive decimal.
        from decimal import Decimal

        last_price = Decimal(last_price_str)
        assert last_price > 0, f"Ticker last-trade price non-positive: {last_price_str!r}"


class TestAssets:
    """Assert per-asset precision fields the adapter consults for display/format."""

    async def test_xbt_and_usd_have_required_fields(self, kraken_client: httpx.AsyncClient) -> None:
        payload = await _get_json(kraken_client, "/0/public/Assets?asset=XBT,USD")
        result = _unwrap(payload, "Assets")

        assert "XXBT" in result, f"Assets keys: {list(result.keys())}"
        assert "ZUSD" in result, f"Assets keys: {list(result.keys())}"

        for code in ("XXBT", "ZUSD"):
            asset = result[code]
            for field in ("altname", "decimals", "display_decimals", "status"):
                assert field in asset, f"Assets[{code}] missing {field!r}"
            assert isinstance(asset["decimals"], int)
            assert isinstance(asset["display_decimals"], int)

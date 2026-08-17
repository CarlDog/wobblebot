"""ADR-038 — get_fee_rates across the three exchange adapters."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit

BTC_USD = Symbol(base="BTC", quote="USD")


class TestKrakenGetFeeRates:
    @pytest.mark.asyncio
    async def test_parses_tradevolume_positionally(self) -> None:
        """Kraken keys the response by its INTERNAL pair name (XXBTZUSD
        for an XBTUSD request) — parsing must be positional. Fixture
        mirrors the live 2026-08-17 probe response shape."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            body = request.content.decode()
            captured["body"] = body
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "currency": "ZUSD",
                        "volume": "74.9400",
                        "fees": {"XXBTZUSD": {"fee": "0.8000", "tiervolume": "0.00000"}},
                        "fees_maker": {"XXBTZUSD": {"fee": "0.4000"}},
                    },
                },
            )

        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="k", api_secret="c2VjcmV0"),
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="https://api.kraken.com"
            ),
        )
        rates = await adapter.get_fee_rates(BTC_USD)
        assert captured["path"] == "/0/private/TradeVolume"
        assert "pair=XBTUSD" in captured["body"]
        assert rates.maker == Decimal("0.004")
        assert rates.taker == Decimal("0.008")
        assert rates.symbol == BTC_USD
        await adapter.aclose()

    @pytest.mark.asyncio
    async def test_missing_fee_data_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": [], "result": {"volume": "0"}})

        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="k", api_secret="c2VjcmV0"),
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="https://api.kraken.com"
            ),
        )
        with pytest.raises(ExchangeError, match="no fee data"):
            await adapter.get_fee_rates(BTC_USD)
        await adapter.aclose()


class TestMockAndShadowFeeRates:
    @pytest.mark.asyncio
    async def test_mock_returns_flat_rate(self) -> None:
        adapter = MockExchangeAdapter(fee_rate=Decimal("0.001"))
        rates = await adapter.get_fee_rates(BTC_USD)
        assert rates.maker == rates.taker == Decimal("0.001")

    @pytest.mark.asyncio
    async def test_shadow_echoes_configured_model(self) -> None:
        adapter = ShadowExchangeAdapter(
            live_exchange=MockExchangeAdapter(),
            starting_balances={"USD": Decimal("100")},
            maker_fee_rate=Decimal("0.004"),
            taker_fee_rate=Decimal("0.008"),
        )
        rates = await adapter.get_fee_rates(BTC_USD)
        assert rates.maker == Decimal("0.004")
        assert rates.taker == Decimal("0.008")

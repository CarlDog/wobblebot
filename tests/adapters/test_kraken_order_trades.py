"""Unit tests for ``KrakenAdapter.get_order_trades`` (ADR-046).

Test seam is ``httpx.MockTransport``. Public metadata endpoints return
canned data; ``QueryOrders`` and ``QueryTrades`` are scripted per test
and every request body is captured so the wire shape (``trades=true``,
comma-joined ``txid`` chunks of at most 20) is asserted, not assumed.

Field names follow docs.kraken.com (Get Orders Info / Get Trades Info,
read 2026-09-18). They are NOT a captured live response — the pre-deploy
live check with the trader key is what confirms them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import _QUERY_TRADES_MAX_IDS, KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_TEST_SECRET = "c2VjcmV0"  # base64("secret")
BTC_USD = Symbol(base="BTC", quote="USD")
ORDER_TXID = "OLG4OV-BXTHW-T6IS2H"

_ASSETS: dict[str, Any] = {
    "error": [],
    "result": {
        "XXBT": {"altname": "XBT", "decimals": 10, "display_decimals": 5, "status": "enabled"},
        "ZUSD": {"altname": "USD", "decimals": 4, "display_decimals": 2, "status": "enabled"},
    },
}
_ASSETPAIRS: dict[str, Any] = {
    "error": [],
    "result": {
        "XXBTZUSD": {
            "altname": "XBTUSD",
            "wsname": "XBT/USD",
            "base": "XXBT",
            "quote": "ZUSD",
            "pair_decimals": 1,
            "lot_decimals": 8,
            "ordermin": "0.0001",
            "costmin": "0.5",
            "status": "online",
        }
    },
}


def _order(exchange_id: str | None = ORDER_TXID) -> Order:
    order = Order(
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("50000"), currency="USD"),
        amount=Amount(value=Decimal("0.002"), asset="BTC"),
        created_at=Timestamp(dt=datetime(2026, 9, 10, 12, 47, tzinfo=UTC)),
    )
    if exchange_id is not None:
        order.mark_open(exchange_id)
    return order


def _trade_entry(ordertxid: str, vol: str, time: float) -> dict[str, Any]:
    return {
        "ordertxid": ordertxid,
        "postxid": "TKH2SE-M7IF5-CFI7LT",
        "pair": "XXBTZUSD",
        "time": time,
        "type": "buy",
        "ordertype": "limit",
        "price": "50000.0",
        "cost": str(Decimal("50000.0") * Decimal(vol)),
        "fee": "0.02",
        "vol": vol,
        "margin": "0.00000",
        "misc": "",
    }


class _Script:
    """Scripted private endpoints plus a request-body log for assertions."""

    def __init__(
        self,
        *,
        query_orders: dict[str, Any] | None,
        query_trades: list[dict[str, Any]] | None = None,
    ) -> None:
        self.query_orders = query_orders
        self.query_trades = list(query_trades or [])
        self.bodies: list[tuple[str, dict[str, str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/0/public/Assets":
            return httpx.Response(200, json=_ASSETS)
        if path == "/0/public/AssetPairs":
            return httpx.Response(200, json=_ASSETPAIRS)
        body = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        self.bodies.append((path, body))
        if path == "/0/private/QueryOrders":
            return httpx.Response(200, json={"error": [], "result": self.query_orders or {}})
        if path == "/0/private/QueryTrades":
            page = self.query_trades.pop(0) if self.query_trades else {}
            return httpx.Response(200, json={"error": [], "result": page})
        raise AssertionError(f"unexpected request to {path}")


def _adapter(script: _Script) -> KrakenAdapter:
    client = httpx.AsyncClient(
        base_url="https://api.kraken.com", transport=httpx.MockTransport(script.handler)
    )
    return KrakenAdapter(
        config=KrakenConfig(api_key="public-half", api_secret=_TEST_SECRET),
        http_client=client,
    )


def _order_entry(trades: Any = None, *, include_key: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "status": "closed",
        "vol": "0.002",
        "vol_exec": "0.002",
        "descr": {"pair": "XXBTZUSD", "type": "buy", "price": "50000.0"},
        "opentm": 1789044400.0,
    }
    if include_key:
        entry["trades"] = trades
    return {ORDER_TXID: entry}


class TestGetOrderTrades:
    async def test_happy_path_asks_for_trades_and_returns_them_oldest_first(self) -> None:
        script = _Script(
            query_orders=_order_entry(["TSECOND-000000-000002", "TFIRST0-000000-000001"]),
            query_trades=[
                {
                    "TSECOND-000000-000002": _trade_entry(ORDER_TXID, "0.001", 1789044434.5),
                    "TFIRST0-000000-000001": _trade_entry(ORDER_TXID, "0.001", 1789044433.9),
                }
            ],
        )
        adapter = _adapter(script)

        trades = await adapter.get_order_trades(_order())

        assert [t.id for t in trades] == ["TFIRST0-000000-000001", "TSECOND-000000-000002"]
        assert all(t.order_id == ORDER_TXID for t in trades)
        assert sum(t.amount.value for t in trades) == Decimal("0.002")
        paths = [p for p, _ in script.bodies]
        assert paths == ["/0/private/QueryOrders", "/0/private/QueryTrades"]
        query_orders_body = script.bodies[0][1]
        assert query_orders_body["txid"] == ORDER_TXID
        assert query_orders_body["trades"] == "true"
        query_trades_body = script.bodies[1][1]
        assert set(query_trades_body["txid"].split(",")) == {
            "TSECOND-000000-000002",
            "TFIRST0-000000-000001",
        }

    async def test_no_trade_ids_yet_returns_empty_without_calling_query_trades(self) -> None:
        script = _Script(query_orders=_order_entry([]))
        adapter = _adapter(script)

        assert await adapter.get_order_trades(_order()) == []
        assert [p for p, _ in script.bodies] == ["/0/private/QueryOrders"]

    async def test_trades_key_absent_returns_empty(self) -> None:
        # "if trades info requested and data available" — Kraken may omit
        # the key entirely rather than send an empty list.
        script = _Script(query_orders=_order_entry(include_key=False))
        adapter = _adapter(script)

        assert await adapter.get_order_trades(_order()) == []

    async def test_trades_null_returns_empty(self) -> None:
        script = _Script(query_orders=_order_entry(None))
        adapter = _adapter(script)

        assert await adapter.get_order_trades(_order()) == []

    async def test_foreign_ordertxid_is_filtered_out(self) -> None:
        script = _Script(
            query_orders=_order_entry(["TX000000-000000-000001"]),
            query_trades=[
                {"TX000000-000000-000001": _trade_entry("OOTHER0-000000-000000", "0.002", 1.0)}
            ],
        )
        adapter = _adapter(script)

        assert await adapter.get_order_trades(_order()) == []

    async def test_more_than_twenty_ids_are_chunked(self) -> None:
        ids = [f"T{i:06d}-000000-000001" for i in range(_QUERY_TRADES_MAX_IDS + 1)]
        first_page = {
            tid: _trade_entry(ORDER_TXID, "0.0001", float(i)) for i, tid in enumerate(ids[:20])
        }
        second_page = {ids[20]: _trade_entry(ORDER_TXID, "0.0001", 99.0)}
        script = _Script(query_orders=_order_entry(ids), query_trades=[first_page, second_page])
        adapter = _adapter(script)

        trades = await adapter.get_order_trades(_order())

        assert len(trades) == _QUERY_TRADES_MAX_IDS + 1
        query_trades_calls = [b for p, b in script.bodies if p == "/0/private/QueryTrades"]
        assert [len(b["txid"].split(",")) for b in query_trades_calls] == [20, 1]

    async def test_dry_run_order_returns_empty_with_no_requests(self) -> None:
        script = _Script(query_orders=None)
        adapter = _adapter(script)

        assert await adapter.get_order_trades(_order("DRYRUN-abc")) == []
        assert script.bodies == []

    async def test_missing_exchange_id_raises(self) -> None:
        adapter = _adapter(_Script(query_orders=None))

        with pytest.raises(ExchangeError, match="no exchange_id"):
            await adapter.get_order_trades(_order(exchange_id=None))

    async def test_missing_order_entry_raises(self) -> None:
        adapter = _adapter(_Script(query_orders={}))

        with pytest.raises(ExchangeError, match="missing entry"):
            await adapter.get_order_trades(_order())

    async def test_non_list_trades_field_raises(self) -> None:
        adapter = _adapter(_Script(query_orders=_order_entry("TX000000-000000-000001")))

        with pytest.raises(ExchangeError, match="not a list"):
            await adapter.get_order_trades(_order())

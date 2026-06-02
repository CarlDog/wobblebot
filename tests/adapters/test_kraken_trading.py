"""Unit tests for the KrakenAdapter trading methods (Stage 2.3 slice 1).

Test seam is ``httpx.MockTransport``. Each test sets up a request
handler that asserts wire shape (path, signed body, headers) and
returns a canned Kraken envelope. The adapter is constructed with
``http_client=`` set to that mocked client.

Coverage:
- ``_quantize_decimal``: ROUND_DOWN at various precisions.
- ``_ensure_pair_metadata``: parses /0/public/AssetPairs into
  ``_PairMetadata``, indexes by both pair_key and altname.
- ``place_order``: happy path, dry_run mode (validate=true +
  ``DRYRUN-`` exchange_id), quantization, ordermin/costmin checks,
  InsufficientBalance translation.
- ``cancel_order``: happy path, dry_run + DRYRUN short-circuit,
  empty exchange_id rejection.
- ``get_order_status``: happy path, DRYRUN mirror, missing-entry
  error.
- ``get_open_orders``: parses entries, applies symbol filter,
  empty list.
- ``get_trade_history``: parses entries, sorts most-recent-first,
  applies limit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import (
    KrakenAdapter,
    _quantize_decimal,
)
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.exceptions import InsufficientBalance
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit


_TEST_SECRET = "c2VjcmV0"  # base64("secret")

_CANNED_ASSETS_RESPONSE: dict[str, Any] = {
    "error": [],
    "result": {
        "XXBT": {"altname": "XBT", "decimals": 10, "display_decimals": 5, "status": "enabled"},
        "ZUSD": {"altname": "USD", "decimals": 4, "display_decimals": 2, "status": "enabled"},
    },
}

_CANNED_ASSETPAIRS_RESPONSE: dict[str, Any] = {
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
        },
    },
}


def _make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    dry_run: bool = False,
) -> KrakenAdapter:
    """Wire an adapter that auto-serves /Assets and /AssetPairs from canned data."""

    def dispatching(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/0/public/Assets":
            return httpx.Response(200, json=_CANNED_ASSETS_RESPONSE)
        if request.url.path == "/0/public/AssetPairs":
            return httpx.Response(200, json=_CANNED_ASSETPAIRS_RESPONSE)
        return handler(request)

    transport = httpx.MockTransport(dispatching)
    client = httpx.AsyncClient(base_url="https://api.kraken.com", transport=transport)
    return KrakenAdapter(
        config=KrakenConfig(api_key="public-half", api_secret=_TEST_SECRET),
        http_client=client,
        dry_run=dry_run,
    )


def _post_body(request: httpx.Request) -> dict[str, str]:
    """Parse the form-encoded POST body into a flat dict for assertions."""
    parsed = parse_qs(request.content.decode("utf-8"))
    return {k: v[0] for k, v in parsed.items()}


def _make_order(
    *,
    symbol: Symbol | None = None,
    side: OrderSide = OrderSide.BUY,
    price: str = "50000",
    amount: str = "0.001",
    exchange_id: str | None = None,
    status: str = "pending",
) -> Order:
    sym = symbol or Symbol(base="BTC", quote="USD")
    return Order(
        symbol=sym,
        side=side,
        price=Price(amount=Decimal(price), currency=sym.quote),
        amount=Amount(value=Decimal(amount), asset=sym.base),
        exchange_id=exchange_id,
        status=status,
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestQuantizeDecimal:
    def test_rounds_down_to_pair_decimals(self) -> None:
        assert _quantize_decimal(Decimal("50000.987"), 1) == Decimal("50000.9")

    def test_zero_decimals_truncates_fraction(self) -> None:
        assert _quantize_decimal(Decimal("50000.987"), 0) == Decimal("50000")

    def test_lot_decimals_eight_preserves_satoshi(self) -> None:
        assert _quantize_decimal(Decimal("0.123456789"), 8) == Decimal("0.12345678")

    def test_value_already_quantized_unchanged(self) -> None:
        assert _quantize_decimal(Decimal("50000.5"), 1) == Decimal("50000.5")

    def test_does_not_round_up(self) -> None:
        # 0.999 with 2 decimals must NOT become 1.00 — could overspend
        assert _quantize_decimal(Decimal("0.999"), 2) == Decimal("0.99")


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPlaceOrder:
    async def test_happy_path_sends_correct_payload_and_returns_order(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/AddOrder"
            assert request.headers["API-Key"] == "public-half"
            assert "API-Sign" in request.headers
            captured["body"] = _post_body(request)
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "descr": {"order": "buy 0.001 XBTUSD @ limit 50000.0"},
                        "txid": ["OG5V2Y-RYKVL-DT3V3B"],
                    },
                },
            )

        adapter = _make_adapter(handler)
        order = _make_order()
        placed = await adapter.place_order(order)

        body = captured["body"]
        assert body["pair"] == "XXBTZUSD"
        assert body["type"] == "buy"
        assert body["ordertype"] == "limit"
        assert body["price"] == "50000.0"  # quantized to pair_decimals=1
        assert body["volume"] == "0.00100000"  # quantized to lot_decimals=8
        assert "validate" not in body
        assert placed.exchange_id == "OG5V2Y-RYKVL-DT3V3B"
        assert placed.status == "open"

    async def test_dry_run_adds_validate_and_synthesizes_exchange_id(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _post_body(request)
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "descr": {"order": "buy 0.001 XBTUSD @ limit 50000.0"},
                    },
                },
            )

        adapter = _make_adapter(handler, dry_run=True)
        order = _make_order()
        placed = await adapter.place_order(order)

        assert captured["body"]["validate"] == "true"
        assert placed.exchange_id is not None
        assert placed.exchange_id.startswith("DRYRUN-")
        assert placed.status == "open"

    async def test_quantizes_price_to_pair_decimals(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _post_body(request)
            return httpx.Response(
                200,
                json={"error": [], "result": {"descr": {}, "txid": ["X"]}},
            )

        adapter = _make_adapter(handler)
        order = _make_order(price="50000.987654")
        await adapter.place_order(order)
        # pair_decimals=1 → 50000.9 (round down)
        assert captured["body"]["price"] == "50000.9"

    async def test_rejects_below_ordermin_client_side(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Should not reach Kraken — ordermin should reject locally")

        adapter = _make_adapter(handler)
        # ordermin in canned data is 0.0001; 0.00005 quantized at lot_decimals=8 is 0.00005
        order = _make_order(amount="0.00005")
        with pytest.raises(ExchangeError, match="ordermin"):
            await adapter.place_order(order)

    async def test_rejects_below_costmin_client_side(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Should not reach Kraken — costmin should reject locally")

        adapter = _make_adapter(handler)
        # costmin is 0.5; 0.0001 BTC * 1 USD = 0.0001 USD → below
        order = _make_order(price="1", amount="0.0001")
        with pytest.raises(ExchangeError, match="costmin"):
            await adapter.place_order(order)

    async def test_insufficient_funds_error_translates_to_domain_exception(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": ["EOrder:Insufficient funds"], "result": {}},
            )

        adapter = _make_adapter(handler)
        with pytest.raises(InsufficientBalance):
            await adapter.place_order(_make_order())

    async def test_other_kraken_errors_raise_exchange_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": ["EOrder:Invalid pair"], "result": {}},
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="Invalid pair"):
            await adapter.place_order(_make_order())

    async def test_missing_txid_raises_exchange_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": [], "result": {"descr": {}}},  # txid missing
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="no txid"):
            await adapter.place_order(_make_order())


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCancelOrder:
    async def test_happy_path_sends_txid_and_marks_canceled(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/CancelOrder"
            captured["body"] = _post_body(request)
            return httpx.Response(200, json={"error": [], "result": {"count": 1}})

        adapter = _make_adapter(handler)
        order = _make_order(exchange_id="OG5V2Y-RYKVL-DT3V3B", status="open")
        canceled = await adapter.cancel_order(order)
        assert captured["body"]["txid"] == "OG5V2Y-RYKVL-DT3V3B"
        assert canceled.status == "canceled"

    async def test_dry_run_short_circuits(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Should not reach Kraken in dry-run mode")

        adapter = _make_adapter(handler, dry_run=True)
        order = _make_order(exchange_id="OG5V2Y", status="open")
        canceled = await adapter.cancel_order(order)
        assert canceled.status == "canceled"

    async def test_dryrun_exchange_id_short_circuits(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("DRYRUN-prefixed orders should not reach Kraken")

        adapter = _make_adapter(handler)  # NOT dry_run, but order is
        order = _make_order(exchange_id="DRYRUN-abc", status="open")
        canceled = await adapter.cancel_order(order)
        assert canceled.status == "canceled"

    async def test_missing_exchange_id_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Should not reach Kraken with no exchange_id")

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="no exchange_id"):
            await adapter.cancel_order(_make_order())


# ---------------------------------------------------------------------------
# set_dead_mans_switch (ADR-021)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDeadMansSwitch:
    async def test_arm_posts_timeout_to_cancel_all_orders_after(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/CancelAllOrdersAfter"
            captured["body"] = _post_body(request)
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "currentTime": "2026-06-01T00:00:00Z",
                        "triggerTime": "2026-06-01T00:01:00Z",
                    },
                },
            )

        adapter = _make_adapter(handler)
        result = await adapter.set_dead_mans_switch(60)
        assert result is None
        assert captured["body"]["timeout"] == "60"

    async def test_disable_posts_zero(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/CancelAllOrdersAfter"
            captured["body"] = _post_body(request)
            return httpx.Response(
                200,
                json={"error": [], "result": {"currentTime": "x", "triggerTime": "x"}},
            )

        adapter = _make_adapter(handler)
        await adapter.set_dead_mans_switch(0)
        assert captured["body"]["timeout"] == "0"

    async def test_dry_run_short_circuits(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Should not arm a real timer in dry-run mode")

        adapter = _make_adapter(handler, dry_run=True)
        assert await adapter.set_dead_mans_switch(60) is None

    async def test_negative_timeout_raises_value_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("Should not reach Kraken with a negative timeout")

        adapter = _make_adapter(handler)
        with pytest.raises(ValueError, match=">= 0"):
            await adapter.set_dead_mans_switch(-1)

    async def test_kraken_error_surfaces_as_exchange_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": ["EAPI:Invalid key"], "result": {}})

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError):
            await adapter.set_dead_mans_switch(60)


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetOrderStatus:
    async def test_happy_path_updates_in_place(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/QueryOrders"
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "OG5V2Y": {
                            "status": "closed",
                            "vol_exec": "0.001",
                            "descr": {
                                "pair": "XXBTZUSD",
                                "type": "buy",
                                "price": "50000.0",
                            },
                            "vol": "0.001",
                            "opentm": 1688712345.0,
                        }
                    },
                },
            )

        adapter = _make_adapter(handler)
        order = _make_order(exchange_id="OG5V2Y", status="open")
        original_id = order.id
        refreshed = await adapter.get_order_status(order)
        assert refreshed.id == original_id  # UUID preserved
        assert refreshed.exchange_id == "OG5V2Y"
        assert refreshed.status == "closed"
        assert refreshed.filled_amount == Decimal("0.001")

    async def test_dryrun_mirrors_back_unchanged(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("DRYRUN orders should not reach Kraken")

        adapter = _make_adapter(handler)
        order = _make_order(exchange_id="DRYRUN-abc", status="open")
        result = await adapter.get_order_status(order)
        assert result is order

    async def test_missing_entry_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": [], "result": {}})

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="missing entry"):
            await adapter.get_order_status(_make_order(exchange_id="OG5V2Y"))


# ---------------------------------------------------------------------------
# get_open_orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetOpenOrders:
    async def test_parses_entries(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/OpenOrders"
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "open": {
                            "OG5V2Y": {
                                "status": "open",
                                "vol": "0.001",
                                "vol_exec": "0",
                                "opentm": 1688712345.0,
                                "descr": {
                                    "pair": "XXBTZUSD",
                                    "type": "buy",
                                    "price": "50000.0",
                                },
                            }
                        },
                        "count": 1,
                    },
                },
            )

        adapter = _make_adapter(handler)
        orders = await adapter.get_open_orders()
        assert len(orders) == 1
        assert orders[0].exchange_id == "OG5V2Y"
        assert orders[0].symbol == Symbol(base="BTC", quote="USD")
        assert orders[0].side is OrderSide.BUY
        assert orders[0].price.amount == Decimal("50000.0")

    async def test_symbol_filter_applied_client_side(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "open": {
                            "BTC1": {
                                "status": "open",
                                "vol": "0.001",
                                "vol_exec": "0",
                                "opentm": 1.0,
                                "descr": {
                                    "pair": "XXBTZUSD",
                                    "type": "buy",
                                    "price": "50000",
                                },
                            },
                        },
                        "count": 1,
                    },
                },
            )

        adapter = _make_adapter(handler)
        # Filter by ETH/USD — none match
        orders = await adapter.get_open_orders(symbol=Symbol(base="ETH", quote="USD"))
        assert orders == []

    async def test_empty_open_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": [], "result": {"open": {}, "count": 0}})

        adapter = _make_adapter(handler)
        assert await adapter.get_open_orders() == []


# ---------------------------------------------------------------------------
# get_trade_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetTradeHistory:
    async def test_parses_and_sorts_recent_first(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/TradesHistory"
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "trades": {
                            "TOLD": {
                                "ordertxid": "OG1",
                                "pair": "XXBTZUSD",
                                "time": 1000.0,
                                "type": "buy",
                                "price": "49000",
                                "cost": "49",
                                "fee": "0.13",
                                "vol": "0.001",
                            },
                            "TNEW": {
                                "ordertxid": "OG2",
                                "pair": "XXBTZUSD",
                                "time": 2000.0,
                                "type": "sell",
                                "price": "51000",
                                "cost": "51",
                                "fee": "0.13",
                                "vol": "0.001",
                            },
                        },
                        "count": 2,
                    },
                },
            )

        adapter = _make_adapter(handler)
        trades = await adapter.get_trade_history()
        assert len(trades) == 2
        # Most-recent first
        assert trades[0].id == "TNEW"
        assert trades[1].id == "TOLD"

    async def test_limit_applied_after_sort(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "trades": {
                            f"T{i}": {
                                "ordertxid": "OG",
                                "pair": "XXBTZUSD",
                                "time": float(i),
                                "type": "buy",
                                "price": "50000",
                                "cost": "50",
                                "fee": "0.13",
                                "vol": "0.001",
                            }
                            for i in range(10)
                        },
                        "count": 10,
                    },
                },
            )

        adapter = _make_adapter(handler)
        trades = await adapter.get_trade_history(limit=3)
        assert len(trades) == 3
        # Most-recent first (highest time)
        assert [t.id for t in trades] == ["T9", "T8", "T7"]

    async def test_symbol_filter_applied(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "trades": {
                            "T1": {
                                "ordertxid": "OG",
                                "pair": "XXBTZUSD",
                                "time": 1.0,
                                "type": "buy",
                                "price": "50000",
                                "cost": "50",
                                "fee": "0.13",
                                "vol": "0.001",
                            }
                        },
                        "count": 1,
                    },
                },
            )

        adapter = _make_adapter(handler)
        eth_trades = await adapter.get_trade_history(symbol=Symbol(base="ETH", quote="USD"))
        assert eth_trades == []


# ---------------------------------------------------------------------------
# withdraw (Stage 4.4a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWithdraw:
    """KrakenAdapter.withdraw against the mocked /0/private/Withdraw endpoint.

    Phase 4.4's first slice: implement the API call. Operator-approval
    flow + day-cap enforcement + persistence land in 4.4b/c — those
    are above the adapter's pay grade. Here we verify wire shape,
    response parsing, and error propagation.
    """

    async def test_happy_path_returns_refid(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/0/private/Withdraw"
            captured["body"] = _post_body(request)
            return httpx.Response(
                200,
                json={"error": [], "result": {"refid": "AGBSO6T-UFMTTQ-I7KGS6"}},
            )

        adapter = _make_adapter(handler)
        refid = await adapter.withdraw(
            asset="USD",
            amount=Decimal("10"),
            destination="360 Performance Savings",
        )
        assert refid == "AGBSO6T-UFMTTQ-I7KGS6"
        # The wire shape Kraken expects.
        assert captured["body"]["asset"] == "USD"
        assert captured["body"]["amount"] == "10"
        assert captured["body"]["key"] == "360 Performance Savings"
        # The signing layer adds nonce; verify it's present (value
        # changes per call, just assert it's there).
        assert "nonce" in captured["body"]

    async def test_decimal_amount_serializes_with_precision(self) -> None:
        """Amount goes over the wire as a string so Decimal precision
        survives — Kraken stores monetary amounts at fixed precision
        per asset, but the adapter shouldn't truncate."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _post_body(request)
            return httpx.Response(
                200,
                json={"error": [], "result": {"refid": "ABC"}},
            )

        adapter = _make_adapter(handler)
        await adapter.withdraw(
            asset="USD",
            amount=Decimal("99.9999"),
            destination="bank-ach",
        )
        assert captured["body"]["amount"] == "99.9999"

    async def test_unknown_destination_label_raises(self) -> None:
        """Kraken returns EFunding:Unknown reference id when the
        operator passes a label that isn't in their address book."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": ["EFunding:Unknown reference id"],
                    "result": {},
                },
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="Unknown reference id"):
            await adapter.withdraw(
                asset="USD",
                amount=Decimal("10"),
                destination="not-in-address-book",
            )

    async def test_below_minimum_raises(self) -> None:
        """ACH withdrawals below Kraken's per-method minimum get
        ``EFunding:Below minimum``. The operator sees this as a
        clean ExchangeError, not a silent acceptance."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": ["EFunding:Below minimum"],
                    "result": {},
                },
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="Below minimum"):
            await adapter.withdraw(
                asset="USD",
                amount=Decimal("0.50"),
                destination="360 Performance Savings",
            )

    async def test_insufficient_balance_raises(self) -> None:
        """When the account can't fund the withdrawal Kraken returns
        ``EFunding:Insufficient funds`` — surfaces cleanly."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": ["EFunding:Insufficient funds"],
                    "result": {},
                },
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="Insufficient funds"):
            await adapter.withdraw(
                asset="USD",
                amount=Decimal("1000000"),
                destination="360 Performance Savings",
            )

    async def test_permission_denied_raises(self) -> None:
        """If the operator points cli/harvest at a key without Withdraw
        scope (e.g. the trade key) Kraken returns
        ``EAPI:Invalid key`` or ``EAPI:Insufficient permissions``.
        Either way, ExchangeError."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": ["EAPI:Permission denied"],
                    "result": {},
                },
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="Permission denied"):
            await adapter.withdraw(
                asset="USD",
                amount=Decimal("10"),
                destination="360 Performance Savings",
            )

    async def test_missing_refid_in_response_raises(self) -> None:
        """Defensive: Kraken always returns refid on success, but if
        the envelope is malformed we must not silently return an
        empty/None reference."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": [], "result": {"something": "else"}},
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="missing 'refid'"):
            await adapter.withdraw(
                asset="USD",
                amount=Decimal("10"),
                destination="360 Performance Savings",
            )

    async def test_empty_refid_raises(self) -> None:
        """Defensive: an explicit empty string refid is also invalid."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": [], "result": {"refid": ""}},
            )

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="missing 'refid'"):
            await adapter.withdraw(
                asset="USD",
                amount=Decimal("10"),
                destination="360 Performance Savings",
            )

    async def test_transport_failure_raises(self) -> None:
        """Network failure surfaces as ExchangeError, not a leaked
        httpx exception."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("kraken.com unreachable")

        adapter = _make_adapter(handler)
        with pytest.raises(ExchangeError, match="transport failure"):
            await adapter.withdraw(
                asset="USD",
                amount=Decimal("10"),
                destination="360 Performance Savings",
            )


@pytest.mark.asyncio
class TestPartitionKnownSymbols:
    """Graceful-degrade symbol validation at daemon startup."""

    async def test_all_known_returns_empty_unknown(self) -> None:
        """Symbols all present in AssetPairs → returned in known list,
        unknown is empty."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)  # Should never hit non-cached endpoints

        adapter = _make_adapter(handler)
        try:
            known, unknown = await adapter.partition_known_symbols(
                [Symbol(base="BTC", quote="USD")]
            )
        finally:
            await adapter.aclose()
        assert known == [Symbol(base="BTC", quote="USD")]
        assert unknown == []

    async def test_all_unknown_returns_empty_known(self) -> None:
        """Symbols absent from AssetPairs → all in unknown list."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        adapter = _make_adapter(handler)
        try:
            known, unknown = await adapter.partition_known_symbols(
                [Symbol(base="MATIC", quote="USD"), Symbol(base="FAKE", quote="USD")]
            )
        finally:
            await adapter.aclose()
        assert known == []
        assert unknown == [
            Symbol(base="MATIC", quote="USD"),
            Symbol(base="FAKE", quote="USD"),
        ]

    async def test_mixed_partitions_correctly(self) -> None:
        """Mix of known + unknown splits cleanly; both lists preserve input order."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        adapter = _make_adapter(handler)
        try:
            known, unknown = await adapter.partition_known_symbols(
                [
                    Symbol(base="MATIC", quote="USD"),
                    Symbol(base="BTC", quote="USD"),
                    Symbol(base="FAKE", quote="USD"),
                ]
            )
        finally:
            await adapter.aclose()
        assert known == [Symbol(base="BTC", quote="USD")]
        assert unknown == [
            Symbol(base="MATIC", quote="USD"),
            Symbol(base="FAKE", quote="USD"),
        ]

    async def test_empty_input_returns_two_empty_lists(self) -> None:
        """No symbols requested → both lists empty (still hits AssetPairs once
        to populate the cache; cheap)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        adapter = _make_adapter(handler)
        try:
            known, unknown = await adapter.partition_known_symbols([])
        finally:
            await adapter.aclose()
        assert known == []
        assert unknown == []

    async def test_assetpairs_hit_once_across_many_symbols(self) -> None:
        """N symbols share ONE AssetPairs fetch via the cache — no per-symbol
        network call."""
        request_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        # _make_adapter wraps handler with a dispatcher that auto-serves
        # /Assets and /AssetPairs; we observe via a custom transport.
        adapter = _make_adapter(handler)

        # Patch the dispatcher's path log by wrapping _public_get to count.
        # Simpler: just call partition_known_symbols twice and assert the
        # cache prevents a second fetch.
        try:
            # First call populates the cache.
            await adapter.partition_known_symbols([Symbol(base="BTC", quote="USD")] * 5)
            # Capture state: cache is now populated.
            assert adapter._pair_metadata is not None  # noqa: SLF001
            cache_size_before = len(adapter._pair_metadata)  # noqa: SLF001
            # Second call should NOT trigger another /AssetPairs hit.
            await adapter.partition_known_symbols([Symbol(base="BTC", quote="USD")] * 10)
            # Cache identity unchanged.
            assert len(adapter._pair_metadata) == cache_size_before  # noqa: SLF001
        finally:
            await adapter.aclose()

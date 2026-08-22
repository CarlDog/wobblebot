"""Malformed-response handling for KrakenAdapter (2026-08-22 audit).

Kraken's JSON is untyped at the wire, and every coercion into a domain
value can fail on a malformed payload. Per ``ports/exceptions.py`` the
port contract for that is ``ExchangeError`` -- callers catch
``WobbleBotPortError`` and degrade. Before this hardening the parsers
raised bare builtins (``decimal.InvalidOperation``, ``TypeError``,
``KeyError``, ``IndexError``, ``OverflowError``, pydantic
``ValidationError``), none of which subclass ``WobbleBotPortError``, so
they sailed straight through every graceful-degradation handler in the
codebase -- including ``cli/live``'s per-tick isolation, its shutdown
cancel-all, and ``cli/harvest``'s balance read.

The most-likely-to-bite value here is ``"abc"`` in a numeric field:
``Decimal("abc")`` raises ``decimal.InvalidOperation``, which is an
``ArithmeticError`` and NOT a ``ValueError`` -- the exact reason the
pre-existing ``except (KeyError, ValueError)`` guard in
``_ensure_pair_metadata`` had a hole.

Test seam: httpx.MockTransport, same as the sibling adapter tests.
"""

from __future__ import annotations

import decimal
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit

_TEST_SECRET = "c2VjcmV0"  # base64("secret")
BTC_USD = Symbol(base="BTC", quote="USD")


def _make_adapter(handler: Callable[[httpx.Request], httpx.Response]) -> KrakenAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.kraken.com", transport=transport)
    return KrakenAdapter(
        config=KrakenConfig(api_key="public-half", api_secret=_TEST_SECRET),
        http_client=client,
    )


def _json_handler(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json=payload)


def _routing_handler(
    routes: dict[str, dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve a different canned envelope per URL path suffix."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"error": [], "result": {}})

    return handler


_GOOD_ASSETS = {
    "error": [],
    "result": {"XXBT": {"altname": "XBT"}, "ZUSD": {"altname": "USD"}},
}
_GOOD_PAIRS = {
    "error": [],
    "result": {
        "XXBTZUSD": {
            "altname": "XBTUSD",
            "base": "XXBT",
            "quote": "ZUSD",
            "pair_decimals": 1,
            "lot_decimals": 8,
            "ordermin": "0.0001",
            "costmin": "5",
        }
    },
}


class TestTickerParsing:
    """get_current_price / get_ticker are the hot per-tick path; a bare
    exception here kills the live daemon mid-session rather than
    skipping one symbol."""

    @pytest.mark.asyncio
    async def test_non_numeric_price_raises_exchange_error(self) -> None:
        adapter = _make_adapter(
            _json_handler({"error": [], "result": {"XXBTZUSD": {"c": ["abc", "1.0"]}}})
        )
        try:
            with pytest.raises(ExchangeError, match="malformed") as exc_info:
                await adapter.get_current_price(BTC_USD)
            # The cause chain is contractual (ports/exceptions.py) and
            # this is the case a plain `except ValueError` would MISS.
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_missing_close_key_raises_exchange_error(self) -> None:
        adapter = _make_adapter(_json_handler({"error": [], "result": {"XXBTZUSD": {}}}))
        try:
            with pytest.raises(ExchangeError, match="malformed") as exc_info:
                await adapter.get_current_price(BTC_USD)
            assert isinstance(exc_info.value.__cause__, KeyError)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_empty_close_array_raises_exchange_error(self) -> None:
        adapter = _make_adapter(_json_handler({"error": [], "result": {"XXBTZUSD": {"c": []}}}))
        try:
            with pytest.raises(ExchangeError, match="malformed") as exc_info:
                await adapter.get_current_price(BTC_USD)
            assert isinstance(exc_info.value.__cause__, IndexError)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_null_price_raises_exchange_error(self) -> None:
        """dict.get(k, default) returns None when k EXISTS with a JSON
        null, so a default never protects against this."""
        adapter = _make_adapter(
            _json_handler({"error": [], "result": {"XXBTZUSD": {"c": [None, "1.0"]}}})
        )
        try:
            with pytest.raises(ExchangeError, match="malformed") as exc_info:
                await adapter.get_current_price(BTC_USD)
            assert isinstance(exc_info.value.__cause__, TypeError)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_get_ticker_malformed_bid_raises_exchange_error(self) -> None:
        adapter = _make_adapter(
            _json_handler(
                {
                    "error": [],
                    "result": {"XXBTZUSD": {"c": ["1"], "b": ["nope"], "a": ["1"]}},
                }
            )
        )
        try:
            with pytest.raises(ExchangeError, match="malformed"):
                await adapter.get_ticker(BTC_USD)
        finally:
            await adapter.aclose()


class TestOHLCParsing:
    """The timestamp coercion used to sit OUTSIDE the try, and the try
    caught only ValidationError -- so its sibling Decimal() calls
    escaped through it."""

    @pytest.mark.asyncio
    async def test_non_numeric_open_raises_exchange_error(self) -> None:
        adapter = _make_adapter(
            _json_handler(
                {
                    "error": [],
                    "result": {
                        "XXBTZUSD": [[1748191200, "abc", "1", "1", "1", "1", "1", 4]],
                        "last": 1748191200,
                    },
                }
            )
        )
        try:
            with pytest.raises(ExchangeError, match="failed validation") as exc_info:
                await adapter.get_ohlc(BTC_USD, interval_minutes=1)
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_non_numeric_timestamp_raises_exchange_error(self) -> None:
        adapter = _make_adapter(
            _json_handler(
                {
                    "error": [],
                    "result": {
                        "XXBTZUSD": [["not-a-time", "1", "1", "1", "1", "1", "1", 4]],
                        "last": 1748191200,
                    },
                }
            )
        )
        try:
            with pytest.raises(ExchangeError, match="failed validation"):
                await adapter.get_ohlc(BTC_USD, interval_minutes=1)
        finally:
            await adapter.aclose()


class TestPairMetadataParsing:
    """The pre-existing guard caught (KeyError, ValueError) and MISSED
    the InvalidOperation/TypeError its own Decimal()/int() calls raise.
    This cache sits on the path of place_order, get_open_orders and
    get_trade_history."""

    @pytest.mark.asyncio
    async def test_non_numeric_ordermin_raises_exchange_error(self) -> None:
        bad_pairs = {
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "altname": "XBTUSD",
                    "base": "XXBT",
                    "quote": "ZUSD",
                    "pair_decimals": 1,
                    "lot_decimals": 8,
                    "ordermin": "abc",
                    "costmin": "5",
                }
            },
        }
        adapter = _make_adapter(
            _routing_handler({"/Assets": _GOOD_ASSETS, "/AssetPairs": bad_pairs})
        )
        try:
            with pytest.raises(ExchangeError, match="AssetPairs entry") as exc_info:
                await adapter.get_open_orders()
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_null_pair_decimals_raises_exchange_error(self) -> None:
        bad_pairs = {
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "altname": "XBTUSD",
                    "base": "XXBT",
                    "quote": "ZUSD",
                    "pair_decimals": None,
                    "lot_decimals": 8,
                    "ordermin": "0.0001",
                    "costmin": "5",
                }
            },
        }
        adapter = _make_adapter(
            _routing_handler({"/Assets": _GOOD_ASSETS, "/AssetPairs": bad_pairs})
        )
        try:
            with pytest.raises(ExchangeError, match="AssetPairs entry") as exc_info:
                await adapter.get_open_orders()
            assert isinstance(exc_info.value.__cause__, TypeError)
        finally:
            await adapter.aclose()


class TestTradeAndOrderBuilders:
    """The fill-detection path. A bare exception from these is the
    silent-fill-loss shape: cancels have already executed against
    Kraken by the time the trade history is parsed."""

    @pytest.mark.asyncio
    async def test_malformed_trade_raises_exchange_error(self) -> None:
        trades = {
            "error": [],
            "result": {
                "trades": {
                    "TXID-1": {
                        "pair": "XXBTZUSD",
                        "ordertxid": "OID-1",
                        "type": "buy",
                        "price": "abc",
                        "vol": "1",
                        "fee": "0",
                        "cost": "1",
                        "time": 1748191200.0,
                    }
                },
                "count": 1,
            },
        }
        adapter = _make_adapter(
            _routing_handler(
                {
                    "/Assets": _GOOD_ASSETS,
                    "/AssetPairs": _GOOD_PAIRS,
                    "/TradesHistory": trades,
                }
            )
        )
        try:
            with pytest.raises(ExchangeError, match="trade entry") as exc_info:
                await adapter.get_trade_history(symbol=BTC_USD)
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_unknown_trade_side_raises_exchange_error(self) -> None:
        """OrderSide('sideways') raises a plain ValueError, a different
        branch of the tuple than the Decimal cases."""
        trades = {
            "error": [],
            "result": {
                "trades": {
                    "TXID-1": {
                        "pair": "XXBTZUSD",
                        "ordertxid": "OID-1",
                        "type": "sideways",
                        "price": "1",
                        "vol": "1",
                        "fee": "0",
                        "cost": "1",
                        "time": 1748191200.0,
                    }
                },
                "count": 1,
            },
        }
        adapter = _make_adapter(
            _routing_handler(
                {
                    "/Assets": _GOOD_ASSETS,
                    "/AssetPairs": _GOOD_PAIRS,
                    "/TradesHistory": trades,
                }
            )
        )
        try:
            with pytest.raises(ExchangeError, match="trade entry") as exc_info:
                await adapter.get_trade_history(symbol=BTC_USD)
            assert isinstance(exc_info.value.__cause__, ValueError)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_malformed_open_order_raises_exchange_error(self) -> None:
        orders = {
            "error": [],
            "result": {
                "open": {
                    "OID-1": {
                        "descr": {"pair": "XXBTZUSD", "type": "buy", "price": "abc"},
                        "vol": "1",
                        "vol_exec": "0",
                        "opentm": 1748191200.0,
                        "status": "open",
                    }
                }
            },
        }
        adapter = _make_adapter(
            _routing_handler(
                {
                    "/Assets": _GOOD_ASSETS,
                    "/AssetPairs": _GOOD_PAIRS,
                    "/OpenOrders": orders,
                }
            )
        )
        try:
            with pytest.raises(ExchangeError, match="order entry") as exc_info:
                await adapter.get_open_orders()
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_unknown_pair_still_raises_its_own_exchange_error(self) -> None:
        """_symbol_for_pair_key raises ExchangeError itself and sits
        OUTSIDE the parse guard by design -- its specific message must
        survive rather than being re-wrapped as 'malformed'. Pins that
        so a later edit adding ExchangeError to _PARSE_ERRORS can't
        silently mangle it."""
        trades = {
            "error": [],
            "result": {
                "trades": {
                    "TXID-1": {
                        "pair": "WHOKNOWS",
                        "ordertxid": "OID-1",
                        "type": "buy",
                        "price": "1",
                        "vol": "1",
                        "fee": "0",
                        "cost": "1",
                        "time": 1748191200.0,
                    }
                },
                "count": 1,
            },
        }
        adapter = _make_adapter(
            _routing_handler(
                {
                    "/Assets": _GOOD_ASSETS,
                    "/AssetPairs": _GOOD_PAIRS,
                    "/TradesHistory": trades,
                }
            )
        )
        try:
            with pytest.raises(ExchangeError) as exc_info:
                await adapter.get_trade_history(symbol=BTC_USD)
            assert "trade entry" not in str(exc_info.value)
        finally:
            await adapter.aclose()


class TestResidualEscapes:
    """2026-08-22 full-branch review: three escapes the first hardening
    pass missed, each verified with exact breaking inputs."""

    @pytest.mark.asyncio
    async def test_non_dict_trade_entry_raises_exchange_error(self) -> None:
        """A string where a trade-entry dict belongs: .get() on it
        raises bare AttributeError, and the pre-fix guard started
        AFTER those lines."""
        trades = {
            "error": [],
            "result": {"trades": {"TXID-1": "not-a-dict"}, "count": 1},
        }
        adapter = _make_adapter(
            _routing_handler(
                {"/Assets": _GOOD_ASSETS, "/AssetPairs": _GOOD_PAIRS, "/TradesHistory": trades}
            )
        )
        try:
            with pytest.raises(ExchangeError, match="trade entry") as exc_info:
                await adapter.get_trade_history(symbol=BTC_USD)
            assert isinstance(exc_info.value.__cause__, AttributeError)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_non_dict_order_entry_raises_exchange_error(self) -> None:
        orders = {"error": [], "result": {"open": {"OID-1": "not-a-dict"}}}
        adapter = _make_adapter(
            _routing_handler(
                {"/Assets": _GOOD_ASSETS, "/AssetPairs": _GOOD_PAIRS, "/OpenOrders": orders}
            )
        )
        try:
            with pytest.raises(ExchangeError, match="order entry") as exc_info:
                await adapter.get_open_orders()
            assert isinstance(exc_info.value.__cause__, AttributeError)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_unhashable_pair_value_raises_exchange_error(self) -> None:
        """A JSON array as "pair" is a well-formed dict entry whose
        value makes _symbol_for_pair_key's dict lookup raise bare
        TypeError (unhashable) — invisible to any entry-shape check."""
        trades = {
            "error": [],
            "result": {
                "trades": {
                    "TXID-1": {
                        "pair": ["XXBTZUSD"],
                        "ordertxid": "OID-1",
                        "type": "buy",
                        "price": "1",
                        "vol": "1",
                        "fee": "0",
                        "cost": "1",
                        "time": 1748191200.0,
                    }
                },
                "count": 1,
            },
        }
        adapter = _make_adapter(
            _routing_handler(
                {"/Assets": _GOOD_ASSETS, "/AssetPairs": _GOOD_PAIRS, "/TradesHistory": trades}
            )
        )
        try:
            with pytest.raises(ExchangeError, match="trade entry") as exc_info:
                await adapter.get_trade_history(symbol=BTC_USD)
            assert isinstance(exc_info.value.__cause__, TypeError)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_nan_count_is_ignored_not_fatal(self) -> None:
        """json.loads parses NaN by default; NaN passes the isinstance
        (int, float) gate and int(nan) raises bare ValueError. A bogus
        count must degrade to "no count" (the page cap still bounds
        the walk), never crash the fetch."""
        # httpx's json= kwarg refuses to ENCODE NaN, but Kraken's wire
        # can carry it and the adapter's response.json() (stdlib
        # json.loads) parses it happily -- so ship raw bytes.
        good_trade = (
            '{"pair": "XXBTZUSD", "ordertxid": "OID-1", "type": "buy", "price": "1", '
            '"vol": "1", "fee": "0", "cost": "1", "time": 1748191200.0}'
        )
        page = f'{{"error": [], "result": {{"trades": {{"TXID-1": {good_trade}}}, "count": NaN}}}}'
        empty = '{"error": [], "result": {"trades": {}, "count": NaN}}'
        calls = {"n": 0}
        json_headers = {"content-type": "application/json"}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/Assets"):
                return httpx.Response(200, json=_GOOD_ASSETS)
            if request.url.path.endswith("/AssetPairs"):
                return httpx.Response(200, json=_GOOD_PAIRS)
            calls["n"] += 1
            # First TradesHistory page has the trade; later pages empty
            # so the walk terminates via the empty-page break.
            body = page if calls["n"] == 1 else empty
            return httpx.Response(200, content=body.encode(), headers=json_headers)

        adapter = _make_adapter(handler)
        try:
            trades = await adapter.get_trade_history(symbol=BTC_USD)
            assert [t.id for t in trades] == ["TXID-1"]
        finally:
            await adapter.aclose()


class TestBalanceParsing:
    """cli/live's shutdown finally-block reads balances; a bare
    exception there aborts the rest of the block, skipping cancel-all
    and leaving real orders live on Kraken."""

    @pytest.mark.asyncio
    async def test_non_numeric_balance_raises_exchange_error(self) -> None:
        balances = {"error": [], "result": {"ZUSD": {"balance": "abc", "hold_trade": "0"}}}
        adapter = _make_adapter(_routing_handler({"/Assets": _GOOD_ASSETS, "/BalanceEx": balances}))
        try:
            with pytest.raises(ExchangeError, match="BalanceEx entry") as exc_info:
                await adapter.get_balances()
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_string_entry_shape_raises_exchange_error(self) -> None:
        """The plain Balance endpoint returns a bare string per asset
        where BalanceEx returns a dict; entry["balance"] on a str raises
        TypeError, not KeyError. The dict[str, str] annotation is
        unenforced at runtime."""
        balances = {"error": [], "result": {"ZUSD": "123.45"}}
        adapter = _make_adapter(_routing_handler({"/Assets": _GOOD_ASSETS, "/BalanceEx": balances}))
        try:
            with pytest.raises(ExchangeError, match="BalanceEx entry") as exc_info:
                await adapter.get_balances()
            assert isinstance(exc_info.value.__cause__, TypeError)
        finally:
            await adapter.aclose()


class TestFeeRateParsing:
    @pytest.mark.asyncio
    async def test_non_numeric_fee_raises_exchange_error(self) -> None:
        fees = {
            "error": [],
            "result": {
                "fees": {"XXBTZUSD": {"fee": "abc"}},
                "fees_maker": {"XXBTZUSD": {"fee": "0.4"}},
            },
        }
        adapter = _make_adapter(_json_handler(fees))
        try:
            with pytest.raises(ExchangeError, match="fee entry") as exc_info:
                await adapter.get_fee_rates(BTC_USD)
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()


class TestEnvelopeErrorField:
    """The envelope normalizer is the one place EVERY call passes
    through -- a bare exception from it is the worst possible case."""

    @pytest.mark.asyncio
    async def test_non_iterable_error_field_raises_exchange_error(self) -> None:
        """A truthy non-iterable `error` (Kraken sending 1 or true) made
        the codes comprehension raise a bare TypeError."""
        adapter = _make_adapter(_json_handler({"error": 1, "result": {}}))
        try:
            with pytest.raises(ExchangeError, match="returned errors"):
                await adapter.get_current_price(BTC_USD)
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_normal_error_list_still_carries_codes(self) -> None:
        """The isinstance gate must not disturb the normal list path --
        ADR-037's auth classification reads ExchangeError.codes."""
        adapter = _make_adapter(_json_handler({"error": ["EAPI:Invalid key"], "result": {}}))
        try:
            with pytest.raises(ExchangeError) as exc_info:
                await adapter.get_current_price(BTC_USD)
            assert exc_info.value.codes == ["EAPI:Invalid key"]
        finally:
            await adapter.aclose()


class TestOrderStatusUpdate:
    @pytest.mark.asyncio
    async def test_unknown_status_literal_raises_exchange_error(self) -> None:
        """Order.status is a Literal and the model runs
        validate_assignment, so a status value outside Kraken's
        canonical vocabulary raises ValidationError AT ASSIGNMENT —
        the exact branch the in-code comment cites as the reason the
        assignments sit inside the guard, previously untested
        (2026-08-22 full-branch review)."""
        order = Order(
            exchange_id="OID-1",
            symbol=BTC_USD,
            side="buy",  # type: ignore[arg-type]
            price=Price(amount=Decimal("50000"), currency="USD"),
            amount=Amount(value=Decimal("0.001"), asset="BTC"),
            status="open",
            created_at=Timestamp(dt=datetime.now(tz=UTC)),
        )
        queried = {
            "error": [],
            "result": {"OID-1": {"status": "definitely-not-a-status", "vol_exec": "0"}},
        }
        adapter = _make_adapter(_json_handler(queried))
        try:
            with pytest.raises(ExchangeError, match="order-status update") as exc_info:
                await adapter.get_order_status(order)
            assert isinstance(exc_info.value.__cause__, ValueError)  # ValidationError
        finally:
            await adapter.aclose()

    @pytest.mark.asyncio
    async def test_malformed_vol_exec_raises_exchange_error(self) -> None:
        order = Order(
            exchange_id="OID-1",
            symbol=BTC_USD,
            side="buy",  # type: ignore[arg-type]
            price=Price(amount=Decimal("50000"), currency="USD"),
            amount=Amount(value=Decimal("0.001"), asset="BTC"),
            status="open",
            created_at=Timestamp(dt=datetime.now(tz=UTC)),
        )
        queried = {
            "error": [],
            "result": {"OID-1": {"status": "closed", "vol_exec": "abc"}},
        }
        adapter = _make_adapter(_json_handler(queried))
        try:
            with pytest.raises(ExchangeError, match="order-status update") as exc_info:
                await adapter.get_order_status(order)
            assert isinstance(exc_info.value.__cause__, decimal.InvalidOperation)
        finally:
            await adapter.aclose()

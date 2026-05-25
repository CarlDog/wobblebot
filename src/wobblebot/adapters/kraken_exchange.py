"""KrakenAdapter — concrete ``ExchangePort`` implementation backed by the Kraken REST API.

Phase 2.1 added read paths (ticker / balances). Stage 2.3 adds the
trading and bookkeeping methods (``place_order``, ``cancel_order``,
``get_order_status``, ``get_open_orders``, ``get_trade_history``) plus
a per-pair precision cache so submitted prices/volumes match Kraken's
``pair_decimals`` / ``lot_decimals`` and pass ``ordermin`` /
``costmin`` checks.

**Authentication.** Kraken signs private endpoints with HMAC-SHA512
over ``URI path + SHA256(nonce + POST body)``, keyed by the
base64-decoded API secret; the signature is base64-encoded and sent as
the ``API-Sign`` header. The nonce is sent as a POST field and must
strictly increase per API key. Public endpoints (Ticker, AssetPairs)
are unsigned.

**Permissions required** (per the Stage 2.3 trading scope):
- ``Query Funds`` for ``BalanceEx``
- ``Query Open Orders & Trades`` for ``OpenOrders`` / ``QueryOrders``
- ``Query Closed Orders & Trades`` for ``TradesHistory``
- ``Create & Modify Orders`` (Kraken's "Trade" scope) for
  ``AddOrder`` / ``CancelOrder`` — even when called with
  ``validate=true``
- **No** ``Withdraw Funds`` — that scope lives on the separate
  Harvester key per ADR-003.

**Dry-run mode.** ``KrakenAdapter(config, dry_run=True)`` sets
``validate=true`` on every ``AddOrder`` request. Kraken validates the
order params (auth, serialization, pair, precision, balance,
ordermin) and returns a confirmation **without placing** the order.
The adapter synthesizes a ``DRYRUN-<order.id>`` exchange_id so the
engine's bookkeeping path still works for diagnostic runs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from uuid import uuid4

import httpx

from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.exceptions import InsufficientBalance
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import (
    Amount,
    OHLCBar,
    OrderSide,
    Price,
    Symbol,
    Timestamp,
)
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.ports.exchange import ExchangePort

_API_VERSION = "0"

# Colloquial-naming aliases between our domain vocabulary and Kraken's
# altname vocabulary. These are conventions we *choose* — Kraken's data
# doesn't tell us "BTC means the same thing as XBT". Anything not listed
# falls through as identity (USD, ETH, ADA, SOL, ...).
#
# The legacy X/Z-prefixed *response codes* (XXBT, ZUSD, XETH, etc.) are
# NOT here — those are resolved dynamically via the live Assets
# endpoint cache (see ``KrakenAdapter._ensure_asset_metadata``), so new
# assets Kraken adds work without code changes.
_INTERNAL_TO_KRAKEN_ALTNAME: dict[str, str] = {
    "BTC": "XBT",
    "DOGE": "XDG",
}
_KRAKEN_ALTNAME_TO_INTERNAL: dict[str, str] = {v: k for k, v in _INTERNAL_TO_KRAKEN_ALTNAME.items()}


@dataclass(frozen=True)
class _PairMetadata:
    """Subset of ``/0/public/AssetPairs`` fields the trading code uses.

    All four are required for safe order submission. Kraken rejects an
    AddOrder whose price has more digits past the decimal than
    ``pair_decimals``, or whose volume has more than ``lot_decimals``,
    or whose volume is below ``ordermin``, or whose cost is below
    ``costmin``.
    """

    pair_key: str  # canonical Kraken pair key, e.g. "XXBTZUSD"
    base_code: str  # Kraken response code for base, e.g. "XXBT"
    quote_code: str  # Kraken response code for quote, e.g. "ZUSD"
    pair_decimals: int
    lot_decimals: int
    ordermin: Decimal
    costmin: Decimal


# Kraken errors that should map to InsufficientBalance rather than the
# generic ExchangeError. Substring match — Kraken's error strings are
# stable enough for this.
_INSUFFICIENT_FUNDS_MARKERS = ("EOrder:Insufficient funds",)


class KrakenAdapter(ExchangePort):  # pylint: disable=too-many-instance-attributes
    """Concrete ``ExchangePort`` for the Kraken REST API.

    R0902 disabled: the adapter holds two independent lazy caches
    (``_asset_altnames`` and ``_pair_metadata``) each paired with its
    own asyncio.Lock, plus the HTTP client / config / ownership flag /
    nonce / dry_run flag — nine attributes total, each with a clear
    single role. Bundling the cache+lock pairs into helper objects
    would shave the count but obscure the data flow.

    Args:
        config: Credentials + connection parameters.
        http_client: Optional ``httpx.AsyncClient``. When omitted, the
            adapter constructs its own with ``config.base_url`` and the
            configured timeout. Injection is the test seam for mocking
            transport.
    """

    def __init__(
        self,
        config: KrakenConfig,
        http_client: httpx.AsyncClient | None = None,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._dry_run = dry_run
        if http_client is None:
            self._http = httpx.AsyncClient(
                base_url=config.base_url,
                timeout=config.request_timeout_seconds,
            )
            self._owns_client = True
        else:
            self._http = http_client
            self._owns_client = False
        self._last_nonce = 0
        # Lazy cache: Kraken response code -> altname (e.g. XXBT -> XBT,
        # ZUSD -> USD). Populated from /0/public/Assets on first need.
        self._asset_altnames: dict[str, str] | None = None
        self._asset_metadata_lock = asyncio.Lock()
        # Lazy cache of per-pair precision + minimums. Indexed by both
        # the pair key (XXBTZUSD) and the altname (XBTUSD) for ergonomic
        # lookup from either side. Populated from /0/public/AssetPairs.
        self._pair_metadata: dict[str, _PairMetadata] | None = None
        self._pair_metadata_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if the adapter created it.

        No-op when the client was injected — the injector owns its
        lifecycle. Safe to call multiple times.
        """
        if self._owns_client:
            await self._http.aclose()

    # ------------------------------------------------ Startup validation

    async def partition_known_symbols(
        self, symbols: Iterable[Symbol]
    ) -> tuple[list[Symbol], list[Symbol]]:
        """Split ``symbols`` into (known, unknown) against Kraken's pairs.

        Hits ``/0/public/AssetPairs`` once via the shared pair-metadata
        cache, then resolves each requested symbol via the same lookup
        the trading path uses (altname first, then base/quote fallback).
        Returns two ordered lists preserving the caller's input order:
        ``known`` (tradeable on Kraken right now) and ``unknown``
        (missing or delisted).

        Call at daemon startup to graceful-degrade on a partially-bad
        symbol list. The original "refuse-to-start" design was rejected
        as too aggressive — losing 1 of 12 symbols shouldn't sideline
        the other 11. The daemon's policy is:
          - all symbols known → proceed normally
          - some unknown → log WARNING listing the bad ones, drop them,
            proceed with the known subset
          - none known → refuse to start (no work to do)
        This caught the MATIC/USD → POL/USD post-Polygon-migration
        drift during soak Day 6.
        """
        await self._ensure_pair_metadata()
        known: list[Symbol] = []
        unknown: list[Symbol] = []
        for symbol in symbols:
            try:
                self._pair_metadata_for(symbol)
                known.append(symbol)
            except ExchangeError:
                unknown.append(symbol)
        return known, unknown

    # ------------------------------------------------ ExchangePort: read paths

    async def get_current_price(self, symbol: Symbol) -> Price:
        """Fetch the last-trade price for ``symbol`` via ``/0/public/Ticker``.

        Kraken's Ticker response keys by the X/Z-prefixed pair name
        (``XXBTZUSD``) regardless of which form the caller used to
        query. Since each request asks for exactly one pair, we don't
        need to translate the response key — we take the single entry.
        """
        altname = _symbol_to_kraken_altname(symbol)
        result = await self._public_get("/0/public/Ticker", {"pair": altname})
        if not result:
            raise ExchangeError(f"Kraken returned no ticker data for pair {altname!r}")
        ticker = next(iter(result.values()))
        # ``c`` is [last_trade_price, lot_volume]. c[0] is the canonical
        # "current price" per docs/reference/kraken-api-reference.md.
        last_price_str = ticker["c"][0]
        return Price(amount=Decimal(last_price_str), currency=symbol.quote)

    async def get_ohlc(
        self,
        symbol: Symbol,
        interval_minutes: int = 1,
        since: datetime | None = None,
    ) -> list[OHLCBar]:
        """Fetch historical OHLC bars via ``/0/public/OHLC``.

        Kraken returns the bars keyed by the X/Z-prefixed pair name
        (e.g. ``XXBTZUSD``) alongside a ``last`` cursor. We accept any
        key shape — single-pair requests mean the bars are the only
        list-valued entry in the result dict, regardless of how Kraken
        labels it.

        Each bar in the response is the array
        ``[time, open, high, low, close, vwap, volume, count]`` per
        Kraken's published schema.

        v1.1 addition driving the cli/observe --backfill feature.
        """
        if interval_minutes not in OHLCBar.ALLOWED_INTERVALS:
            raise ValueError(
                f"interval_minutes must be one of "
                f"{sorted(OHLCBar.ALLOWED_INTERVALS)}; got {interval_minutes}"
            )
        altname = _symbol_to_kraken_altname(symbol)
        params: dict[str, str] = {
            "pair": altname,
            "interval": str(interval_minutes),
        }
        if since is not None:
            if since.tzinfo is None:
                raise ValueError("`since` must be timezone-aware")
            params["since"] = str(int(since.astimezone(UTC).timestamp()))
        result = await self._public_get("/0/public/OHLC", params)
        if not result:
            raise ExchangeError(f"Kraken returned no OHLC data for pair {altname!r}")
        # The result dict has one list-valued entry (the bars) plus an
        # int "last" cursor. Take whichever value is a list.
        bars_raw = next(
            (v for v in result.values() if isinstance(v, list)),
            None,
        )
        if bars_raw is None:
            raise ExchangeError(f"Kraken OHLC response missing bars array for pair {altname!r}")
        out: list[OHLCBar] = []
        for entry in bars_raw:
            # Defensive on shape — Kraken occasionally evolves wire
            # formats and a missing field should fail-fast at the
            # boundary rather than wedge a backfill mid-stream.
            if not isinstance(entry, list) or len(entry) < 8:
                raise ExchangeError(f"Kraken OHLC entry has unexpected shape: {entry!r}")
            opened_at = datetime.fromtimestamp(int(entry[0]), tz=UTC)
            out.append(
                OHLCBar(
                    symbol=symbol,
                    interval_minutes=interval_minutes,
                    opened_at=opened_at,
                    open=Decimal(str(entry[1])),
                    high=Decimal(str(entry[2])),
                    low=Decimal(str(entry[3])),
                    close=Decimal(str(entry[4])),
                    vwap=Decimal(str(entry[5])),
                    volume=Decimal(str(entry[6])),
                    count=int(entry[7]),
                )
            )
        return out

    async def get_balances(self) -> list[Balance]:
        """Fetch all account balances via ``/0/private/BalanceEx``.

        BalanceEx (not Balance) is the canonical endpoint: it returns
        ``hold_trade`` per asset, which our ``Balance.locked`` field
        mirrors directly. The plain ``Balance`` endpoint omits hold
        info and forces clients to cross-reference OpenOrders.

        First call also populates the asset-metadata cache so Kraken's
        legacy X/Z-prefixed response codes (``XXBT``, ``ZUSD``) can be
        resolved to our internal vocabulary without hand-maintaining a
        per-asset list. Subsequent calls reuse the cache.
        """
        await self._ensure_asset_metadata()
        result = await self._private_post("/0/private/BalanceEx")
        return [self._parse_balance_entry(code, entry) for code, entry in result.items()]

    async def get_balance(self, asset: str) -> Balance | None:
        """Fetch the balance for ``asset`` (e.g., "BTC", "USD") or ``None``.

        BalanceEx has no per-asset filter parameter, so we fetch the
        full set and filter locally. The set is small (one entry per
        held asset) and this method is rarely called in a tight loop.
        """
        normalized = asset.upper()
        balances = await self.get_balances()
        for b in balances:
            if b.asset == normalized:
                return b
        return None

    async def get_order_status(self, order: Order) -> Order:
        """Fetch fresh status of ``order`` from Kraken via QueryOrders.

        Preserves the input ``order``'s internal UUID — only updates
        ``status``, ``filled_amount``, and ``updated_at`` from Kraken's
        view. The engine relies on UUID stability for storage tracking.
        """
        if not order.exchange_id:
            raise ExchangeError("Cannot query an order with no exchange_id")
        if order.exchange_id.startswith("DRYRUN-"):
            # Dry-run orders never made it to Kraken; mirror back as-is.
            return order
        result = await self._private_post("/0/private/QueryOrders", {"txid": order.exchange_id})
        entry = result.get(order.exchange_id)
        if not isinstance(entry, dict):
            raise ExchangeError(f"Kraken QueryOrders missing entry for {order.exchange_id!r}")
        return _apply_kraken_order_update(order, entry)

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        """Fetch all open orders from Kraken; client-side filter by symbol.

        Kraken's ``OpenOrders`` endpoint takes no pair filter. The
        result set per account is small (single-digit per coin in a
        well-behaved grid), so client-side filtering is fine.
        Constructed Orders carry fresh UUIDs — the engine matches by
        ``exchange_id``, not UUID, when diffing against storage.
        """
        await self._ensure_pair_metadata()
        result = await self._private_post("/0/private/OpenOrders")
        open_map = result.get("open", {})
        if not isinstance(open_map, dict):
            raise ExchangeError("Kraken OpenOrders response missing 'open' object")
        orders: list[Order] = []
        for txid, entry in open_map.items():
            order = self._build_order_from_kraken(txid, entry)
            if symbol is None or order.symbol == symbol:
                orders.append(order)
        return orders

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        """Fetch recent trades from Kraken; client-side filter by symbol.

        Kraken's ``TradesHistory`` returns up to 50 entries per page by
        default. We pass no time filter — the most recent ``limit``
        trades after symbol filtering are returned. Larger ``limit``
        callers should paginate; Stage 2.2's engine asks for at most
        200 trades after detecting a small fill batch, so a single page
        suffices in normal operation.
        """
        await self._ensure_pair_metadata()
        result = await self._private_post("/0/private/TradesHistory")
        trades_map = result.get("trades", {})
        if not isinstance(trades_map, dict):
            raise ExchangeError("Kraken TradesHistory response missing 'trades' object")
        trades: list[Trade] = []
        for txid, entry in trades_map.items():
            trade = self._build_trade_from_kraken(txid, entry)
            if symbol is None or trade.symbol == symbol:
                trades.append(trade)
        # Most-recent first to match the ExchangePort convention.
        trades.sort(key=lambda t: t.executed_at.dt, reverse=True)
        return trades[:limit]

    # ------------------------------------------------ ExchangePort: write paths

    async def place_order(self, order: Order) -> Order:
        """Submit ``order`` via AddOrder. Honors ``self._dry_run``.

        With ``dry_run=True``, sets ``validate=true`` in the request —
        Kraken validates auth + serialization + pair + precision +
        balance + ordermin without placing the order. The returned
        ``Order`` carries a synthesized ``DRYRUN-<order.id>``
        ``exchange_id`` so storage tracking still functions.

        Per-pair precision quantization (``pair_decimals`` for price,
        ``lot_decimals`` for volume, both rounded DOWN — never round up
        into more spend than the engine intended) is applied before
        submission. ``ordermin`` and ``costmin`` are checked client-side
        too; failing either raises ``ExchangeError`` rather than letting
        Kraken reject and waste a nonce.
        """
        await self._ensure_pair_metadata()
        meta = self._pair_metadata_for(order.symbol)
        price_q = _quantize_decimal(order.price.amount, meta.pair_decimals)
        volume_q = _quantize_decimal(order.amount.value, meta.lot_decimals)
        if volume_q < meta.ordermin:
            raise ExchangeError(
                f"Order volume {volume_q} below ordermin {meta.ordermin} for {order.symbol}"
            )
        cost = price_q * volume_q
        if cost < meta.costmin:
            raise ExchangeError(
                f"Order cost {cost} below costmin {meta.costmin} for {order.symbol}"
            )

        params: dict[str, Any] = {
            "pair": meta.pair_key,
            "type": order.side.value,
            "ordertype": "limit",
            "price": str(price_q),
            "volume": str(volume_q),
        }
        if self._dry_run:
            params["validate"] = "true"

        try:
            result = await self._private_post("/0/private/AddOrder", params)
        except ExchangeError as exc:
            if any(marker in str(exc) for marker in _INSUFFICIENT_FUNDS_MARKERS):
                raise InsufficientBalance(
                    required=cost,
                    available=Decimal("0"),  # Kraken doesn't tell us
                    asset=order.symbol.quote if order.side is OrderSide.BUY else order.symbol.base,
                ) from exc
            raise

        if self._dry_run:
            order.mark_open(exchange_id=f"DRYRUN-{order.id}")
            return order

        txids = result.get("txid")
        if not isinstance(txids, list) or not txids:
            raise ExchangeError(f"Kraken AddOrder returned no txid: result={result!r}")
        order.mark_open(exchange_id=str(txids[0]))
        return order

    async def cancel_order(self, order: Order) -> Order:
        """Cancel ``order`` via CancelOrder. No dry-run path on Kraken's side.

        Dry-run mode short-circuits locally so cancellations during a
        diagnostic run don't try to cancel orders that never existed.
        """
        if not order.exchange_id:
            raise ExchangeError("Cannot cancel an order with no exchange_id")
        if self._dry_run or order.exchange_id.startswith("DRYRUN-"):
            order.mark_canceled()
            return order
        await self._private_post("/0/private/CancelOrder", {"txid": order.exchange_id})
        order.mark_canceled()
        return order

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        """Withdraw funds via Kraken's ``/0/private/Withdraw`` endpoint.

        Per ADR-003 this requires the Harvester-scope API key (Withdraw
        permission). Per ADR-004 it's the project's sole fund-transfer
        mechanism — there's no separate banking adapter.

        Kraken's withdrawal API only accepts ``key`` values
        (destination labels) that the operator has pre-registered via
        Kraken Pro → Funding → Withdraw → New Address (or New Wire
        Recipient). Calling with an unknown label returns
        ``EFunding:Unknown reference id`` (surfaced as
        ``ExchangeError``). Below-minimum amounts surface as
        ``EFunding:Below minimum``.

        Args:
            asset: Asset code (``"USD"``, ``"BTC"``, ...). Kraken's
                endpoint accepts the friendly altname directly.
            amount: Amount to withdraw in asset units. Serialized as
                a string so Decimal precision survives the wire.
            destination: Pre-registered destination label from the
                operator's Kraken Pro address book.

        Returns:
            Kraken's withdrawal reference ID (``refid``). Lives in
            ``TransferResult.transaction_id`` for forensic linking
            to Kraken Pro's Funding history.

        Raises:
            ExchangeError: On transport failure, malformed response,
                or any Kraken-side rejection (unknown destination
                label, below-minimum amount, insufficient balance,
                API key scope mismatch, etc.).
        """
        result = await self._private_post(
            "/0/private/Withdraw",
            {
                "asset": asset,
                "key": destination,
                "amount": str(amount),
            },
        )
        refid = result.get("refid")
        if not isinstance(refid, str) or not refid:
            raise ExchangeError(
                f"Kraken /0/private/Withdraw response missing 'refid'; got {result!r}"
            )
        return refid

    # ------------------------------------------------ Asset metadata cache

    async def _ensure_asset_metadata(self) -> None:
        """Populate the asset-altname cache from ``/0/public/Assets``.

        Lazy + one-shot per adapter instance. Concurrent first-call
        invocations are serialized by ``_asset_metadata_lock`` so we
        never issue two parallel fetches.

        The Assets response gives the canonical ``response_code →
        altname`` map (e.g. ``XXBT → XBT``, ``ZUSD → USD``, ``ADA →
        ADA``). Combined with ``_KRAKEN_ALTNAME_TO_INTERNAL`` (which
        captures conventions like XBT↔BTC, XDG↔DOGE), this lets us
        translate any Kraken response code to internal vocabulary
        without a hand-maintained per-asset list.
        """
        if self._asset_altnames is not None:
            return
        async with self._asset_metadata_lock:
            if self._asset_altnames is not None:
                return  # another coroutine populated it while we waited
            assets = await self._public_get("/0/public/Assets")
            altnames: dict[str, str] = {}
            for kraken_code, info in assets.items():
                altname = info.get("altname") if isinstance(info, dict) else None
                if not isinstance(altname, str):
                    raise ExchangeError(
                        f"Kraken /0/public/Assets entry {kraken_code!r} missing altname"
                    )
                altnames[kraken_code] = altname
            self._asset_altnames = altnames

    async def _ensure_pair_metadata(self) -> None:
        """Populate the per-pair precision + minimums cache from AssetPairs.

        Lazy + one-shot. Concurrent first-call invocations serialize on
        ``_pair_metadata_lock``. The cache is indexed by both pair key
        (XXBTZUSD) and altname (XBTUSD) — Kraken accepts either form in
        AddOrder's ``pair`` parameter, but OpenOrders responses key by
        the canonical pair key, so we need bidirectional lookup.
        """
        if self._pair_metadata is not None:
            return
        async with self._pair_metadata_lock:
            if self._pair_metadata is not None:
                return
            await self._ensure_asset_metadata()
            pairs = await self._public_get("/0/public/AssetPairs")
            metadata: dict[str, _PairMetadata] = {}
            for pair_key, info in pairs.items():
                if not isinstance(info, dict):
                    continue
                try:
                    meta = _PairMetadata(
                        pair_key=pair_key,
                        base_code=info["base"],
                        quote_code=info["quote"],
                        pair_decimals=int(info["pair_decimals"]),
                        lot_decimals=int(info["lot_decimals"]),
                        ordermin=Decimal(info.get("ordermin", "0")),
                        costmin=Decimal(info.get("costmin", "0")),
                    )
                except (KeyError, ValueError) as exc:
                    raise ExchangeError(
                        f"Kraken AssetPairs entry {pair_key!r} malformed: {exc}"
                    ) from exc
                metadata[pair_key] = meta
                altname = info.get("altname")
                if isinstance(altname, str) and altname != pair_key:
                    metadata[altname] = meta
            self._pair_metadata = metadata

    def _pair_metadata_for(self, symbol: Symbol) -> _PairMetadata:
        """Look up pair metadata by ``Symbol``. Tries altname first
        (XBTUSD), then falls back to pair key (XXBTZUSD)."""
        if self._pair_metadata is None:
            raise ExchangeError(
                "Pair metadata cache not initialized; call _ensure_pair_metadata first"
            )
        altname = _symbol_to_kraken_altname(symbol)
        meta = self._pair_metadata.get(altname)
        if meta is not None:
            return meta
        # Fallback for pairs whose altname differs from pair key.
        for candidate in self._pair_metadata.values():
            if (
                self._kraken_code_to_internal(candidate.base_code) == symbol.base
                and self._kraken_code_to_internal(candidate.quote_code) == symbol.quote
            ):
                return candidate
        raise ExchangeError(f"No Kraken pair metadata found for {symbol}")

    def _symbol_for_pair_key(self, pair_key: str) -> Symbol:
        """Reverse: Kraken pair key (XXBTZUSD) → ``Symbol(BTC, USD)``."""
        if self._pair_metadata is None:
            raise ExchangeError("Pair metadata cache not initialized")
        meta = self._pair_metadata.get(pair_key)
        if meta is None:
            # Try altname → meta
            for candidate in self._pair_metadata.values():
                if pair_key in (candidate.pair_key,):
                    meta = candidate
                    break
            if meta is None:
                raise ExchangeError(f"Unknown Kraken pair key {pair_key!r}")
        return Symbol(
            base=self._kraken_code_to_internal(meta.base_code),
            quote=self._kraken_code_to_internal(meta.quote_code),
        )

    def _build_order_from_kraken(self, txid: str, entry: dict[str, Any]) -> Order:
        """Construct an ``Order`` from a Kraken OpenOrders/QueryOrders entry.

        Generated UUID — engine matches by ``exchange_id`` (the txid),
        not by UUID, so a fresh one here is harmless.
        """
        descr = entry.get("descr") or {}
        pair_key = descr.get("pair", "")
        symbol = self._symbol_for_pair_key(pair_key)
        side = OrderSide(descr.get("type", "buy"))
        price_amount = Decimal(descr.get("price", "0"))
        volume = Decimal(entry.get("vol", "0"))
        vol_exec = Decimal(entry.get("vol_exec", "0"))
        opentm = float(entry.get("opentm", 0.0))
        return Order(
            id=uuid4(),
            exchange_id=txid,
            symbol=symbol,
            side=side,
            price=Price(amount=price_amount, currency=symbol.quote),
            amount=Amount(value=volume, asset=symbol.base),
            status=entry.get("status", "open"),
            filled_amount=vol_exec,
            created_at=Timestamp(dt=datetime.fromtimestamp(opentm, tz=UTC)),
        )

    def _build_trade_from_kraken(self, txid: str, entry: dict[str, Any]) -> Trade:
        """Construct a ``Trade`` from a Kraken TradesHistory entry."""
        pair_key = entry.get("pair", "")
        symbol = self._symbol_for_pair_key(pair_key)
        return Trade(
            id=txid,
            order_id=entry.get("ordertxid", ""),
            symbol=symbol,
            side=OrderSide(entry.get("type", "buy")),
            price=Price(amount=Decimal(entry.get("price", "0")), currency=symbol.quote),
            amount=Amount(value=Decimal(entry.get("vol", "0")), asset=symbol.base),
            fee=Decimal(entry.get("fee", "0")),
            cost=Decimal(entry.get("cost", "0")),
            executed_at=Timestamp(dt=datetime.fromtimestamp(float(entry.get("time", 0.0)), tz=UTC)),
        )

    def _kraken_code_to_internal(self, kraken_code: str) -> str:
        """Translate a Kraken response code (``XXBT``) to internal vocabulary (``BTC``).

        Composes two lookups:

        1. ``response_code → altname`` from the cached Assets map.
        2. ``altname → internal`` from the conventional dict.

        Falls through identity at both stages if no mapping exists, so
        new assets Kraken adds work without code changes.

        Precondition: ``_ensure_asset_metadata`` has been awaited.
        """
        if self._asset_altnames is None:
            raise ExchangeError(
                "Asset metadata cache not initialized; call _ensure_asset_metadata first"
            )
        altname = self._asset_altnames.get(kraken_code, kraken_code)
        return _KRAKEN_ALTNAME_TO_INTERNAL.get(altname, altname)

    # ------------------------------------------------ Response parsing

    def _parse_balance_entry(self, kraken_code: str, entry: dict[str, str]) -> Balance:
        """Translate one BalanceEx entry into our domain ``Balance``.

        BalanceEx entry shape: ``{"balance": "<decimal>", "hold_trade": "<decimal>"}``.
        ``balance`` is the total holding (Kraken's source of truth);
        ``hold_trade`` is the portion locked by open orders.
        """
        internal_code = self._kraken_code_to_internal(kraken_code)
        total = Decimal(entry["balance"])
        hold = Decimal(entry["hold_trade"])
        return Balance(
            asset=internal_code,
            total=total,
            available=total - hold,
            locked=hold,
        )

    # ------------------------------------------------ Kraken HTTP plumbing

    async def _public_get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """Issue a GET to a public Kraken endpoint and return ``result``.

        Raises:
            ExchangeError: On HTTP error, malformed envelope, or
                non-empty ``error`` array in the response.
        """
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ExchangeError(f"Kraken {path} transport failure: {exc}") from exc
        return self._unwrap_envelope(response, path)

    async def _private_post(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue a signed POST to a private Kraken endpoint and return ``result``.

        Builds the nonce, signs the request, and sets ``API-Key`` /
        ``API-Sign`` headers. The body is form-encoded
        (``application/x-www-form-urlencoded``), which is what Kraken
        signs over.

        Raises:
            ExchangeError: On HTTP error, malformed envelope, or
                non-empty ``error`` array in the response.
        """
        body = dict(params or {})
        nonce = self._make_nonce()
        body["nonce"] = nonce
        signature = self._sign(uri_path=path, nonce=nonce, post_data=body)
        headers = {
            "API-Key": self._config.api_key,
            "API-Sign": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            response = await self._http.post(path, data=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ExchangeError(f"Kraken {path} transport failure: {exc}") from exc
        return self._unwrap_envelope(response, path)

    @staticmethod
    def _unwrap_envelope(response: httpx.Response, path: str) -> dict[str, Any]:
        """Validate Kraken's ``{"error": [...], "result": {...}}`` envelope.

        Kraken returns HTTP 200 even on application errors — the
        ``error`` array is the signal. A non-empty array means "the
        endpoint understood your request and rejected it for business
        reasons" (invalid nonce, insufficient permissions, etc.).
        """
        if response.status_code >= 400:
            raise ExchangeError(f"Kraken {path} HTTP {response.status_code}: {response.text[:200]}")
        try:
            envelope = response.json()
        except ValueError as exc:
            raise ExchangeError(f"Kraken {path} returned non-JSON body") from exc
        if not isinstance(envelope, dict):
            raise ExchangeError(f"Kraken {path} returned non-object JSON envelope")
        errors = envelope.get("error", [])
        if errors:
            raise ExchangeError(f"Kraken {path} returned errors: {errors}")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ExchangeError(f"Kraken {path} response missing 'result' object")
        return result

    def _make_nonce(self) -> int:
        """Generate a strictly increasing nonce (milliseconds since epoch).

        Kraken rejects non-increasing nonces with ``EAPI:Invalid nonce``.
        Two failure modes the naive ``time.time_ns() // 1_000_000``
        approach has:

        - **Same-millisecond collisions.** Two consecutive calls inside
          one millisecond produce the same value.
        - **Wall-clock backwards jumps.** ``time.time_ns()`` is wall-clock
          (not monotonic — that's ``time.monotonic_ns()``), so an NTP
          correction can move it backward.

        Guard both by clamping against ``self._last_nonce + 1``. The
        nonce remains roughly equal to wall-clock ms in the common case
        and walks forward by 1ms-at-a-time during a collision or jump.

        If this adapter is ever instantiated across multiple processes
        sharing one API key, replace this in-process counter with a
        persistent monotonic store (file lock, Redis, etc.).
        """
        now = time.time_ns() // 1_000_000
        self._last_nonce = max(now, self._last_nonce + 1)
        return self._last_nonce

    def _sign(self, uri_path: str, nonce: int, post_data: dict[str, Any]) -> str:
        """Compute the Kraken ``API-Sign`` header value.

        Algorithm (from Kraken's API docs): HMAC-SHA512 over
        ``uri_path_bytes + SHA256(nonce_str + post_body)``, keyed by
        the base64-decoded API secret. Output is base64-encoded.

        Args:
            uri_path: Path portion of the request URI including the API
                version prefix, e.g. ``"/0/private/Balance"``.
            nonce: The same nonce included in ``post_data``.
            post_data: The form fields being POSTed (must include
                ``nonce``). Encoded with ``urlencode`` — order matters
                for the hash, but Python dicts preserve insertion order
                in 3.7+, so the encoded form is stable as long as
                callers build the dict consistently.

        Returns:
            Base64-encoded HMAC-SHA512 signature.
        """
        encoded_body = urllib.parse.urlencode(post_data)
        sha256_input = (str(nonce) + encoded_body).encode("utf-8")
        sha256_digest = hashlib.sha256(sha256_input).digest()
        mac_input = uri_path.encode("utf-8") + sha256_digest
        secret_bytes = base64.b64decode(self._config.api_secret)
        signature = hmac.new(secret_bytes, mac_input, hashlib.sha512).digest()
        return base64.b64encode(signature).decode("utf-8")


def _quantize_decimal(value: Decimal, decimals: int) -> Decimal:
    """Quantize ``value`` to ``decimals`` digits past the decimal point.

    ROUND_DOWN — never round up, since rounding up a price/volume could
    push spending past the engine's intended order_size_usd budget.
    Kraken rejects any decimal with more digits than its per-pair
    precision allows, so quantization is mandatory for AddOrder.
    """
    quantum = Decimal(10) ** -decimals
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _apply_kraken_order_update(order: Order, entry: dict[str, Any]) -> Order:
    """Update ``order`` in-place with Kraken's QueryOrders/OpenOrders fields.

    Preserves the input ``order``'s UUID, ``symbol``, ``side``, original
    price/amount, and ``created_at``. Updates ``status``,
    ``filled_amount``, and ``updated_at``. Mutation rather than
    reconstruction so the engine's storage UUID linkage survives.
    """
    new_status = entry.get("status", order.status)
    vol_exec = Decimal(entry.get("vol_exec", str(order.filled_amount)))
    # Bypass the Order.record_fill / mark_canceled invariants — they
    # assume forward-only transitions and we're applying an external
    # source-of-truth snapshot. Use direct assignment with
    # validate_assignment to keep field-level validation.
    order.status = new_status
    order.filled_amount = vol_exec
    order.updated_at = Timestamp(dt=datetime.now(UTC))
    return order


def _symbol_to_kraken_altname(symbol: Symbol) -> str:
    """Translate ``Symbol(base, quote)`` to a Kraken altname pair string.

    Examples (assuming the alias maps above):
        Symbol("BTC", "USD")  -> "XBTUSD"
        Symbol("ETH", "USD")  -> "ETHUSD"
        Symbol("DOGE", "USD") -> "XDGUSD"
        Symbol("ADA", "USD")  -> "ADAUSD"

    Returns the altname (no X/Z prefixes), which Kraken accepts in the
    ``pair=`` query parameter of every public endpoint.
    """
    base_alt = _INTERNAL_TO_KRAKEN_ALTNAME.get(symbol.base, symbol.base)
    quote_alt = _INTERNAL_TO_KRAKEN_ALTNAME.get(symbol.quote, symbol.quote)
    return f"{base_alt}{quote_alt}"

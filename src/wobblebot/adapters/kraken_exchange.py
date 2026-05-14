"""KrakenAdapter — concrete ``ExchangePort`` implementation backed by the Kraken REST API.

Phase 2.1 scope is read-only: ticker / balances / open orders / trade
history. Order placement, cancellation, and withdrawals raise
``NotImplementedError`` and are filled in by later stages (2.3 for
orders, 4.x for withdrawals via the separate Harvester key).

**Authentication.** Kraken signs private endpoints with HMAC-SHA512
over ``URI path + SHA256(nonce + POST body)``, keyed by the
base64-decoded API secret; the signature is base64-encoded and sent as
the ``API-Sign`` header. The nonce is sent as a POST field and must
strictly increase per API key. Public endpoints (Ticker, AssetPairs)
are unsigned.

**Read-only invariant.** The API key passed at construction time is
expected to have only "Query Funds" / "Query Open Orders & Trades"
permissions — no trading, no withdrawals. Phase 4's Harvester key
lives separately and is wired through a different ``ExchangePort``
instance (per ADR-003).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Any

import httpx

from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Price, Symbol
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.ports.exchange import ExchangePort

_API_VERSION = "0"

# Asset-code aliases between our domain vocabulary and Kraken's.
#
# Kraken uses legacy short-names ("XBT" for BTC, "XDG" for DOGE) in altname
# pair strings, and X/Z-prefixed forms ("XXBT", "ZUSD") in response keys.
# Newer assets (ADA, SOL, MATIC, etc.) use identity codes — no entry needed.
#
# Slice 2 hand-maintained these dicts. Slice 2.5 will replace them with a
# startup AssetPairs fetch + cache, so the lists stay tight.
_INTERNAL_TO_KRAKEN_ALTNAME: dict[str, str] = {
    "BTC": "XBT",
    "DOGE": "XDG",
}

# Kraken response/canonical asset codes → internal vocabulary.
# The Balance/BalanceEx endpoints return these prefixed forms.
_KRAKEN_TO_INTERNAL_ASSET: dict[str, str] = {
    "XXBT": "BTC",
    "XETH": "ETH",
    "XXDG": "DOGE",
    "XLTC": "LTC",
    "XXRP": "XRP",
    "XXLM": "XLM",
    "ZUSD": "USD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZJPY": "JPY",
    "ZCAD": "CAD",
    "ZAUD": "AUD",
    "ZCHF": "CHF",
}


class KrakenAdapter(ExchangePort):
    """Concrete ``ExchangePort`` for the Kraken REST API.

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
    ) -> None:
        self._config = config
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

    async def aclose(self) -> None:
        """Close the underlying HTTP client if the adapter created it.

        No-op when the client was injected — the injector owns its
        lifecycle. Safe to call multiple times.
        """
        if self._owns_client:
            await self._http.aclose()

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

    async def get_balances(self) -> list[Balance]:
        """Fetch all account balances via ``/0/private/BalanceEx``.

        BalanceEx (not Balance) is the canonical endpoint: it returns
        ``hold_trade`` per asset, which our ``Balance.locked`` field
        mirrors directly. The plain ``Balance`` endpoint omits hold
        info and forces clients to cross-reference OpenOrders.
        """
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
        raise NotImplementedError("Order tracking deferred to Stage 2.3")

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        raise NotImplementedError("Order tracking deferred to Stage 2.3")

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        raise NotImplementedError("Trade history deferred to Stage 2.3")

    # ------------------------------------------------ ExchangePort: write paths

    async def place_order(self, order: Order) -> Order:
        raise NotImplementedError("Order placement deferred to Stage 2.3")

    async def cancel_order(self, order: Order) -> Order:
        raise NotImplementedError("Order cancellation deferred to Stage 2.3")

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        raise NotImplementedError(
            "Withdrawals are exclusive to the Phase 4 Harvester key (ADR-003)"
        )

    # ------------------------------------------------ Response parsing

    @staticmethod
    def _parse_balance_entry(kraken_code: str, entry: dict[str, str]) -> Balance:
        """Translate one BalanceEx entry into our domain ``Balance``.

        BalanceEx entry shape: ``{"balance": "<decimal>", "hold_trade": "<decimal>"}``.
        ``balance`` is the total holding (Kraken's source of truth);
        ``hold_trade`` is the portion locked by open orders.
        """
        internal_code = _KRAKEN_TO_INTERNAL_ASSET.get(kraken_code, kraken_code)
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

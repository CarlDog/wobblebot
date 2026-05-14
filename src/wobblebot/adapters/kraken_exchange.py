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
from wobblebot.ports.exchange import ExchangePort

_API_VERSION = "0"


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
        raise NotImplementedError("Implemented in Stage 2.1 slice 2 (read paths)")

    async def get_balances(self) -> list[Balance]:
        raise NotImplementedError("Implemented in Stage 2.1 slice 2 (read paths)")

    async def get_balance(self, asset: str) -> Balance | None:
        raise NotImplementedError("Implemented in Stage 2.1 slice 2 (read paths)")

    async def get_order_status(self, order: Order) -> Order:
        raise NotImplementedError("Implemented in Stage 2.1 slice 2 (read paths)")

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        raise NotImplementedError("Implemented in Stage 2.1 slice 2 (read paths)")

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        raise NotImplementedError("Implemented in Stage 2.1 slice 2 (read paths)")

    # ------------------------------------------------ ExchangePort: write paths

    async def place_order(self, order: Order) -> Order:
        raise NotImplementedError("Order placement deferred to Stage 2.3")

    async def cancel_order(self, order: Order) -> Order:
        raise NotImplementedError("Order cancellation deferred to Stage 2.3")

    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        raise NotImplementedError(
            "Withdrawals are exclusive to the Phase 4 Harvester key (ADR-003)"
        )

    # ------------------------------------------------ Kraken HTTP plumbing

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

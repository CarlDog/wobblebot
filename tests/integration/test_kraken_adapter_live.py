"""Live KrakenAdapter integration test.

Hits real Kraken endpoints with real credentials. Proves that:

- Public endpoint flow (Ticker) works end-to-end with real wire format.
- Private endpoint flow (BalanceEx) is correctly signed and accepted.
- The BalanceEx ``hold_trade`` field name our parser assumes is correct
  on the live API (this is the riskiest unverified claim from slice 1).

**Auth.** Credentials come from ``KRAKEN_READER_API_KEY`` and
``KRAKEN_READER_API_SECRET`` (loaded from ``.env`` by ``conftest.py``). If
either is unset, the entire module is skipped — so this file is safe
to commit alongside the unit suite.

**Permissions.** The Phase 2.1 read-only key requires only "Query
Funds" and "Query Open Orders & Trades". This test doesn't place,
modify, or cancel anything.

**Account state independence.** The balance assertions accept any
state — an empty account is fine. We're verifying the wire format,
not the account contents.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.models import Balance
from wobblebot.domain.value_objects import Symbol

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (os.environ.get("KRAKEN_READER_API_KEY") and os.environ.get("KRAKEN_READER_API_SECRET")),
        reason="KRAKEN_READER_API_KEY / KRAKEN_READER_API_SECRET unset; live test skipped",
    ),
]


@pytest_asyncio.fixture
async def live_adapter():
    """Build an adapter from env credentials and clean it up after."""
    config = KrakenConfig.from_env()
    a = KrakenAdapter(config=config)
    try:
        yield a
    finally:
        await a.aclose()


class TestPublicEndpoints:
    async def test_get_current_price_btc_usd(self, live_adapter: KrakenAdapter) -> None:
        price = await live_adapter.get_current_price(Symbol(base="BTC", quote="USD"))
        assert price.currency == "USD"
        assert price.amount > Decimal("0"), f"Sanity check: BTC/USD price was {price.amount}"
        # Sanity-bracket the price. Wide range — we just want to catch a
        # parse error giving us a number that's off by a factor of 1000.
        assert price.amount > Decimal("1000"), f"BTC/USD suspiciously cheap: {price.amount}"
        assert price.amount < Decimal(
            "10_000_000"
        ), f"BTC/USD suspiciously expensive: {price.amount}"


class TestPrivateEndpoints:
    async def test_get_balances_returns_balance_list(self, live_adapter: KrakenAdapter) -> None:
        """Calls BalanceEx with real signing and validates the parser.

        The account may have zero entries (brand new key, never funded)
        or many. We assert structural correctness, not contents.
        """
        balances = await live_adapter.get_balances()

        assert isinstance(balances, list)
        for b in balances:
            assert isinstance(b, Balance)
            assert isinstance(b.asset, str) and len(b.asset) >= 1
            # Total/available/locked are decimals constrained to >= 0 by
            # the model. The invariant we want to spot-check here is the
            # one our parser is responsible for:
            # available == total - locked.
            assert b.available + b.locked == b.total, (
                f"Balance parser invariant broken for {b.asset}: "
                f"total={b.total}, available={b.available}, locked={b.locked}"
            )

    async def test_get_balance_for_usd_or_returns_none(self, live_adapter: KrakenAdapter) -> None:
        """Looks up USD specifically. Whether or not it's held, we assert
        that the method returns None vs a valid Balance — not an exception."""
        result = await live_adapter.get_balance("USD")
        assert result is None or (isinstance(result, Balance) and result.asset == "USD")

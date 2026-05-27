"""Live Kraken trading integration tests (Stage 2.3 slice 2).

Hits real Kraken endpoints with the operator's *trade* API key
(separate from the read-only key per ADR-003-style separation).
Designed to exercise the full trading code path **without moving any
money**:

- ``place_order`` is invoked with ``dry_run=True`` (Kraken's
  ``validate=true`` flag) — the request is signed, sent, and validated
  by Kraken's matching engine for pair / precision / balance /
  ordermin / costmin compliance, but no order is placed.
- ``cancel_order`` is invoked against a fabricated txid — Kraken
  returns ``EOrder:Unknown order`` which we assert as an
  ``ExchangeError``. This proves the cancel signing + error parsing
  paths without requiring a real order to cancel.
- ``get_order_status`` is invoked against a fabricated txid — Kraken
  returns an empty ``QueryOrders`` result, which our adapter raises as
  ``ExchangeError`` with "missing entry".
- ``get_open_orders`` and ``get_trade_history`` are read-only — they
  return whatever's actually in the account (possibly empty), which we
  only assert is structurally correct.

**Auth.** Credentials come from ``KRAKEN_TRADER_API_KEY`` /
``KRAKEN_TRADER_API_SECRET`` (loaded from ``.env`` by ``conftest.py``).
The whole module is skipped when either is unset — so this file is
safe to commit even on machines without trade credentials.

**Permissions required.** Query Funds + Query open & closed orders &
trades + Create & modify orders + Cancel & close orders. **Withdraw
must be off** per ADR-003.

**Account state independence.** Read-only assertions accept any
state — empty account is fine. Validate-only AddOrder assertions just
verify the validation succeeds (Kraken's response shape on success).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (os.environ.get("KRAKEN_TRADER_API_KEY") and os.environ.get("KRAKEN_TRADER_API_SECRET")),
        reason="KRAKEN_TRADER_API_KEY / KRAKEN_TRADER_API_SECRET unset; live trade test skipped",
    ),
]


BTC_USD = Symbol(base="BTC", quote="USD")


@pytest_asyncio.fixture
async def trade_adapter():
    """Build an adapter from the trade-key env vars and clean up after."""
    config = KrakenConfig.from_env(
        key_var="KRAKEN_TRADER_API_KEY",
        secret_var="KRAKEN_TRADER_API_SECRET",
    )
    a = KrakenAdapter(config=config)
    try:
        yield a
    finally:
        await a.aclose()


@pytest_asyncio.fixture
async def dry_run_adapter():
    """Adapter wired in dry_run mode — every place_order adds validate=true."""
    config = KrakenConfig.from_env(
        key_var="KRAKEN_TRADER_API_KEY",
        secret_var="KRAKEN_TRADER_API_SECRET",
    )
    a = KrakenAdapter(config=config, dry_run=True)
    try:
        yield a
    finally:
        await a.aclose()


# ---------------------------------------------------------------------------
# Read-only methods against live data
# ---------------------------------------------------------------------------


class TestReadOnlyAgainstLive:
    async def test_get_balances_includes_usd_or_returns_empty(
        self, trade_adapter: KrakenAdapter
    ) -> None:
        """Smoke test: BalanceEx via the trade key signs and parses correctly."""
        balances = await trade_adapter.get_balances()
        assert isinstance(balances, list)
        for b in balances:
            assert isinstance(b, Balance)
            assert b.available + b.locked == b.total

    async def test_get_open_orders_returns_list(self, trade_adapter: KrakenAdapter) -> None:
        """OpenOrders signs, parses, and returns a list (possibly empty)."""
        orders = await trade_adapter.get_open_orders()
        assert isinstance(orders, list)
        for o in orders:
            assert isinstance(o, Order)
            assert o.exchange_id is not None and len(o.exchange_id) > 0

    async def test_get_open_orders_filtered_by_symbol(self, trade_adapter: KrakenAdapter) -> None:
        """Symbol filter is applied client-side — should not raise even if
        the account has no BTC/USD orders."""
        orders = await trade_adapter.get_open_orders(symbol=BTC_USD)
        assert isinstance(orders, list)
        for o in orders:
            assert o.symbol == BTC_USD

    async def test_get_trade_history_returns_list(self, trade_adapter: KrakenAdapter) -> None:
        """TradesHistory signs, parses, and returns a list (possibly empty).
        New accounts may have zero trades — we just verify structure."""
        trades = await trade_adapter.get_trade_history(limit=10)
        assert isinstance(trades, list)
        assert len(trades) <= 10
        for t in trades:
            assert isinstance(t, Trade)

    async def test_get_order_status_unknown_txid_raises(self, trade_adapter: KrakenAdapter) -> None:
        """QueryOrders for a fabricated txid: Kraken rejects with
        ``EOrder:Invalid order`` (the txid format itself is invalid).
        Our adapter wraps it as ExchangeError. The exact message is
        Kraken-side; we just assert the error path fires."""
        fake = Order(
            exchange_id="OFAKE-XXXXX-XXXXX",  # well-formed-looking but doesn't exist
            symbol=BTC_USD,
            side=OrderSide.BUY,
            price=Price(amount=Decimal("1"), currency="USD"),
            amount=Amount(value=Decimal("0.001"), asset="BTC"),
            status="open",
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        with pytest.raises(ExchangeError):
            await trade_adapter.get_order_status(fake)


# ---------------------------------------------------------------------------
# Cancel against fabricated order — proves signing + error parse, no real cancel
# ---------------------------------------------------------------------------


class TestCancelErrorPath:
    async def test_cancel_unknown_txid_raises_exchange_error(
        self, trade_adapter: KrakenAdapter
    ) -> None:
        """CancelOrder against a fake txid: Kraken returns EOrder:Unknown
        order. Asserts the cancel signing + error parsing path works
        without ever cancelling a real order."""
        fake = Order(
            exchange_id="OFAKE-XXXXX-XXXXX",
            symbol=BTC_USD,
            side=OrderSide.BUY,
            price=Price(amount=Decimal("1"), currency="USD"),
            amount=Amount(value=Decimal("0.001"), asset="BTC"),
            status="open",
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        with pytest.raises(ExchangeError):
            await trade_adapter.cancel_order(fake)


# ---------------------------------------------------------------------------
# AddOrder dry runs (validate=true) — the headline of slice 2.3.2
# ---------------------------------------------------------------------------


class TestAddOrderDryRun:
    """All tests use dry_run=True so Kraken validates without placing.

    These are the "spotless dry-run" gate before the $10 live test:
    they prove auth + signing + serialization + per-pair precision
    quantization + ordermin/costmin checks all work end-to-end against
    Kraken's real validation engine.
    """

    async def test_validates_well_formed_buy_order(self, dry_run_adapter: KrakenAdapter) -> None:
        """A reasonable BUY at the current ask validates cleanly."""
        # Use a price comfortably below current market so even if dry_run
        # were misconfigured it wouldn't fill. $20 worth.
        current = (await dry_run_adapter.get_current_price(BTC_USD)).amount
        order = Order(
            symbol=BTC_USD,
            side=OrderSide.BUY,
            price=Price(amount=current * Decimal("0.5"), currency="USD"),  # 50% below
            amount=Amount(value=Decimal("20") / current, asset="BTC"),  # ~$20 worth
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        validated = await dry_run_adapter.place_order(order)
        # In dry_run mode, exchange_id is synthesized
        assert validated.exchange_id is not None
        assert validated.exchange_id.startswith("DRYRUN-")
        assert validated.status == "open"

    async def test_validates_well_formed_sell_order(self, dry_run_adapter: KrakenAdapter) -> None:
        """A SELL above market validates cleanly. Account must hold
        enough BTC for Kraken's balance check to pass even in
        validate-mode — so size to the *minimum* legal amount."""
        # Read live AssetPairs to size at the actual ordermin
        await dry_run_adapter._ensure_pair_metadata()  # type: ignore[reportPrivateUsage]
        meta = dry_run_adapter._pair_metadata_for(BTC_USD)  # type: ignore[reportPrivateUsage]
        current = (await dry_run_adapter.get_current_price(BTC_USD)).amount
        order = Order(
            symbol=BTC_USD,
            side=OrderSide.SELL,
            price=Price(amount=current * Decimal("2"), currency="USD"),  # 100% above
            amount=Amount(value=meta.ordermin, asset="BTC"),  # exactly ordermin
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        # If the account holds zero BTC, Kraken validate=true will reject
        # with EOrder:Insufficient funds. Skip the test in that case
        # rather than failing — the test's purpose is to verify the
        # validate-mode signing path, not that the operator holds BTC.
        balance = await dry_run_adapter.get_balance("BTC")
        if balance is None or balance.available < meta.ordermin:
            pytest.skip(
                f"Account holds insufficient BTC for SELL validation "
                f"(need >= {meta.ordermin}, have {balance.available if balance else 0})"
            )
        validated = await dry_run_adapter.place_order(order)
        assert validated.exchange_id is not None
        assert validated.exchange_id.startswith("DRYRUN-")

    async def test_validate_rejects_invalid_pair_via_kraken(self) -> None:
        """A pair Kraken doesn't recognize raises ExchangeError. The
        pair lookup against our AssetPairs cache catches it client-side
        before the AddOrder request is even built."""
        config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADER_API_KEY",
            secret_var="KRAKEN_TRADER_API_SECRET",
        )
        adapter = KrakenAdapter(config=config, dry_run=True)
        try:
            order = Order(
                symbol=Symbol(base="FAKECOIN", quote="USD"),  # 8 chars, fits Symbol's max
                side=OrderSide.BUY,
                price=Price(amount=Decimal("1"), currency="USD"),
                amount=Amount(value=Decimal("1"), asset="FAKECOIN"),
                created_at=Timestamp(dt=datetime.now(UTC)),
            )
            with pytest.raises(ExchangeError):
                await adapter.place_order(order)
        finally:
            await adapter.aclose()

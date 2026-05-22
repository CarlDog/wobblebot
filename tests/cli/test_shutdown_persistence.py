"""Tests for the Stage 8.1.B persistence-on-cancel fix.

Verifies the cli/live + cli/shadow shutdown loops persist the
``status="canceled"`` transition back to storage after each
successful ``adapter.cancel_order()`` call. Bug surfaced
2026-05-18 in a 60-minute shadow session: 3 BUYs cancelled per
log, all 3 still ``status="open"`` in shadow.db at exit.

The test exercises the ``_cancel_all_open`` helper directly with
a mock adapter + in-memory storage, asserting the storage row
transitions iff the adapter cancel succeeded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _cancel_all_open as _cancel_all_open_live
from wobblebot.cli.shadow import _cancel_all_open as _cancel_all_open_shadow
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_order(*, exchange_id: str = "OID-1", side: str = "buy") -> Order:
    return Order(
        id=uuid4(),
        exchange_id=exchange_id,
        symbol=Symbol(base="BTC", quote="USD"),
        side=side,  # type: ignore[arg-type]
        price=Price(amount=Decimal("30000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        status="open",
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


# --------------------------------------------------------------------- #
# Mock adapter — drop-in for KrakenAdapter / ShadowExchangeAdapter in   #
# the shutdown loop. Records cancel calls and returns the orders in    #
# get_open_orders.                                                      #
# --------------------------------------------------------------------- #


class _FakeAdapter:
    """Just enough surface for ``_cancel_all_open``.

    Two knobs:
    - ``open_orders``: list returned by get_open_orders.
    - ``fail_on_cancel``: when True, cancel_order raises.

    Tracks every cancel call in ``cancelled_ids``.
    """

    def __init__(self, *, open_orders: list[Order], fail_on_cancel: bool = False) -> None:
        self._open = open_orders
        self._fail = fail_on_cancel
        self.cancelled_ids: list[str] = []

    async def get_open_orders(self, *, symbol: Symbol | None = None) -> list[Order]:
        if symbol is None:
            return list(self._open)
        return [
            o for o in self._open if o.symbol.base == symbol.base and o.symbol.quote == symbol.quote
        ]

    async def cancel_order(self, order: Order) -> None:
        if self._fail:
            raise ExchangeError("simulated cancel failure")
        self.cancelled_ids.append(order.exchange_id or "")


# --------------------------------------------------------------------- #
# cli/live persistence                                                  #
# --------------------------------------------------------------------- #


class TestLivePersistenceOnCancel:
    async def test_storage_row_transitions_to_canceled(self, storage: SQLiteStorageAdapter) -> None:
        order = _make_order(exchange_id="OID-A")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order])

        cancelled, failed = await _cancel_all_open_live(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 1
        assert failed == 0
        assert adapter.cancelled_ids == ["OID-A"]
        # The storage row's status now reflects the cancellation.
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "canceled"

    async def test_cancel_failure_leaves_storage_at_open(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Don't lie in the audit trail: if the cancel raised, the
        storage row stays ``status='open'`` so reconciliation knows
        the order may still exist."""
        order = _make_order(exchange_id="OID-B")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order], fail_on_cancel=True)

        cancelled, failed = await _cancel_all_open_live(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 0
        assert failed == 1
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "open"

    async def test_multi_order_all_persist(self, storage: SQLiteStorageAdapter) -> None:
        orders = [_make_order(exchange_id=f"OID-{i}") for i in range(3)]
        for o in orders:
            await storage.save_order(o)
        adapter = _FakeAdapter(open_orders=orders)

        cancelled, _ = await _cancel_all_open_live(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 3
        for o in orders:
            roundtripped = await storage.get_order(o.id)
            assert roundtripped is not None
            assert roundtripped.status == "canceled"


# --------------------------------------------------------------------- #
# Stage 8.4 hotfix: finally-block resilience                            #
# --------------------------------------------------------------------- #
#
# 2026-05-19 soak outage: thunderstorm-induced DNS failure made
# _session_usd_balance() raise inside the cli/live finally block. The
# uncaught ExchangeError propagated, skipping the subsequent
# _cancel_all_open() call and leaving three open BUYs on Kraken —
# one of which filled overnight. Per the runbook's "Hard stop"
# discipline, the finally block must cancel orders regardless of
# what else fails. Fix wraps each cleanup step in its own try/except.


class TestSessionEndResilience:
    """Stage 8.4: session-end cleanup must run every step independently.

    If ``_session_usd_balance`` raises (e.g. network down at shutdown),
    the subsequent ``_cancel_all_open`` MUST still run — leaving open
    orders on Kraken is a hard-stop per the v1.0 soak runbook.
    """

    async def test_balance_fetch_failure_does_not_skip_cancel(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the 2026-05-19 soak outage path.

        Mocks ``_session_usd_balance`` to succeed once (for ``started_usd``)
        then raise (for ``ended_usd``), and verifies ``_cancel_all_open``
        still cancels the open order and persists the transition.
        """
        from unittest.mock import AsyncMock, MagicMock

        from wobblebot.cli import live as live_module
        from wobblebot.config.cli import LiveConfig

        # Pre-seed storage with one open order to verify gets cancelled.
        order = _make_order(exchange_id="OID-OUTAGE")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order])

        # First call succeeds (started_usd); second raises (ended_usd).
        call_log = {"n": 0}

        async def flaky_balance(_adapter: Any) -> Decimal:
            call_log["n"] += 1
            if call_log["n"] == 1:
                return Decimal("100")
            raise ExchangeError("simulated DNS failure")

        # Both helpers must be patched: _run_loop calls _session_usd_balance
        # then _session_portfolio_value_usd at startup, and again in the
        # finally block. Outage simulation = both succeed once, then both
        # raise at session-end. The test asserts cancel still runs.
        portfolio_log = {"n": 0}

        async def flaky_portfolio(_adapter: Any, _symbols: Any) -> Decimal:
            portfolio_log["n"] += 1
            if portfolio_log["n"] == 1:
                return Decimal("100")
            raise ExchangeError("simulated DNS failure")

        monkeypatch.setattr(live_module, "_session_usd_balance", flaky_balance)
        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", flaky_portfolio)

        # Stop event pre-set so the main loop body never executes; only
        # the session-start prelude + finally block run.
        stop_event = __import__("asyncio").Event()
        stop_event.set()

        live_cfg = LiveConfig(
            symbols=[Symbol(base="BTC", quote="USD")],
            db=":memory:",
            tick_seconds=5.0,
            max_runtime_minutes=None,
            max_session_loss_usd=Decimal("5"),
        )

        # The engine is never stepped (stop_event pre-set), so a Mock
        # standing in for GridEngine is sufficient. is_stop_requested
        # is read inside the loop but the loop doesn't iterate.
        engine = MagicMock()
        engine.is_stop_requested = False

        exit_code = await live_module._run_loop(
            adapter,  # type: ignore[arg-type]
            engine,
            live_cfg,
            storage,
            stop_event,
        )

        # The fix: cancel still ran despite balance fetch raising.
        assert adapter.cancelled_ids == ["OID-OUTAGE"]
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "canceled"
        # And the loop returned a normal exit code (didn't raise out).
        assert exit_code == 0


# --------------------------------------------------------------------- #
# cli/shadow persistence                                                #
# --------------------------------------------------------------------- #


class TestShadowPersistenceOnCancel:
    async def test_storage_row_transitions_to_canceled(self, storage: SQLiteStorageAdapter) -> None:
        order = _make_order(exchange_id="SHDW-A")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order])

        cancelled, failed = await _cancel_all_open_shadow(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 1
        assert failed == 0
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "canceled"

    async def test_cancel_failure_leaves_storage_at_open(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _make_order(exchange_id="SHDW-B")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order], fail_on_cancel=True)

        cancelled, failed = await _cancel_all_open_shadow(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 0
        assert failed == 1
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "open"


# --------------------------------------------------------------------- #
# Stage 8.4 hotfix #2: per-tick balance fetch resilience                #
# --------------------------------------------------------------------- #
#
# 2026-05-20 15:03 UTC: cli/live crashed with httpcore.ReadTimeout on
# the per-tick loss-cap balance check (line 209 of _run_one_tick). The
# earlier e2b6cfc fix only protected the finally-block call site;
# this scenario is the OTHER call site — during normal operation, a
# transient Kraken timeout to /0/private/BalanceEx killed the daemon.
# Fix wraps the call in try/except; on error, skip the cap check for
# this tick, log a warning, and return False (no cap trip).


class TestPerTickBalanceResilience:
    """Regression: transient balance-fetch failures during normal
    operation must NOT kill cli/live. The loss-cap check is opt-in
    safety; skipping it for one tick because Kraken timed out is
    survivable. Killing the whole engine because of one timeout is
    not — orders sit on the book until manual recovery, exactly
    the failure mode the 2026-05-19 outage already demonstrated."""

    async def test_balance_fetch_failure_returns_false_no_raise(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the 2026-05-20 15:03 UTC crash.

        Mocks ``_session_portfolio_value_usd`` to raise ``ExchangeError``
        and verifies ``_run_one_tick`` swallows it: returns ``False``
        (no cap trip) instead of propagating up to the main loop.

        Note: 2026-05-22 hotfix replaced the cap-check helper from
        ``_session_usd_balance`` to ``_session_portfolio_value_usd``
        (mark-to-market). The resilience contract is unchanged; the
        patched name updates to match the new helper.
        """
        from unittest.mock import MagicMock

        from wobblebot.cli import live as live_module
        from wobblebot.cli.live import _run_one_tick
        from wobblebot.config.cli import LiveConfig

        async def always_fails(_adapter: Any, _symbols: Any) -> Decimal:
            raise ExchangeError("simulated Kraken /BalanceEx timeout")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", always_fails)

        # Engine doesn't get stepped (we'd need a real grid + symbol).
        # _run_one_tick iterates symbols first, then calls
        # _session_portfolio_value_usd. With one symbol step that
        # returns a benign result, the loop body completes; the
        # cap-check call site is where the exception lands.
        live_cfg = LiveConfig(
            symbols=[Symbol(base="BTC", quote="USD")],
            db=":memory:",
            tick_seconds=5.0,
            max_runtime_minutes=None,
            max_session_loss_usd=Decimal("5"),
        )
        engine = MagicMock()
        # engine.step is awaited; mock it to return a benign result.
        from unittest.mock import AsyncMock

        engine.step = AsyncMock(return_value=MagicMock(action="stepped", fills=0))

        # _run_one_tick should NOT raise; should return False (no cap trip).
        result = await _run_one_tick(
            adapter=MagicMock(),
            engine=engine,
            live=live_cfg,
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )
        assert result is False


# --------------------------------------------------------------------- #
# Stage 8.4 hotfix #3 (2026-05-22): session-loss cap is mark-to-market  #
# --------------------------------------------------------------------- #
#
# Soak Day 5 morning: cli/live crashed at 09:18:31 with "session loss
# cap exceeded" immediately after a $10 BUY filled. USD balance had
# dropped by ~$10 (asset conversion: USD → BTC) but portfolio value
# was preserved (BTC held now worth ~$10). The cap was checking USD
# balance only, so the first BUY of any session where
# order_size_usd > max_session_loss_usd would trip it.
#
# Fix: cap is checked against mark-to-market portfolio value (USD
# balance + Σ base × current_price). A BUY no longer reads as a loss.


class TestSessionPortfolioValueUsd:
    """The new helper — USD + Σ base × ticker for configured symbols."""

    async def test_usd_only_when_no_base_balance(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from wobblebot.adapters.kraken_exchange import KrakenAdapter
        from wobblebot.cli.live import _session_portfolio_value_usd
        from wobblebot.domain.models import Balance

        adapter = MagicMock(spec=KrakenAdapter)
        adapter.get_balances = AsyncMock(
            return_value=[
                Balance(
                    asset="USD", total=Decimal("100"), available=Decimal("100"), locked=Decimal("0")
                )
            ]
        )
        # get_current_price not called when base balance is zero.
        adapter.get_current_price = AsyncMock(side_effect=AssertionError("must not call"))

        value = await _session_portfolio_value_usd(adapter, (Symbol(base="BTC", quote="USD"),))
        assert value == Decimal("100")

    async def test_adds_base_mark_to_market(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from wobblebot.adapters.kraken_exchange import KrakenAdapter
        from wobblebot.cli.live import _session_portfolio_value_usd
        from wobblebot.domain.models import Balance

        adapter = MagicMock(spec=KrakenAdapter)
        adapter.get_balances = AsyncMock(
            return_value=[
                Balance(
                    asset="USD", total=Decimal("90"), available=Decimal("90"), locked=Decimal("0")
                ),
                Balance(
                    asset="BTC",
                    total=Decimal("0.0001"),
                    available=Decimal("0.0001"),
                    locked=Decimal("0"),
                ),
            ]
        )
        adapter.get_current_price = AsyncMock(
            return_value=Price(amount=Decimal("100000"), currency="USD")
        )

        value = await _session_portfolio_value_usd(adapter, (Symbol(base="BTC", quote="USD"),))
        # 90 USD + (0.0001 BTC * $100,000) = 100
        assert value == Decimal("100.00000000")

    async def test_dedupes_repeated_base_across_symbols(self) -> None:
        """If symbols share a base (e.g. BTC/USD and BTC/EUR, hypothetically),
        the base balance gets counted once, not N times."""
        from unittest.mock import AsyncMock, MagicMock

        from wobblebot.adapters.kraken_exchange import KrakenAdapter
        from wobblebot.cli.live import _session_portfolio_value_usd
        from wobblebot.domain.models import Balance

        adapter = MagicMock(spec=KrakenAdapter)
        adapter.get_balances = AsyncMock(
            return_value=[
                Balance(
                    asset="USD", total=Decimal("0"), available=Decimal("0"), locked=Decimal("0")
                ),
                Balance(
                    asset="BTC", total=Decimal("1"), available=Decimal("1"), locked=Decimal("0")
                ),
            ]
        )
        adapter.get_current_price = AsyncMock(
            return_value=Price(amount=Decimal("100000"), currency="USD")
        )

        # Two USD-quoted symbols with same base — still counts BTC once.
        value = await _session_portfolio_value_usd(
            adapter,
            (
                Symbol(base="BTC", quote="USD"),
                Symbol(base="BTC", quote="USD"),
            ),
        )
        assert value == Decimal("100000")
        # get_current_price called only once (deduplication).
        assert adapter.get_current_price.await_count == 1


class TestSessionLossCapAccountsForAssetConversion:
    """Regression for the 2026-05-22 09:18:31 cli/live crash.

    A BUY fill is USD → BTC, an asset conversion. Mark-to-market
    portfolio value is preserved. The cap check must use portfolio
    value, not USD balance, or every BUY whose order_size_usd >
    max_session_loss_usd would trip it.
    """

    async def test_buy_fill_does_not_trip_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from wobblebot.cli import live as live_module
        from wobblebot.cli.live import _run_one_tick
        from wobblebot.config.cli import LiveConfig

        # Started session at $100 portfolio value (e.g., $100 USD + 0 BTC).
        # A BUY filled mid-tick: now $90 USD + 0.0001 BTC × $100k = still $100.
        # USD balance alone reads -$10; portfolio value reads $0 delta.
        async def portfolio_value(_adapter: Any, _symbols: Any) -> Decimal:
            return Decimal("100")  # mark-to-market preserved

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", portfolio_value)

        live_cfg = LiveConfig(
            symbols=[Symbol(base="BTC", quote="USD")],
            db=":memory:",
            tick_seconds=5.0,
            max_runtime_minutes=None,
            max_session_loss_usd=Decimal("5"),  # the Day-4 setting that crashed
        )
        engine = MagicMock()
        engine.step = AsyncMock(
            return_value=MagicMock(
                action="filled",
                fills=1,
                counters_placed=1,
                placed=0,
                refusals=0,
                offside=False,
            )
        )

        result = await _run_one_tick(
            adapter=MagicMock(),
            engine=engine,
            live=live_cfg,
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )
        assert result is False  # cap did NOT trip

    async def test_actual_realized_loss_still_trips_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap must still fire on a real mark-to-market drawdown
        (e.g., bought high, price collapsed). This is the regression's
        twin: don't over-correct by suppressing the cap entirely."""
        from unittest.mock import AsyncMock, MagicMock

        from wobblebot.cli import live as live_module
        from wobblebot.cli.live import _run_one_tick
        from wobblebot.config.cli import LiveConfig

        # Portfolio value dropped from $100 to $90 — a real $10 mark-to-market
        # loss (e.g., held BTC lost value relative to start).
        async def portfolio_value(_adapter: Any, _symbols: Any) -> Decimal:
            return Decimal("90")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", portfolio_value)

        live_cfg = LiveConfig(
            symbols=[Symbol(base="BTC", quote="USD")],
            db=":memory:",
            tick_seconds=5.0,
            max_runtime_minutes=None,
            max_session_loss_usd=Decimal("5"),
        )
        engine = MagicMock()
        engine.step = AsyncMock(
            return_value=MagicMock(
                action="held", fills=0, counters_placed=0, placed=0, refusals=0, offside=False
            )
        )

        result = await _run_one_tick(
            adapter=MagicMock(),
            engine=engine,
            live=live_cfg,
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
        )
        assert result is True  # cap DID trip

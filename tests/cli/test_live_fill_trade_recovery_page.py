"""ADR-046 in cli/live: when the engine gives up recovering a confirmed
fill's trade rows, the operator gets a critical page naming the order,
and a restart resumes a sweep the previous process left behind.

Drives ``_run_one_tick`` against the mock exchange with the trade
withheld (Kraken's 2026-09-10 shape) and the attempt bound patched to
one, so the second tick abandons.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _AuthEscalation, _resume_pending_fill_trades, _run_one_tick
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services import grid_engine as grid_engine_module
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


def _exchange() -> MockExchangeAdapter:
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
    )


def _live() -> LiveConfig:
    return LiveConfig(symbols=[BTC_USD])


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _fill_with_hidden_trade(
    exchange: MockExchangeAdapter, engine: GridEngine, storage: SQLiteStorageAdapter
) -> str:
    await engine.step(BTC_USD)
    buy = next(
        o
        for o in await storage.get_open_orders(symbol=BTC_USD)
        if o.side is OrderSide.BUY and o.price.amount == Decimal("49500")
    )
    assert buy.exchange_id
    exchange.withhold_trades(buy.exchange_id)
    exchange.set_price(BTC_USD, Decimal("49400"))
    return buy.exchange_id


async def test_abandoned_recovery_pages_the_operator_with_the_order_id(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    exchange_id = await _fill_with_hidden_trade(exchange, engine, storage)
    escalation = _AuthEscalation()

    # Tick 1 detects the fill and writes the marker; tick 2's sweep hits the bound.
    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )
    await _run_one_tick(
        exchange, engine, _live(), 2, Decimal("100000"), notifier, escalation=escalation
    )

    rows = await storage.get_notifications()
    pages = [r.notification for r in rows if r.notification.title.startswith("Fill recorded")]
    assert len(pages) == 1
    page = pages[0]
    assert page.level == "critical"
    assert exchange_id in page.message
    assert "reconcile_trade_history" in page.message
    assert page.context["exchange_ids"] == [exchange_id]
    assert page.context["reason"] == "fill_trades_unrecovered"
    # The ordinary fill notification from tick 1 is still there too.
    assert any(r.notification.title.startswith("Fills:") for r in rows)


async def test_successful_recovery_does_not_page(storage: SQLiteStorageAdapter) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    exchange_id = await _fill_with_hidden_trade(exchange, engine, storage)
    escalation = _AuthEscalation()

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )
    exchange.release_trades(exchange_id)
    await _run_one_tick(
        exchange, engine, _live(), 2, Decimal("100000"), notifier, escalation=escalation
    )

    rows = await storage.get_notifications()
    assert not any(r.notification.title.startswith("Fill recorded") for r in rows)
    assert len(await storage.get_trades(symbol=BTC_USD)) == 1
    assert await storage.get_pending_fill_trades(BTC_USD) == []


async def test_page_survives_a_step_that_raises_right_after_the_give_up(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding (2026-09-18): the give-up write is durable and one-shot,
    but the page rode the StepResult. If the trading step raised after the
    sweep, the per-symbol handler swallowed it and the page was lost."""
    monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_LOOKUP_EVERY_TICKS", 1)

    class _TickerFails(MockExchangeAdapter):
        fail_ticker = False

        async def get_ticker(self, symbol: Symbol):  # type: ignore[no-untyped-def]
            if self.fail_ticker:
                raise ExchangeError("Ticker unavailable")
            return await super().get_ticker(symbol)

    exchange = _TickerFails(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    exchange_id = await _fill_with_hidden_trade(exchange, engine, storage)
    escalation = _AuthEscalation()
    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )
    exchange.fail_ticker = True

    # Tick 2: the sweep gives up, then the step raises inside the same try.
    await _run_one_tick(
        exchange, engine, _live(), 2, Decimal("100000"), notifier, escalation=escalation
    )
    # The page must already exist HERE, from the failure-path drain -- not
    # only after a later healthy tick happens to drain the buffer.
    after_failure = await storage.get_notifications()
    assert [
        r.notification.title
        for r in after_failure
        if r.notification.title.startswith("Fill recorded")
    ] == [f"Fill recorded without its trade rows: {BTC_USD}"]

    exchange.fail_ticker = False
    await _run_one_tick(
        exchange, engine, _live(), 3, Decimal("100000"), notifier, escalation=escalation
    )

    rows = await storage.get_notifications()
    pages = [r.notification for r in rows if r.notification.title.startswith("Fill recorded")]
    assert len(pages) == 1  # and not paged a second time
    assert exchange_id in pages[0].message


async def test_boot_resume_calls_out_markers_on_symbols_it_will_not_sweep(
    storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    await _fill_with_hidden_trade(exchange, engine, storage)
    await engine.step(BTC_USD)  # writes the BTC marker
    restarted = GridEngine(exchange, storage, grid_config(), safety_config())

    with caplog.at_level(logging.WARNING, logger="wobblebot.cli.live"):
        await _resume_pending_fill_trades(restarted, [Symbol(base="SOL", quote="USD")])

    assert "not in live.symbols" in caplog.text
    assert "recovery resumes" not in caplog.text
    assert restarted.pending_fill_trade_symbols() == frozenset({BTC_USD})


async def test_fee_drift_found_by_the_sweep_pages_on_the_recovery_tick(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completeness critic F2 (2026-09-18): the ADR-038 drift page was gated on
    ``fills > 0`` in the same tick, so a drift on a row the sweep recovered
    paged only at the symbol's NEXT fill. The mock charges 0.26%, which
    matches neither believed rate, so the recovered row IS a drift."""
    monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_LOOKUP_EVERY_TICKS", 1)
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    escalation = _AuthEscalation()
    fee_alerted: set[Symbol] = set()
    exchange_id = await _fill_with_hidden_trade(exchange, engine, storage)

    await _run_one_tick(
        exchange,
        engine,
        _live(),
        1,
        Decimal("100000"),
        notifier,
        escalation=escalation,
        fee_alerted=fee_alerted,
    )
    # No row yet, so nothing to judge and nothing paged.
    assert engine.fee_anomaly_count(BTC_USD) == 0
    exchange.release_trades(exchange_id)
    await _run_one_tick(
        exchange,
        engine,
        _live(),
        2,
        Decimal("100000"),
        notifier,
        escalation=escalation,
        fee_alerted=fee_alerted,
    )

    assert engine.fee_anomaly_count(BTC_USD) == 1
    titles = [r.notification.title for r in await storage.get_notifications()]
    assert titles.count(f"Fee drift: {BTC_USD}") == 1
    assert fee_alerted == {BTC_USD}


async def test_boot_resume_warning_counts_only_the_symbols_it_will_sweep(
    storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    """R6 finding 3: the WARNING printed the all-symbols marker count beside
    the swept symbols, so "2 fill(s) ... (symbols: BTC/USD)" read as if both
    were being worked while the next line said SOL would not be."""
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    await _fill_with_hidden_trade(exchange, engine, storage)
    await engine.step(BTC_USD)  # writes the BTC marker
    sol = Symbol(base="SOL", quote="USD")
    stray = Order(
        symbol=sol,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("100"), currency="USD"),
        amount=Amount(value=Decimal("1"), asset="SOL"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )
    stray.mark_open("SOL-STRAY-1")
    await storage.save_order(stray)
    await storage.save_fill_pending_trades(
        stray.model_copy(update={"status": "closed", "filled_amount": Decimal("1")})
    )
    restarted = GridEngine(exchange, storage, grid_config(), safety_config())

    with caplog.at_level(logging.WARNING, logger="wobblebot.cli.live"):
        await _resume_pending_fill_trades(restarted, [BTC_USD])

    resumes = [r for r in caplog.records if "recovery resumes" in r.getMessage()]
    assert len(resumes) == 1
    assert resumes[0].getMessage().startswith("1 fill(s)")
    assert f"(symbols: {BTC_USD})" in resumes[0].getMessage()
    assert "SOL/USD" in caplog.text and "not in live.symbols" in caplog.text


async def test_fee_drift_on_a_partially_recovered_row_pages_when_recorded(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix-round reviewer, round 2: the page keyed off COMPLETED recoveries, so
    a drift on a half row the sweep recorded (fill still pending) waited for
    the completing row -- or, after a give-up, for the symbol's next fill. The
    page keys off the anomaly counter alone; fee_alerted keeps it to one."""
    monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_LOOKUP_EVERY_TICKS", 1)

    class _HalfFirst(MockExchangeAdapter):
        half: list[Trade] = []

        async def get_order_trades(self, order: Order) -> list[Trade]:
            if self.half:
                return list(self.half)
            return await super().get_order_trades(order)

    exchange = _HalfFirst(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    escalation = _AuthEscalation()
    fee_alerted: set[Symbol] = set()
    await engine.step(BTC_USD)
    buy = next(
        o
        for o in await storage.get_open_orders(symbol=BTC_USD)
        if o.side is OrderSide.BUY and o.price.amount == Decimal("49500")
    )
    assert buy.exchange_id
    exchange.withhold_trades(buy.exchange_id)
    exchange.set_price(BTC_USD, Decimal("49400"))
    half_qty = buy.amount.value / 2
    half = Trade(
        id="T-HALF",
        order_id=buy.exchange_id,
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("49500"), currency="USD"),
        amount=Amount(value=half_qty, asset="BTC"),
        fee=Decimal("0"),  # matches neither believed rate: a drift
        cost=Decimal("49500") * half_qty,
        executed_at=Timestamp(dt=datetime.now(UTC)),
    )

    async def tick(n: int) -> list[str]:
        await _run_one_tick(
            exchange,
            engine,
            _live(),
            n,
            Decimal("100000"),
            notifier,
            escalation=escalation,
            fee_alerted=fee_alerted,
        )
        return [r.notification.title for r in await storage.get_notifications()]

    await tick(1)  # fill detected, rows hidden
    exchange.half = [half]
    titles = await tick(2)  # half recorded; fill still pending
    assert engine.fee_anomaly_count(BTC_USD) == 1
    assert len(await storage.get_pending_fill_trades(BTC_USD)) == 1
    assert titles.count(f"Fee drift: {BTC_USD}") == 1

    exchange.half = []
    exchange.release_trades(buy.exchange_id)
    titles = await tick(3)  # the real row completes the fill: no second page
    assert engine.fee_anomaly_count(BTC_USD) == 2
    assert await storage.get_pending_fill_trades(BTC_USD) == []
    assert titles.count(f"Fee drift: {BTC_USD}") == 1

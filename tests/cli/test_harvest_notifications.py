"""Tests for cli/harvest notification emit (Stage 5.5.B).

The full proposal-generated / withdrawal-executed / withdrawal-failed
flow is covered by the Stage 5.7 integration check. These unit tests
target the ``notify`` helper in isolation plus the proposal-emit
path through ``_run_cycle`` against an in-memory storage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import notify
from wobblebot.cli.harvest import _run_cycle
from wobblebot.config.cli import HarvestConfig
from wobblebot.config.grid import GridConfig, GridLevels
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.config.schedules import SchedulesConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import NotifierError
from wobblebot.ports.notifier import Notification

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _wob_config(*, harvester_enabled: bool = False) -> WobbleBotConfig:
    """Minimal WobbleBotConfig sufficient for _run_cycle."""
    return WobbleBotConfig(
        grid=GridConfig(
            default=GridLevels(
                spacing_percentage=Decimal("1"),
                levels_above=3,
                levels_below=3,
                order_size_usd=Decimal("10"),
            )
        ),
        safety=SafetyConfig(
            max_total_exposure_usd=Decimal("100000"),
            max_daily_spend_usd=Decimal("100000"),
            max_per_coin_exposure_usd=Decimal("100000"),
            max_orders_per_coin=100,
            emergency_stop=EmergencyStopConfig(
                enabled=True,
                max_loss_percentage=Decimal("20"),
                min_exchange_balance_usd=Decimal("0"),
            ),
        ),
        harvester=HarvesterConfig(
            enabled=harvester_enabled,
            min_exchange_liquidity_usd=Decimal("100"),
            topup_threshold_usd=Decimal("200"),
            surplus_threshold_usd=Decimal("500"),
            max_withdrawal_per_day_usd=Decimal("1000"),
        ),
        harvest=HarvestConfig(),
        schedules=SchedulesConfig(root={"harvest": "5m"}),  # type: ignore[arg-type]
    )


async def test_notify_with_none_is_noop() -> None:
    await notify(None, level="info", title="t", message="m")


async def test_notify_persists_via_storage(storage: SQLiteStorageAdapter) -> None:
    notifier = SqliteNotifierAdapter(storage)
    await notify(
        notifier,
        level="warning",
        title="proposal generated",
        message="exchange_to_bank 100 USD",
        context={"asset": "USD", "amount": "100"},
    )
    rows = await storage.get_notifications()
    assert len(rows) == 1
    assert rows[0].notification.level == "warning"


async def test_notify_swallows_notifier_errors() -> None:
    """Notifier errors must NOT break the harvest loop."""

    class _FailingNotifier:
        async def send_notification(self, _: Notification) -> None:
            raise NotifierError("transport down")

        async def send_error_alert(self, error: Exception, context: dict) -> None:
            pass

    # Should NOT raise.
    await notify(
        _FailingNotifier(),  # type: ignore[arg-type]
        level="info",
        title="x",
        message="y",
    )


async def test_run_cycle_emits_notification_on_proposal(
    storage: SQLiteStorageAdapter,
) -> None:
    # Surplus balance (700 > 500 threshold) → harvester proposes a
    # scrape to bank → _run_cycle writes a notification.
    config = _wob_config()
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("700"), "BTC": Decimal("0")},
        starting_prices={Symbol(base="BTC", quote="USD"): Decimal("50000")},
    )
    notifier = SqliteNotifierAdapter(storage)
    ok = await _run_cycle(exchange, config=config, storage=storage, notifier=notifier)
    assert ok is True
    rows = await storage.get_notifications()
    assert len(rows) == 1
    n = rows[0].notification
    assert "Harvester proposal" in n.title
    assert n.context["direction"] == "exchange_to_bank"


async def test_run_cycle_no_notification_when_balance_in_hold_band(
    storage: SQLiteStorageAdapter,
) -> None:
    # 300 USD: above topup (200), below surplus (500) → no proposal → no notification.
    config = _wob_config()
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("300"), "BTC": Decimal("0")},
        starting_prices={Symbol(base="BTC", quote="USD"): Decimal("50000")},
    )
    notifier = SqliteNotifierAdapter(storage)
    ok = await _run_cycle(exchange, config=config, storage=storage, notifier=notifier)
    assert ok is True
    rows = await storage.get_notifications()
    assert rows == []

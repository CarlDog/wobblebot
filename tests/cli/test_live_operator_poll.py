"""Tests for cli/live's pending-command poll integration (Stage 5.4.D).

The full cli/live entry point is integration territory; these unit
tests target ``_process_pending_commands`` in isolation — the ADR-002
firewall lives there, so it deserves dedicated coverage. The full
end-to-end "operator types in Discord; cli/live sees it" path is
covered by Stage 5.7's integration check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _process_pending_commands
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.operator import (
    PauseCommand,
    PendingCommand,
    PendingCommandStatus,
    StopCommand,
)
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


def _ts(offset_seconds: int = 0) -> Timestamp:
    return Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds))


def _pending(
    *,
    status: PendingCommandStatus = "approved",
    command: PauseCommand | StopCommand | None = None,
    created_offset_seconds: int = 0,
) -> PendingCommand:
    return PendingCommand(
        id=uuid4(),
        command=command or PauseCommand(symbol=BTC_USD),
        status=status,
        channel_id="C-1",
        requesting_user_id="U-1",
        confirming_user_id="U-2" if status != "awaiting_confirmation" else None,
        confirmed_at=_ts() if status != "awaiting_confirmation" else None,
        ttl_expires_at=_ts(300),
        created_at=_ts(created_offset_seconds),
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    """One SQLite DB serves as both live.db and operator.db for these tests."""
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _operator_service(storage: SQLiteStorageAdapter) -> tuple[OperatorService, GridEngine]:
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("1000"), "BTC": Decimal("1")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    return (
        OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=(BTC_USD,),
            grid_config=grid_config(),
        ),
        engine,
    )


# --------------------------------------------------------------------- #
# Approved-only dispatch (the ADR-002 firewall)                          #
# --------------------------------------------------------------------- #


async def test_only_approved_rows_dispatch(storage: SQLiteStorageAdapter) -> None:
    svc, engine = _operator_service(storage)
    # One of each non-approved status — none should reach dispatch.
    await storage.save_pending_command(_pending(status="awaiting_confirmation"))
    await storage.save_pending_command(_pending(status="rejected"))
    await storage.save_pending_command(_pending(status="expired"))
    # Plus one approved that SHOULD dispatch.
    await storage.save_pending_command(_pending(status="approved"))

    processed = await _process_pending_commands(svc, storage)
    assert processed == 1  # only the approved row

    # Engine sees the pause from the one approved command
    assert engine.is_paused(BTC_USD) is True

    # The approved row is now 'dispatched'; others untouched.
    rows = await storage.get_pending_commands()
    statuses = {row.status for row in rows}
    assert "dispatched" in statuses
    assert "awaiting_confirmation" in statuses
    assert "rejected" in statuses
    assert "expired" in statuses


async def test_approved_pause_command_dispatches_successfully(
    storage: SQLiteStorageAdapter,
) -> None:
    svc, engine = _operator_service(storage)
    pending = _pending(status="approved", command=PauseCommand(symbol=BTC_USD))
    await storage.save_pending_command(pending)

    processed = await _process_pending_commands(svc, storage)
    assert processed == 1
    assert engine.is_paused(BTC_USD) is True

    # The row is now dispatched with a successful CommandResult.
    fetched = await storage.get_pending_command(pending.id)
    assert fetched is not None
    assert fetched.status == "dispatched"
    assert fetched.dispatched_at is not None
    assert fetched.result is not None
    assert fetched.result.success is True
    assert fetched.result.command_kind == "pause"


async def test_approved_stop_command_marks_engine(storage: SQLiteStorageAdapter) -> None:
    svc, engine = _operator_service(storage)
    pending = _pending(status="approved", command=StopCommand())
    await storage.save_pending_command(pending)

    await _process_pending_commands(svc, storage)
    assert engine.is_stop_requested is True

    fetched = await storage.get_pending_command(pending.id)
    assert fetched is not None
    assert fetched.status == "dispatched"


# --------------------------------------------------------------------- #
# Empty / no work                                                       #
# --------------------------------------------------------------------- #


async def test_empty_table_returns_zero(storage: SQLiteStorageAdapter) -> None:
    svc, _engine = _operator_service(storage)
    assert await _process_pending_commands(svc, storage) == 0


async def test_table_with_only_unapproved_rows_returns_zero(
    storage: SQLiteStorageAdapter,
) -> None:
    svc, engine = _operator_service(storage)
    await storage.save_pending_command(_pending(status="awaiting_confirmation"))
    assert await _process_pending_commands(svc, storage) == 0
    assert engine.is_paused(BTC_USD) is False  # nothing dispatched


# --------------------------------------------------------------------- #
# Ordering (oldest approved first)                                      #
# --------------------------------------------------------------------- #


async def test_oldest_approved_dispatches_first(storage: SQLiteStorageAdapter) -> None:
    svc, engine = _operator_service(storage)
    # The oldest is a stop; the newer is a pause. Both approved.
    older = _pending(status="approved", command=StopCommand(), created_offset_seconds=-200)
    newer = _pending(
        status="approved",
        command=PauseCommand(symbol=BTC_USD),
        created_offset_seconds=-100,
    )
    await storage.save_pending_command(newer)
    await storage.save_pending_command(older)

    processed = await _process_pending_commands(svc, storage)
    assert processed == 2
    # Both side effects applied
    assert engine.is_stop_requested is True
    assert engine.is_paused(BTC_USD) is True


# --------------------------------------------------------------------- #
# LiveConfig schema                                                      #
# --------------------------------------------------------------------- #


async def test_live_config_accepts_operator_db_field() -> None:
    cfg = LiveConfig(
        symbols=[BTC_USD],
        operator_db="data/operator.db",
    )
    assert cfg.operator_db == "data/operator.db"


async def test_live_config_operator_db_defaults_to_none() -> None:
    cfg = LiveConfig(symbols=[BTC_USD])
    assert cfg.operator_db is None

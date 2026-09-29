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
    ExecuteProposalCommand,
    PauseCommand,
    PendingCommand,
    PendingCommandStatus,
    QueueableCommand,
    ReanchorCommand,
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
    command: QueueableCommand | None = None,
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

    processed = await _process_pending_commands(svc, storage, None)
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

    processed = await _process_pending_commands(svc, storage, None)
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


async def test_approved_reanchor_dispatches_through_firewall(
    storage: SQLiteStorageAdapter,
) -> None:
    """ADR-031 end-to-end: an approved reanchor row moves the anchor and
    places the layout in-process, via the same WHERE status='approved'
    poll every other command uses (zero new firewall machinery)."""
    svc, _engine = _operator_service(storage)
    pending = _pending(status="approved", command=ReanchorCommand(symbol=BTC_USD))
    await storage.save_pending_command(pending)

    from datetime import UTC, datetime, timedelta

    processed = await _process_pending_commands(
        svc,
        storage,
        None,
        reanchor_approval_after=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert processed == 1

    fetched = await storage.get_pending_command(pending.id)
    assert fetched is not None
    assert fetched.status == "dispatched"
    assert fetched.result is not None
    assert fetched.result.success is True
    assert fetched.result.command_kind == "reanchor"
    assert "re-anchored" in fetched.result.message
    state = await storage.get_grid_state(BTC_USD)
    assert state is not None  # anchor saved at the mock price
    opens = await storage.get_open_orders(symbol=BTC_USD)
    assert len(opens) > 0  # layout placed in-process


async def test_stale_valuation_refuses_reanchor_but_still_dispatches_stop(
    storage: SQLiteStorageAdapter,
) -> None:
    """A stale loss-cap valuation must block the command path that places a grid."""
    svc, engine = _operator_service(storage)
    reanchor = _pending(status="approved", command=ReanchorCommand(symbol=BTC_USD))
    stop = _pending(status="approved", command=StopCommand(), created_offset_seconds=1)
    await storage.save_pending_command(reanchor)
    await storage.save_pending_command(stop)

    processed = await _process_pending_commands(svc, storage, None, valuation_stale=True)
    assert processed == 2

    refused = await storage.get_pending_command(reanchor.id)
    assert refused is not None
    assert refused.status == "failed"
    assert refused.result is not None
    assert refused.result.success is False
    assert "valuation unavailable" in refused.result.message
    assert await storage.get_grid_state(BTC_USD) is None
    assert await storage.get_open_orders(symbol=BTC_USD) == []
    assert engine.is_stop_requested is True


async def test_failed_refusal_write_cannot_replay_reanchor_after_recovery(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient operator DB failure must not turn a refused approval into a grid."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from wobblebot.ports.exceptions import StorageError

    svc, _engine = _operator_service(storage)
    pending = _pending(status="approved", command=ReanchorCommand(symbol=BTC_USD))
    await storage.save_pending_command(pending)
    save = storage.save_pending_command
    attempts = 0

    async def _fail_once(row: PendingCommand) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StorageError("simulated operator DB write failure")
        await save(row)

    monkeypatch.setattr(storage, "save_pending_command", _fail_once)
    dispatch = AsyncMock(side_effect=AssertionError("old approval must not dispatch"))
    monkeypatch.setattr(svc, "dispatch_command", dispatch)

    await _process_pending_commands(
        svc,
        storage,
        None,
        valuation_stale=True,
        reanchor_approval_after=datetime.now(UTC),
    )
    still_approved = await storage.get_pending_command(pending.id)
    assert still_approved is not None and still_approved.status == "approved"

    await _process_pending_commands(
        svc,
        storage,
        None,
        valuation_stale=False,
        reanchor_approval_after=datetime.now(UTC),
    )
    refused = await storage.get_pending_command(pending.id)
    assert refused is not None and refused.status == "failed"
    assert refused.result is not None and "predates" in refused.result.message
    dispatch.assert_not_awaited()


async def test_pre_session_reanchor_approval_needs_new_confirmation(
    storage: SQLiteStorageAdapter,
) -> None:
    """A fresh session must not replay an approval left by a prior outage."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    svc, _engine = _operator_service(storage)
    pending = _pending(status="approved", command=ReanchorCommand(symbol=BTC_USD))
    await storage.save_pending_command(pending)
    floor = datetime.now(UTC)
    dispatch = AsyncMock(side_effect=AssertionError("pre-session approval must not dispatch"))
    svc.dispatch_command = dispatch  # type: ignore[method-assign]

    await _process_pending_commands(svc, storage, None, reanchor_approval_after=floor)
    refused = await storage.get_pending_command(pending.id)
    assert refused is not None and refused.status == "failed"
    dispatch.assert_not_awaited()


async def test_loop_refuses_reanchor_queued_after_failed_valuation(
    storage: SQLiteStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop must pass its stale gate to the operator-command poll."""
    import asyncio
    from unittest.mock import AsyncMock

    from wobblebot.cli import live as live_module
    from wobblebot.cli.live import _run_loop
    from wobblebot.ports.exceptions import ExchangeError

    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("1000"), "BTC": Decimal("1")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    service = OperatorService(
        engine=engine,
        storage=storage,
        active_symbols=(BTC_USD,),
        grid_config=grid_config(),
    )
    pending = _pending(status="approved", command=ReanchorCommand(symbol=BTC_USD))
    valuation_calls = 0

    async def _value(*_args: object, **_kwargs: object) -> Decimal:
        nonlocal valuation_calls
        valuation_calls += 1
        if valuation_calls == 2:  # tick 1, after an ordinary engine step
            await storage.save_pending_command(pending)
            raise ExchangeError("simulated BalanceEx outage")
        if valuation_calls == 4:  # tick 2 post-check; exit after the gate was exercised
            engine.request_stop()
        return Decimal("100")

    monkeypatch.setattr(live_module, "_session_portfolio_value_usd", _value)
    monkeypatch.setattr(
        service,
        "dispatch_command",
        AsyncMock(side_effect=AssertionError("stale re-anchor must not dispatch")),
    )

    await _run_loop(
        exchange,
        engine,
        LiveConfig(symbols=[BTC_USD], tick_seconds=0.001),
        storage,
        asyncio.Event(),
        operator_service=service,
        operator_storage=storage,
    )

    assert valuation_calls >= 4
    refused = await storage.get_pending_command(pending.id)
    assert refused is not None
    assert refused.status == "failed"
    assert refused.result is not None
    assert "valuation unavailable" in refused.result.message
    service.dispatch_command.assert_not_awaited()


async def test_loop_does_not_replay_reanchor_after_refusal_write_fails(
    storage: SQLiteStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop must advance and pass the approval floor on valuation recovery."""
    import asyncio
    from unittest.mock import AsyncMock

    from wobblebot.cli import live as live_module
    from wobblebot.cli.live import _run_loop
    from wobblebot.ports.exceptions import ExchangeError, StorageError

    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("1000"), "BTC": Decimal("1")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    service = OperatorService(
        engine=engine,
        storage=storage,
        active_symbols=(BTC_USD,),
        grid_config=grid_config(),
    )
    dispatch = AsyncMock(side_effect=AssertionError("refused approval must never dispatch"))
    monkeypatch.setattr(service, "dispatch_command", dispatch)
    save = storage.save_pending_command
    refused_writes = 0
    pending_ids = []

    async def _fail_first_refusal(row: PendingCommand) -> None:
        nonlocal refused_writes
        if row.status == "failed" and refused_writes == 0:
            refused_writes += 1
            raise StorageError("simulated refusal write failure")
        await save(row)

    monkeypatch.setattr(storage, "save_pending_command", _fail_first_refusal)
    valuation_calls = 0

    async def _value(*_args: object, **_kwargs: object) -> Decimal:
        nonlocal valuation_calls
        valuation_calls += 1
        if valuation_calls == 2:
            row = _pending(status="approved", command=ReanchorCommand(symbol=BTC_USD))
            pending_ids.append(row.id)
            await storage.save_pending_command(row)
            raise ExchangeError("simulated BalanceEx outage")
        if valuation_calls == 5:
            engine.request_stop()
        return Decimal("100")

    monkeypatch.setattr(live_module, "_session_portfolio_value_usd", _value)

    await _run_loop(
        exchange,
        engine,
        LiveConfig(symbols=[BTC_USD], tick_seconds=0.001),
        storage,
        asyncio.Event(),
        operator_service=service,
        operator_storage=storage,
    )

    assert refused_writes == 1
    assert valuation_calls >= 5
    assert len(pending_ids) == 1
    refused = await storage.get_pending_command(pending_ids[0])
    assert refused is not None and refused.status == "failed"
    assert refused.result is not None and "predates" in refused.result.message
    dispatch.assert_not_awaited()


async def test_approved_stop_command_marks_engine(storage: SQLiteStorageAdapter) -> None:
    svc, engine = _operator_service(storage)
    pending = _pending(status="approved", command=StopCommand())
    await storage.save_pending_command(pending)

    await _process_pending_commands(svc, storage, None)
    assert engine.is_stop_requested is True

    fetched = await storage.get_pending_command(pending.id)
    assert fetched is not None
    assert fetched.status == "dispatched"


# --------------------------------------------------------------------- #
# Empty / no work                                                       #
# --------------------------------------------------------------------- #


async def test_empty_table_returns_zero(storage: SQLiteStorageAdapter) -> None:
    svc, _engine = _operator_service(storage)
    assert await _process_pending_commands(svc, storage, None) == 0


async def test_table_with_only_unapproved_rows_returns_zero(
    storage: SQLiteStorageAdapter,
) -> None:
    svc, engine = _operator_service(storage)
    await storage.save_pending_command(_pending(status="awaiting_confirmation"))
    assert await _process_pending_commands(svc, storage, None) == 0
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

    processed = await _process_pending_commands(svc, storage, None)
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


# --------------------------------------------------------------------- #
# Command-result echo (P3 renderers slice — the ✅'s receipt)             #
# --------------------------------------------------------------------- #


async def test_dispatch_echoes_command_result_notification(
    storage: SQLiteStorageAdapter,
) -> None:
    """The 2026-08-09 finding's fix: an approved command's dispatch
    writes a CommandResultEvent notification row, so the forwarder
    posts the outcome back to Discord instead of the ✅ getting
    silence."""
    from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
    from wobblebot.ports.notification_events import CommandResultEvent

    svc, _engine = _operator_service(storage)
    await storage.save_pending_command(
        _pending(status="approved", command=ReanchorCommand(symbol=BTC_USD))
    )

    await _process_pending_commands(svc, storage, SqliteNotifierAdapter(storage))

    rows = await storage.get_notifications(forwarded=False)
    echoes = [r for r in rows if isinstance(r.notification.event, CommandResultEvent)]
    assert len(echoes) == 1
    event = echoes[0].notification.event
    assert isinstance(event, CommandResultEvent)
    assert event.command_kind == "reanchor"
    assert event.symbol == "BTC/USD"
    assert event.success is True
    assert "re-anchored" in event.message
    assert echoes[0].notification.level == "info"


async def test_dispatch_echo_none_notifier_is_silent_noop(
    storage: SQLiteStorageAdapter,
) -> None:
    """A live deployment without operator_db wiring keeps working —
    the echo is best-effort like every notification."""
    svc, _engine = _operator_service(storage)
    await storage.save_pending_command(_pending(status="approved"))
    processed = await _process_pending_commands(svc, storage, None)
    assert processed == 1
    assert await storage.get_notifications() == []


# --------------------------------------------------------------------- #
# ADR-034 — cli/live must not claim the Harvester's rows                #
# --------------------------------------------------------------------- #


async def test_execute_proposal_rows_are_left_for_harvest(
    storage: SQLiteStorageAdapter,
) -> None:
    """The kind-scoped SELECT keeps money-out rows out of this loop.

    An unfiltered poll would hand the row to OperatorService (which has
    no dispatcher for it) and mark the operator's approved withdrawal
    ``failed`` — using a key that cannot withdraw at all (ADR-003).
    """
    svc, _engine = _operator_service(storage)
    row = _pending(
        command=ExecuteProposalCommand(
            proposal_id="p-1",
            amount_usd=Decimal("100"),
            destination="bank-label",
        )
    )
    await storage.save_pending_command(row)

    processed = await _process_pending_commands(svc, storage, None)

    assert processed == 0
    # Still claimable by cli/harvest on its next poll.
    untouched = await storage.get_pending_command(row.id)
    assert untouched is not None
    assert untouched.status == "approved"
    assert untouched.result is None


async def test_engine_commands_still_dispatch_alongside(
    storage: SQLiteStorageAdapter,
) -> None:
    """Scoping must not cost cli/live its own rows."""
    svc, _engine = _operator_service(storage)
    await storage.save_pending_command(
        _pending(
            command=ExecuteProposalCommand(
                proposal_id="p-1",
                amount_usd=Decimal("100"),
                destination="bank-label",
            )
        )
    )
    pause = _pending(command=PauseCommand(symbol=BTC_USD))
    await storage.save_pending_command(pause)

    processed = await _process_pending_commands(svc, storage, None)

    assert processed == 1
    dispatched = await storage.get_pending_command(pause.id)
    assert dispatched is not None
    assert dispatched.status == "dispatched"

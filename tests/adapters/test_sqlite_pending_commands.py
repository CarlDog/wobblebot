"""SQLiteStorageAdapter tests for the pending-commands persistence (Stage 5.4.B)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.operator import (
    CommandResult,
    OperatorCommand,
    PauseCommand,
    PendingCommand,
    PendingCommandStatus,
    StopCommand,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _ts(offset_seconds: int = 0) -> Timestamp:
    return Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds))


def _pending(
    *,
    command: OperatorCommand | None = None,
    status: PendingCommandStatus = "awaiting_confirmation",
    pending_id: UUID | None = None,
    channel_id: str = "C-1",
    requesting_user_id: str = "U-1",
    confirming_user_id: str | None = None,
    confirmed_at: Timestamp | None = None,
    dispatched_at: Timestamp | None = None,
    result: CommandResult | None = None,
    created_offset_seconds: int = 0,
) -> PendingCommand:
    return PendingCommand(
        id=pending_id or uuid4(),
        command=command or PauseCommand(symbol=Symbol(base="BTC", quote="USD")),
        status=status,
        channel_id=channel_id,
        requesting_user_id=requesting_user_id,
        confirming_user_id=confirming_user_id,
        confirmed_at=confirmed_at,
        dispatched_at=dispatched_at,
        result=result,
        ttl_expires_at=_ts(300),
        created_at=_ts(created_offset_seconds),
    )


# --------------------------------------------------------------------- #
# Save + read                                                           #
# --------------------------------------------------------------------- #


async def test_save_then_get_by_id_round_trips(storage: SQLiteStorageAdapter) -> None:
    pending = _pending()
    await storage.save_pending_command(pending)
    fetched = await storage.get_pending_command(pending.id)
    assert fetched is not None
    assert fetched == pending


async def test_get_pending_command_returns_none_when_missing(
    storage: SQLiteStorageAdapter,
) -> None:
    assert await storage.get_pending_command(uuid4()) is None


async def test_command_kind_persists_correctly(storage: SQLiteStorageAdapter) -> None:
    pending = _pending(command=StopCommand())
    await storage.save_pending_command(pending)
    fetched = await storage.get_pending_command(pending.id)
    assert fetched is not None
    assert isinstance(fetched.command, StopCommand)
    assert fetched.command.kind == "stop"


# --------------------------------------------------------------------- #
# Upsert semantics                                                      #
# --------------------------------------------------------------------- #


async def test_save_again_with_same_id_upserts_status_change(
    storage: SQLiteStorageAdapter,
) -> None:
    pending = _pending()
    await storage.save_pending_command(pending)

    approved = pending.model_copy(
        update={
            "status": "approved",
            "confirming_user_id": "U-2",
            "confirmed_at": _ts(60),
        }
    )
    await storage.save_pending_command(approved)

    rows = await storage.get_pending_commands()
    assert len(rows) == 1  # single row, not a duplicate
    assert rows[0].status == "approved"
    assert rows[0].confirming_user_id == "U-2"


async def test_dispatched_with_result_round_trips(storage: SQLiteStorageAdapter) -> None:
    pending = _pending(
        status="dispatched",
        confirming_user_id="U-2",
        confirmed_at=_ts(60),
        dispatched_at=_ts(70),
        result=CommandResult(
            success=True,
            command_kind="pause",
            message="BTC paused",
            executed_at=_ts(71),
        ),
    )
    await storage.save_pending_command(pending)
    fetched = await storage.get_pending_command(pending.id)
    assert fetched is not None
    assert fetched.result is not None
    assert fetched.result.success is True
    assert fetched.result.command_kind == "pause"


# --------------------------------------------------------------------- #
# Query: status filter + ordering + limit                               #
# --------------------------------------------------------------------- #


async def test_get_pending_commands_filters_by_status(
    storage: SQLiteStorageAdapter,
) -> None:
    awaiting = _pending(status="awaiting_confirmation")
    approved = _pending(status="approved", confirming_user_id="U-2", confirmed_at=_ts())
    rejected = _pending(status="rejected")
    for p in (awaiting, approved, rejected):
        await storage.save_pending_command(p)

    awaiting_rows = await storage.get_pending_commands(status="awaiting_confirmation")
    assert [r.id for r in awaiting_rows] == [awaiting.id]

    approved_rows = await storage.get_pending_commands(status="approved")
    assert [r.id for r in approved_rows] == [approved.id]


async def test_get_pending_commands_no_filter_returns_all(
    storage: SQLiteStorageAdapter,
) -> None:
    for _ in range(3):
        await storage.save_pending_command(_pending())
    rows = await storage.get_pending_commands()
    assert len(rows) == 3


async def test_get_pending_commands_orders_by_created_at_asc(
    storage: SQLiteStorageAdapter,
) -> None:
    # Oldest first so the polling cli/live picks up the longest-waiting approval.
    oldest = _pending(created_offset_seconds=-100)
    middle = _pending(created_offset_seconds=-50)
    newest = _pending(created_offset_seconds=0)
    # Save out of order to ensure the ORDER BY is doing the work, not insert order.
    await storage.save_pending_command(middle)
    await storage.save_pending_command(newest)
    await storage.save_pending_command(oldest)
    rows = await storage.get_pending_commands()
    assert [r.id for r in rows] == [oldest.id, middle.id, newest.id]


async def test_get_pending_commands_respects_limit(
    storage: SQLiteStorageAdapter,
) -> None:
    for _ in range(5):
        await storage.save_pending_command(_pending())
    rows = await storage.get_pending_commands(limit=2)
    assert len(rows) == 2


# --------------------------------------------------------------------- #
# Schema-level guarantees                                               #
# --------------------------------------------------------------------- #


async def test_status_check_rejects_unknown_value(
    storage: SQLiteStorageAdapter,
) -> None:
    # The CHECK constraint on `status` is the last line of defense
    # against a coding bug that would persist an out-of-band string.
    # We can't construct an invalid PendingCommand (Pydantic blocks
    # that), so this asserts the constraint by going around the model
    # via direct SQL.
    conn = storage._require_conn()  # pylint: disable=protected-access
    with pytest.raises(Exception):  # sqlite3.IntegrityError or similar
        await conn.execute(
            """
            INSERT INTO pending_commands (
                id, command_kind, command_json, status,
                channel_id, requesting_user_id,
                confirming_user_id, confirmed_at,
                dispatched_at, result_json,
                ttl_expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (
                str(uuid4()),
                "pause",
                '{"kind":"pause","symbol":{"base":"BTC","quote":"USD"}}',
                "not_a_real_status",  # CHECK rejects this
                "C-1",
                "U-1",
                _ts(300).dt.isoformat(),
                _ts().dt.isoformat(),
            ),
        )
        await conn.commit()

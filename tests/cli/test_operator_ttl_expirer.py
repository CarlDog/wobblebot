"""Tests for the cli/operator TTL expirer (Stage 5.7.A).

The expirer is the safety net for awaiting_confirmation rows the
operator never reacted to. Per ADR-013 decision 3 the confirm/reject
reaction is the only way out of awaiting_confirmation; without TTL
expiry the table accumulates stale rows forever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.operator import _expire_stale_pending_commands
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.operator import PauseCommand, PendingCommand, StopCommand

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _pending(
    *, status: str = "awaiting_confirmation", ttl_offset_seconds: int = 300
) -> PendingCommand:
    now = datetime.now(UTC)
    return PendingCommand(
        id=uuid4(),
        command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")),
        status=status,  # type: ignore[arg-type]
        channel_id="C-1",
        requesting_user_id="U-1",
        ttl_expires_at=Timestamp(dt=now + timedelta(seconds=ttl_offset_seconds)),
        created_at=Timestamp(dt=now),
    )


async def test_empty_table_returns_zero(storage: SQLiteStorageAdapter) -> None:
    assert await _expire_stale_pending_commands(storage) == 0


async def test_expires_only_past_ttl(storage: SQLiteStorageAdapter) -> None:
    # Three rows: one already-past-TTL, one with future TTL, one approved.
    stale = _pending(ttl_offset_seconds=-100)
    fresh = _pending(ttl_offset_seconds=300)
    approved = _pending(status="approved", ttl_offset_seconds=-100)
    for row in (stale, fresh, approved):
        await storage.save_pending_command(row)

    count = await _expire_stale_pending_commands(storage)
    assert count == 1

    stale_after = await storage.get_pending_command(stale.id)
    fresh_after = await storage.get_pending_command(fresh.id)
    approved_after = await storage.get_pending_command(approved.id)
    assert stale_after is not None and stale_after.status == "expired"
    assert fresh_after is not None and fresh_after.status == "awaiting_confirmation"
    assert approved_after is not None and approved_after.status == "approved"


async def test_does_not_expire_already_expired(storage: SQLiteStorageAdapter) -> None:
    # An already-expired row is filtered out at the query level (the
    # WHERE status='awaiting_confirmation' filter).
    expired = _pending(status="expired", ttl_offset_seconds=-100)
    await storage.save_pending_command(expired)
    count = await _expire_stale_pending_commands(storage)
    assert count == 0


async def test_multiple_expired_in_one_pass(storage: SQLiteStorageAdapter) -> None:
    for _ in range(5):
        await storage.save_pending_command(_pending(ttl_offset_seconds=-60))
    count = await _expire_stale_pending_commands(storage)
    assert count == 5
    all_rows = await storage.get_pending_commands()
    assert all(row.status == "expired" for row in all_rows)


async def test_mix_of_command_kinds_expires_correctly(
    storage: SQLiteStorageAdapter,
) -> None:
    pause = _pending(ttl_offset_seconds=-30)
    stop = PendingCommand(
        id=uuid4(),
        command=StopCommand(),
        status="awaiting_confirmation",
        channel_id="C-1",
        requesting_user_id="U-1",
        ttl_expires_at=Timestamp(dt=datetime.now(UTC) - timedelta(seconds=10)),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )
    for row in (pause, stop):
        await storage.save_pending_command(row)
    count = await _expire_stale_pending_commands(storage)
    assert count == 2

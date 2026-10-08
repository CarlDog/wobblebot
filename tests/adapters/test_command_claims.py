"""N3: real cross-connection races, immutable approval and crash recovery."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.operator import PauseCommand, PendingCommand, StopCommand

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def command(*, seconds=300, status="approved"):
    now = datetime.now(UTC)
    return PendingCommand(
        id=uuid4(),
        command=StopCommand(),
        status=status,
        channel_id="fixture",
        requesting_user_id="requester",
        confirming_user_id="human" if status == "approved" else None,
        confirmed_at=Timestamp(dt=now) if status == "approved" else None,
        created_at=Timestamp(dt=now),
        ttl_expires_at=Timestamp(dt=now + timedelta(seconds=seconds)),
    )


async def test_two_consumers_only_one_claim_and_restart_never_replays(tmp_path):
    path = tmp_path / "operator.db"
    first = SQLiteStorageAdapter(path)
    second = SQLiteStorageAdapter(path)
    await first.connect()
    await second.connect()
    pending = command()
    try:
        await first.save_pending_command(pending)
        results = await asyncio.gather(
            first.claim_pending_command(pending), second.claim_pending_command(pending)
        )
        assert sorted(results) == [False, True]
        assert await first.get_pending_commands(status="approved") == []
        assert (await first.get_pending_command(pending.id)).status == "claimed"
    finally:
        await first.close()
        await second.close()
    restarted = SQLiteStorageAdapter(path)
    await restarted.connect()
    try:
        assert not await restarted.claim_pending_command(pending)
        assert (await restarted.get_pending_commands(status="claimed"))[0].id == pending.id
    finally:
        await restarted.close()


async def test_expired_and_mutated_approvals_cannot_dispatch(tmp_path):
    storage = SQLiteStorageAdapter(tmp_path / "operator.db")
    await storage.connect()
    try:
        expired = command(seconds=-1)
        await storage.save_pending_command(expired)
        assert not await storage.claim_pending_command(expired)
        assert (await storage.get_pending_command(expired.id)).status == "expired"
        pending = command()
        await storage.save_pending_command(pending)
        mutated = pending.model_copy(update={"command": PauseCommand(symbol="BTC/USD")})
        with pytest.raises(StorageError):
            await storage.save_pending_command(mutated)
        assert not await storage.claim_pending_command(mutated)
        assert await storage.claim_pending_command(pending)
    finally:
        await storage.close()


async def test_conflicting_human_decisions_cannot_overwrite_winner(tmp_path):
    storage = SQLiteStorageAdapter(tmp_path / "operator.db")
    await storage.connect()
    try:
        pending = command(status="awaiting_confirmation")
        await storage.save_pending_command(pending)
        approved = pending.model_copy(
            update={
                "status": "approved",
                "confirming_user_id": "first",
                "confirmed_at": Timestamp(dt=datetime.now(UTC)),
            }
        )
        await storage.save_pending_command(approved)
        with pytest.raises(StorageError):
            await storage.save_pending_command(
                approved.model_copy(update={"status": "rejected", "confirming_user_id": "second"})
            )
        assert (await storage.get_pending_command(pending.id)).confirming_user_id == "first"
    finally:
        await storage.close()


async def test_read_only_consumer_cannot_escape_grant_to_claim(tmp_path):
    path = tmp_path / "operator.db"
    writer = SQLiteStorageAdapter(path)
    await writer.connect()
    pending = command()
    await writer.save_pending_command(pending)
    reader = SQLiteStorageAdapter(path, read_only=True)
    await reader.connect()
    try:
        with pytest.raises(StorageError, match="Read-only"):
            await reader.claim_pending_command(pending)
        assert (await writer.get_pending_command(pending.id)).status == "approved"
    finally:
        await reader.close()
        await writer.close()


@pytest.mark.parametrize("expires_during_wait", [True, False])
async def test_claim_checks_expiry_after_writer_lock(tmp_path, monkeypatch, expires_during_wait):
    """Real SQLite contention with a controlled clock, without timing-sensitive sleeps."""
    from wobblebot.adapters import sqlite_storage
    from wobblebot.sqlite_connection import open_connection

    path = tmp_path / "operator.db"
    storage = SQLiteStorageAdapter(path)
    await storage.connect()
    pending = command(seconds=5 if expires_during_wait else 300)
    await storage.save_pending_command(pending)
    clock = pending.created_at.dt
    entered_write = asyncio.Event()
    loop = asyncio.get_running_loop()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz)

    async def observed_connection(*args, **kwargs):
        conn = await open_connection(*args, **kwargs)

        def trace(statement):
            if statement.strip().upper().startswith("BEGIN"):
                loop.call_soon_threadsafe(entered_write.set)

        await conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite_storage, "datetime", Clock)
    monkeypatch.setattr(sqlite_storage, "open_connection", observed_connection)
    blocker = await open_connection(str(path))
    claim = None
    try:
        await blocker.execute("BEGIN IMMEDIATE")
        claim = asyncio.create_task(storage.claim_pending_command(pending))
        await asyncio.wait_for(entered_write.wait(), timeout=2)
        assert not claim.done()
        # Advance across the short approval's TTL while the real writer holds
        # the lock. The long-lived approval is the positive contention control.
        clock += timedelta(seconds=10)
        await blocker.rollback()
        assert await asyncio.wait_for(claim, timeout=5) is not expires_during_wait
        saved = await storage.get_pending_command(pending.id)
        assert saved.status == ("expired" if expires_during_wait else "claimed")
    finally:
        await blocker.rollback()
        if claim is not None and not claim.done():
            claim.cancel()
            await asyncio.gather(claim, return_exceptions=True)
        await blocker.close()
        await storage.close()


async def test_in_memory_claim_preserves_single_dispatch():
    storage = SQLiteStorageAdapter(":memory:")
    await storage.connect()
    try:
        pending = command()
        await storage.save_pending_command(pending)
        assert await storage.claim_pending_command(pending)
        assert not await storage.claim_pending_command(pending)
        assert (await storage.get_pending_command(pending.id)).status == "claimed"
    finally:
        await storage.close()

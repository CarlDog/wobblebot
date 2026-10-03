"""Real outbox persistence plus synthetic sends: races, crashes and retry bounds."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.delivery import DeliveryError
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.notifier import Notification
from wobblebot.services.delivery import forward_notifications

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def seed(storage):
    return await storage.save_notification(
        Notification(
            level="critical",
            title="Fixture alert",
            message="synthetic only",
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )
    )


async def test_competing_processes_send_once_and_save_receipt(tmp_path):
    first = SQLiteStorageAdapter(tmp_path / "operator.db")
    second = SQLiteStorageAdapter(tmp_path / "operator.db")
    await first.connect()
    await second.connect()
    try:
        await seed(first)

        async def send(*args, **kwargs):
            await asyncio.sleep(0.02)
            return "123456"

        transport = AsyncMock()
        transport.send_embed.side_effect = send
        outcomes = await asyncio.gather(
            forward_notifications(first, transport, "123"),
            forward_notifications(second, transport, "123"),
        )
        assert sorted(outcomes) == [0, 1]
        assert transport.send_embed.await_count == 1
        (row,) = await first.get_notifications()
        assert row.forwarded and row.delivery_state == "sent"
        assert row.delivery_message_id == "123456"
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("sent_before_crash", [False, True])
async def test_expired_sender_claim_is_uncertain_never_automatically_retried(
    tmp_path, sent_before_crash
):
    path = tmp_path / "operator.db"
    storage = SQLiteStorageAdapter(path)
    await storage.connect()
    identifier = await seed(storage)
    assert await storage.claim_notification_delivery(identifier) == 1
    # Simulated process loss before or after the transport side effect has the
    # same missing-receipt state; the database cannot infer which occurred.
    remote_effects = ["delivered"] if sent_before_crash else []
    await storage._require_conn().execute(
        "UPDATE notification_delivery SET lease_until='2000-01-01T00:00:00+00:00'"
    )
    await storage._require_conn().commit()
    await storage.close()
    restarted = SQLiteStorageAdapter(path)
    await restarted.connect()
    try:
        transport = AsyncMock()
        assert await forward_notifications(restarted, transport, "123") == 0
        transport.send_embed.assert_not_awaited()
        (row,) = await restarted.get_notifications()
        assert row.delivery_state == "uncertain" and not row.forwarded
        assert len(remote_effects) == int(sent_before_crash)
    finally:
        await restarted.close()


async def test_known_pre_send_failure_has_bounded_retry_and_terminal_visibility(tmp_path):
    storage = SQLiteStorageAdapter(tmp_path / "operator.db")
    await storage.connect()
    try:
        await seed(storage)
        transport = AsyncMock()
        transport.send_embed.side_effect = DeliveryError("safe fixture", retryable=True)
        for attempt in range(1, 6):
            assert await forward_notifications(storage, transport, "123") == 0
            (row,) = await storage.get_notifications()
            assert row.delivery_attempts == attempt
            # Not due yet: a normal immediate poll must not retry.
            assert await forward_notifications(storage, transport, "123") == 0
            assert transport.send_embed.await_count == attempt
            await storage._require_conn().execute(
                "UPDATE notification_delivery SET next_attempt='2000-01-01T00:00:00+00:00'"
            )
            await storage._require_conn().commit()
        (row,) = await storage.get_notifications()
        assert row.delivery_state == "failed"
        assert await forward_notifications(storage, transport, "123") == 0
        assert transport.send_embed.await_count == 5
    finally:
        await storage.close()


async def test_lost_post_send_receipt_cannot_cause_second_send(tmp_path, monkeypatch):
    storage = SQLiteStorageAdapter(tmp_path / "operator.db")
    await storage.connect()
    try:
        await seed(storage)
        transport = AsyncMock()
        transport.send_embed.return_value = "123456"
        monkeypatch.setattr(
            storage, "finish_notification_delivery", AsyncMock(side_effect=StorageError("fixture"))
        )
        assert await forward_notifications(storage, transport, "123") == 0
        assert await forward_notifications(storage, transport, "123") == 0
        assert transport.send_embed.await_count == 1
        (row,) = await storage.get_notifications()
        assert not row.forwarded and row.delivery_state == "sending"
    finally:
        await storage.close()


async def test_provider_backoff_survives_restart_and_blocks_other_messages(tmp_path):
    path = tmp_path / "operator.db"
    storage = SQLiteStorageAdapter(path)
    await storage.connect()
    try:
        await seed(storage)
        await seed(storage)
        transport = AsyncMock()
        transport.send_embed.side_effect = DeliveryError(
            "rate limit", retryable=True, retry_after_seconds=120
        )
        assert await forward_notifications(storage, transport, "123") == 0
        assert transport.send_embed.await_count == 1
    finally:
        await storage.close()
    restarted = SQLiteStorageAdapter(path)
    await restarted.connect()
    try:
        assert await forward_notifications(restarted, transport, "123") == 0
        assert transport.send_embed.await_count == 1
    finally:
        await restarted.close()

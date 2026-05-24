"""Tests for the /audit view (Stage 7.4.B)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.notifier import Notification
from wobblebot.ports.operator import PauseCommand, PendingCommand
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(TEST_USERNAME, hash_password(TEST_PASSWORD, cost=10))
    yield adapter
    await adapter.close()


def _build_client(storage: SQLiteStorageAdapter) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=storage,
        session_secret="x" * 64,
    )
    return TestClient(app, follow_redirects=False)


def _pending(
    *,
    status: str = "awaiting_confirmation",
    channel: str = "web",
    user: str = "operator",
) -> PendingCommand:
    now = Timestamp(dt=datetime.now(UTC))
    return PendingCommand(
        id=uuid4(),
        command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")),
        status=status,  # type: ignore[arg-type]
        channel_id=channel,
        requesting_user_id=user,
        ttl_expires_at=Timestamp(dt=now.dt + timedelta(minutes=10)),
        created_at=now,
    )


def _notification(*, level: str = "info", title: str = "test event") -> Notification:
    return Notification(
        level=level,  # type: ignore[arg-type]
        title=title,
        message="…",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


class TestAuditRoute:
    def test_anonymous_redirects(self, storage: SQLiteStorageAdapter) -> None:
        with _build_client(storage) as client:
            resp = client.get("/audit")
            assert resp.status_code == 302

    def test_empty_renders_placeholders(self, storage: SQLiteStorageAdapter) -> None:
        with _build_client(storage) as client:
            login_as(client)
            resp = client.get("/audit")
            assert resp.status_code == 200
            assert "No pending commands" in resp.text
            assert "No notifications" in resp.text

    @pytest.mark.asyncio
    async def test_renders_pending_commands(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_pending_command(_pending(channel="discord"))
        await storage.save_pending_command(_pending(status="approved"))
        with _build_client(storage) as client:
            login_as(client)
            resp = client.get("/audit")
            assert resp.status_code == 200
            assert "discord" in resp.text
            assert "approved" in resp.text
            assert "awaiting_confirmation" in resp.text
            # Stage 8.4.E soak Day 4 — display caps + total count
            # appended after the displayed count.
            assert "Pending Commands (2 of 2)" in resp.text

    @pytest.mark.asyncio
    async def test_renders_notifications_with_forwarded_state(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        nid1 = await storage.save_notification(_notification(title="not yet"))
        nid2 = await storage.save_notification(
            _notification(level="warning", title="forwarded one")
        )
        await storage.mark_notification_forwarded(nid2, Timestamp(dt=datetime.now(UTC)))
        assert nid1 != nid2
        with _build_client(storage) as client:
            login_as(client)
            resp = client.get("/audit")
            assert resp.status_code == 200
            assert "not yet" in resp.text
            assert "forwarded one" in resp.text
            assert "Notifications (2 of 2)" in resp.text
            # Both forwarded states render
            assert "forwarded" in resp.text
            assert "pending" in resp.text

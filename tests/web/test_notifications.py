"""Tests for /notifications + the bell-badge endpoint (fleet-review #19 finding 3).

The regression these lock down: ``get_notifications`` used to order ASC
and apply LIMIT after, so both the page and the badge fetched the *oldest*
rows — the badge's ``latest_at`` froze at the first notification ever
written, and the page could never show anything past the first 100.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.notifier import Notification
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.routes.notifications import _NOTIFICATIONS_LIMIT

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


def _notification(*, title: str) -> Notification:
    return Notification(
        level="info",
        title=title,
        message="…",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


class TestNotificationsPage:
    def test_anonymous_redirects(self, storage: SQLiteStorageAdapter) -> None:
        with _build_client(storage) as client:
            resp = client.get("/notifications")
            assert resp.status_code == 302

    @pytest.mark.asyncio
    async def test_page_shows_newest_rows_when_over_limit(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        total = _NOTIFICATIONS_LIMIT + 5
        for i in range(total):
            await storage.save_notification(_notification(title=f"evt-{i:03d}"))
        with _build_client(storage) as client:
            login_as(client)
            resp = client.get("/notifications")
            assert resp.status_code == 200
            # Newest row is present; the rows pushed past the limit are not.
            assert f"evt-{total - 1:03d}" in resp.text
            assert "evt-000" not in resp.text
            assert "evt-004" not in resp.text
            assert f"Recent (Last {_NOTIFICATIONS_LIMIT})" in resp.text


class TestLatestTimestampBadge:
    @pytest.mark.asyncio
    async def test_returns_null_when_empty(self, storage: SQLiteStorageAdapter) -> None:
        with _build_client(storage) as client:
            login_as(client)
            resp = client.get("/notifications/latest-timestamp")
            assert resp.status_code == 200
            assert resp.json() == {"latest_at": None}

    @pytest.mark.asyncio
    async def test_returns_newest_created_at(self, storage: SQLiteStorageAdapter) -> None:
        for i in range(3):
            await storage.save_notification(_notification(title=f"evt-{i}"))
        all_rows = await storage.get_notifications()
        newest = max(r.created_at.dt for r in all_rows)
        with _build_client(storage) as client:
            login_as(client)
            resp = client.get("/notifications/latest-timestamp")
            assert resp.status_code == 200
            assert resp.json() == {"latest_at": newest.isoformat()}

"""Tests for /notifications, read-state actions, and the bell badge.

Two regressions these lock down:

1. (fleet-review #19 finding 3) ``get_notifications`` used to order ASC
   and apply LIMIT after, so both the page and the badge fetched the
   *oldest* rows — the badge's ``latest_at`` froze at the first
   notification ever written, and the page could never show anything
   past the first 100.
2. (P3 slice 19) read-state lived in browser localStorage, so the badge
   disagreed across devices and opening the page silently marked
   everything read. It is now a server-side ``read_at`` column with
   explicit acknowledge actions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, csrf_from, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.notification_events import (
    CommandResultEvent,
    FillEvent,
    HarvestProposalEvent,
    LossCapEvent,
    WithdrawalFailedEvent,
    WithdrawalSubmittedEvent,
)
from wobblebot.ports.notifier import Notification
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.routes.notifications import _NOTIFICATIONS_LIMIT, deep_link

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
            assert resp.json() == {"latest_at": None, "unread": 0}

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
            assert resp.json() == {"latest_at": newest.isoformat(), "unread": 3}

    @pytest.mark.asyncio
    async def test_unread_count_drops_as_rows_are_acknowledged(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The badge is server-side, so acknowledging must move it."""
        for i in range(3):
            await storage.save_notification(_notification(title=f"evt-{i}"))
        rows = await storage.get_notifications()
        await storage.mark_notifications_read([rows[0].id], Timestamp(dt=datetime.now(UTC)))
        with _build_client(storage) as client:
            login_as(client)
            assert client.get("/notifications/latest-timestamp").json()["unread"] == 2


class TestReadState:
    @pytest.mark.asyncio
    async def test_acknowledge_one_marks_only_that_row(self, storage: SQLiteStorageAdapter) -> None:
        for i in range(3):
            await storage.save_notification(_notification(title=f"evt-{i}"))
        rows = await storage.get_notifications()
        target = rows[1]
        with _build_client(storage) as client:
            login_as(client)
            token = csrf_from(client.get("/notifications").text)
            resp = client.post(f"/notifications/{target.id}/read", data={"csrf_token": token})
            assert resp.status_code == 303
        after = {r.id: r.read_at for r in await storage.get_notifications()}
        assert after[target.id] is not None
        assert [rid for rid, read in after.items() if read is None] == [
            r.id for r in rows if r.id != target.id
        ]

    @pytest.mark.asyncio
    async def test_mark_all_read_clears_everything(self, storage: SQLiteStorageAdapter) -> None:
        for i in range(4):
            await storage.save_notification(_notification(title=f"evt-{i}"))
        with _build_client(storage) as client:
            login_as(client)
            token = csrf_from(client.get("/notifications").text)
            resp = client.post("/notifications/read-all", data={"csrf_token": token})
            assert resp.status_code == 303
        assert await storage.count_unread_notifications() == 0

    @pytest.mark.asyncio
    async def test_acknowledge_requires_csrf(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_notification(_notification(title="evt"))
        rows = await storage.get_notifications()
        with _build_client(storage) as client:
            login_as(client)
            resp = client.post(f"/notifications/{rows[0].id}/read")
            assert resp.status_code == 403
        assert await storage.count_unread_notifications() == 1

    def test_acknowledge_requires_auth(self, storage: SQLiteStorageAdapter) -> None:
        with _build_client(storage) as client:
            assert client.post("/notifications/read-all").status_code in (302, 403)

    @pytest.mark.asyncio
    async def test_read_state_writes_no_firewall_rows(self, storage: SQLiteStorageAdapter) -> None:
        """Acknowledging is UI-local, like reanchor snoozes (P3 slice 5).

        Reading a notification moves no money and touches no engine
        state, so it must NOT create a ``pending_commands`` row — that
        queue is the ADR-002 firewall and every row in it is something
        a daemon will act on.
        """
        await storage.save_notification(_notification(title="evt"))
        with _build_client(storage) as client:
            login_as(client)
            token = csrf_from(client.get("/notifications").text)
            client.post("/notifications/read-all", data={"csrf_token": token})
        assert await storage.get_pending_commands() == []

    @pytest.mark.asyncio
    async def test_unread_row_shows_acknowledge_read_row_does_not(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await storage.save_notification(_notification(title="a fill happened"))
        with _build_client(storage) as client:
            login_as(client)
            page = client.get("/notifications")
            assert "Acknowledge" in page.text
            assert "1 unread" in page.text
            assert "notif-unread" in page.text
            client.post("/notifications/read-all", data={"csrf_token": csrf_from(page.text)})
            after = client.get("/notifications")
            assert "Acknowledge" not in after.text
            assert "unread" not in after.text
            assert "Mark all read" not in after.text

    @pytest.mark.asyncio
    async def test_acknowledging_twice_keeps_the_first_timestamp(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Idempotent — a double-click must not rewrite when it happened."""
        await storage.save_notification(_notification(title="evt"))
        rows = await storage.get_notifications()
        first = Timestamp(dt=datetime(2026, 1, 1, tzinfo=UTC))
        assert await storage.mark_notifications_read([rows[0].id], first) == 1
        assert (
            await storage.mark_notifications_read(
                [rows[0].id], Timestamp(dt=datetime(2026, 2, 2, tzinfo=UTC))
            )
            == 0
        )
        again = await storage.get_notifications()
        assert again[0].read_at is not None
        assert again[0].read_at.dt == first.dt

    @pytest.mark.asyncio
    async def test_empty_id_list_is_a_no_op_not_mark_all(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """An empty selection must never be read as "everything"."""
        for i in range(3):
            await storage.save_notification(_notification(title=f"evt-{i}"))
        assert await storage.mark_notifications_read([], Timestamp(dt=datetime.now(UTC))) == 0
        assert await storage.count_unread_notifications() == 3


class TestDeepLinks:
    """Typed events link somewhere useful; untyped rows link nowhere."""

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (FillEvent(symbol="BTC/USD", fills=1, counters_placed=1, tick=4), "/"),
            (
                LossCapEvent(session_pnl_usd=Decimal("-5"), limit_usd=Decimal("5"), tick=9),
                "/",
            ),
            (
                CommandResultEvent(
                    command_kind="pause", symbol="BTC/USD", success=True, message="ok"
                ),
                "/",
            ),
            (
                HarvestProposalEvent(
                    proposal_id="p1",
                    direction="withdraw",
                    asset="USD",
                    amount=Decimal("10"),
                    current_exchange_balance=Decimal("100"),
                    target_exchange_balance=Decimal("90"),
                    rationale="over target",
                ),
                "/harvester",
            ),
            (
                WithdrawalSubmittedEvent(
                    proposal_id="p1",
                    transaction_id="tx1",
                    asset="USD",
                    amount=Decimal("10"),
                    destination="bank",
                    status="pending",
                ),
                "/harvester",
            ),
            (
                WithdrawalFailedEvent(
                    proposal_id="p1",
                    asset="USD",
                    amount=Decimal("10"),
                    destination="bank",
                    error="nope",
                    error_type="ExchangeError",
                ),
                "/harvester",
            ),
        ],
    )
    def test_typed_events_link(self, event: object, expected: str) -> None:
        assert deep_link(event) == expected  # type: ignore[arg-type]

    def test_legacy_row_has_no_link(self) -> None:
        assert deep_link(None) is None

    @pytest.mark.asyncio
    async def test_page_renders_the_link(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_notification(
            Notification(
                level="info",
                title="Withdrawal submitted",
                message="…",
                timestamp=Timestamp(dt=datetime.now(UTC)),
                event=WithdrawalSubmittedEvent(
                    proposal_id="p1",
                    transaction_id="tx1",
                    asset="USD",
                    amount=Decimal("10"),
                    destination="bank",
                    status="pending",
                ),
            )
        )
        with _build_client(storage) as client:
            login_as(client)
            assert (
                '<a href="/harvester">Withdrawal submitted</a>' in client.get("/notifications").text
            )

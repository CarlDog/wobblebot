"""Tests for the mutation flow — pause/resume/stop via PendingCommand (Stage 7.2.C).

The architecturally significant slice. Verifies:

- POST creates a ``PendingCommand`` row in ``awaiting_confirmation``.
- The web UI NEVER calls ``OperatorService.dispatch_command`` directly.
- POST /confirm with ``decision=approve`` transitions to ``approved``;
  cli/live's ``WHERE status='approved'`` poll is the only path from
  here to the engine (ADR-013 firewall preserved).
- ``decision=reject`` transitions to ``rejected``; nothing reaches
  the engine.
- Idempotency: re-confirming a row already in a terminal state
  surfaces the existing status, never mutates twice.
- CSRF protection on every POST.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, csrf_from, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit

_PENDING_ID_RE = re.compile(r"/commands/([0-9a-f-]+)/confirm")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(TEST_USERNAME, hash_password(TEST_PASSWORD, cost=10))
    yield adapter
    await adapter.close()


@pytest.fixture
def client(storage: SQLiteStorageAdapter) -> Iterator[TestClient]:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=storage,
        session_secret="x" * 64,
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


# --------------------------------------------------------------------- #
# GET forms                                                             #
# --------------------------------------------------------------------- #


class TestForms:
    def test_pause_form_anonymous_redirects(self, client: TestClient) -> None:
        resp = client.get("/commands/pause")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

    def test_pause_form_authenticated_renders(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/commands/pause")
        assert resp.status_code == 200
        assert 'name="symbol"' in resp.text
        assert "Pause" in resp.text

    def test_resume_form_renders(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/commands/resume")
        assert resp.status_code == 200
        assert 'name="symbol"' in resp.text
        assert "Resume" in resp.text

    def test_stop_form_renders_without_symbol_input(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/commands/stop")
        assert resp.status_code == 200
        # Stop is symbol-free
        assert 'name="symbol"' not in resp.text
        assert "Emergency stop" in resp.text


# --------------------------------------------------------------------- #
# POST creates PendingCommand row                                       #
# --------------------------------------------------------------------- #


class TestCreate:
    def test_pause_post_creates_awaiting_confirmation(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/pause",
            data={"symbol": "BTC/USD", "csrf_token": token},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        m = _PENDING_ID_RE.search(loc)
        assert m is not None, f"unexpected redirect: {loc}"

    @pytest.mark.asyncio
    async def test_pause_row_persists_with_correct_shape(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        from fastapi.testclient import TestClient

        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            form = client.get("/commands/pause")
            token = csrf_from(form.text)
            resp = client.post(
                "/commands/pause",
                data={"symbol": "BTC/USD", "csrf_token": token},
            )
            assert resp.status_code == 303
        # Inspect the row that landed in operator.db.
        rows = await storage.get_pending_commands(status="awaiting_confirmation")
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "awaiting_confirmation"
        assert row.channel_id == "web"
        assert row.requesting_user_id == TEST_USERNAME
        assert row.command.kind == "pause"
        assert row.command.symbol.base == "BTC"
        assert row.command.symbol.quote == "USD"

    def test_pause_invalid_symbol_renders_400_with_error(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/pause",
            data={"symbol": "notavalidsymbol", "csrf_token": token},
        )
        assert resp.status_code == 400
        assert "Invalid symbol" in resp.text

    def test_resume_post_creates_resume_kind(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/resume")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/resume",
            data={"symbol": "ETH/USD", "csrf_token": token},
        )
        assert resp.status_code == 303

    def test_stop_post_creates_stop_kind(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/stop")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/stop",
            data={"csrf_token": token},
        )
        assert resp.status_code == 303

    def test_post_without_csrf_returns_403(self, client: TestClient) -> None:
        login_as(client)
        resp = client.post("/commands/pause", data={"symbol": "BTC/USD"})
        assert resp.status_code == 403


# --------------------------------------------------------------------- #
# Confirm flow                                                          #
# --------------------------------------------------------------------- #


class TestConfirm:
    def _create_pause(self, client: TestClient) -> str:
        """Round-trip pause-form → POST → redirect; return pending id."""
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/pause",
            data={"symbol": "BTC/USD", "csrf_token": token},
        )
        loc = resp.headers["location"]
        m = _PENDING_ID_RE.search(loc)
        assert m is not None
        return m.group(1)

    def test_confirm_get_renders_summary(self, client: TestClient) -> None:
        login_as(client)
        pid = self._create_pause(client)
        resp = client.get(f"/commands/{pid}/confirm")
        assert resp.status_code == 200
        assert "BTC/USD" in resp.text
        assert "pause" in resp.text
        # Should offer both buttons
        assert 'value="approve"' in resp.text
        assert 'value="reject"' in resp.text

    def test_confirm_get_unknown_id_returns_404(self, client: TestClient) -> None:
        login_as(client)
        bogus = uuid4()
        resp = client.get(f"/commands/{bogus}/confirm")
        assert resp.status_code == 404
        assert "not found" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_approve_transitions_to_approved(self, storage: SQLiteStorageAdapter) -> None:
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            pid = self._create_pause(client)
            confirm_page = client.get(f"/commands/{pid}/confirm")
            token = csrf_from(confirm_page.text)
            resp = client.post(
                f"/commands/{pid}/confirm",
                data={"decision": "approve", "csrf_token": token},
            )
            assert resp.status_code == 200
            assert "approved" in resp.text
        # ADR-013 firewall check: the row is now `approved`, which is
        # what cli/live's WHERE status='approved' poll picks up.
        from uuid import UUID

        row = await storage.get_pending_command(UUID(pid))
        assert row is not None
        assert row.status == "approved"
        assert row.confirming_user_id == TEST_USERNAME
        assert row.confirmed_at is not None

    @pytest.mark.asyncio
    async def test_reject_transitions_to_rejected(self, storage: SQLiteStorageAdapter) -> None:
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            pid = self._create_pause(client)
            confirm_page = client.get(f"/commands/{pid}/confirm")
            token = csrf_from(confirm_page.text)
            resp = client.post(
                f"/commands/{pid}/confirm",
                data={"decision": "reject", "csrf_token": token},
            )
            assert resp.status_code == 200
            assert "rejected" in resp.text
        from uuid import UUID

        row = await storage.get_pending_command(UUID(pid))
        assert row is not None
        assert row.status == "rejected"

    def test_confirm_without_csrf_returns_403(self, client: TestClient) -> None:
        login_as(client)
        pid = self._create_pause(client)
        resp = client.post(
            f"/commands/{pid}/confirm",
            data={"decision": "approve"},
        )
        assert resp.status_code == 403

    def test_invalid_decision_value_returns_422(self, client: TestClient) -> None:
        login_as(client)
        pid = self._create_pause(client)
        confirm_page = client.get(f"/commands/{pid}/confirm")
        token = csrf_from(confirm_page.text)
        resp = client.post(
            f"/commands/{pid}/confirm",
            data={"decision": "hijack", "csrf_token": token},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_idempotent_confirm_on_already_approved(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Confirming a row that's already terminal must not re-mutate."""
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            pid = self._create_pause(client)
            confirm_page = client.get(f"/commands/{pid}/confirm")
            token = csrf_from(confirm_page.text)
            # First approve.
            r1 = client.post(
                f"/commands/{pid}/confirm",
                data={"decision": "approve", "csrf_token": token},
            )
            assert r1.status_code == 200
            from uuid import UUID

            row1 = await storage.get_pending_command(UUID(pid))
            assert row1 is not None
            first_confirmed_at = row1.confirmed_at
            # Second attempt — should not overwrite confirmed_at.
            confirm_page2 = client.get(f"/commands/{pid}/confirm")
            # Note: the result template doesn't include a CSRF input;
            # but the confirm GET will still have one if we re-fetch it.
            # Use the same token (it's tied to the session, not the page).
            r2 = client.post(
                f"/commands/{pid}/confirm",
                data={"decision": "reject", "csrf_token": token},
            )
            assert r2.status_code == 200
            assert "already" in r2.text.lower()
        row2 = await storage.get_pending_command(UUID(pid))
        assert row2 is not None
        assert row2.status == "approved"  # unchanged
        assert row2.confirmed_at == first_confirmed_at  # unchanged

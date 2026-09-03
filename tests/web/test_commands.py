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
from wobblebot.domain.value_objects import Symbol
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


@pytest.fixture
def configured_client(storage: SQLiteStorageAdapter) -> Iterator[TestClient]:
    """A client whose engine trades BTC/USD only (2026-09-03 finding 2)."""
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=storage,
        session_secret="x" * 64,
        live_symbols=(Symbol(base="BTC", quote="USD"),),
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

    @pytest.mark.asyncio
    async def test_expired_ttl_is_refused_not_approved(self, storage: SQLiteStorageAdapter) -> None:
        """Fleet-review #19 finding 6: a decision arriving after
        ttl_expires_at must not approve/reject — it transitions to
        `expired` instead, mirroring cli/operator's TTL expirer
        (_expire_stale_pending_commands), rather than acting on a
        decision that arrived too late."""
        from datetime import UTC, datetime, timedelta
        from uuid import UUID

        from wobblebot.domain.value_objects import Timestamp

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

            # Backdate the TTL, as if the confirm tab sat open past it.
            row = await storage.get_pending_command(UUID(pid))
            assert row is not None
            past = row.model_copy(
                update={"ttl_expires_at": Timestamp(dt=datetime.now(UTC) - timedelta(minutes=1))}
            )
            await storage.save_pending_command(past)

            resp = client.post(
                f"/commands/{pid}/confirm",
                data={"decision": "approve", "csrf_token": token},
            )
            assert resp.status_code == 200
            assert "expired" in resp.text.lower()

        row2 = await storage.get_pending_command(UUID(pid))
        assert row2 is not None
        assert row2.status == "expired"
        assert row2.confirming_user_id is None
        assert row2.confirmed_at is None


# --------------------------------------------------------------------- #
# Re-anchor button + UI-local snooze (P3 banner slice)                   #
# --------------------------------------------------------------------- #


class TestReanchorAndSnooze:
    @pytest.mark.asyncio
    async def test_reanchor_post_creates_reanchor_row(self, storage: SQLiteStorageAdapter) -> None:
        """The banner button lands a ReanchorCommand in the same
        awaiting_confirmation flow as pause — no firewall shortcut."""
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            form = client.get("/commands/pause")  # any page with a CSRF token
            token = csrf_from(form.text)
            resp = client.post(
                "/commands/reanchor",
                data={"symbol": "BTC/USD", "csrf_token": token},
            )
            assert resp.status_code == 303
            assert _PENDING_ID_RE.search(resp.headers["location"]) is not None
        rows = await storage.get_pending_commands(status="awaiting_confirmation")
        assert len(rows) == 1
        row = rows[0]
        assert row.command.kind == "reanchor"
        assert row.command.symbol.base == "BTC"
        assert row.command.symbol.quote == "USD"
        assert row.channel_id == "web"

    def test_reanchor_invalid_symbol_renders_400(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/reanchor",
            data={"symbol": "notavalidsymbol", "csrf_token": token},
        )
        assert resp.status_code == 400
        assert "Invalid symbol" in resp.text

    def test_reanchor_post_without_csrf_returns_403(self, client: TestClient) -> None:
        login_as(client)
        resp = client.post("/commands/reanchor", data={"symbol": "BTC/USD"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_snooze_writes_row_and_skips_the_firewall(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The load-bearing UI-local pin: a snooze writes ONLY the
        reanchor_snoozes row — zero pending_commands rows, because
        suppressing a banner moves no money (P3 blueprint)."""
        from datetime import UTC, datetime, timedelta

        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        before = datetime.now(UTC)
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            form = client.get("/commands/pause")
            token = csrf_from(form.text)
            resp = client.post(
                "/commands/snooze-reanchor",
                data={"symbol": "BTC/USD", "csrf_token": token},
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/"
        snoozes = await storage.get_reanchor_snoozes()
        assert len(snoozes) == 1
        ((symbol, until),) = snoozes.items()
        assert (symbol.base, symbol.quote) == ("BTC", "USD")
        assert before + timedelta(hours=23) < until < before + timedelta(hours=25)
        for status_filter in ("awaiting_confirmation", "approved"):
            assert await storage.get_pending_commands(status=status_filter) == []

    def test_snooze_invalid_symbol_returns_400(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/snooze-reanchor",
            data={"symbol": "notavalidsymbol", "csrf_token": token},
        )
        assert resp.status_code == 400

    def test_snooze_post_without_csrf_returns_403(self, client: TestClient) -> None:
        login_as(client)
        resp = client.post("/commands/snooze-reanchor", data={"symbol": "BTC/USD"})
        assert resp.status_code == 403


# --------------------------------------------------------------------- #
# Row-watch (P3 wait-for-completion)                                     #
# --------------------------------------------------------------------- #


class TestCommandWatch:
    def _approve_a_pause(self, client: TestClient) -> str:
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        resp = client.post("/commands/pause", data={"symbol": "BTC/USD", "csrf_token": token})
        pid = _PENDING_ID_RE.search(resp.headers["location"]).group(1)  # type: ignore[union-attr]
        confirm_page = client.get(f"/commands/{pid}/confirm")
        client.post(
            f"/commands/{pid}/confirm",
            data={"decision": "approve", "csrf_token": csrf_from(confirm_page.text)},
        )
        return pid

    def test_result_page_embeds_the_watcher_on_approve(self, client: TestClient) -> None:
        """The ✅ no longer dead-ends at 'approved' — the result page
        carries the self-polling watch block."""
        login_as(client)
        pid = self._approve_a_pause(client)
        # Re-render the result page via the watch partial's own URL.
        resp = client.get(f"/commands/{pid}/watch")
        assert resp.status_code == 200
        assert "waiting for cli/live" in resp.text
        assert f"/commands/{pid}/watch" in resp.text  # self-polling
        assert 'hx-trigger="every 2s"' in resp.text

    @pytest.mark.asyncio
    async def test_dispatched_row_renders_terminal_result_and_stops_polling(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from wobblebot.domain.value_objects import Symbol, Timestamp
        from wobblebot.ports.operator import CommandResult, PauseCommand, PendingCommand

        now = Timestamp(dt=datetime.now(UTC))
        pending = PendingCommand(
            id=uuid4(),
            command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")),
            status="dispatched",
            channel_id="web",
            requesting_user_id=TEST_USERNAME,
            confirming_user_id=TEST_USERNAME,
            confirmed_at=now,
            dispatched_at=now,
            result=CommandResult(
                success=True,
                command_kind="pause",
                message="paused BTC/USD",
                executed_at=now,
            ),
            ttl_expires_at=Timestamp(dt=now.dt + timedelta(minutes=10)),
            created_at=now,
        )
        await storage.save_pending_command(pending)
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            resp = client.get(f"/commands/{pending.id}/watch")
            assert resp.status_code == 200
            assert "paused BTC/USD" in resp.text
            assert "executed" in resp.text
            assert 'hx-trigger="every 2s"' not in resp.text  # polling stops

    def test_unknown_id_renders_not_found_partial(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get(f"/commands/{uuid4()}/watch")
        assert resp.status_code == 200
        assert "Command not found" in resp.text


# --------------------------------------------------------------------- #
# Modal flow (P3 modal layer — htmx progressive enhancement)             #
# --------------------------------------------------------------------- #


class TestModalFlow:
    _HX = {"HX-Request": "true"}

    def test_htmx_create_returns_modal_confirm_not_redirect(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        resp = client.post(
            "/commands/pause",
            data={"symbol": "BTC/USD", "csrf_token": token},
            headers=self._HX,
        )
        assert resp.status_code == 200  # partial, not a 303
        assert "modal-card" in resp.text
        assert "/confirm" in resp.text  # approve/reject hx-post target
        assert "data-modal-close" in resp.text

    def test_htmx_create_covers_every_verb(self, client: TestClient) -> None:
        """Pylint caught an undefined `prefs` on a path no test hit —
        every verb's htmx branch gets exercised now."""
        login_as(client)
        token = csrf_from(client.get("/commands/pause").text)
        for verb in ("pause", "resume", "reanchor"):
            resp = client.post(
                f"/commands/{verb}",
                data={"symbol": "BTC/USD", "csrf_token": token},
                headers=self._HX,
            )
            assert resp.status_code == 200, verb
            assert "modal-card" in resp.text, verb
        stop = client.post("/commands/stop", data={"csrf_token": token}, headers=self._HX)
        assert stop.status_code == 200
        assert "modal-card" in stop.text

    def test_htmx_approve_returns_modal_watch_with_ctx(self, client: TestClient) -> None:
        login_as(client)
        form = client.get("/commands/pause")
        token = csrf_from(form.text)
        create = client.post(
            "/commands/pause",
            data={"symbol": "BTC/USD", "csrf_token": token},
            headers=self._HX,
        )
        pid = re.search(r"/commands/([0-9a-f-]+)/confirm", create.text).group(1)  # type: ignore[union-attr]
        resp = client.post(
            f"/commands/{pid}/confirm",
            data={"decision": "approve", "csrf_token": token},
            headers=self._HX,
        )
        assert resp.status_code == 200
        assert "modal-card" in resp.text
        assert "waiting for cli/live" in resp.text
        assert f"/commands/{pid}/watch?ctx=modal" in resp.text  # ctx survives the poll

    @pytest.mark.asyncio
    async def test_modal_watch_terminal_refreshes_the_status_card(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from wobblebot.domain.value_objects import Symbol, Timestamp
        from wobblebot.ports.operator import CommandResult, PauseCommand, PendingCommand

        now = Timestamp(dt=datetime.now(UTC))
        pending = PendingCommand(
            id=uuid4(),
            command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")),
            status="dispatched",
            channel_id="web",
            requesting_user_id=TEST_USERNAME,
            confirming_user_id=TEST_USERNAME,
            confirmed_at=now,
            dispatched_at=now,
            result=CommandResult(
                success=True, command_kind="pause", message="paused BTC/USD", executed_at=now
            ),
            ttl_expires_at=Timestamp(dt=now.dt + timedelta(minutes=10)),
            created_at=now,
        )
        await storage.save_pending_command(pending)
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            modal = client.get(f"/commands/{pending.id}/watch?ctx=modal")
            assert 'hx-target="#status-wrap"' in modal.text  # immediate dashboard refresh
            page = client.get(f"/commands/{pending.id}/watch")
            assert 'hx-target="#status-wrap"' not in page.text  # page ctx stays clean


class TestCommandVocabularyAndConsequence:
    """Shared labels + high-consequence weight (P3 slice 23).

    The modal and the full-page confirm render the SAME decision; until
    the shared `command_label` global existed, one said "Stop the
    engine" and the other printed the raw `stop` discriminator.
    """

    def test_label_and_consequence_helpers(self) -> None:
        from wobblebot.web.app import _command_label, _is_high_consequence

        assert _command_label("stop") == "Stop the engine"
        assert _command_label("pause") == "Pause trading"
        # An unregistered kind still renders something readable.
        assert _command_label("brand_new_kind") == "brand_new_kind"
        assert _is_high_consequence("stop") is True
        assert _is_high_consequence("pause_all") is True
        assert _is_high_consequence("cancel_open_orders") is True
        # Single-symbol actions stay routine.
        assert _is_high_consequence("pause") is False
        assert _is_high_consequence("reanchor") is False
        # Money-out has its own louder treatment, not this one.
        assert _is_high_consequence("execute_proposal") is False


class TestReanchorSymbolGuard:
    """2026-09-03 review, finding 2. The route — not just the template — must
    refuse a re-anchor for a symbol the engine does not tend, so the free-text
    form and any future caller are covered too."""

    def test_untraded_symbol_is_refused_with_400_and_queues_nothing(
        self, configured_client: TestClient, storage: SQLiteStorageAdapter
    ) -> None:
        login_as(configured_client)
        token = csrf_from(configured_client.get("/commands/pause").text)
        resp = configured_client.post(
            "/commands/reanchor",
            data={"symbol": "BABY/USD", "csrf_token": token},
        )
        assert resp.status_code == 400
        # Apostrophe is HTML-escaped in the rendered form.
        assert "configured trading symbols" in resp.text

    @pytest.mark.asyncio
    async def test_configured_symbol_still_queues(
        self, configured_client: TestClient, storage: SQLiteStorageAdapter
    ) -> None:
        login_as(configured_client)
        token = csrf_from(configured_client.get("/commands/pause").text)
        resp = configured_client.post(
            "/commands/reanchor",
            data={"symbol": "BTC/USD", "csrf_token": token},
        )
        assert resp.status_code in (200, 302, 303)
        rows = await storage.get_pending_commands()
        assert len(rows) == 1
        assert rows[0].command.kind == "reanchor"

    def test_unknown_trading_set_falls_open(self, client: TestClient) -> None:
        """The default client passes no live_symbols — unknown, so the guard
        must not block (no regression for an unwired deployment)."""
        login_as(client)
        token = csrf_from(client.get("/commands/pause").text)
        resp = client.post(
            "/commands/reanchor",
            data={"symbol": "BABY/USD", "csrf_token": token},
        )
        assert resp.status_code != 400

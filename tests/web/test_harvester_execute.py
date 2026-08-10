"""ADR-034 — the Execute button and its money-out queueing.

The web half of ADR-034. Two things are load-bearing here and both are
asserted repeatedly below:

1. The web NEVER withdraws. Every path ends at a ``pending_commands``
   row in ``awaiting_confirmation``; ``cli/harvest`` is the only module
   that can move money (ADR-003).
2. Amount and destination are read SERVER-side from the stored proposal
   and config. If the browser could supply them, the daemon's echo
   validation would be checking the client's claim against itself.
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
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.ports.operator import ExecuteProposalCommand
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit

_DESTINATIONS = {"USD": "test-bank-label"}


@pytest_asyncio.fixture
async def operator_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(TEST_USERNAME, hash_password(TEST_PASSWORD, cost=10))
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def harvest_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _proposal(
    *,
    proposal_id: str = "prop-1",
    direction: str = "exchange_to_bank",
    amount: str = "300.00",
) -> TransferProposal:
    return TransferProposal(
        proposal_id=proposal_id,
        direction=direction,  # type: ignore[arg-type]
        asset="USD",
        amount=Decimal(amount),
        rationale="surplus over threshold",
        current_exchange_balance=Decimal("500"),
        target_exchange_balance=Decimal("200"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _result(*, proposal_id: str = "prop-1", status: str = "pending") -> TransferResult:
    return TransferResult(
        proposal_id=proposal_id,
        transaction_id="txn-1",
        status=status,  # type: ignore[arg-type]
        executed_amount=Decimal("300.00"),
        direction="exchange_to_bank",
        asset="USD",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


def _client(
    operator: SQLiteStorageAdapter,
    harvest: SQLiteStorageAdapter | None,
    *,
    destinations: dict[str, str] | None = None,
) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator,
        session_secret="x" * 64,
        harvest_storage=harvest,
        withdrawal_destinations=_DESTINATIONS if destinations is None else destinations,
    )
    return TestClient(app, follow_redirects=False)


@pytest.mark.asyncio
class TestExecuteButtonVisibility:
    """Which proposals get a button — and which must not."""

    async def test_executable_proposal_shows_button(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-go"))
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert "/commands/execute-proposal" in resp.text
            assert "prop-go" in resp.text

    async def test_deposit_proposal_has_no_button(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        """A bank_to_exchange transfer has no API path at all."""
        await harvest_storage.save_transfer_proposal(
            _proposal(proposal_id="prop-deposit", direction="bank_to_exchange")
        )
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert "/commands/execute-proposal" not in resp.text

    async def test_already_executed_proposal_has_no_button(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-spent"))
        await harvest_storage.save_transfer_result(_result(proposal_id="prop-spent"))
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert "/commands/execute-proposal" not in resp.text

    async def test_failed_attempt_still_offers_the_button(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        """Kraken rejected it, so no money moved — a retry is legitimate."""
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-retry"))
        await harvest_storage.save_transfer_result(
            _result(proposal_id="prop-retry", status="failed")
        )
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert "/commands/execute-proposal" in resp.text

    async def test_no_destination_configured_has_no_button(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-nodest"))
        with _client(operator_storage, harvest_storage, destinations={}) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert "/commands/execute-proposal" not in resp.text

    async def test_warns_when_harvest_daemon_is_down(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        """No heartbeat → say the approval will sit queued."""
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-go"))
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert "daemon down" in resp.text

    async def test_no_warning_when_daemon_is_beating(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-go"))
        await operator_storage.upsert_daemon_heartbeat("cli/harvest", datetime.now(UTC))
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert "daemon down" not in resp.text


@pytest.mark.asyncio
class TestExecuteSubmit:
    """POST /commands/execute-proposal — queues, never withdraws."""

    async def test_queues_command_with_server_side_amount(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(
            _proposal(proposal_id="prop-q", amount="250.00")
        )
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            page = client.get("/harvester")
            token = csrf_from(page.text)
            resp = client.post(
                "/commands/execute-proposal",
                data={"proposal_id": "prop-q", "csrf_token": token},
            )
            assert resp.status_code == 303

        rows = await operator_storage.get_pending_commands(kinds=("execute_proposal",))
        assert len(rows) == 1
        command = rows[0].command
        assert isinstance(command, ExecuteProposalCommand)
        assert command.amount_usd == Decimal("250.00")
        assert command.destination == "test-bank-label"
        # Queued only — the confirm gate is still ahead of it.
        assert rows[0].status == "awaiting_confirmation"

    async def test_form_supplied_amount_cannot_override_the_proposal(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        """Extra form fields are ignored; the server owns the numbers."""
        await harvest_storage.save_transfer_proposal(
            _proposal(proposal_id="prop-tamper", amount="10.00")
        )
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            page = client.get("/harvester")
            token = csrf_from(page.text)
            client.post(
                "/commands/execute-proposal",
                data={
                    "proposal_id": "prop-tamper",
                    "amount_usd": "99999.00",
                    "destination": "attacker-wallet",
                    "csrf_token": token,
                },
            )

        rows = await operator_storage.get_pending_commands(kinds=("execute_proposal",))
        command = rows[0].command
        assert isinstance(command, ExecuteProposalCommand)
        assert command.amount_usd == Decimal("10.00")
        assert command.destination == "test-bank-label"

    async def test_htmx_post_returns_modal_with_money_details(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(
            _proposal(proposal_id="prop-modal", amount="42.00")
        )
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            page = client.get("/harvester")
            token = csrf_from(page.text)
            resp = client.post(
                "/commands/execute-proposal",
                data={"proposal_id": "prop-modal", "csrf_token": token},
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 200
            assert "modal-card" in resp.text
            assert "42.00" in resp.text
            assert "test-bank-label" in resp.text
            # The money warning, not the engine-firewall note.
            assert "real money" in resp.text

    async def test_unknown_proposal_is_refused(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            page = client.get("/harvester")
            token = csrf_from(page.text)
            resp = client.post(
                "/commands/execute-proposal",
                data={"proposal_id": "does-not-exist", "csrf_token": token},
            )
            assert resp.status_code == 404
        assert await operator_storage.get_pending_commands(kinds=("execute_proposal",)) == []

    async def test_asset_without_destination_is_refused(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-nodest"))
        with _client(operator_storage, harvest_storage, destinations={}) as client:
            login_as(client)
            page = client.get("/harvester")
            token = csrf_from(page.text)
            resp = client.post(
                "/commands/execute-proposal",
                data={"proposal_id": "prop-nodest", "csrf_token": token},
            )
            assert resp.status_code == 400
        assert await operator_storage.get_pending_commands(kinds=("execute_proposal",)) == []

    async def test_requires_csrf(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-csrf"))
        with _client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.post(
                "/commands/execute-proposal",
                data={"proposal_id": "prop-csrf"},
            )
            assert resp.status_code == 403
        assert await operator_storage.get_pending_commands(kinds=("execute_proposal",)) == []

    async def test_anonymous_cannot_queue(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(_proposal(proposal_id="prop-anon"))
        with _client(operator_storage, harvest_storage) as client:
            resp = client.post(
                "/commands/execute-proposal",
                data={"proposal_id": "prop-anon"},
            )
            assert resp.status_code in (302, 303, 403)
        assert await operator_storage.get_pending_commands(kinds=("execute_proposal",)) == []

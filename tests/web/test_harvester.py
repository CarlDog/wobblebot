"""Tests for the /harvester view (Stage 7.3.B)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit


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


def _make_proposal(
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


def _make_result(
    *,
    proposal_id: str = "prop-1",
    transaction_id: str = "txn-1",
    status: str = "completed",
) -> TransferResult:
    return TransferResult(
        proposal_id=proposal_id,
        transaction_id=transaction_id,
        status=status,  # type: ignore[arg-type]
        executed_amount=Decimal("300.00"),
        direction="exchange_to_bank",
        asset="USD",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


def _build_client(
    operator: SQLiteStorageAdapter,
    harvest: SQLiteStorageAdapter | None,
) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator,
        session_secret="x" * 64,
        harvest_storage=harvest,
    )
    return TestClient(app, follow_redirects=False)


class TestHarvesterRoute:
    def test_anonymous_redirects(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            resp = client.get("/harvester")
            assert resp.status_code == 302

    def test_no_harvest_db_renders_unwired(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert resp.status_code == 200
            assert "unset" in resp.text.lower()

    def test_empty_harvest_db_renders_placeholders(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        with _build_client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert resp.status_code == 200
            assert "No transfer proposals" in resp.text
            assert "No executed withdrawals" in resp.text

    @pytest.mark.asyncio
    async def test_renders_proposals(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_proposal(
            _make_proposal(proposal_id="prop-A", amount="123.45")
        )
        with _build_client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert resp.status_code == 200
            assert "123.45" in resp.text
            assert "exchange_to_bank" in resp.text
            assert "USD" in resp.text
            assert "surplus over threshold" in resp.text

    @pytest.mark.asyncio
    async def test_renders_results_with_status_tags(
        self,
        operator_storage: SQLiteStorageAdapter,
        harvest_storage: SQLiteStorageAdapter,
    ) -> None:
        await harvest_storage.save_transfer_result(
            _make_result(transaction_id="REF-OK", status="completed")
        )
        await harvest_storage.save_transfer_result(
            _make_result(
                proposal_id="prop-2",
                transaction_id="REF-PENDING",
                status="pending",
            )
        )
        with _build_client(operator_storage, harvest_storage) as client:
            login_as(client)
            resp = client.get("/harvester")
            assert resp.status_code == 200
            assert "REF-OK" in resp.text
            assert "REF-PENDING" in resp.text
            assert "completed" in resp.text
            assert "pending" in resp.text

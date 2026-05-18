"""Tests for the cost dashboard (Stage 7.2.A)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.value_objects import Timestamp
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.routes.cost import _empty_snapshot, _rollup

pytestmark = pytest.mark.unit

_TEST_USERNAME = "operator"
_TEST_PASSWORD = "hunter2"
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="(?P<token>[^"]+)"')


def _row(
    *,
    cost: str,
    role: str = "operator",
    provider: str = "anthropic",
    hours_ago: float = 1.0,
) -> LLMCallRecord:
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    return LLMCallRecord(
        id=uuid4(),
        timestamp=Timestamp(dt=when),
        role=role,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        tokens_in=10,
        tokens_out=20,
        cost_usd=Decimal(cost),
        success=True,
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(_TEST_USERNAME, hash_password(_TEST_PASSWORD, cost=10))
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


def _login(client: TestClient) -> None:
    page = client.get("/auth/login")
    token = _CSRF_RE.search(page.text)
    assert token is not None
    resp = client.post(
        "/auth/login",
        data={
            "username": _TEST_USERNAME,
            "password": _TEST_PASSWORD,
            "csrf_token": token.group("token"),
        },
    )
    assert resp.status_code == 302


# --------------------------------------------------------------------- #
# Rollup logic (pure)                                                   #
# --------------------------------------------------------------------- #


class TestRollup:
    def test_empty_input_returns_zero_snapshot(self) -> None:
        snap = _rollup([], now=datetime.now(UTC))
        assert snap.total_24h_usd == Decimal("0")
        assert snap.total_7d_usd == Decimal("0")
        assert snap.call_count_24h == 0
        assert snap.call_count_7d == 0
        assert snap.per_day == ()
        assert snap.per_provider_role == ()

    def test_groups_by_provider_role(self) -> None:
        rows = [
            _row(cost="0.001", provider="anthropic", role="operator"),
            _row(cost="0.002", provider="anthropic", role="operator"),
            _row(cost="0.0005", provider="openai", role="quant"),
        ]
        snap = _rollup(rows, now=datetime.now(UTC))
        assert snap.total_24h_usd == Decimal("0.0035")
        assert snap.call_count_24h == 3
        # Sorted by cost desc
        assert snap.per_provider_role[0].key == "anthropic / operator"
        assert snap.per_provider_role[0].cost_usd == Decimal("0.003")
        assert snap.per_provider_role[1].key == "openai / quant"

    def test_24h_window_excludes_older_rows(self) -> None:
        rows = [
            _row(cost="0.001", hours_ago=1),
            _row(cost="0.005", hours_ago=48),
        ]
        snap = _rollup(rows, now=datetime.now(UTC))
        assert snap.total_24h_usd == Decimal("0.001")
        assert snap.total_7d_usd == Decimal("0.006")
        assert snap.call_count_24h == 1
        assert snap.call_count_7d == 2

    def test_per_day_sorted_desc(self) -> None:
        now = datetime.now(UTC)
        rows = [
            _row(cost="0.001", hours_ago=1),  # today
            _row(cost="0.002", hours_ago=24 + 1),  # yesterday
            _row(cost="0.003", hours_ago=48 + 1),  # day-before
        ]
        snap = _rollup(rows, now=now)
        days = [d.day for d in snap.per_day]
        assert days == sorted(days, reverse=True)
        assert len(snap.per_day) == 3


# --------------------------------------------------------------------- #
# Routes                                                                #
# --------------------------------------------------------------------- #


class TestCostRoute:
    def test_anonymous_redirects_to_login(self, client: TestClient) -> None:
        resp = client.get("/cost")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

    def test_authenticated_empty_renders(self, client: TestClient) -> None:
        _login(client)
        resp = client.get("/cost")
        assert resp.status_code == 200
        assert "Cost" in resp.text
        assert "No cloud LLM calls" in resp.text  # empty-state copy

    @pytest.mark.asyncio
    async def test_authenticated_with_data_renders_rollup(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Seed the operator first.
        # The fixture's already seeded a user; we just need to add rows.
        await storage.save_llm_call(_row(cost="0.001234"))
        await storage.save_llm_call(_row(cost="0.000567", provider="openai"))

        # Use a separate client since the per-fixture client doesn't
        # see async-test seeding without explicit fixture composition.
        from fastapi.testclient import TestClient

        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            _login(client)
            resp = client.get("/cost")
            assert resp.status_code == 200
            assert "0.001234" in resp.text
            assert "anthropic / operator" in resp.text
            assert "openai / operator" in resp.text


class TestCostCardFragment:
    def test_anonymous_redirects_to_login(self, client: TestClient) -> None:
        resp = client.get("/cost/card")
        assert resp.status_code == 302

    def test_authenticated_returns_fragment(self, client: TestClient) -> None:
        _login(client)
        resp = client.get("/cost/card")
        assert resp.status_code == 200
        assert "cost-card" in resp.text
        # No full-page chrome — no nav links inside the fragment.
        assert "Sign out" not in resp.text


# --------------------------------------------------------------------- #
# _empty_snapshot                                                       #
# --------------------------------------------------------------------- #


class TestEmptySnapshot:
    def test_carries_error_string(self) -> None:
        snap = _empty_snapshot(error="db down")
        assert snap.error == "db down"
        assert snap.total_24h_usd == Decimal("0")

    def test_default_no_error(self) -> None:
        snap = _empty_snapshot()
        assert snap.error is None

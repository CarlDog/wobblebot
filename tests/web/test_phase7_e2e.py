"""Phase 7 integration check — end-to-end web UI walkthrough (Stage 7.5).

One TestClient that:

1. Logs in.
2. Visits every page (dashboard / cost / advisor / harvester / news / audit).
3. Creates a pause command via POST /commands/pause.
4. Approves it via POST /commands/<id>/confirm.
5. Confirms the row is now ``approved`` in operator.db — which is
   what cli/live's ``WHERE status='approved'`` poll picks up (ADR-013
   firewall preserved end-to-end).
6. Logs out and verifies the session is gone.

This is the integration-grade equivalent of the Phase 5 e2e suite
that exercised the cli/operator → cli/live round-trip. Phase 7's
twist: the entire flow runs against in-memory storage with no
network, no Discord, no Kraken, no LLM — fully self-contained
unit test that exercises every Phase 7 surface.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.models import NewsItem, Order, Trade
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion
from wobblebot.ports.harvester import TransferProposal
from wobblebot.ports.notifier import Notification
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit

_TEST_USERNAME = "operator"
_TEST_PASSWORD = "hunter2"
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="(?P<token>[^"]+)"')
_PENDING_ID_RE = re.compile(r"/commands/([0-9a-f-]+)/confirm")


@pytest_asyncio.fixture
async def operator_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(
        _TEST_USERNAME, hash_password(_TEST_PASSWORD, cost=10)
    )
    # Pre-seed an LLM cost row + a notification so the cost +
    # audit pages have something to render.
    await adapter.save_llm_call(
        LLMCallRecord(
            id=uuid4(),
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="operator",
            provider="anthropic",
            model="claude-sonnet-4-6",
            tokens_in=100,
            tokens_out=200,
            cost_usd=Decimal("0.00123"),
            success=True,
        )
    )
    await adapter.save_notification(
        Notification(
            level="info",
            title="session started",
            message="cli/live session start",
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )
    )
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def live_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.save_order(
        Order(
            id=uuid4(),
            exchange_id="O-1",
            symbol=Symbol(base="BTC", quote="USD"),
            side="buy",
            price=Price(amount=Decimal("30000"), currency="USD"),
            amount=Amount(value=Decimal("0.001"), asset="BTC"),
            status="open",
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
    )
    await adapter.save_trade(
        Trade(
            id="TXID-1",
            order_id="O-2",
            symbol=Symbol(base="BTC", quote="USD"),
            side="buy",
            price=Price(amount=Decimal("30000"), currency="USD"),
            amount=Amount(value=Decimal("0.001"), asset="BTC"),
            fee=Decimal("0.12"),
            cost=Decimal("30.00"),
            executed_at=Timestamp(dt=datetime.now(UTC)),
        )
    )
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def advise_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.save_advisor_suggestion(
        AdvisorSuggestion(
            recommendation=AdvisorRecommendation(
                recommendation_id="rec-e2e",
                timestamp=Timestamp(dt=datetime.now(UTC)),
                role="single",
                recommendations={"spacing_percentage": 1.1},
                rationale="e2e rationale",
                confidence="medium",
            ),
            created_at=Timestamp(dt=datetime.now(UTC)),
            input_summary={"symbol": "BTC/USD"},
            model_name="phi4:14b",
        )
    )
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def harvest_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.save_transfer_proposal(
        TransferProposal(
            proposal_id="prop-e2e",
            direction="exchange_to_bank",
            asset="USD",
            amount=Decimal("250.00"),
            rationale="surplus over threshold",
            current_exchange_balance=Decimal("500"),
            target_exchange_balance=Decimal("250"),
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
    )
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def news_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.save_news_item(
        NewsItem(
            source="rss:coindesk",
            external_id="e2e-1",
            published_at=Timestamp(dt=datetime.now(UTC)),
            headline="BTC e2e walkthrough headline",
            body="",
            mentioned_coins=["BTC"],
        )
    )
    yield adapter
    await adapter.close()


@pytest.fixture
def client(
    operator_storage: SQLiteStorageAdapter,
    live_storage: SQLiteStorageAdapter,
    advise_storage: SQLiteStorageAdapter,
    harvest_storage: SQLiteStorageAdapter,
    news_storage: SQLiteStorageAdapter,
) -> "TestClient":
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator_storage,
        session_secret="x" * 64,
        live_storage=live_storage,
        advise_storage=advise_storage,
        harvest_storage=harvest_storage,
        news_storage=news_storage,
    )
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------- #
# End-to-end walkthrough                                                #
# --------------------------------------------------------------------- #


class TestE2EWalkthrough:
    """One test that exercises every Phase 7 surface end-to-end."""

    def _login(self, client: TestClient) -> None:
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

    def test_full_walkthrough(self, client: TestClient) -> None:
        # 1. Anonymous → root redirects.
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        # 2. /dashboard while anonymous → 302 /auth/login.
        resp = client.get("/dashboard")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

        # 3. Log in.
        self._login(client)

        # 4. Visit every page; verify each loads with seeded data.
        for path, must_contain in [
            ("/dashboard", "Live trading status"),
            ("/cost", "0.00123"),
            ("/advisor", "BTC/USD"),
            ("/harvester", "exchange_to_bank"),
            ("/news", "BTC e2e walkthrough headline"),
            ("/audit", "session started"),
        ]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            assert must_contain in resp.text, (
                f"{path} missing expected content {must_contain!r}; "
                f"got snippet: {resp.text[:300]!r}"
            )

        # 5. Visit /commands/pause form and create a pending command.
        form = client.get("/commands/pause")
        assert form.status_code == 200
        token = _CSRF_RE.search(form.text)
        assert token is not None
        resp = client.post(
            "/commands/pause",
            data={
                "symbol": "BTC/USD",
                "csrf_token": token.group("token"),
            },
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        m = _PENDING_ID_RE.search(loc)
        assert m is not None
        pid = m.group(1)

        # 6. Confirm page summarizes the command.
        confirm = client.get(f"/commands/{pid}/confirm")
        assert confirm.status_code == 200
        assert "BTC/USD" in confirm.text
        assert "pause" in confirm.text

        # 7. Approve.
        token = _CSRF_RE.search(confirm.text)
        assert token is not None
        resp = client.post(
            f"/commands/{pid}/confirm",
            data={
                "decision": "approve",
                "csrf_token": token.group("token"),
            },
        )
        assert resp.status_code == 200
        assert "approved" in resp.text

        # 8. ADR-013 firewall verification: the row is now `approved`
        #    in operator.db. cli/live's WHERE status='approved' poll
        #    is the only path from here to the engine — not exercised
        #    in this test (it's the live daemon's job).
        operator_storage = client.app.state.operator_storage
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            row = loop.run_until_complete(
                operator_storage.get_pending_command(UUID(pid))
            )
        finally:
            loop.close()
        assert row is not None
        assert row.status == "approved"
        assert row.channel_id == "web"
        assert row.requesting_user_id == _TEST_USERNAME
        assert row.confirming_user_id == _TEST_USERNAME

        # 9. The same row should now appear on /audit.
        resp = client.get("/audit")
        assert resp.status_code == 200
        assert "approved" in resp.text
        assert "web" in resp.text

        # 10. Logout. Get a fresh CSRF token first.
        dash = client.get("/dashboard")
        assert dash.status_code == 200
        token = _CSRF_RE.search(dash.text)
        assert token is not None
        resp = client.post(
            "/auth/logout",
            data={"csrf_token": token.group("token")},
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

        # 11. After logout, the dashboard again redirects to login.
        resp = client.get("/dashboard")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

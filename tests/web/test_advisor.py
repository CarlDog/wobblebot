"""Tests for the /advisor view (Stage 7.3.A)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion
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
async def advise_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_suggestion(
    *,
    symbol: str = "BTC/USD",
    role: str = "single",
    recommendations: dict[str, Any] | None = None,
    confidence: str = "medium",
    model: str = "phi4:14b",
    with_experts: bool = False,
) -> AdvisorSuggestion:
    rec_kwargs: dict[str, Any] = {
        "recommendation_id": "rec-" + symbol.replace("/", "-"),
        "timestamp": Timestamp(dt=datetime.now(UTC)),
        "role": role,
        "recommendations": recommendations or {"spacing_percentage": 1.2},
        "rationale": "test rationale",
        "confidence": confidence,
    }
    if with_experts:
        rec_kwargs["expert_opinions"] = [
            AdvisorRecommendation(
                recommendation_id="exp-quant",
                timestamp=Timestamp(dt=datetime.now(UTC)),
                role="quant",
                recommendations={"spacing_percentage": 1.1},
                rationale="quant says",
                confidence="high",
            ),
            AdvisorRecommendation(
                recommendation_id="exp-risk",
                timestamp=Timestamp(dt=datetime.now(UTC)),
                role="risk",
                recommendations={"spacing_percentage": 1.3},
                rationale="risk says",
                confidence="medium",
            ),
        ]
    return AdvisorSuggestion(
        recommendation=AdvisorRecommendation(**rec_kwargs),
        created_at=Timestamp(dt=datetime.now(UTC)),
        input_summary={"symbol": symbol},
        model_name=model,
    )


def _build_client(
    operator: SQLiteStorageAdapter,
    advise: SQLiteStorageAdapter | None,
) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator,
        session_secret="x" * 64,
        advise_storage=advise,
    )
    return TestClient(app, follow_redirects=False)


class TestAdvisorRoute:
    def test_anonymous_redirects(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            resp = client.get("/advisor")
            assert resp.status_code == 302

    def test_no_advise_db_renders_unwired(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "unset" in resp.text.lower()

    def test_empty_advise_db_renders_placeholder(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "No advisor suggestions" in resp.text

    @pytest.mark.asyncio
    async def test_renders_single_llm_suggestion(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        await advise_storage.save_advisor_suggestion(
            _make_suggestion(symbol="BTC/USD", role="single")
        )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "BTC/USD" in resp.text
            assert "spacing_percentage" in resp.text
            assert "phi4:14b" in resp.text
            # No experts → opinions section not rendered
            assert "Per-Expert Opinions" not in resp.text

    @pytest.mark.asyncio
    async def test_renders_moe_suggestion_with_experts(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        await advise_storage.save_advisor_suggestion(
            _make_suggestion(symbol="ETH/USD", role="aggregated", with_experts=True)
        )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "ETH/USD" in resp.text
            assert "Per-Expert Opinions" in resp.text
            assert "quant" in resp.text
            assert "risk" in resp.text

    @pytest.mark.asyncio
    async def test_lists_multiple_suggestions_newest_first(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        for sym in ("BTC/USD", "ETH/USD", "DOGE/USD"):
            await advise_storage.save_advisor_suggestion(_make_suggestion(symbol=sym))
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "BTC/USD" in resp.text
            assert "ETH/USD" in resp.text
            assert "DOGE/USD" in resp.text

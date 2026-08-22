"""Tests for the /advisor view (Stage 7.3.A)."""

from __future__ import annotations

import re
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
from wobblebot.web.routes.advisor import _as_float

pytestmark = pytest.mark.unit


class TestAsFloat:
    def test_normal_number_passes_through(self) -> None:
        assert _as_float(1.5) == 1.5
        assert _as_float(2) == 2.0
        assert _as_float("3.25") == 3.25

    def test_none_and_bool_reject(self) -> None:
        assert _as_float(None) is None
        assert _as_float(True) is None
        assert _as_float(False) is None

    def test_non_numeric_rejects(self) -> None:
        assert _as_float("not a number") is None
        assert _as_float([1, 2]) is None

    def test_nan_and_infinity_reject(self) -> None:
        """json.loads accepts bare NaN/Infinity/-Infinity tokens, and
        float() on any of them doesn't raise — _as_float must filter them
        out itself rather than relying on the try/except."""
        assert _as_float(float("nan")) is None
        assert _as_float(float("inf")) is None
        assert _as_float(float("-inf")) is None
        assert _as_float("NaN") is None
        assert _as_float("Infinity") is None
        assert _as_float("-Infinity") is None


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
    current_spacing: float | None = None,
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
    input_summary: dict[str, Any] = {"symbol": symbol}
    if current_spacing is not None:
        input_summary["current_grid"] = {"spacing_percentage": current_spacing}
    return AdvisorSuggestion(
        recommendation=AdvisorRecommendation(**rec_kwargs),
        created_at=Timestamp(dt=datetime.now(UTC)),
        input_summary=input_summary,
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
    async def test_below_floor_suggestion_is_dimmed_and_badged(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        # Proposes 1.0% while looking at a 3.0% grid -> the auto-apply floor
        # would reject it, so the card is dimmed and badged.
        await advise_storage.save_advisor_suggestion(
            _make_suggestion(
                symbol="BTC/USD",
                recommendations={"spacing_percentage": 1.0},
                current_spacing=3.0,
            )
        )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "below-floor" in resp.text  # dim class on the card
            assert "below floor" in resp.text  # the badge label

    @pytest.mark.asyncio
    async def test_non_finite_proposed_spacing_is_not_tagged_below_floor(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        """A garbled -Infinity proposal must not spuriously trip below_floor.

        json.loads accepts bare -Infinity/Infinity/NaN tokens, and
        float("-inf") is a valid, non-raising float. Unchecked, `-inf` reads
        as "less than" any finite current_spacing, so the row would render
        `below-floor` + a tooltip literally saying "Proposed -inf% is
        tighter than...". `_as_float` now rejects non-finite values (same
        class of fix as services/auto_apply._coerce_numeric)."""
        await advise_storage.save_advisor_suggestion(
            _make_suggestion(
                symbol="BTC/USD",
                recommendations={"spacing_percentage": float("-inf")},
                current_spacing=3.0,
            )
        )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "below-floor" not in resp.text
            assert "below floor" not in resp.text

    @pytest.mark.asyncio
    async def test_at_or_above_floor_is_not_tagged(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        # Spacing == current (at floor) and a widen above it: neither tagged.
        await advise_storage.save_advisor_suggestion(
            _make_suggestion(
                symbol="ETH/USD",
                recommendations={"spacing_percentage": 3.0},
                current_spacing=3.0,
            )
        )
        await advise_storage.save_advisor_suggestion(
            _make_suggestion(
                symbol="DOGE/USD",
                recommendations={"spacing_percentage": 4.0},
                current_spacing=3.0,
            )
        )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200
            assert "below-floor" not in resp.text
            assert "below floor" not in resp.text

    @pytest.mark.asyncio
    async def test_confidence_badges_carry_descriptive_titles(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        """The confidence tag on the aggregate row and on each per-expert
        row must explain what high/medium/low mean, not just show the bare
        word — the badges alone don't tell an unfamiliar user anything."""
        await advise_storage.save_advisor_suggestion(
            _make_suggestion(symbol="BTC/USD", confidence="medium", with_experts=True)
        )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            resp = client.get("/advisor")
            assert resp.status_code == 200

        titles = re.findall(r'title="([^"]*confidence[^"]*)"', resp.text, re.IGNORECASE)
        # Aggregate row: medium. Experts: quant=high, risk=medium.
        assert titles == [
            "Medium confidence — a real but mixed signal; worth weighing, not decisive on its own.",
            "High confidence — a clear, well-supported signal; the advisor would act on this call.",
            "Medium confidence — a real but mixed signal; worth weighing, not decisive on its own.",
        ]

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


class TestAdvisorCollapse:
    """Older suggestions collapse to their header (P3 slice 23).

    Each row is a full stacked card with a nested per-expert table, so
    a busy cli/advise turned this page into a wall. Native <details>,
    no JS — the CSP is script-src 'self'.
    """

    @pytest.mark.asyncio
    async def test_newest_three_are_open_the_rest_collapsed(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        for i in range(5):
            await advise_storage.save_advisor_suggestion(
                _make_suggestion(symbol=f"SYM{i}/USD", role="single")
            )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            body = client.get("/advisor").text
        assert body.count('<details class="card advisor-card') == 5
        # Exactly the newest three carry the `open` attribute; the rest
        # render collapsed.
        opens = re.findall(r'<details class="card advisor-card[^>]*?\bopen\s*>', body)
        assert len(opens) == 3

    @pytest.mark.asyncio
    async def test_collapsed_header_still_carries_what_you_scan_for(
        self,
        operator_storage: SQLiteStorageAdapter,
        advise_storage: SQLiteStorageAdapter,
    ) -> None:
        """A closed card must still answer symbol / when / confidence —
        collapsing must not hide the fields you triage on."""
        for i in range(5):
            await advise_storage.save_advisor_suggestion(
                _make_suggestion(symbol=f"SYM{i}/USD", role="single")
            )
        with _build_client(operator_storage, advise_storage) as client:
            login_as(client)
            body = client.get("/advisor").text
        # Every symbol renders, including the ones past the open cutoff.
        for i in range(5):
            assert f"SYM{i}/USD" in body
        assert '<summary class="card-header">' in body

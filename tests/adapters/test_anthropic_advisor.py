"""Unit tests for AnthropicAdvisorAdapter (Stage 6.2.A).

HTTP layer mocked via ``httpx.MockTransport`` so tests stay
deterministic and never touch a real Anthropic endpoint. Storage
is a real in-memory ``SQLiteStorageAdapter`` so the
cost-tracking flow (gate check + LLMCallRecord persistence)
exercises end-to-end against actual schema.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from wobblebot.adapters.anthropic import (
    AnthropicAdvisorAdapter,
    extract_anthropic_tokens,
    parse_text_blocks,
)
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.prompts import Prompt, PromptMetadata
from wobblebot.domain.exceptions import LLMCostCapExceeded, LLMRetryExhausted
from wobblebot.ports.advisor import PerformanceSummary
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_pricing import estimate_cost_ceiling
from wobblebot.services.llm_retry import LLMRetryConfig

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- #
# Fixtures + helpers                                                    #
# --------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_prompt() -> Prompt:
    return Prompt(
        metadata=PromptMetadata(
            role="quant",
            description="Test prompt",
            response_schema="advisor_recommendation_v1",
            temperature_hint=0.5,
        ),
        body="You are a test quant expert. Emit JSON.",
        source_path=Path("config/prompts/quant.md"),
    )


def _make_summary() -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=24.0,
        latest_price=80000.0,
        snapshot_count=1000,
        volatility=0.0004,
        max_drawdown=-0.03,
        flatness=0.97,
        cycle_count=0,
        win_rate=0.0,
        total_pnl=0.0,
    )


def _anthropic_envelope(
    *,
    inner: dict[str, object] | str,
    tokens_in: int = 250,
    tokens_out: int = 120,
    msg_id: str = "msg_abc123",
    model: str = "claude-sonnet-4-6",
) -> dict[str, object]:
    """Build an Anthropic Messages-API response envelope."""
    if isinstance(inner, dict):
        text = json.dumps(inner)
    else:
        text = inner
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
        },
    }


def _valid_recommendation_dict() -> dict[str, object]:
    return {
        "role": "quant",
        "recommendations": {"spacing_percentage": 1.2},
        "rationale": "Volatility is low; widen slightly.",
        "confidence": "medium",
    }


def _build_adapter(
    transport: httpx.MockTransport,
    storage: SQLiteStorageAdapter,
    *,
    cost_config: LLMCostConfig | None = None,
    retry_config: LLMRetryConfig | None = None,
    tracker: SessionCostTracker | None = None,
    model: str = "claude-sonnet-4-6",
    role: str = "quant",
) -> AnthropicAdvisorAdapter:
    client = httpx.AsyncClient(transport=transport)
    return AnthropicAdvisorAdapter(
        model=model,
        prompt=_make_prompt(),
        role=role,  # type: ignore[arg-type]
        api_key="sk-test",
        storage=storage,
        session_tracker=tracker or SessionCostTracker(),
        cost_config=cost_config or LLMCostConfig(),
        retry_config=retry_config or LLMRetryConfig(max_retries=2, initial_backoff_seconds=0.01),
        client=client,
    )


# --------------------------------------------------------------------- #
# Pure helpers                                                          #
# --------------------------------------------------------------------- #


class TestPureHelpers:
    def test_estimate_cost_ceiling_uses_max_tokens(self) -> None:
        # 1000 chars / 4 = 250 input tokens; max_tokens=500 → cost reflects both.
        prompt = "a" * 1000
        cost = estimate_cost_ceiling(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_text=prompt,
            max_tokens=500,
        )
        # claude-sonnet-4-6: $3/1M in, $15/1M out
        # 250 * 3 / 1M + 500 * 15 / 1M = 0.00075 + 0.0075 = 0.00825
        assert cost == Decimal("0.008250")

    def test_estimate_cost_minimum_input_tokens(self) -> None:
        # Empty prompt → at least 1 input token estimated.
        cost = estimate_cost_ceiling(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_text="",
            max_tokens=10,
        )
        # 1 input + 10 output: 1 * 3/1M + 10 * 15/1M = 0.000003 + 0.00015 = 0.000153
        assert cost == Decimal("0.000153")

    def test_parse_text_blocks_concatenates(self) -> None:
        content = [
            {"type": "text", "text": "Hello "},
            {"type": "thinking", "thinking": "reasoning..."},
            {"type": "text", "text": "world"},
        ]
        assert parse_text_blocks(content) == "Hello world"

    def test_parse_text_blocks_skips_non_text(self) -> None:
        content = [
            {"type": "tool_use", "id": "x", "name": "n", "input": {}},
            {"type": "text", "text": "only this"},
        ]
        assert parse_text_blocks(content) == "only this"

    def test_parse_text_blocks_empty(self) -> None:
        assert parse_text_blocks([]) == ""

    def test_extract_anthropic_tokens(self) -> None:
        """Anthropic-specific normalization: tokens_reasoning is None
        because the API lumps thinking with output."""
        envelope = {
            "id": "msg_abc",
            "usage": {"input_tokens": 100, "output_tokens": 200},
        }
        tokens_in, tokens_out, tokens_reasoning, request_id = extract_anthropic_tokens(envelope)
        assert tokens_in == 100
        assert tokens_out == 200
        assert tokens_reasoning is None
        assert request_id == "msg_abc"

    def test_extract_anthropic_tokens_missing_usage(self) -> None:
        """Defensive: missing usage block → zeros + no request_id."""
        envelope: dict[str, object] = {}
        tokens_in, tokens_out, tokens_reasoning, request_id = extract_anthropic_tokens(envelope)
        assert tokens_in == 0
        assert tokens_out == 0
        assert tokens_reasoning is None
        assert request_id is None


# --------------------------------------------------------------------- #
# Happy path                                                            #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestHappyPath:
    async def test_round_trip_persists_cost(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _anthropic_envelope(inner=_valid_recommendation_dict())

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == "sk-test"
            assert request.headers["anthropic-version"] == "2023-06-01"
            body = json.loads(request.content)
            assert body["model"] == "claude-sonnet-4-6"
            assert body["max_tokens"] > 0
            return httpx.Response(200, json=envelope)

        tracker = SessionCostTracker()
        adapter = _build_adapter(httpx.MockTransport(handler), storage, tracker=tracker)
        rec = await adapter.get_recommendation(_make_summary())

        assert rec.role == "quant"
        assert rec.confidence == "medium"
        assert rec.recommendations == {"spacing_percentage": 1.2}

        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        persisted = rows[0]
        assert persisted.provider == "anthropic"
        assert persisted.model == "claude-sonnet-4-6"
        assert persisted.tokens_in == 250
        assert persisted.tokens_out == 120
        assert persisted.success is True
        # 250 * 3/1M + 120 * 15/1M = 0.00075 + 0.0018 = 0.00255
        assert persisted.cost_usd == Decimal("0.002550")
        assert tracker.total == Decimal("0.002550")

    async def test_handles_prose_then_json(self, storage: SQLiteStorageAdapter) -> None:
        """Anthropic often wraps JSON in explanatory prose. The extractor
        should walk past the prose and find the answer."""
        envelope = _anthropic_envelope(
            inner=(
                "Here is my analysis:\n\n"
                "Volatility is low, so I recommend widening the grid.\n\n"
                "```json\n" + json.dumps(_valid_recommendation_dict()) + "\n```"
            )
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        rec = await adapter.get_recommendation(_make_summary())
        assert rec.confidence == "medium"

    async def test_request_id_recorded(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _anthropic_envelope(inner=_valid_recommendation_dict(), msg_id="msg_xyz999")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        await adapter.get_recommendation(_make_summary())
        rows = await storage.get_llm_calls()
        assert rows[0].request_id == "msg_xyz999"


# --------------------------------------------------------------------- #
# Cost gate                                                             #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestCostGate:
    async def test_daily_cap_blocks_call(self, storage: SQLiteStorageAdapter) -> None:
        # Plant rows that put daily total near cap.
        from datetime import UTC, datetime, timedelta

        from wobblebot.domain.llm_cost import LLMCallRecord
        from wobblebot.domain.value_objects import Timestamp

        for i in range(5):
            await storage.save_llm_call(
                LLMCallRecord(
                    timestamp=Timestamp(dt=datetime.now(UTC) - timedelta(minutes=i)),
                    role="operator",
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    tokens_in=10,
                    tokens_out=10,
                    cost_usd=Decimal("0.20"),
                    success=True,
                )
            )
        # 5 × $0.20 = $1.00 spent; default daily cap is $1.00 → next call
        # of any size would exceed.
        adapter = _build_adapter(
            httpx.MockTransport(lambda _: httpx.Response(500)),  # would fail anyway if reached
            storage,
        )
        with pytest.raises(LLMCostCapExceeded) as exc_info:
            await adapter.get_recommendation(_make_summary())
        assert exc_info.value.cap_kind == "daily"
        # No new row added — the gate stopped us BEFORE the call.
        rows = await storage.get_llm_calls()
        assert len(rows) == 5

    async def test_session_cap_blocks_call(self, storage: SQLiteStorageAdapter) -> None:
        # Tracker shows session already near the cap.
        tracker = SessionCostTracker(initial=Decimal("0.495"))
        adapter = _build_adapter(
            httpx.MockTransport(lambda _: httpx.Response(500)),
            storage,
            tracker=tracker,
        )
        with pytest.raises(LLMCostCapExceeded) as exc_info:
            await adapter.get_recommendation(_make_summary())
        assert exc_info.value.cap_kind == "session"

    async def test_dry_run_posture_allows_over_cap(self, storage: SQLiteStorageAdapter) -> None:
        # enforce=False → gate never denies, even with crazy tracker total.
        tracker = SessionCostTracker(initial=Decimal("1000"))
        envelope = _anthropic_envelope(inner=_valid_recommendation_dict())

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(
            httpx.MockTransport(handler),
            storage,
            cost_config=LLMCostConfig(
                max_spend_per_day_usd=Decimal("0.01"),
                max_spend_per_session_usd=Decimal("0.01"),
                enforce=False,
            ),
            tracker=tracker,
        )
        # Should succeed without raising.
        await adapter.get_recommendation(_make_summary())


# --------------------------------------------------------------------- #
# Retry/backoff                                                         #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestRetryPath:
    async def test_succeeds_after_transient_5xx(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _anthropic_envelope(inner=_valid_recommendation_dict())
        responses = [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json=envelope),
        ]
        call_count = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            r = responses[call_count[0]]
            call_count[0] += 1
            return r

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        rec = await adapter.get_recommendation(_make_summary())
        assert rec.confidence == "medium"
        assert call_count[0] == 3

    async def test_permanent_4xx_fails_immediately(self, storage: SQLiteStorageAdapter) -> None:
        call_count = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(401, json={"error": "unauthorized"})

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError, match="HTTP 401"):
            await adapter.get_recommendation(_make_summary())
        assert call_count[0] == 1  # no retries
        # Failure record persisted with error_kind=http_401.
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is False
        assert rows[0].error_kind == "http_401"

    async def test_exhausted_retries_records_failure(self, storage: SQLiteStorageAdapter) -> None:
        call_count = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(503)

        adapter = _build_adapter(
            httpx.MockTransport(handler),
            storage,
            retry_config=LLMRetryConfig(max_retries=2, initial_backoff_seconds=0.01),
        )
        # retry_with_backoff raises LLMRetryExhausted after the budget.
        # The adapter catches httpx.HTTPError (which LLMRetryExhausted is NOT)
        # — so the exhaustion propagates as-is. Operator-notification layer
        # at cli/* handles the surface.
        with pytest.raises(LLMRetryExhausted):
            await adapter.get_recommendation(_make_summary())
        assert call_count[0] == 3  # 1 initial + 2 retries

    async def test_429_classified_transient(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _anthropic_envelope(inner=_valid_recommendation_dict())
        responses = [
            httpx.Response(429),
            httpx.Response(200, json=envelope),
        ]
        call_count = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            r = responses[call_count[0]]
            call_count[0] += 1
            return r

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        await adapter.get_recommendation(_make_summary())
        assert call_count[0] == 2


# --------------------------------------------------------------------- #
# Parse failures                                                        #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestParseFailures:
    async def test_missing_confidence_field(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _anthropic_envelope(
            inner={
                "role": "quant",
                "recommendations": {"spacing_percentage": 1.2},
                "rationale": "blah",
                # confidence missing
            }
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError, match="confidence"):
            await adapter.get_recommendation(_make_summary())
        # Cost still recorded — call succeeded at the API level.
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is True  # API succeeded; parse failed later

    async def test_empty_content(self, storage: SQLiteStorageAdapter) -> None:
        envelope = {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError, match="empty"):
            await adapter.get_recommendation(_make_summary())

    async def test_non_json_response(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _anthropic_envelope(inner="not valid json at all { bad")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError):
            await adapter.get_recommendation(_make_summary())


# --------------------------------------------------------------------- #
# Construction guards                                                   #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestConstruction:
    async def test_empty_api_key_rejected(self, storage: SQLiteStorageAdapter) -> None:
        with pytest.raises(ValueError, match="api_key"):
            AnthropicAdvisorAdapter(
                model="claude-sonnet-4-6",
                prompt=_make_prompt(),
                role="quant",
                api_key="",
                storage=storage,
                session_tracker=SessionCostTracker(),
                cost_config=LLMCostConfig(),
                retry_config=LLMRetryConfig(),
            )

    async def test_validate_recommendation_returns_true(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        envelope = _anthropic_envelope(inner=_valid_recommendation_dict())

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        rec = await adapter.get_recommendation(_make_summary())
        assert await adapter.validate_recommendation(rec) is True

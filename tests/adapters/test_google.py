"""Unit tests for GoogleAdvisorAdapter + GoogleAssistantAdapter (Stage 6.4.A).

The exhaustive cost-flow / retry / cost-gate tests live in
``tests/services/test_llm_cloud_call.py`` (shared orchestrator).
Tests here focus on the Google-specific bits:

- Gemini REST wire shape (``x-goog-api-key`` header,
  ``/v1beta/models/{model}:generateContent`` URL,
  ``systemInstruction`` field, role=``model`` in contents).
- Native additive reasoning-token shape
  (``thoughtsTokenCount`` is separate from ``candidatesTokenCount``).
- ``parse_candidate_text`` handles parts arrays.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from wobblebot.adapters.google import (
    GoogleAdvisorAdapter,
    GoogleAssistantAdapter,
    extract_google_tokens,
    parse_candidate_text,
)
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.prompts import Prompt, PromptMetadata
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.advisor import PerformanceSummary
from wobblebot.ports.assistant import (
    ConversationContext,
    ConversationTurn,
    EngineStateSnapshot,
)
from wobblebot.ports.exceptions import AdvisorError, AssistantError
from wobblebot.ports.operator import (
    IntentCommand,
    IntentQuery,
    PauseCommand,
    StatusQuery,
)
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


def _quant_prompt() -> Prompt:
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


def _operator_prompt() -> Prompt:
    return Prompt(
        metadata=PromptMetadata(
            role="operator",
            description="Test operator prompt",
            response_schema="operator_intent_v1",
            temperature_hint=0.3,
        ),
        body="You are the operator assistant. Emit operator_intent_v1 JSON.",
        source_path=Path("config/prompts/operator.md"),
    )


def _summary() -> PerformanceSummary:
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


def _snapshot() -> EngineStateSnapshot:
    return EngineStateSnapshot(
        snapshot_at=Timestamp(dt=datetime.now(UTC)),
        symbols=[],
        total_usd_balance=100.0,
        session_pnl=0.0,
        session_runtime_seconds=10.0,
    )


def _ctx(current: str = "pause BTC") -> ConversationContext:
    return ConversationContext(
        current_message=current,
        channel_id="C-1",
        user_id="U-1",
        recent_turns=(),
        engine_state_snapshot=_snapshot(),
    )


def _gemini_envelope(
    *,
    text: str,
    prompt_tokens: int = 200,
    candidates_tokens: int = 100,
    thoughts_tokens: int | None = None,
    cached_tokens: int | None = None,
    response_id: str | None = "rsp-abc",
) -> dict[str, object]:
    usage: dict[str, object] = {
        "promptTokenCount": prompt_tokens,
        "candidatesTokenCount": candidates_tokens,
        "totalTokenCount": prompt_tokens + candidates_tokens,
    }
    if thoughts_tokens is not None:
        usage["thoughtsTokenCount"] = thoughts_tokens
        usage["totalTokenCount"] = prompt_tokens + candidates_tokens + thoughts_tokens
    if cached_tokens is not None:
        # Real Gemini shape: promptTokenCount INCLUDES the cached count
        # (API reference: total effective prompt size), so the fixture
        # does NOT add it to the totals — cached is a subset of prompt.
        usage["cachedContentTokenCount"] = cached_tokens
    envelope: dict[str, object] = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": usage,
    }
    if response_id is not None:
        envelope["responseId"] = response_id
    return envelope


def _valid_recommendation() -> dict[str, object]:
    return {
        "role": "quant",
        "recommendations": {"spacing_percentage": 1.2},
        "rationale": "Low vol; widen.",
        "confidence": "medium",
    }


def _build_advisor(
    transport: httpx.MockTransport,
    storage: SQLiteStorageAdapter,
    *,
    tracker: SessionCostTracker | None = None,
    cost_config: LLMCostConfig | None = None,
    model: str = "gemini-2.5-pro",
    role: str = "quant",
) -> GoogleAdvisorAdapter:
    return GoogleAdvisorAdapter(
        model=model,
        prompt=_quant_prompt(),
        role=role,  # type: ignore[arg-type]
        api_key="goog-test",
        storage=storage,
        session_tracker=tracker or SessionCostTracker(),
        cost_config=cost_config or LLMCostConfig(),
        retry_config=LLMRetryConfig(max_retries=2, initial_backoff_seconds=0.01),
        client=httpx.AsyncClient(transport=transport),
    )


def _build_assistant(
    transport: httpx.MockTransport,
    storage: SQLiteStorageAdapter,
    *,
    tracker: SessionCostTracker | None = None,
    prompt: Prompt | None = None,
    model: str = "gemini-2.5-flash",
) -> GoogleAssistantAdapter:
    return GoogleAssistantAdapter(
        model=model,
        prompt=prompt or _operator_prompt(),
        api_key="goog-test",
        storage=storage,
        session_tracker=tracker or SessionCostTracker(),
        cost_config=LLMCostConfig(),
        retry_config=LLMRetryConfig(max_retries=2, initial_backoff_seconds=0.01),
        client=httpx.AsyncClient(transport=transport),
    )


# --------------------------------------------------------------------- #
# Pure helpers                                                          #
# --------------------------------------------------------------------- #


class TestPureHelpers:
    def test_estimate_cost_ceiling_pro(self) -> None:
        # gemini-2.5-pro: $1.25/1M in, $10.00/1M out
        # 1000 chars / 4 = 250 in; max_tokens=500
        # 250 * 1.25/1M + 500 * 10/1M = 0.0003125 + 0.005 = 0.0053125
        # Quantized to 6dp = 0.005313 (ROUND_HALF_UP)
        cost = estimate_cost_ceiling(
            provider="google",
            model="gemini-2.5-pro",
            prompt_text="a" * 1000,
            max_tokens=500,
        )
        assert cost == Decimal("0.005313")

    def test_extract_tokens_no_thinking(self) -> None:
        envelope = _gemini_envelope(text="hi", prompt_tokens=50, candidates_tokens=30)
        usage = extract_google_tokens(envelope)
        assert usage.tokens_in == 50
        assert usage.tokens_out == 30
        assert usage.tokens_reasoning is None
        assert usage.tokens_cache_read == 0
        assert usage.request_id == "rsp-abc"

    def test_extract_tokens_with_thinking_additive(self) -> None:
        """Gemini's thinking shape is natively additive — record as-is."""
        envelope = _gemini_envelope(
            text="hi",
            prompt_tokens=100,
            candidates_tokens=50,
            thoughts_tokens=300,
        )
        usage = extract_google_tokens(envelope)
        assert usage.tokens_in == 100
        assert usage.tokens_out == 50  # NOT subtracted (unlike OpenAI)
        assert usage.tokens_reasoning == 300

    def test_extract_tokens_with_cached(self) -> None:
        """promptTokenCount INCLUDES cachedContentTokenCount (implicit
        caching, 2.5+) — adapter subtracts so the buckets stay disjoint."""
        envelope = _gemini_envelope(
            text="hi",
            prompt_tokens=2000,
            candidates_tokens=100,
            cached_tokens=1500,
        )
        usage = extract_google_tokens(envelope)
        assert usage.tokens_in == 500  # 2000 - 1500 uncached
        assert usage.tokens_cache_read == 1500
        assert usage.tokens_cache_write == 0  # Gemini has no billed write step

    def test_extract_tokens_cached_exceeds_prompt_clamped(self) -> None:
        """Defensive clamp mirrors the OpenAI extractor — no negative
        tokens_in even on a nonsensical payload."""
        envelope = _gemini_envelope(
            text="hi", prompt_tokens=100, candidates_tokens=10, cached_tokens=150
        )
        usage = extract_google_tokens(envelope)
        assert usage.tokens_in == 0
        assert usage.tokens_cache_read == 150

    def test_extract_tokens_zero_thinking(self) -> None:
        """Explicit zero thinking → None (no signal-free zero column)."""
        envelope = _gemini_envelope(
            text="hi", prompt_tokens=10, candidates_tokens=5, thoughts_tokens=0
        )
        usage = extract_google_tokens(envelope)
        assert usage.tokens_reasoning is None

    def test_extract_tokens_empty_usage(self) -> None:
        envelope: dict[str, object] = {"candidates": []}
        usage = extract_google_tokens(envelope)
        assert usage.tokens_in == 0
        assert usage.tokens_out == 0
        assert usage.tokens_reasoning is None
        assert usage.tokens_cache_read == 0
        assert usage.request_id is None

    def test_extract_tokens_no_response_id(self) -> None:
        """Older Gemini responses omitted responseId; we surface None."""
        envelope = _gemini_envelope(text="hi", response_id=None)
        usage = extract_google_tokens(envelope)
        assert usage.request_id is None

    def test_parse_candidate_text_basic(self) -> None:
        envelope = _gemini_envelope(text="hello world")
        assert parse_candidate_text(envelope) == "hello world"

    def test_parse_candidate_text_multiple_parts(self) -> None:
        """Some responses come back as multiple parts (e.g. thinking +
        answer split). We concatenate all text parts."""
        envelope = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Hello "},
                            {"text": "world"},
                        ],
                        "role": "model",
                    },
                }
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
        }
        assert parse_candidate_text(envelope) == "Hello world"

    def test_parse_candidate_text_filters_non_text_parts(self) -> None:
        envelope = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"inlineData": {"mimeType": "image/png", "data": "..."}},
                            {"text": "only this"},
                        ],
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
        assert parse_candidate_text(envelope) == "only this"

    def test_parse_candidate_text_empty(self) -> None:
        envelope: dict[str, object] = {"candidates": []}
        assert parse_candidate_text(envelope) == ""


# --------------------------------------------------------------------- #
# Wire shape                                                            #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestWireShape:
    async def test_api_key_header_and_endpoint(self, storage: SQLiteStorageAdapter) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["api_key"] = request.headers.get("x-goog-api-key")
            return httpx.Response(
                200,
                json=_gemini_envelope(text=json.dumps(_valid_recommendation())),
            )

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        await adapter.get_recommendation(_summary())
        url = captured["url"]
        assert isinstance(url, str)
        assert url.endswith("/v1beta/models/gemini-2.5-pro:generateContent")
        assert captured["api_key"] == "goog-test"

    async def test_body_has_system_instruction_and_user_content(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_gemini_envelope(text=json.dumps(_valid_recommendation())),
            )

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        await adapter.get_recommendation(_summary())
        body = captured["body"]
        assert isinstance(body, dict)
        # System prompt lives in systemInstruction, NOT in contents
        system_text = body["systemInstruction"]["parts"][0]["text"]
        assert "quant expert" in system_text
        # First user turn carries the summary
        first_user = body["contents"][0]
        assert first_user["role"] == "user"
        assert "BTC/USD" in first_user["parts"][0]["text"]
        # Generation config carries the knobs
        assert body["generationConfig"]["temperature"] == 0.5
        assert body["generationConfig"]["maxOutputTokens"] == 1024

    async def test_assistant_maps_assistant_role_to_model(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Critical Gemini quirk: assistant turns use role=model on the wire."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_gemini_envelope(
                    text=json.dumps({"kind": "conversational", "reply_text": "hi"})
                ),
            )

        turns = (
            ConversationTurn(
                id=uuid4(),
                channel_id="C-1",
                user_id="U-1",
                role="operator",
                content="show fills",
                intent=None,
                timestamp=Timestamp(dt=datetime.now(UTC)),
            ),
            ConversationTurn(
                id=uuid4(),
                channel_id="C-1",
                user_id="U-1",
                role="assistant",
                content="here are fills",
                intent=None,
                timestamp=Timestamp(dt=datetime.now(UTC)),
            ),
        )
        ctx = ConversationContext(
            current_message="filter to BTC",
            channel_id="C-1",
            user_id="U-1",
            recent_turns=turns,
            engine_state_snapshot=_snapshot(),
        )
        adapter = _build_assistant(httpx.MockTransport(handler), storage)
        await adapter.parse_intent(ctx)
        contents = captured["body"]["contents"]  # type: ignore[index]
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"  # NOT "assistant"
        assert contents[2]["role"] == "user"  # current message


# --------------------------------------------------------------------- #
# Advisor happy path                                                    #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAdvisorHappyPath:
    async def test_round_trip_records_cost(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _gemini_envelope(
            text=json.dumps(_valid_recommendation()),
            prompt_tokens=400,
            candidates_tokens=100,
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        tracker = SessionCostTracker()
        adapter = _build_advisor(httpx.MockTransport(handler), storage, tracker=tracker)
        rec = await adapter.get_recommendation(_summary())
        assert rec.confidence == "medium"
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].provider == "google"
        assert rows[0].tokens_in == 400
        assert rows[0].tokens_out == 100
        # gemini-2.5-pro: $1.25/1M in, $10/1M out
        # 400*1.25/1M + 100*10/1M = 0.0005 + 0.001 = 0.0015
        assert rows[0].cost_usd == Decimal("0.001500")
        assert tracker.total == Decimal("0.001500")
        assert rows[0].request_id == "rsp-abc"

    async def test_thinking_tokens_recorded_additively(self, storage: SQLiteStorageAdapter) -> None:
        """Gemini-flash thinking mode: 100 visible output + 300 thoughts.
        Adapter records as-is (no subtraction). Cost uses the explicit
        thinking rate per the pricing override."""
        envelope = _gemini_envelope(
            text=json.dumps(_valid_recommendation()),
            prompt_tokens=100,
            candidates_tokens=100,
            thoughts_tokens=300,
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_advisor(httpx.MockTransport(handler), storage, model="gemini-2.5-flash")
        await adapter.get_recommendation(_summary())
        rows = await storage.get_llm_calls()
        assert rows[0].tokens_out == 100
        assert rows[0].tokens_reasoning == 300
        # gemini-2.5-flash: $0.30/1M in, $2.50/1M out. Thoughts bill at the
        # output rate — Google folded thinking into "Output price (including
        # thinking tokens)" (re-verified 2026-07-23), so there is no separate
        # thoughts rate. Still additive: the 300 thought tokens are charged
        # on top of the 100 output tokens, not folded into them.
        # 100*0.30/1M + 100*2.50/1M + 300*2.50/1M
        # = 0.00003 + 0.00025 + 0.00075 = 0.00103
        assert rows[0].cost_usd == Decimal("0.001030")

    async def test_prose_wrapping_json(self, storage: SQLiteStorageAdapter) -> None:
        wrapped = (
            "Here's my analysis:\n\n" "```json\n" + json.dumps(_valid_recommendation()) + "\n```\n"
        )
        envelope = _gemini_envelope(text=wrapped)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        rec = await adapter.get_recommendation(_summary())
        assert rec.confidence == "medium"


# --------------------------------------------------------------------- #
# Advisor failures                                                      #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAdvisorFailures:
    async def test_http_403_wraps_as_advisor_error(self, storage: SQLiteStorageAdapter) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"message": "API key invalid"}})

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError, match="HTTP 403"):
            await adapter.get_recommendation(_summary())
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is False
        assert rows[0].error_kind == "http_403"

    async def test_empty_content_raises(self, storage: SQLiteStorageAdapter) -> None:
        envelope = {"candidates": [], "usageMetadata": {"promptTokenCount": 5}}

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError, match="empty"):
            await adapter.get_recommendation(_summary())


# --------------------------------------------------------------------- #
# Assistant                                                             #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAssistant:
    async def test_command_intent_round_trip(self, storage: SQLiteStorageAdapter) -> None:
        intent = {
            "kind": "command",
            "command": {"kind": "pause", "symbol": {"base": "BTC", "quote": "USD"}},
        }
        envelope = _gemini_envelope(text=json.dumps(intent))

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_assistant(httpx.MockTransport(handler), storage)
        result = await adapter.parse_intent(_ctx())
        assert isinstance(result, IntentCommand)
        assert isinstance(result.command, PauseCommand)
        assert result.command.symbol == Symbol(base="BTC", quote="USD")
        rows = await storage.get_llm_calls()
        assert rows[0].role == "operator"
        assert rows[0].provider == "google"

    async def test_query_intent_round_trip(self, storage: SQLiteStorageAdapter) -> None:
        intent = {"kind": "query", "query": {"kind": "status"}}
        envelope = _gemini_envelope(text=json.dumps(intent))

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_assistant(httpx.MockTransport(handler), storage)
        result = await adapter.parse_intent(_ctx(current="status?"))
        assert isinstance(result, IntentQuery)
        assert isinstance(result.query, StatusQuery)

    async def test_unpriced_model_wraps_as_assistant_error(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Fleet-review #19 finding 7: an unpriced model must surface as
        # AssistantError, not a raw PricingLookupError escaping past
        # wrap_provider_errors. The estimate now runs inside
        # wrap_provider_errors on the assistant path too, so the lookup
        # miss is translated before it escapes — and the HTTP call never
        # fires.
        def handler(_r: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("HTTP call must not fire when pricing lookup fails")

        adapter = _build_assistant(
            httpx.MockTransport(handler), storage, model="gemini-nonexistent-unpriced"
        )
        with pytest.raises(AssistantError, match="pricing unavailable"):
            await adapter.parse_intent(_ctx())

    async def test_non_operator_prompt_rejected(self) -> None:
        with pytest.raises(AssistantError, match="operator-role"):
            GoogleAssistantAdapter(
                model="gemini-2.5-flash",
                prompt=_quant_prompt(),
                api_key="goog-test",
                storage=None,  # type: ignore[arg-type]
                session_tracker=SessionCostTracker(),
                cost_config=LLMCostConfig(),
                retry_config=LLMRetryConfig(),
            )

    async def test_empty_api_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            GoogleAssistantAdapter(
                model="gemini-2.5-flash",
                prompt=_operator_prompt(),
                api_key="",
                storage=None,  # type: ignore[arg-type]
                session_tracker=SessionCostTracker(),
                cost_config=LLMCostConfig(),
                retry_config=LLMRetryConfig(),
            )

    async def test_cost_cap_trips_blocks_call(self, storage: SQLiteStorageAdapter) -> None:
        # Push tracker close enough to the $0.50 session cap that the
        # gemini-2.5-flash estimate ($0.30/1M in + $2.50/1M out * 512
        # max_tokens ≈ $0.00128 + input) tips it over.
        tracker = SessionCostTracker(initial=Decimal("0.499"))
        adapter = _build_assistant(
            httpx.MockTransport(lambda _r: httpx.Response(500)),
            storage,
            tracker=tracker,
        )
        with pytest.raises(LLMCostCapExceeded):
            await adapter.parse_intent(_ctx())


# --------------------------------------------------------------------- #
# Construction guards                                                   #
# --------------------------------------------------------------------- #


def test_advisor_empty_api_key_rejected() -> None:
    with pytest.raises(ValueError, match="api_key"):
        GoogleAdvisorAdapter(
            model="gemini-2.5-pro",
            prompt=_quant_prompt(),
            role="quant",
            api_key="",
            storage=None,  # type: ignore[arg-type]
            session_tracker=SessionCostTracker(),
            cost_config=LLMCostConfig(),
            retry_config=LLMRetryConfig(),
        )

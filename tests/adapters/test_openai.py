"""Unit tests for OpenAIAdvisorAdapter + OpenAIAssistantAdapter (Stage 6.3.B).

HTTP layer mocked via ``httpx.MockTransport``; storage is a real
in-memory SQLite so cost-tracking round-trips through the actual
schema.

The exhaustive cost-flow / retry / cost-gate tests already live in
``tests/services/test_llm_cloud_call.py`` since both adapters now
share that orchestrator. Tests here focus on the OpenAI-specific
bits:

- Chat Completions wire shape (Bearer auth, system-as-message,
  ``max_completion_tokens``).
- Reasoning-token normalization (``completion_tokens -
  reasoning_tokens = tokens_out``).
- ``is_reasoning_model`` detection.
- Prompt-of-wrong-role rejection (assistant only).
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

from wobblebot.adapters.openai import (
    OpenAIAdvisorAdapter,
    OpenAIAssistantAdapter,
    extract_openai_tokens,
    is_reasoning_model,
    parse_message_content,
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
from wobblebot.ports.operator import IntentCommand, IntentQuery, PauseCommand, StatusQuery
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


def _envelope(
    *,
    content: str,
    prompt_tokens: int = 200,
    completion_tokens: int = 100,
    reasoning_tokens: int | None = None,
    msg_id: str = "chatcmpl-abc",
    model: str = "gpt-4o",
) -> dict[str, object]:
    usage: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return {
        "id": msg_id,
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


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
    model: str = "gpt-4o",
    role: str = "quant",
    organization: str | None = None,
) -> OpenAIAdvisorAdapter:
    return OpenAIAdvisorAdapter(
        model=model,
        prompt=_quant_prompt(),
        role=role,  # type: ignore[arg-type]
        api_key="sk-test",
        organization=organization,
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
    model: str = "gpt-4o",
) -> OpenAIAssistantAdapter:
    return OpenAIAssistantAdapter(
        model=model,
        prompt=prompt or _operator_prompt(),
        api_key="sk-test",
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
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("o1", True),
            ("o1-preview", True),
            ("o3-mini", True),
            ("o3", True),
            ("o4-mini", True),  # Q2 fix: was misclassified False (priced in _PRICING, unmatched)
            ("o5", True),  # future o-series handled without a code change
            ("gpt-4o", False),
            ("gpt-4o-mini", False),
            ("gpt-3.5-turbo", False),
            ("gpt-5", True),  # 2026-06-03: reasoning-shape verified via OpenAI docs
            ("gpt-5-mini", True),  # gpt-5 family folded in
            ("gpt-5.5", True),  # priced in llm_pricing._PRICING — must classify as reasoning
            ("gpt-5.5-pro", True),  # priced; pre-fix would have been sent temperature → rejected
        ],
    )
    def test_is_reasoning_model(self, model: str, expected: bool) -> None:
        assert is_reasoning_model(model) is expected

    def test_estimate_cost_ceiling_gpt4o(self) -> None:
        # gpt-4o: $2.50/1M in, $10.00/1M out
        # 1000 chars / 4 = 250 input tokens; max_tokens=500
        # 250 * 2.5 / 1M + 500 * 10 / 1M = 0.000625 + 0.005 = 0.005625
        cost = estimate_cost_ceiling(
            provider="openai", model="gpt-4o", prompt_text="a" * 1000, max_tokens=500
        )
        assert cost == Decimal("0.005625")

    def test_extract_openai_tokens_no_reasoning(self) -> None:
        envelope = {
            "id": "x",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        }
        ti, to, tr, rid = extract_openai_tokens(envelope)
        assert ti == 100
        assert to == 50
        assert tr is None
        assert rid == "x"

    def test_extract_openai_tokens_with_reasoning(self) -> None:
        """o-series: reasoning is a subset of completion. Adapter
        subtracts to satisfy the additive convention."""
        envelope = {
            "id": "x",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 300,  # includes 200 reasoning
                "completion_tokens_details": {"reasoning_tokens": 200},
            },
        }
        ti, to, tr, _rid = extract_openai_tokens(envelope)
        assert ti == 100
        assert to == 100  # 300 - 200 = 100 visible
        assert tr == 200

    def test_extract_openai_tokens_zero_reasoning(self) -> None:
        envelope = {
            "id": "x",
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 75,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        _ti, to, tr, _rid = extract_openai_tokens(envelope)
        assert to == 75
        assert tr is None  # zero reasoning → None (no operator-visible signal)

    def test_extract_openai_tokens_empty_usage(self) -> None:
        envelope = {"id": "x"}
        ti, to, tr, _rid = extract_openai_tokens(envelope)
        assert ti == 0
        assert to == 0
        assert tr is None

    def test_parse_message_content_plain(self) -> None:
        envelope = _envelope(content="hello world")
        assert parse_message_content(envelope) == "hello world"

    def test_parse_message_content_empty_choices(self) -> None:
        envelope = {"id": "x", "choices": [], "usage": {}}
        assert parse_message_content(envelope) == ""

    def test_parse_message_content_multimodal_text_parts(self) -> None:
        envelope = {
            "id": "x",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Hello "},
                            {"type": "text", "text": "world"},
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        assert parse_message_content(envelope) == "Hello world"


# --------------------------------------------------------------------- #
# Wire shape                                                            #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestWireShape:
    async def test_bearer_auth_header_and_endpoint(self, storage: SQLiteStorageAdapter) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers["authorization"]
            captured["org"] = request.headers.get("openai-organization")
            return httpx.Response(
                200,
                json=_envelope(content=json.dumps(_valid_recommendation())),
            )

        adapter = _build_advisor(httpx.MockTransport(handler), storage, organization="org-test")
        await adapter.get_recommendation(_summary())
        assert captured["url"].endswith("/v1/chat/completions")  # type: ignore[union-attr]
        assert captured["auth"] == "Bearer sk-test"
        assert captured["org"] == "org-test"

    async def test_system_message_carries_prompt(self, storage: SQLiteStorageAdapter) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_envelope(content=json.dumps(_valid_recommendation())),
            )

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        await adapter.get_recommendation(_summary())
        body = captured["body"]
        assert isinstance(body, dict)
        messages = body["messages"]
        assert messages[0]["role"] == "system"
        assert "quant expert" in messages[0]["content"]
        assert messages[1]["role"] == "user"

    async def test_temperature_omitted_for_reasoning_models(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_envelope(
                    content=json.dumps(_valid_recommendation()),
                    model="o1",
                    reasoning_tokens=50,
                ),
            )

        adapter = _build_advisor(httpx.MockTransport(handler), storage, model="o1")
        await adapter.get_recommendation(_summary())
        body = captured["body"]
        assert isinstance(body, dict)
        assert "temperature" not in body  # o-series rejects it
        assert "max_completion_tokens" in body

    async def test_temperature_present_for_chat_models(self, storage: SQLiteStorageAdapter) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_envelope(content=json.dumps(_valid_recommendation())),
            )

        adapter = _build_advisor(httpx.MockTransport(handler), storage, model="gpt-4o")
        await adapter.get_recommendation(_summary())
        body = captured["body"]
        assert isinstance(body, dict)
        assert "temperature" in body


# --------------------------------------------------------------------- #
# Advisor: happy path + cost recording                                  #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAdvisorHappyPath:
    async def test_round_trip_records_cost(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _envelope(content=json.dumps(_valid_recommendation()))

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        tracker = SessionCostTracker()
        adapter = _build_advisor(httpx.MockTransport(handler), storage, tracker=tracker)
        rec = await adapter.get_recommendation(_summary())
        assert rec.confidence == "medium"
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].provider == "openai"
        assert rows[0].tokens_in == 200
        assert rows[0].tokens_out == 100
        # gpt-4o: $2.50/1M in, $10/1M out
        # 200 * 2.5/1M + 100 * 10/1M = 0.0005 + 0.001 = 0.0015
        assert rows[0].cost_usd == Decimal("0.001500")
        assert tracker.total == Decimal("0.001500")

    async def test_reasoning_tokens_recorded_separately(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # o1: $15/1M in, $60/1M out (reasoning falls back to output rate)
        envelope = _envelope(
            content=json.dumps(_valid_recommendation()),
            prompt_tokens=100,
            completion_tokens=300,  # includes 200 reasoning
            reasoning_tokens=200,
            model="o1",
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_advisor(httpx.MockTransport(handler), storage, model="o1")
        await adapter.get_recommendation(_summary())
        rows = await storage.get_llm_calls()
        assert rows[0].tokens_in == 100
        assert rows[0].tokens_out == 100  # 300 - 200
        assert rows[0].tokens_reasoning == 200
        # Cost: 100*15/1M + 100*60/1M + 200*60/1M = 0.0015 + 0.006 + 0.012 = 0.0195
        assert rows[0].cost_usd == Decimal("0.019500")

    async def test_prose_wrapping_json_handled(self, storage: SQLiteStorageAdapter) -> None:
        wrapped = (
            "I've analyzed the metrics. Here's my recommendation:\n\n"
            "```json\n" + json.dumps(_valid_recommendation()) + "\n```\n"
        )
        envelope = _envelope(content=wrapped)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        rec = await adapter.get_recommendation(_summary())
        assert rec.confidence == "medium"


# --------------------------------------------------------------------- #
# Advisor: failure paths                                                #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAdvisorFailures:
    async def test_http_401_wraps_as_advisor_error(self, storage: SQLiteStorageAdapter) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError, match="HTTP 401"):
            await adapter.get_recommendation(_summary())
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is False
        assert rows[0].error_kind == "http_401"

    async def test_empty_content_raises_advisor_error(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _envelope(content="")

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_advisor(httpx.MockTransport(handler), storage)
        with pytest.raises(AdvisorError, match="empty"):
            await adapter.get_recommendation(_summary())

    async def test_unpriced_model_wraps_as_advisor_error(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # An unpriced model must surface as AdvisorError, not a raw
        # PricingLookupError. The latter leaked past _run_cycle's
        # AdvisorError handler and crash-looped the advise daemon (a
        # stale image missing an o3 price entry). The estimate now runs
        # inside wrap_provider_errors, so the lookup miss is translated
        # before it escapes — and the HTTP call never fires.
        def handler(_r: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("HTTP call must not fire when pricing lookup fails")

        adapter = _build_advisor(
            httpx.MockTransport(handler), storage, model="gpt-nonexistent-unpriced"
        )
        with pytest.raises(AdvisorError, match="pricing unavailable"):
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
        envelope = _envelope(content=json.dumps(intent))

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_assistant(httpx.MockTransport(handler), storage)
        result = await adapter.parse_intent(_ctx())
        assert isinstance(result, IntentCommand)
        assert isinstance(result.command, PauseCommand)
        assert result.command.symbol == Symbol(base="BTC", quote="USD")
        rows = await storage.get_llm_calls()
        assert rows[0].role == "operator"
        assert rows[0].provider == "openai"

    async def test_query_intent_round_trip(self, storage: SQLiteStorageAdapter) -> None:
        intent = {"kind": "query", "query": {"kind": "status"}}
        envelope = _envelope(content=json.dumps(intent))

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_assistant(httpx.MockTransport(handler), storage)
        result = await adapter.parse_intent(_ctx(current="status?"))
        assert isinstance(result, IntentQuery)
        assert isinstance(result.query, StatusQuery)

    async def test_multi_turn_messages_in_order(self, storage: SQLiteStorageAdapter) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_envelope(content=json.dumps({"kind": "conversational", "reply_text": "ok"})),
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
        messages = captured["body"]["messages"]  # type: ignore[index]
        assert messages[0]["role"] == "system"
        assert messages[1] == {"role": "user", "content": "show fills"}
        assert messages[2] == {"role": "assistant", "content": "here are fills"}
        assert messages[3] == {"role": "user", "content": "filter to BTC"}

    async def test_non_operator_prompt_rejected(self) -> None:
        with pytest.raises(AssistantError, match="operator-role"):
            OpenAIAssistantAdapter(
                model="gpt-4o",
                prompt=_quant_prompt(),
                api_key="sk-test",
                storage=None,  # type: ignore[arg-type]
                session_tracker=SessionCostTracker(),
                cost_config=LLMCostConfig(),
                retry_config=LLMRetryConfig(),
            )

    async def test_empty_api_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            OpenAIAssistantAdapter(
                model="gpt-4o",
                prompt=_operator_prompt(),
                api_key="",
                storage=None,  # type: ignore[arg-type]
                session_tracker=SessionCostTracker(),
                cost_config=LLMCostConfig(),
                retry_config=LLMRetryConfig(),
            )

    async def test_cost_cap_trips_blocks_call(self, storage: SQLiteStorageAdapter) -> None:
        tracker = SessionCostTracker(initial=Decimal("0.495"))
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
        OpenAIAdvisorAdapter(
            model="gpt-4o",
            prompt=_quant_prompt(),
            role="quant",
            api_key="",
            storage=None,  # type: ignore[arg-type]
            session_tracker=SessionCostTracker(),
            cost_config=LLMCostConfig(),
            retry_config=LLMRetryConfig(),
        )

"""Unit tests for AnthropicAssistantAdapter (Stage 6.2.B).

Sister to ``tests/adapters/test_anthropic_advisor.py`` for the
operator-assistant role. HTTP layer mocked via ``httpx.MockTransport``;
storage is a real in-memory SQLite so the cost-tracking flow round-trips
through the actual schema.
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

from wobblebot.adapters.anthropic_assistant import AnthropicAssistantAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.prompts import Prompt, PromptMetadata
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.assistant import (
    ConversationContext,
    ConversationTurn,
    EngineStateSnapshot,
)
from wobblebot.ports.exceptions import AssistantError
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    IntentQuery,
    IntentUnparseable,
    PauseCommand,
    StatusQuery,
)
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
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


def _quant_prompt() -> Prompt:
    return Prompt(
        metadata=PromptMetadata(
            role="quant",
            description="Wrong role",
            response_schema="advisor_recommendation_v1",
            temperature_hint=0.5,
        ),
        body="quant prompt body",
        source_path=Path("config/prompts/quant.md"),
    )


def _snapshot(**overrides: object) -> EngineStateSnapshot:
    base: dict[str, object] = {
        "snapshot_at": Timestamp(dt=datetime.now(UTC)),
        "symbols": [],
        "total_usd_balance": 100.0,
        "session_pnl": 0.0,
        "session_runtime_seconds": 10.0,
    }
    base.update(overrides)
    return EngineStateSnapshot(**base)  # type: ignore[arg-type]


def _context(
    *,
    current: str = "pause BTC",
    turns: tuple[ConversationTurn, ...] = (),
    snapshot: EngineStateSnapshot | None = None,
) -> ConversationContext:
    return ConversationContext(
        current_message=current,
        channel_id="C-1",
        user_id="U-1",
        recent_turns=turns,
        engine_state_snapshot=snapshot or _snapshot(),
    )


def _anthropic_envelope(
    *,
    inner: dict[str, object] | str,
    tokens_in: int = 100,
    tokens_out: int = 50,
    msg_id: str = "msg_assist_1",
) -> dict[str, object]:
    if isinstance(inner, dict):
        text = json.dumps(inner)
    else:
        text = inner
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }


def _build_adapter(
    transport: httpx.MockTransport,
    storage: SQLiteStorageAdapter,
    *,
    cost_config: LLMCostConfig | None = None,
    retry_config: LLMRetryConfig | None = None,
    tracker: SessionCostTracker | None = None,
    prompt: Prompt | None = None,
) -> AnthropicAssistantAdapter:
    client = httpx.AsyncClient(transport=transport)
    return AnthropicAssistantAdapter(
        model="claude-sonnet-4-6",
        prompt=prompt or _operator_prompt(),
        api_key="sk-test",
        storage=storage,
        session_tracker=tracker or SessionCostTracker(),
        cost_config=cost_config or LLMCostConfig(),
        retry_config=retry_config or LLMRetryConfig(max_retries=2, initial_backoff_seconds=0.01),
        client=client,
    )


# --------------------------------------------------------------------- #
# Construction                                                          #
# --------------------------------------------------------------------- #


def test_non_operator_prompt_rejected(storage_path: Path | None = None) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))

    class _DummyStorage:
        pass

    with pytest.raises(AssistantError, match="operator-role"):
        AnthropicAssistantAdapter(
            model="claude-sonnet-4-6",
            prompt=_quant_prompt(),
            api_key="sk-test",
            storage=_DummyStorage(),  # type: ignore[arg-type]
            session_tracker=SessionCostTracker(),
            cost_config=LLMCostConfig(),
            retry_config=LLMRetryConfig(),
            client=client,
        )


def test_empty_api_key_rejected() -> None:
    with pytest.raises(ValueError, match="api_key"):
        AnthropicAssistantAdapter(
            model="claude-sonnet-4-6",
            prompt=_operator_prompt(),
            api_key="",
            storage=None,  # type: ignore[arg-type]
            session_tracker=SessionCostTracker(),
            cost_config=LLMCostConfig(),
            retry_config=LLMRetryConfig(),
        )


# --------------------------------------------------------------------- #
# Happy paths — every OperatorIntent variant round-trips                #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestIntentVariants:
    async def test_command_pause(self, storage: SQLiteStorageAdapter) -> None:
        inner = {
            "kind": "command",
            "command": {"kind": "pause", "symbol": {"base": "BTC", "quote": "USD"}},
        }
        envelope = _anthropic_envelope(inner=inner)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        intent = await adapter.parse_intent(_context(current="pause BTC"))
        assert isinstance(intent, IntentCommand)
        assert isinstance(intent.command, PauseCommand)
        assert intent.command.symbol == Symbol(base="BTC", quote="USD")

    async def test_query_status(self, storage: SQLiteStorageAdapter) -> None:
        inner = {"kind": "query", "query": {"kind": "status"}}
        envelope = _anthropic_envelope(inner=inner)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        intent = await adapter.parse_intent(_context(current="status?"))
        assert isinstance(intent, IntentQuery)
        assert isinstance(intent.query, StatusQuery)

    async def test_conversational(self, storage: SQLiteStorageAdapter) -> None:
        inner = {"kind": "conversational", "reply_text": "thanks!"}
        envelope = _anthropic_envelope(inner=inner)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        intent = await adapter.parse_intent(_context(current="thanks"))
        assert isinstance(intent, IntentConversational)
        assert intent.reply_text == "thanks!"

    async def test_unparseable(self, storage: SQLiteStorageAdapter) -> None:
        inner = {"kind": "unparseable", "reason": "couldn't tell"}
        envelope = _anthropic_envelope(inner=inner)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        intent = await adapter.parse_intent(_context(current="???"))
        assert isinstance(intent, IntentUnparseable)


# --------------------------------------------------------------------- #
# Wire shape verification                                               #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestWireShape:
    async def test_system_carries_prompt_and_snapshot(self, storage: SQLiteStorageAdapter) -> None:
        inner = {"kind": "conversational", "reply_text": "hi"}
        envelope = _anthropic_envelope(inner=inner)
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        await adapter.parse_intent(_context())
        body = captured["body"]
        assert isinstance(body, dict)
        system = body["system"]
        assert isinstance(system, str)
        assert "operator assistant" in system
        assert "Current engine state" in system

    async def test_messages_carry_history_in_order(self, storage: SQLiteStorageAdapter) -> None:
        inner = {"kind": "conversational", "reply_text": "hi"}
        envelope = _anthropic_envelope(inner=inner)
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=envelope)

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
        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        await adapter.parse_intent(_context(current="filter to BTC", turns=turns))
        body = captured["body"]
        assert isinstance(body, dict)
        messages = body["messages"]
        assert isinstance(messages, list)
        assert messages[0] == {"role": "user", "content": "show fills"}
        assert messages[1] == {"role": "assistant", "content": "here are fills"}
        assert messages[2] == {"role": "user", "content": "filter to BTC"}

    async def test_required_anthropic_headers(self, storage: SQLiteStorageAdapter) -> None:
        inner = {"kind": "conversational", "reply_text": "hi"}
        envelope = _anthropic_envelope(inner=inner)
        captured_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update({k: v for k, v in request.headers.items()})
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        await adapter.parse_intent(_context())
        assert captured_headers["x-api-key"] == "sk-test"
        assert captured_headers["anthropic-version"] == "2023-06-01"


# --------------------------------------------------------------------- #
# Cost-tracking flow                                                    #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestCostTracking:
    async def test_records_cost_against_operator_role(self, storage: SQLiteStorageAdapter) -> None:
        inner = {"kind": "conversational", "reply_text": "ok"}
        envelope = _anthropic_envelope(inner=inner, tokens_in=200, tokens_out=80)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        tracker = SessionCostTracker()
        adapter = _build_adapter(httpx.MockTransport(handler), storage, tracker=tracker)
        await adapter.parse_intent(_context())
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].role == "operator"
        # 200 * 3/1M + 80 * 15/1M = 0.0006 + 0.0012 = 0.0018
        assert rows[0].cost_usd == Decimal("0.001800")
        assert tracker.total == Decimal("0.001800")

    async def test_cost_cap_trips_blocks_call(self, storage: SQLiteStorageAdapter) -> None:
        tracker = SessionCostTracker(initial=Decimal("0.495"))
        adapter = _build_adapter(
            httpx.MockTransport(lambda _r: httpx.Response(500)),
            storage,
            tracker=tracker,
        )
        with pytest.raises(LLMCostCapExceeded):
            await adapter.parse_intent(_context())

    async def test_dry_run_posture_records_but_does_not_deny(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        inner = {"kind": "conversational", "reply_text": "ok"}
        envelope = _anthropic_envelope(inner=inner)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(
            httpx.MockTransport(handler),
            storage,
            cost_config=LLMCostConfig(
                max_spend_per_day_usd=Decimal("0.01"),
                max_spend_per_session_usd=Decimal("0.01"),
                enforce=False,
            ),
            tracker=SessionCostTracker(initial=Decimal("999")),
        )
        await adapter.parse_intent(_context())  # no raise
        rows = await storage.get_llm_calls()
        assert len(rows) == 1


# --------------------------------------------------------------------- #
# Retry + failure                                                       #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestRetryAndFailure:
    async def test_succeeds_after_transient_5xx(self, storage: SQLiteStorageAdapter) -> None:
        envelope = _anthropic_envelope(inner={"kind": "conversational", "reply_text": "ok"})
        responses = [
            httpx.Response(503),
            httpx.Response(200, json=envelope),
        ]
        call_count = [0]

        def handler(_r: httpx.Request) -> httpx.Response:
            r = responses[call_count[0]]
            call_count[0] += 1
            return r

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        intent = await adapter.parse_intent(_context())
        assert isinstance(intent, IntentConversational)
        assert call_count[0] == 2

    async def test_permanent_400_records_failure_with_error_kind(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad_request"})

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        with pytest.raises(AssistantError, match="HTTP 400"):
            await adapter.parse_intent(_context())
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is False
        assert rows[0].error_kind == "http_400"


# --------------------------------------------------------------------- #
# Parse failures                                                        #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestParseFailures:
    async def test_invalid_intent_schema(self, storage: SQLiteStorageAdapter) -> None:
        # `kind` missing → discriminator can't resolve.
        inner = {"reply_text": "hello"}
        envelope = _anthropic_envelope(inner=inner)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        with pytest.raises(AssistantError, match="schema validation"):
            await adapter.parse_intent(_context())
        # Cost record DID persist — API call succeeded; parse failed after.
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is True

    async def test_empty_content(self, storage: SQLiteStorageAdapter) -> None:
        envelope = {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 0},
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        with pytest.raises(AssistantError, match="empty"):
            await adapter.parse_intent(_context())

    async def test_prose_wrapping_json(self, storage: SQLiteStorageAdapter) -> None:
        """Anthropic likes to add explanatory prose. Walk for the final JSON."""
        wrapped_text = (
            "Looking at the engine state, the operator is asking about status.\n\n"
            "I should respond with: ```json\n"
            + json.dumps({"kind": "query", "query": {"kind": "status"}})
            + "\n```"
        )
        envelope = _anthropic_envelope(inner=wrapped_text)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=envelope)

        adapter = _build_adapter(httpx.MockTransport(handler), storage)
        intent = await adapter.parse_intent(_context(current="status?"))
        assert isinstance(intent, IntentQuery)

"""Unit tests for OllamaAssistantAdapter (Stage 5.3).

Sister to ``tests/adapters/test_ollama.py`` (advisor). HTTP layer is
mocked via ``httpx.MockTransport`` so tests stay deterministic and
never touch a real Ollama server.

Covers:

- Happy paths for each ``OperatorIntent`` variant (command, query,
  conversational, unparseable).
- Multi-turn conversation context propagates as role-tagged messages.
- Engine state snapshot embeds in the system message.
- Thinking-mode handling: drops ``format: json`` constraint, walks
  free-text body via the shared ``extract_last_json_object`` helper.
- Split-response envelope (newer Ollama versions surface the answer
  on a separate ``thinking`` field while ``message.content`` is empty).
- Constructor refuses non-operator prompts.
- Error paths: HTTP 5xx, malformed envelope, validation failure.
- ``aclose`` lifecycle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from wobblebot.adapters.ollama_assistant import OllamaAssistantAdapter
from wobblebot.config.prompts import Prompt, PromptMetadata
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.assistant import (
    ConversationContext,
    ConversationTurn,
    EngineStateSnapshot,
    SymbolStateSnapshot,
)
from wobblebot.ports.exceptions import AssistantError
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    IntentQuery,
    IntentUnparseable,
    PauseCommand,
    RecentFillsQuery,
    StatusQuery,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- #
# Builders                                                              #
# --------------------------------------------------------------------- #


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
            description="Test quant prompt (wrong role for assistant)",
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


def _chat_envelope(content: str, *, thinking: str | None = None) -> dict[str, object]:
    """Build an Ollama /api/chat response envelope."""
    env: dict[str, object] = {
        "model": "test-model",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }
    if thinking is not None:
        env["thinking"] = thinking
    return env


def _build_adapter(
    transport: httpx.MockTransport,
    *,
    model: str = "test-model",
    prompt: Prompt | None = None,
) -> OllamaAssistantAdapter:
    client = httpx.AsyncClient(transport=transport)
    return OllamaAssistantAdapter(
        model=model,
        prompt=prompt or _operator_prompt(),
        client=client,
    )


# --------------------------------------------------------------------- #
# Constructor                                                           #
# --------------------------------------------------------------------- #


class TestConstructor:
    def test_requires_operator_role_prompt(self) -> None:
        with pytest.raises(AssistantError, match="requires an operator-role prompt"):
            OllamaAssistantAdapter(model="x", prompt=_quant_prompt())

    def test_rejects_phi4_mini_reasoning(self) -> None:
        # Empirically 0/14 on the 2026-05-24 routing battery -- math
        # specialist, never emits JSON. Hard-blocked.
        with pytest.raises(AssistantError, match="known-incompatible"):
            OllamaAssistantAdapter(model="phi4-mini-reasoning:3.8b-fp16", prompt=_operator_prompt())

    def test_rejects_llava(self) -> None:
        # Vision model; not text-instruct-tuned for JSON-schema output.
        with pytest.raises(AssistantError, match="known-incompatible"):
            OllamaAssistantAdapter(model="llava:13b", prompt=_operator_prompt())

    def test_incompatible_error_lists_alternatives(self) -> None:
        with pytest.raises(AssistantError) as exc_info:
            OllamaAssistantAdapter(model="phi4-mini-reasoning:3.8b", prompt=_operator_prompt())
        assert "phi4:14b-q8_0" in str(exc_info.value)
        assert "mistral-nemo" in str(exc_info.value)

    def test_qwen36_warns_but_allows(self, caplog: pytest.LogCaptureFixture) -> None:
        # 11/14 with 3 empty-content errors -- soft-warn, not block.
        with caplog.at_level("WARNING", logger="wobblebot.adapters.ollama_assistant"):
            OllamaAssistantAdapter(model="qwen3.6:35b-a3b-q8_0", prompt=_operator_prompt())
        assert any("known-degraded" in r.message for r in caplog.records)

    def test_compatible_model_passes_silently(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="wobblebot.adapters.ollama_assistant"):
            OllamaAssistantAdapter(model="phi4:14b-q8_0", prompt=_operator_prompt())
        assert not any("known-degraded" in r.message for r in caplog.records)
        assert not any("known-incompatible" in r.message for r in caplog.records)

    def test_case_insensitive_match(self) -> None:
        # The model tag case shouldn't matter.
        with pytest.raises(AssistantError, match="known-incompatible"):
            OllamaAssistantAdapter(model="PHI4-MINI-Reasoning:3.8b", prompt=_operator_prompt())

    def test_thinking_model_auto_bumps_low_max_tokens(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # phi4-reasoning matches the "reasoning" thinking pattern.
        with caplog.at_level("INFO", logger="wobblebot.adapters.ollama_assistant"):
            adapter = OllamaAssistantAdapter(
                model="phi4-reasoning:14b-plus-q8_0",
                prompt=_operator_prompt(),
                max_tokens=512,
            )
        assert adapter._max_tokens == 4096  # noqa: SLF001  # pylint: disable=protected-access
        assert any("raising max_tokens" in r.message for r in caplog.records)

    def test_thinking_model_honors_high_max_tokens(self) -> None:
        # Operator already set a generous value -- adapter must not lower it.
        adapter = OllamaAssistantAdapter(
            model="phi4-reasoning:14b-plus-q8_0",
            prompt=_operator_prompt(),
            max_tokens=8192,
        )
        assert adapter._max_tokens == 8192  # noqa: SLF001  # pylint: disable=protected-access

    def test_non_thinking_model_keeps_configured_max_tokens(self) -> None:
        # phi4 (no "reasoning"/"thinking" substring) keeps the configured 512.
        adapter = OllamaAssistantAdapter(
            model="phi4:14b-q8_0",
            prompt=_operator_prompt(),
            max_tokens=512,
        )
        assert adapter._max_tokens == 512  # noqa: SLF001  # pylint: disable=protected-access


# --------------------------------------------------------------------- #
# Happy paths — each intent variant                                     #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestParseIntentHappyPaths:
    async def test_command_pause_btc(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_chat_envelope(
                    json.dumps(
                        {
                            "kind": "command",
                            "command": {"kind": "pause", "symbol": "BTC/USD"},
                        }
                    )
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            intent = await adapter.parse_intent(_context(current="pause BTC"))
        finally:
            await adapter.aclose()

        assert isinstance(intent, IntentCommand)
        assert isinstance(intent.command, PauseCommand)
        assert intent.command.symbol == Symbol(base="BTC", quote="USD")
        # Endpoint + payload shape
        assert captured["url"] == "http://localhost:11434/api/chat"
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["model"] == "test-model"
        assert body["format"] == "json"
        assert body["stream"] is False
        # System + 1 user message (no recent turns)
        msgs = body["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert "operator assistant" in msgs[0]["content"]
        assert "Current engine state" in msgs[0]["content"]
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "pause BTC"

    async def test_query_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_envelope(json.dumps({"kind": "query", "query": {"kind": "status"}})),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            intent = await adapter.parse_intent(_context(current="how's it going?"))
        finally:
            await adapter.aclose()
        assert isinstance(intent, IntentQuery)
        assert isinstance(intent.query, StatusQuery)

    async def test_query_with_args(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_envelope(
                    json.dumps(
                        {
                            "kind": "query",
                            "query": {
                                "kind": "recent_fills",
                                "symbol": "ETH/USD",
                                "lookback_hours": 6,
                            },
                        }
                    )
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            intent = await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()
        assert isinstance(intent, IntentQuery)
        assert isinstance(intent.query, RecentFillsQuery)
        assert intent.query.lookback_hours == 6
        assert intent.query.symbol is not None
        assert intent.query.symbol.base == "ETH"

    async def test_conversational(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_envelope(
                    json.dumps({"kind": "conversational", "reply_text": "you're welcome"})
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            intent = await adapter.parse_intent(_context(current="thanks"))
        finally:
            await adapter.aclose()
        assert isinstance(intent, IntentConversational)
        assert intent.reply_text == "you're welcome"

    async def test_unparseable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_envelope(
                    json.dumps({"kind": "unparseable", "reason": "no symbol named XYZ"})
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            intent = await adapter.parse_intent(_context(current="wibble"))
        finally:
            await adapter.aclose()
        assert isinstance(intent, IntentUnparseable)
        assert intent.reason == "no symbol named XYZ"


# --------------------------------------------------------------------- #
# Conversation context handling                                         #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestConversationContext:
    async def test_recent_turns_become_role_tagged_messages(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_chat_envelope(json.dumps({"kind": "conversational", "reply_text": "ok"})),
            )

        ts = Timestamp(dt=datetime.now(UTC))
        op_turn = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-1",
            role="operator",
            content="show me yesterday's fills",
            timestamp=ts,
        )
        bot_turn = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-bot",
            role="assistant",
            content="Here are 5 fills...",
            timestamp=ts,
        )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            await adapter.parse_intent(
                _context(current="now filter to ETH", turns=(op_turn, bot_turn))
            )
        finally:
            await adapter.aclose()

        body = captured["body"]
        assert isinstance(body, dict)
        msgs = body["messages"]
        # system + operator turn + assistant turn + current = 4
        assert len(msgs) == 4
        assert msgs[0]["role"] == "system"
        assert msgs[1] == {"role": "user", "content": "show me yesterday's fills"}
        assert msgs[2] == {"role": "assistant", "content": "Here are 5 fills..."}
        assert msgs[3] == {"role": "user", "content": "now filter to ETH"}

    async def test_engine_state_snapshot_in_system_message(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_chat_envelope(json.dumps({"kind": "conversational", "reply_text": "ok"})),
            )

        snap = _snapshot(
            symbols=[SymbolStateSnapshot(symbol="BTC/USD", state="active", open_order_count=6)],
            total_usd_balance=123.45,
            session_pnl=0.04,
        )
        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            await adapter.parse_intent(_context(snapshot=snap))
        finally:
            await adapter.aclose()

        body = captured["body"]
        assert isinstance(body, dict)
        system = body["messages"][0]["content"]
        assert isinstance(system, str)
        assert "BTC/USD" in system
        assert "123.45" in system
        assert "open_order_count" in system


# --------------------------------------------------------------------- #
# Thinking-mode + split-response envelope                               #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestThinkingMode:
    async def test_thinking_model_drops_format_json(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            # Thinking model emits CoT + JSON in message.content
            reply = (
                "<think>I should pause BTC since the user said so.</think>\n\n"
                "Here is my answer:\n"
                + json.dumps(
                    {
                        "kind": "command",
                        "command": {"kind": "pause", "symbol": "BTC/USD"},
                    }
                )
            )
            return httpx.Response(200, json=_chat_envelope(reply))

        adapter = _build_adapter(httpx.MockTransport(handler), model="deepseek-r1:14b")
        try:
            intent = await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

        assert isinstance(intent, IntentCommand)
        body = captured["body"]
        assert isinstance(body, dict)
        # Thinking models drop the format=json constraint
        assert "format" not in body

    async def test_force_json_on_thinking_model_sends_format_json(self) -> None:
        """The 2026-05-25 escape hatch: force_json=True overrides
        is_thinking_model so probe tools can re-evaluate whether a
        candidate's thinking-name pattern is still load-bearing."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_chat_envelope(
                    json.dumps(
                        {
                            "kind": "command",
                            "command": {"kind": "pause", "symbol": "BTC/USD"},
                        }
                    )
                ),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = OllamaAssistantAdapter(
            model="deepseek-r1:14b",
            prompt=_operator_prompt(),
            client=client,
            force_json=True,
        )
        try:
            intent = await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

        assert isinstance(intent, IntentCommand)
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["format"] == "json", (
            "force_json=True must override is_thinking_model and add format=json"
        )

    async def test_force_json_default_preserves_thinking_behavior(self) -> None:
        """Regression guard: default force_json=False keeps the
        thinking-model free-text extraction path. The production
        cli/operator path must not change behavior."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            reply = "<think>thinking</think>\n" + json.dumps(
                {
                    "kind": "command",
                    "command": {"kind": "pause", "symbol": "BTC/USD"},
                }
            )
            return httpx.Response(200, json=_chat_envelope(reply))

        adapter = _build_adapter(httpx.MockTransport(handler), model="deepseek-r1:14b")
        try:
            await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

        body = captured["body"]
        assert isinstance(body, dict)
        assert "format" not in body, "default force_json=False must keep existing behavior"

    async def test_split_response_envelope(self) -> None:
        # Some Ollama versions surface the answer in 'thinking' with
        # empty message.content even for non-R1 models. Adapter combines
        # both and extracts the final JSON object.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_envelope(
                    "",
                    thinking=json.dumps(
                        {
                            "kind": "command",
                            "command": {"kind": "pause", "symbol": "BTC/USD"},
                        }
                    ),
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            intent = await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()
        assert isinstance(intent, IntentCommand)


# --------------------------------------------------------------------- #
# Error paths                                                           #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestErrorPaths:
    async def test_http_5xx_wraps_as_assistant_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "model exploded"})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AssistantError, match="chat request failed"):
                await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

    async def test_envelope_missing_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"model": "x", "done": True})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AssistantError, match="missing 'message' object"):
                await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

    async def test_empty_content_and_no_thinking_after_retry(self) -> None:
        # Both attempts return empty -- expected to surface as AssistantError.
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json=_chat_envelope(""))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AssistantError, match="empty 'message.content'"):
                await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()
        # Confirms a retry actually fired (2 POSTs, not 1).
        assert call_count == 2

    async def test_empty_content_retry_succeeds(self, caplog: pytest.LogCaptureFixture) -> None:
        # First response is empty; second succeeds. Operator never sees an
        # error -- the transient hiccup is invisible end-to-end.
        call_count = 0
        intent_payload = {
            "kind": "query",
            "query": {"kind": "status"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json=_chat_envelope(""))
            return httpx.Response(200, json=_chat_envelope(json.dumps(intent_payload)))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with caplog.at_level("WARNING", logger="wobblebot.adapters.ollama_assistant"):
                intent = await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()
        assert isinstance(intent, IntentQuery)
        assert call_count == 2
        assert any("retrying once" in r.message for r in caplog.records)

    async def test_invalid_json_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_envelope("not json at all"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AssistantError, match="not valid JSON"):
                await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

    async def test_top_level_non_object_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_envelope("[1, 2, 3]"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AssistantError, match="expected object"):
                await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

    async def test_validation_failure_wraps(self) -> None:
        # Valid JSON, but doesn't satisfy OperatorIntent discriminator.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_envelope(json.dumps({"kind": "telepathy", "thought": "..."})),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AssistantError, match="operator_intent_v1 schema"):
                await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()

    async def test_thinking_mode_no_json_in_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_envelope("I cannot help with this request."),
            )

        adapter = _build_adapter(httpx.MockTransport(handler), model="deepseek-r1:7b")
        try:
            with pytest.raises(AssistantError, match="no parseable JSON object"):
                await adapter.parse_intent(_context())
        finally:
            await adapter.aclose()


# --------------------------------------------------------------------- #
# Lifecycle                                                             #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestLifecycle:
    async def test_aclose_owned_client(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_envelope("{}"))

        adapter = OllamaAssistantAdapter(
            model="x",
            prompt=_operator_prompt(),
            client=None,  # adapter owns the client
        )
        await adapter.aclose()  # should not raise

    async def test_aclose_borrowed_client_left_open(self) -> None:
        # When the caller passes in a client, the adapter does NOT close it.
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=_chat_envelope("{}")))
        client = httpx.AsyncClient(transport=transport)
        adapter = OllamaAssistantAdapter(model="x", prompt=_operator_prompt(), client=client)
        await adapter.aclose()
        assert not client.is_closed
        await client.aclose()

    async def test_warmup_fires_generate_endpoint(self, caplog: pytest.LogCaptureFixture) -> None:
        # Warmup should POST /api/generate with an empty prompt + tiny
        # num_predict, NOT /api/chat. The mocktransport asserts the URL.
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            return httpx.Response(200, json={"model": "test-model", "response": "", "done": True})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with caplog.at_level("INFO", logger="wobblebot.adapters.ollama_assistant"):
                await adapter.warmup()
        finally:
            await adapter.aclose()
        assert seen_paths == ["/api/generate"]
        assert any("warmed up" in r.message for r in caplog.records)

    async def test_warmup_swallows_http_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        # Ollama down at startup should NOT crash cli/operator -- the
        # first real parse_intent retries the model load.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "model not loaded"})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with caplog.at_level("WARNING", logger="wobblebot.adapters.ollama_assistant"):
                await adapter.warmup()  # must NOT raise
        finally:
            await adapter.aclose()
        assert any("warmup" in r.message and "failed" in r.message for r in caplog.records)

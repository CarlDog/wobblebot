"""Tests for ``services/llm_cloud_call.py`` (Stage 6.3.A).

The shared ADR-014/015 flow gets a dedicated test surface because:
  - Per-adapter tests (test_anthropic_*) exercise it end-to-end via
    integration paths, but unit-isolation tests here pin the
    contract independent of any specific provider's HTTP shape.
  - Stage 6.3.B (OpenAI) + Stage 6.4 (Google) consume the same
    helper; a stable test suite here documents the invariants those
    adapters can rely on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.exceptions import LLMCostCapExceeded, LLMRetryExhausted
from wobblebot.services.llm_cloud_call import (
    CloudCallContext,
    TokenUsage,
    classify_error,
    execute_cloud_call,
)
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_retry import LLMRetryConfig
from wobblebot.services.llm_trace import llm_trace

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


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.invalid/v1/test")
    response = httpx.Response(status_code=code, request=request)
    return httpx.HTTPStatusError(message=f"HTTP {code}", request=request, response=response)


def _ctx(
    storage: SQLiteStorageAdapter,
    *,
    tracker: SessionCostTracker | None = None,
    cost_config: LLMCostConfig | None = None,
    retry_config: LLMRetryConfig | None = None,
    model: str = "claude-sonnet-4-6",
    role: str = "quant",
) -> CloudCallContext:
    return CloudCallContext(
        storage=storage,
        session_tracker=tracker or SessionCostTracker(),
        cost_config=cost_config or LLMCostConfig(),
        retry_config=retry_config or LLMRetryConfig(max_retries=2, initial_backoff_seconds=0.01),
        role=role,  # type: ignore[arg-type]
        provider="anthropic",
        model=model,
    )


def _simple_extract(envelope: dict[str, Any]) -> TokenUsage:
    """Generic extractor used by tests that don't care about provider shape."""
    usage = envelope.get("usage", {})
    return TokenUsage(
        tokens_in=int(usage.get("input_tokens", 0)),
        tokens_out=int(usage.get("output_tokens", 0)),
        tokens_reasoning=usage.get("reasoning"),
        tokens_cache_read=int(usage.get("cache_read", 0)),
        tokens_cache_write=int(usage.get("cache_write", 0)),
        request_id=envelope.get("id"),
    )


# --------------------------------------------------------------------- #
# classify_error                                                        #
# --------------------------------------------------------------------- #


class TestClassifyError:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (429, "rate_limited"),
            (500, "server_error"),
            (502, "server_error"),
            (503, "server_error"),
            (599, "server_error"),
            (400, "http_400"),
            (401, "http_401"),
            (404, "http_404"),
            (422, "http_422"),
        ],
    )
    def test_http_status(self, code: int, expected: str) -> None:
        assert classify_error(_http_status_error(code)) == expected

    def test_connect_error(self) -> None:
        assert classify_error(httpx.ConnectError("dns")) == "connect_error"

    def test_connect_timeout(self) -> None:
        assert classify_error(httpx.ConnectTimeout("t")) == "connect_error"

    def test_read_timeout(self) -> None:
        assert classify_error(httpx.ReadTimeout("t")) == "timeout"

    def test_pool_timeout(self) -> None:
        assert classify_error(httpx.PoolTimeout("t")) == "timeout"

    def test_other_exception_uses_type_name(self) -> None:
        assert classify_error(ValueError("x")) == "ValueError"


# --------------------------------------------------------------------- #
# Happy path                                                            #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestExecuteCloudCall:
    async def test_success_persists_record_and_returns_envelope(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        envelope = {
            "id": "msg_x",
            "usage": {"input_tokens": 100, "output_tokens": 200},
            "extra": "data",
        }

        async def call_fn() -> dict[str, Any]:
            return envelope

        tracker = SessionCostTracker()
        result = await execute_cloud_call(
            ctx=_ctx(storage, tracker=tracker),
            estimated_cost_usd=Decimal("0.005"),
            call_fn=call_fn,
            extract_tokens=_simple_extract,
        )
        assert result is envelope  # passes through unchanged
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        rec = rows[0]
        assert rec.tokens_in == 100
        assert rec.tokens_out == 200
        assert rec.success is True
        # 100 * 3 / 1M + 200 * 15 / 1M = 0.0033
        assert rec.cost_usd == Decimal("0.003300")
        # Tracker reflects real (not estimated) cost.
        assert tracker.total == Decimal("0.003300")

    async def test_extractor_can_return_reasoning_tokens(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Stage 6.3.B (OpenAI) exercises this path — reasoning column
        populated when the extractor returns a non-None value."""
        envelope = {
            "id": "x",
            "usage": {"input_tokens": 50, "output_tokens": 30, "reasoning": 500},
        }

        async def call_fn() -> dict[str, Any]:
            return envelope

        await execute_cloud_call(
            ctx=_ctx(storage),
            estimated_cost_usd=Decimal("0.005"),
            call_fn=call_fn,
            extract_tokens=_simple_extract,
        )
        rows = await storage.get_llm_calls()
        assert rows[0].tokens_reasoning == 500

    async def test_cache_counts_persist_and_discount_cost(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """ADR-033: cache buckets flow extractor → record → ledger, and
        cache-read tokens bill at the entry's cached rate, not full
        input rate."""
        envelope = {
            "id": "msg_cached",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read": 1000,
                "cache_write": 400,
            },
        }

        async def call_fn() -> dict[str, Any]:
            return envelope

        await execute_cloud_call(
            ctx=_ctx(storage),  # anthropic / claude-sonnet-4-6
            estimated_cost_usd=Decimal("0.005"),
            call_fn=call_fn,
            extract_tokens=_simple_extract,
        )
        rec = (await storage.get_llm_calls())[0]
        assert rec.tokens_cache_read == 1000
        assert rec.tokens_cache_write == 400
        # sonnet-4-6: in 100*$3 + out 200*$15 + read 1000*$0.30
        #             + write 400*$3.75 (all /1M)
        # = 0.0003 + 0.003 + 0.0003 + 0.0015 = 0.0051
        assert rec.cost_usd == Decimal("0.005100")
        # Cheaper than the same tokens uncached (1100 in * $3 = 0.0033
        # + 0.003 out + 0.0015 write-at-input... the direct comparison:
        # billing 1000 cached at full input rate would add 0.0030 not
        # 0.0003).
        assert rec.cost_usd < Decimal("0.007800")


# --------------------------------------------------------------------- #
# Cost gate                                                             #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestCostGate:
    async def test_daily_cap_raises_before_call(self, storage: SQLiteStorageAdapter) -> None:
        # Seed history to trip the daily cap.
        from datetime import UTC, datetime, timedelta

        from wobblebot.domain.llm_cost import LLMCallRecord
        from wobblebot.domain.value_objects import Timestamp

        for i in range(5):
            await storage.save_llm_call(
                LLMCallRecord(
                    timestamp=Timestamp(dt=datetime.now(UTC) - timedelta(minutes=i)),
                    role="quant",
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    tokens_in=10,
                    tokens_out=10,
                    cost_usd=Decimal("0.20"),
                    success=True,
                )
            )
        call_count = [0]

        async def call_fn() -> dict[str, Any]:
            call_count[0] += 1
            return {"usage": {"input_tokens": 0, "output_tokens": 0}}

        with pytest.raises(LLMCostCapExceeded) as exc_info:
            await execute_cloud_call(
                ctx=_ctx(storage),
                estimated_cost_usd=Decimal("0.01"),
                call_fn=call_fn,
                extract_tokens=_simple_extract,
            )
        assert exc_info.value.cap_kind == "daily"
        assert call_count[0] == 0  # never even tried

    async def test_session_cap_raises_before_call(self, storage: SQLiteStorageAdapter) -> None:
        tracker = SessionCostTracker(initial=Decimal("0.495"))

        async def call_fn() -> dict[str, Any]:
            return {"usage": {"input_tokens": 0, "output_tokens": 0}}

        with pytest.raises(LLMCostCapExceeded) as exc_info:
            await execute_cloud_call(
                ctx=_ctx(storage, tracker=tracker),
                estimated_cost_usd=Decimal("0.01"),
                call_fn=call_fn,
                extract_tokens=_simple_extract,
            )
        assert exc_info.value.cap_kind == "session"


# --------------------------------------------------------------------- #
# Trace stamping (P4.4a)                                                #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestTraceStamping:
    async def test_success_record_carries_ambient_trace(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        async def call_fn() -> dict[str, Any]:
            return {"id": "x", "usage": {"input_tokens": 1, "output_tokens": 1}}

        with llm_trace("cycle-42"):
            await execute_cloud_call(
                ctx=_ctx(storage),
                estimated_cost_usd=Decimal("0.005"),
                call_fn=call_fn,
                extract_tokens=_simple_extract,
            )
        assert (await storage.get_llm_calls())[0].trace_id == "cycle-42"

    async def test_failure_record_carries_ambient_trace(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        async def call_fn() -> dict[str, Any]:
            raise _http_status_error(401)

        with pytest.raises(httpx.HTTPStatusError):
            with llm_trace("cycle-43"):
                await execute_cloud_call(
                    ctx=_ctx(storage),
                    estimated_cost_usd=Decimal("0.01"),
                    call_fn=call_fn,
                    extract_tokens=_simple_extract,
                )
        row = (await storage.get_llm_calls())[0]
        assert row.success is False
        assert row.trace_id == "cycle-43"

    async def test_no_scope_records_null_trace(self, storage: SQLiteStorageAdapter) -> None:
        """Callers outside any scope (cli/operator today) keep the
        pre-P4.4a behavior exactly: an untraced row."""

        async def call_fn() -> dict[str, Any]:
            return {"id": "x", "usage": {"input_tokens": 1, "output_tokens": 1}}

        await execute_cloud_call(
            ctx=_ctx(storage),
            estimated_cost_usd=Decimal("0.005"),
            call_fn=call_fn,
            extract_tokens=_simple_extract,
        )
        assert (await storage.get_llm_calls())[0].trace_id is None


# --------------------------------------------------------------------- #
# Failure path: classify, record, re-raise                              #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestFailurePath:
    async def test_permanent_4xx_records_failure_and_reraises(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exc = _http_status_error(401)

        async def call_fn() -> dict[str, Any]:
            raise exc

        with pytest.raises(httpx.HTTPStatusError):
            await execute_cloud_call(
                ctx=_ctx(storage),
                estimated_cost_usd=Decimal("0.01"),
                call_fn=call_fn,
                extract_tokens=_simple_extract,
            )
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is False
        assert rows[0].error_kind == "http_401"
        assert rows[0].cost_usd == Decimal("0")

    async def test_transient_exhaustion_records_failure_and_reraises(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        async def call_fn() -> dict[str, Any]:
            raise _http_status_error(503)

        with pytest.raises(LLMRetryExhausted):
            await execute_cloud_call(
                ctx=_ctx(storage),
                estimated_cost_usd=Decimal("0.01"),
                call_fn=call_fn,
                extract_tokens=_simple_extract,
            )
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        assert rows[0].success is False
        # LLMRetryExhausted is the outermost; classify_error sees that
        # rather than the inner 503.
        assert rows[0].error_kind == "LLMRetryExhausted"

    async def test_connect_error_records_connect_error_kind(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        async def call_fn() -> dict[str, Any]:
            raise httpx.ConnectError("dns lookup failed")

        with pytest.raises(LLMRetryExhausted):
            await execute_cloud_call(
                ctx=_ctx(storage),
                estimated_cost_usd=Decimal("0.01"),
                call_fn=call_fn,
                extract_tokens=_simple_extract,
            )
        rows = await storage.get_llm_calls()
        # All retries exhausted with connect error → recorded once at exhaustion.
        assert rows[-1].success is False
        assert rows[-1].error_kind == "LLMRetryExhausted"


class TestBuildAdvisorRecommendation:
    """The construction tail shared by the cloud path and adapters/ollama.

    Extracted 2026-09-04 (audit finding 14) from a byte-identical copy the
    Ollama adapter carried, error strings included. These tests pin the
    contract ONCE for both call sites.

    They exist because mutation testing found the cloud side had never
    pinned it: breaking the missing-field message turned the Ollama adapter
    tests red and left this module's green. Before the extraction that gap
    was invisible — each copy was only as tested as its own caller.
    """

    def test_a_missing_field_names_the_field_and_the_keys_it_did_get(self) -> None:
        """Both halves are operator-facing. The field name says what the
        model omitted; the key list says what it sent instead, which is how
        you tell a schema drift from a truncated response."""
        from wobblebot.ports.exceptions import AdvisorError
        from wobblebot.services.llm_cloud_call import build_advisor_recommendation

        with pytest.raises(AdvisorError) as exc:
            build_advisor_recommendation(
                {"role": "quant", "recommendations": {}, "rationale": "r"},
                fallback_role="quant",
            )
        message = str(exc.value)
        assert "'confidence'" in message, "the missing field must be named"
        assert "rationale" in message and "role" in message, "the keys it DID send must be listed"

    def test_schema_violations_are_reported_as_schema_violations(self) -> None:
        """A present-but-invalid field is a different diagnosis from a
        missing one, and the message must not conflate them."""
        from wobblebot.ports.exceptions import AdvisorError
        from wobblebot.services.llm_cloud_call import build_advisor_recommendation

        with pytest.raises(AdvisorError, match="schema validation"):
            build_advisor_recommendation(
                {"recommendations": {}, "rationale": "r", "confidence": "not-a-number"},
                fallback_role="quant",
            )

    def test_the_fallback_role_is_used_only_when_the_model_omits_one(self) -> None:
        from wobblebot.services.llm_cloud_call import build_advisor_recommendation

        supplied = build_advisor_recommendation(
            {"role": "risk", "recommendations": {}, "rationale": "r", "confidence": "medium"},
            fallback_role="quant",
        )
        assert supplied.role == "risk"

        omitted = build_advisor_recommendation(
            {"recommendations": {}, "rationale": "r", "confidence": "medium"},
            fallback_role="quant",
        )
        assert omitted.role == "quant"

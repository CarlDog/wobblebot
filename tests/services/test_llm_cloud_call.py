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
    classify_error,
    execute_cloud_call,
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


def _simple_extract(envelope: dict[str, Any]) -> tuple[int, int, int | None, str | None]:
    """Generic extractor used by tests that don't care about provider shape."""
    usage = envelope.get("usage", {})
    return (
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        usage.get("reasoning"),
        envelope.get("id"),
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

"""Tests for the ``llm_calls`` adapter methods (Stage 6.1.A, ADR-014)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.llm_cost import LLMCallRecord, LLMProvider, LLMRole
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _record(
    *,
    role: LLMRole = "operator",
    provider: LLMProvider = "anthropic",
    model: str = "claude-sonnet-4-6",
    cost_usd: str = "0.003",
    tokens_in: int = 100,
    tokens_out: int = 200,
    tokens_reasoning: int | None = None,
    success: bool = True,
    error_kind: str | None = None,
    request_id: str | None = None,
    offset_seconds: int = 0,
) -> LLMCallRecord:
    return LLMCallRecord(
        timestamp=Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds)),
        role=role,
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_reasoning=tokens_reasoning,
        cost_usd=Decimal(cost_usd),
        request_id=request_id,
        success=success,
        error_kind=error_kind,
    )


# --------------------------------------------------------------------- #
# Save + read round-trip                                                #
# --------------------------------------------------------------------- #


class TestRoundTrip:
    async def test_save_then_read(self, storage: SQLiteStorageAdapter) -> None:
        rec = _record()
        await storage.save_llm_call(rec)
        rows = await storage.get_llm_calls()
        assert len(rows) == 1
        loaded = rows[0]
        assert loaded == rec

    async def test_round_trips_thinking_tokens(self, storage: SQLiteStorageAdapter) -> None:
        rec = _record(tokens_reasoning=500, request_id="req-xyz")
        await storage.save_llm_call(rec)
        loaded = (await storage.get_llm_calls())[0]
        assert loaded.tokens_reasoning == 500
        assert loaded.request_id == "req-xyz"

    async def test_round_trips_failed_call(self, storage: SQLiteStorageAdapter) -> None:
        rec = _record(
            success=False,
            error_kind="rate_limited",
            tokens_out=0,
            cost_usd="0",
        )
        await storage.save_llm_call(rec)
        loaded = (await storage.get_llm_calls())[0]
        assert loaded.success is False
        assert loaded.error_kind == "rate_limited"

    async def test_cost_decimal_precision_preserved(self, storage: SQLiteStorageAdapter) -> None:
        rec = _record(cost_usd="0.123456")
        await storage.save_llm_call(rec)
        loaded = (await storage.get_llm_calls())[0]
        assert loaded.cost_usd == Decimal("0.123456")

    async def test_duplicate_id_rejected(self, storage: SQLiteStorageAdapter) -> None:
        shared_id = uuid4()
        first = _record()
        second = _record()
        # Force shared id by re-constructing with explicit value
        first_dup = first.model_copy(update={"id": shared_id})
        second_dup = second.model_copy(update={"id": shared_id})
        await storage.save_llm_call(first_dup)
        with pytest.raises(StorageError):
            await storage.save_llm_call(second_dup)


# --------------------------------------------------------------------- #
# Filtering                                                             #
# --------------------------------------------------------------------- #


class TestFilters:
    async def test_no_filters_returns_all(self, storage: SQLiteStorageAdapter) -> None:
        for i in range(3):
            await storage.save_llm_call(_record(offset_seconds=i))
        rows = await storage.get_llm_calls()
        assert len(rows) == 3

    async def test_ordering_is_newest_first(self, storage: SQLiteStorageAdapter) -> None:
        oldest = _record(offset_seconds=0)
        middle = _record(offset_seconds=10)
        newest = _record(offset_seconds=20)
        # Insert out-of-order to confirm SQL ORDER BY, not insertion order.
        await storage.save_llm_call(middle)
        await storage.save_llm_call(newest)
        await storage.save_llm_call(oldest)
        rows = await storage.get_llm_calls()
        assert rows[0].id == newest.id
        assert rows[2].id == oldest.id

    async def test_since_filter(self, storage: SQLiteStorageAdapter) -> None:
        # Three rows spread across one minute; since-filter past the
        # midpoint returns only the newest two.
        await storage.save_llm_call(_record(offset_seconds=0))
        cutoff_ts = Timestamp(dt=datetime.now(UTC) + timedelta(seconds=15))
        await storage.save_llm_call(_record(offset_seconds=30))
        await storage.save_llm_call(_record(offset_seconds=60))
        rows = await storage.get_llm_calls(since=cutoff_ts)
        assert len(rows) == 2

    async def test_role_filter(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_llm_call(_record(role="operator"))
        await storage.save_llm_call(_record(role="quant", offset_seconds=1))
        await storage.save_llm_call(_record(role="risk", offset_seconds=2))
        rows = await storage.get_llm_calls(role="quant")
        assert len(rows) == 1
        assert rows[0].role == "quant"

    async def test_provider_filter(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_llm_call(_record(provider="anthropic"))
        await storage.save_llm_call(_record(provider="openai", offset_seconds=1))
        await storage.save_llm_call(_record(provider="google", offset_seconds=2))
        rows = await storage.get_llm_calls(provider="openai")
        assert len(rows) == 1
        assert rows[0].provider == "openai"

    async def test_combined_filters(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_llm_call(_record(role="operator", provider="anthropic"))
        await storage.save_llm_call(_record(role="quant", provider="anthropic", offset_seconds=1))
        await storage.save_llm_call(_record(role="operator", provider="openai", offset_seconds=2))
        rows = await storage.get_llm_calls(role="operator", provider="anthropic")
        assert len(rows) == 1
        assert rows[0].role == "operator"
        assert rows[0].provider == "anthropic"

    async def test_limit_caps_returned_rows(self, storage: SQLiteStorageAdapter) -> None:
        for i in range(5):
            await storage.save_llm_call(_record(offset_seconds=i))
        rows = await storage.get_llm_calls(limit=2)
        assert len(rows) == 2


# --------------------------------------------------------------------- #
# Empty + isolation cases                                               #
# --------------------------------------------------------------------- #


class TestEdgeCases:
    async def test_empty_table_returns_empty_list(self, storage: SQLiteStorageAdapter) -> None:
        rows = await storage.get_llm_calls()
        assert rows == []

    async def test_no_match_returns_empty_list(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_llm_call(_record(provider="anthropic"))
        rows = await storage.get_llm_calls(provider="openai")
        assert rows == []

    async def test_save_to_disconnected_raises(self, tmp_path: object) -> None:
        from pathlib import Path  # local import keeps the top-level imports tight

        adapter = SQLiteStorageAdapter(str(Path(str(tmp_path)) / "x.db"))
        # not connected
        with pytest.raises(StorageError):
            await adapter.save_llm_call(_record())

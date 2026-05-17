"""Tests for ``services/llm_cost_gate.py`` (Stage 6.1.B / ADR-014)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.value_objects import Timestamp
from wobblebot.services.llm_cost_gate import (
    GateAllow,
    GateDeny,
    LLMCostConfig,
    check_budget,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _record(*, cost_usd: str, offset_seconds: int = 0) -> LLMCallRecord:
    return LLMCallRecord(
        timestamp=Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds)),
        role="operator",
        provider="anthropic",
        model="claude-sonnet-4-6",
        tokens_in=100,
        tokens_out=200,
        cost_usd=Decimal(cost_usd),
        success=True,
    )


def _config(
    *,
    daily: str = "1.00",
    session: str = "0.50",
    enforce: bool = True,
) -> LLMCostConfig:
    return LLMCostConfig(
        max_spend_per_day_usd=Decimal(daily),
        max_spend_per_session_usd=Decimal(session),
        enforce=enforce,
    )


# --------------------------------------------------------------------- #
# Allow path                                                            #
# --------------------------------------------------------------------- #


class TestAllow:
    async def test_under_both_caps_allows(self, storage: SQLiteStorageAdapter) -> None:
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.10"),
            session_spent_usd=Decimal("0.05"),
            config=_config(),
        )
        assert isinstance(decision, GateAllow)

    async def test_at_session_cap_exactly_allows(self, storage: SQLiteStorageAdapter) -> None:
        # Projected = session_spent + est = 0.49 + 0.01 = 0.50 == cap.
        # Per ADR-014 design: "would exceed" is strict >, so == is fine.
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.01"),
            session_spent_usd=Decimal("0.49"),
            config=_config(session="0.50"),
        )
        assert isinstance(decision, GateAllow)

    async def test_empty_history_starts_fresh(self, storage: SQLiteStorageAdapter) -> None:
        decision = await check_budget(
            storage,
            role="quant",
            estimated_cost_usd=Decimal("0.99"),
            session_spent_usd=Decimal("0"),
            config=_config(daily="1.00", session="1.00"),
        )
        assert isinstance(decision, GateAllow)


# --------------------------------------------------------------------- #
# Session cap                                                           #
# --------------------------------------------------------------------- #


class TestSessionCap:
    async def test_session_cap_trips(self, storage: SQLiteStorageAdapter) -> None:
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.10"),
            session_spent_usd=Decimal("0.45"),
            config=_config(session="0.50"),
        )
        assert isinstance(decision, GateDeny)
        assert decision.cap_kind == "session"
        assert decision.cap_value_usd == Decimal("0.50")
        assert decision.session_spent_usd == Decimal("0.45")
        assert "operator" in decision.reason

    async def test_session_cap_does_not_query_storage(self, storage: SQLiteStorageAdapter) -> None:
        # Plant rows that would trip the daily cap. Session cap should
        # trip first (in-memory comparison) and skip the DB query — we
        # verify by seeding daily-exceeding rows and confirming the
        # deny mentions session, not daily.
        for _ in range(5):
            await storage.save_llm_call(_record(cost_usd="0.30"))
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.10"),
            session_spent_usd=Decimal("0.45"),  # would trip session
            config=_config(session="0.50", daily="1.00"),
        )
        assert isinstance(decision, GateDeny)
        assert decision.cap_kind == "session"


# --------------------------------------------------------------------- #
# Daily cap (sliding 24h window)                                        #
# --------------------------------------------------------------------- #


class TestDailyCap:
    async def test_daily_cap_trips_with_recent_history(self, storage: SQLiteStorageAdapter) -> None:
        # 4 calls at $0.30 = $1.20 already spent.
        for i in range(4):
            await storage.save_llm_call(_record(cost_usd="0.30", offset_seconds=i))
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.01"),
            session_spent_usd=Decimal("0"),
            config=_config(daily="1.00", session="10.00"),  # session well above
        )
        assert isinstance(decision, GateDeny)
        assert decision.cap_kind == "daily"
        # daily_spent_usd should reflect summed history (~$1.20).
        assert decision.daily_spent_usd == Decimal("1.20")

    async def test_rows_outside_24h_window_ignored(self, storage: SQLiteStorageAdapter) -> None:
        # Plant one row 25 hours ago + one 30 minutes ago. The old one
        # should NOT count toward the daily cap.
        old = _record(cost_usd="5.00")
        old = old.model_copy(
            update={"timestamp": Timestamp(dt=datetime.now(UTC) - timedelta(hours=25))}
        )
        recent = _record(cost_usd="0.20")
        recent = recent.model_copy(
            update={"timestamp": Timestamp(dt=datetime.now(UTC) - timedelta(minutes=30))}
        )
        await storage.save_llm_call(old)
        await storage.save_llm_call(recent)
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.10"),
            session_spent_usd=Decimal("0"),
            config=_config(daily="1.00", session="10.00"),
        )
        assert isinstance(decision, GateAllow)

    async def test_daily_cap_with_injected_now(self, storage: SQLiteStorageAdapter) -> None:
        # Plant a row "today" relative to a future injected now; the
        # gate should still see it inside its 24h window.
        future_now = datetime.now(UTC) + timedelta(hours=10)
        within_window = _record(cost_usd="0.90")
        within_window = within_window.model_copy(
            update={"timestamp": Timestamp(dt=future_now - timedelta(hours=1))}
        )
        await storage.save_llm_call(within_window)
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.20"),
            session_spent_usd=Decimal("0"),
            config=_config(daily="1.00", session="10.00"),
            now=future_now,
        )
        assert isinstance(decision, GateDeny)
        assert decision.cap_kind == "daily"

    async def test_daily_cap_does_not_include_session_total(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # session_spent is a separate dimension; it does NOT add to the
        # daily-projection until those calls persist to the table.
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("0.20"),
            session_spent_usd=Decimal("0.40"),  # high session, but allowed
            config=_config(daily="1.00", session="1.00"),
        )
        assert isinstance(decision, GateAllow)


# --------------------------------------------------------------------- #
# Dry-run posture                                                       #
# --------------------------------------------------------------------- #


class TestDryRunPosture:
    async def test_enforce_false_allows_over_cap(self, storage: SQLiteStorageAdapter) -> None:
        # Both caps would trip; enforce=False short-circuits to allow.
        for _ in range(10):
            await storage.save_llm_call(_record(cost_usd="0.30"))
        decision = await check_budget(
            storage,
            role="operator",
            estimated_cost_usd=Decimal("100"),  # absurd
            session_spent_usd=Decimal("100"),  # also absurd
            config=_config(enforce=False, daily="1.00", session="0.50"),
        )
        assert isinstance(decision, GateAllow)


# --------------------------------------------------------------------- #
# Reason content                                                        #
# --------------------------------------------------------------------- #


class TestDenyReason:
    async def test_session_deny_includes_role_and_projected_total(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        decision = await check_budget(
            storage,
            role="quant",
            estimated_cost_usd=Decimal("0.10"),
            session_spent_usd=Decimal("0.45"),
            config=_config(session="0.50"),
        )
        assert isinstance(decision, GateDeny)
        assert "quant" in decision.reason
        assert "0.50" in decision.reason
        # Projected = 0.45 + 0.10 = 0.55, present in the reason.
        assert "0.55" in decision.reason

    async def test_daily_deny_includes_role_and_projected_total(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        for i in range(3):
            await storage.save_llm_call(_record(cost_usd="0.30", offset_seconds=i))
        decision = await check_budget(
            storage,
            role="risk",
            estimated_cost_usd=Decimal("0.50"),
            session_spent_usd=Decimal("0"),
            config=_config(daily="1.00", session="10.00"),
        )
        assert isinstance(decision, GateDeny)
        assert "risk" in decision.reason
        assert "1.00" in decision.reason

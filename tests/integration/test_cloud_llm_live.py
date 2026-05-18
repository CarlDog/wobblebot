"""Live cloud-LLM integration tests (Stage 6.5.A; Phase 6).

Opt-in by both the ``integration`` pytest marker AND the presence of the
provider's API key in the environment — runs against the real provider
APIs and **spends real money** (~$0.001-$0.005 per call). Each test
exercises ONE tiny call to verify the full ADR-014/015 flow end-to-end:

  1. Cost gate check passes against an empty in-memory DB.
  2. ``execute_cloud_call`` orchestrates the retry-wrapped HTTPS call.
  3. Provider returns a response; usage block parses correctly.
  4. ``LLMCallRecord`` persists with success=True + cost_usd > 0 +
     real request_id.
  5. Session tracker reflects the actual (not estimated) cost.

Run::

    # All three (requires all three API keys set):
    pytest -m integration tests/integration/test_cloud_llm_live.py

    # Just one provider:
    pytest -m integration tests/integration/test_cloud_llm_live.py::test_anthropic_live

The skip-when-key-missing pattern mirrors the Kraken live-read tests
in ``test_kraken_trading_live.py``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter
from wobblebot.adapters.google import GoogleAdvisorAdapter
from wobblebot.adapters.openai import OpenAIAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.prompts import Prompt, load_prompt
from wobblebot.ports.advisor import CurrentGridParams, PerformanceSummary
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_retry import LLMRetryConfig

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    """Fresh in-memory operator DB per test — isolation + zero file
    cleanup."""
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _quant_prompt() -> Prompt:
    return load_prompt(Path("config/prompts/quant.md"))


def _summary() -> PerformanceSummary:
    """Tiny summary that fits in a small prompt — keeps the test call
    cheap (~$0.001-$0.005)."""
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=1.0,
        latest_price=80000.0,
        snapshot_count=10,
        volatility=0.001,
        max_drawdown=-0.005,
        flatness=0.99,
        cycle_count=0,
        win_rate=0.0,
        total_pnl=0.0,
        current_grid=CurrentGridParams(
            spacing_percentage=1.0,
            levels_above=3,
            levels_below=3,
            order_size_usd=10.0,
        ),
    )


def _common_kwargs(
    storage: SQLiteStorageAdapter,
    tracker: SessionCostTracker,
) -> dict[str, object]:
    return {
        "prompt": _quant_prompt(),
        "role": "quant",
        "storage": storage,
        "session_tracker": tracker,
        "cost_config": LLMCostConfig(
            max_spend_per_day_usd=Decimal("1.00"),
            max_spend_per_session_usd=Decimal("0.50"),
            enforce=True,
        ),
        "retry_config": LLMRetryConfig(max_retries=2, initial_backoff_seconds=1.0),
        "max_tokens": 100,
    }


async def _assert_call_persisted(
    storage: SQLiteStorageAdapter,
    tracker: SessionCostTracker,
    expected_provider: str,
) -> None:
    """Common post-call assertions: row landed, cost > 0, tracker
    reflects real cost, provider matches."""
    rows = await storage.get_llm_calls()
    assert len(rows) == 1, f"expected exactly one row; got {len(rows)}"
    record = rows[0]
    assert record.success is True, f"failed call: error_kind={record.error_kind}"
    assert record.provider == expected_provider
    assert record.cost_usd > Decimal("0")
    assert record.tokens_in > 0
    assert record.tokens_out > 0
    assert record.request_id is not None
    assert tracker.total == record.cost_usd


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
async def test_anthropic_live(storage: SQLiteStorageAdapter) -> None:
    """Live Anthropic call — spends ~$0.004 against claude-sonnet-4-6."""
    tracker = SessionCostTracker()
    adapter = AnthropicAdvisorAdapter(
        model="claude-sonnet-4-6",
        api_key=os.environ["ANTHROPIC_API_KEY"],
        **_common_kwargs(storage, tracker),  # type: ignore[arg-type]
    )
    try:
        await adapter.get_recommendation(_summary())
    finally:
        await adapter.aclose()
    await _assert_call_persisted(storage, tracker, "anthropic")


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
async def test_openai_live(storage: SQLiteStorageAdapter) -> None:
    """Live OpenAI call — spends ~$0.0002 against gpt-4o-mini (cheap default)."""
    tracker = SessionCostTracker()
    adapter = OpenAIAdvisorAdapter(
        model="gpt-4o-mini",
        api_key=os.environ["OPENAI_API_KEY"],
        organization=os.environ.get("OPENAI_ORGANIZATION") or None,
        **_common_kwargs(storage, tracker),  # type: ignore[arg-type]
    )
    try:
        await adapter.get_recommendation(_summary())
    finally:
        await adapter.aclose()
    await _assert_call_persisted(storage, tracker, "openai")


@pytest.mark.skipif(
    not os.environ.get("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set",
)
async def test_google_live(storage: SQLiteStorageAdapter) -> None:
    """Live Google Gemini call — spends ~$0.0006 against gemini-2.5-flash."""
    tracker = SessionCostTracker()
    adapter = GoogleAdvisorAdapter(
        model="gemini-2.5-flash",
        api_key=os.environ["GOOGLE_API_KEY"],
        **_common_kwargs(storage, tracker),  # type: ignore[arg-type]
    )
    try:
        await adapter.get_recommendation(_summary())
    finally:
        await adapter.aclose()
    await _assert_call_persisted(storage, tracker, "google")

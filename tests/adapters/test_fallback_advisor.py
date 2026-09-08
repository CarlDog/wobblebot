"""Bounded substitution, role safety, recovery and failure propagation."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from wobblebot.adapters.fallback_advisor import AdvisorCandidate, FallbackAdvisorAdapter
from wobblebot.domain.exceptions import LLMCostCapExceeded, LLMRetryExhausted
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorPort, AdvisorRecommendation, PerformanceSummary
from wobblebot.ports.exceptions import AdvisorError, StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _summary() -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=24,
        snapshot_count=100,
        volatility=0.01,
        max_drawdown=-0.01,
        flatness=0.5,
        cycle_count=4,
        win_rate=0.5,
    )


def _result() -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommendation_id="test",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="quant",
        rationale="test",
        confidence="high",
        recommendations={"spacing_percentage": 2.5},
    )


def _candidate(provider: str, model: str) -> AdvisorCandidate:
    stub = AsyncMock(spec=AdvisorPort)
    stub.get_recommendation.return_value = _result()
    stub.aclose = AsyncMock()
    return AdvisorCandidate(provider, model, stub)


def _outage(status: int = 503, body: object = None) -> AdvisorError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cause = httpx.HTTPStatusError(
        "unsafe sentinel",
        request=request,
        response=httpx.Response(status, request=request, json=body),
    )
    error = AdvisorError("provider failed")
    error.__cause__ = LLMRetryExhausted(4, cause) if status >= 500 or status == 429 else cause
    return error


async def test_primary_success_avoids_backup_and_records_actual_model() -> None:
    primary, backup = _candidate("anthropic", "primary"), _candidate("openai", "backup")
    adapter = FallbackAdvisorAdapter(role="news", candidates=[primary, backup])
    result = await adapter.get_recommendation(_summary())
    assert result.role == "news"
    assert [(a.provider, a.model, a.error_kind) for a in result.llm_attempts] == [
        ("anthropic", "primary", None)
    ]
    backup.advisor.get_recommendation.assert_not_awaited()


@pytest.mark.parametrize(
    "status,body",
    [
        (
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API.",
                }
            },
        ),
        (429, {"error": {"code": "insufficient_quota"}}),
        (429, {}),
        (401, {}),
        (403, {}),
        (402, {}),
        (503, {}),
        (529, {}),
        (404, {"error": {"code": "model_not_found"}}),
    ],
)
async def test_interruptions_try_explicit_backup_with_same_context(
    status: int, body: object, caplog
) -> None:
    primary, backup = _candidate("anthropic", "primary"), _candidate("openai", "backup")
    primary.advisor.get_recommendation.side_effect = _outage(status, body)
    adapter = FallbackAdvisorAdapter(role="arbitrator", candidates=[primary, backup])
    summary = _summary()
    result = await adapter.get_recommendation(summary, extra_context="expert opinions")
    primary.advisor.get_recommendation.assert_awaited_once_with(
        summary, extra_context="expert opinions"
    )
    backup.advisor.get_recommendation.assert_awaited_once_with(
        summary, extra_context="expert opinions"
    )
    assert result.role == "arbitrator"
    assert len(result.llm_attempts) == 2
    assert result.llm_attempts[-1].provider == "openai"
    assert "unsafe sentinel" not in caplog.text
    assert "anthropic/primary" in caplog.text and "openai/backup" in caplog.text


@pytest.mark.parametrize(
    "cause",
    [
        httpx.ConnectError("sentinel"),
        httpx.ReadTimeout("sentinel"),
        httpx.RemoteProtocolError("sentinel"),
        httpx.ReadError("sentinel"),
    ],
)
async def test_transport_outages_use_backup(cause: Exception) -> None:
    primary, backup = _candidate("anthropic", "primary"), _candidate("ollama", "local")
    failure = AdvisorError("transport failed")
    failure.__cause__ = cause
    primary.advisor.get_recommendation.side_effect = failure
    result = await FallbackAdvisorAdapter(
        role="single", candidates=[primary, backup]
    ).get_recommendation(_summary())
    assert result.llm_attempts[-1].provider == "ollama"


@pytest.mark.parametrize(
    "error",
    [
        _outage(400, {"error": {"type": "invalid_request_error", "message": "bad temperature"}}),
        _outage(404, {}),
        _outage(403, {"error": {"code": "content_policy_violation"}}),
        AdvisorError("schema validation failed"),
        AdvisorError("content refusal"),
        StorageError("write failed"),
        RuntimeError("bug"),
        asyncio.CancelledError(),
        LLMCostCapExceeded(
            cap_kind="daily",
            cap_value_usd=Decimal("1"),
            daily_spent_usd=Decimal("1"),
            session_spent_usd=Decimal("0"),
        ),
    ],
)
async def test_non_interruptions_never_substitute(error: BaseException) -> None:
    primary, backup = _candidate("anthropic", "primary"), _candidate("openai", "backup")
    primary.advisor.get_recommendation.side_effect = error
    with pytest.raises(type(error)) as caught:
        await FallbackAdvisorAdapter(role="news", candidates=[primary, backup]).get_recommendation(
            _summary()
        )
    assert caught.value is error
    backup.advisor.get_recommendation.assert_not_awaited()


async def test_chain_is_finite_and_next_call_retries_primary() -> None:
    candidates = [
        _candidate("anthropic", "a"),
        _candidate("openai", "b"),
        _candidate("ollama", "c"),
    ]
    for candidate in candidates:
        candidate.advisor.get_recommendation.side_effect = _outage()
    adapter = FallbackAdvisorAdapter(role="news", candidates=candidates)
    with pytest.raises(AdvisorError):
        await adapter.get_recommendation(_summary())
    assert [c.advisor.get_recommendation.await_count for c in candidates] == [1, 1, 1]
    candidates[0].advisor.get_recommendation.side_effect = None
    result = await adapter.get_recommendation(_summary())
    assert len(result.llm_attempts) == 1
    assert [c.advisor.get_recommendation.await_count for c in candidates] == [2, 1, 1]


async def test_second_backup_succeeds_and_all_clients_close() -> None:
    candidates = [
        _candidate("anthropic", "a"),
        _candidate("openai", "b"),
        _candidate("ollama", "c"),
    ]
    for candidate in candidates[:2]:
        candidate.advisor.get_recommendation.side_effect = _outage()
    adapter = FallbackAdvisorAdapter(role="news", candidates=candidates)
    assert len((await adapter.get_recommendation(_summary())).llm_attempts) == 3
    candidates[0].advisor.aclose.side_effect = RuntimeError("close failed")
    with pytest.raises(ExceptionGroup):
        await adapter.aclose()
    for candidate in candidates:
        candidate.advisor.aclose.assert_awaited_once()


async def test_cascade_keeps_heuristic_hold_when_every_target_is_unavailable() -> None:
    from tests.adapters.test_cascading_advisor import _heuristic
    from tests.adapters.test_cascading_advisor import _summary as cascade_summary
    from wobblebot.adapters.cascading_advisor import CascadingAdvisorAdapter

    primary, backup = _candidate("anthropic", "primary"), _candidate("openai", "backup")
    primary.advisor.get_recommendation.side_effect = _outage()
    backup.advisor.get_recommendation.side_effect = _outage()
    route = FallbackAdvisorAdapter(role="single", candidates=[primary, backup])
    cascade = CascadingAdvisorAdapter(heuristic=_heuristic(), llm=route)
    result = await cascade.get_recommendation(
        cascade_summary(current_spacing=1.42, volatility=0.004)
    )
    assert result.role == "heuristic" and result.recommendations == {}
    assert primary.advisor.get_recommendation.await_count == 1
    assert backup.advisor.get_recommendation.await_count == 1

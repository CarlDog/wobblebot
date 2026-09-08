"""Real CLI composition, provider protocols, shared budgets and persisted provenance."""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from tests.cli.test_advise import BTC_USD, _default_grid, _seed_prices
from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter
from wobblebot.adapters.openai import OpenAIAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import advise
from wobblebot.config.advisor import (
    AdvisorConfig,
    ArbitratorConfig,
    ExpertConfig,
    FallbackTarget,
    InferenceParams,
)
from wobblebot.config.llm import LLMConfig
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_retry import LLMRetryConfig
from wobblebot.services.summary_builder import SummaryBuilder

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_PARAMS = InferenceParams(temperature=Decimal("1"), max_tokens=4000, timeout_seconds=10)
_BACKUP = FallbackTarget(provider="openai", model="gpt-5-mini", inference_params=_PARAMS)


@pytest_asyncio.fixture
async def storage():
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    try:
        yield adapter
    finally:
        await adapter.close()


def _wiring(storage, **kwargs):
    return advise._CloudWiring(
        storage=storage,
        session_tracker=SessionCostTracker(),
        llm_config=LLMConfig(
            retry=LLMRetryConfig(max_retries=1, initial_backoff_seconds=0.001), **kwargs
        ),
    )


def _config() -> AdvisorConfig:
    experts = [
        ExpertConfig(
            name=role,
            role=role,
            provider="openai",
            model="gpt-5-mini",
            prompt_file=f"config/prompts/{role}.md",
            inference_params=_PARAMS,
        )
        for role in ("quant", "risk")
    ]
    experts.append(
        ExpertConfig(
            name="news",
            role="news",
            provider="anthropic",
            model="claude-haiku-4-5",
            prompt_file="config/prompts/news.md",
            inference_params=_PARAMS,
            fallbacks=[_BACKUP],
        )
    )
    return AdvisorConfig(
        type="moe",
        aggregator="arbitrator",
        experts=experts,
        arbitrator=ArbitratorConfig(
            provider="anthropic",
            model="claude-haiku-4-5",
            prompt_file="config/prompts/arbitrator.md",
            inference_params=_PARAMS,
            fallbacks=[_BACKUP],
        ),
    )


async def test_real_moe_fallback_preserves_news_gate_context_costs_and_provenance(
    storage, monkeypatch
):
    clients = []
    requests = []
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "test-only")

    def build(adapter_cls, **kwargs):
        role = kwargs["role"]

        def handler(request):
            requests.append((role, request.url.host, json.loads(request.content)))
            if request.url.host == "api.anthropic.com":
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Your credit balance is too low to access the Anthropic API. secret sentinel",
                        }
                    },
                )
            recommendation = {
                "role": "quant",
                "rationale": "synthetic",
                "confidence": "high",
                "recommendations": {
                    "spacing_percentage": 2.5 if role in ("news", "arbitrator") else 1.0
                },
                "llm_attempts": [{"provider": "forged", "model": "forged", "role": "forged"}],
            }
            return httpx.Response(
                200,
                json={
                    "id": f"response-{role}",
                    "choices": [
                        {
                            "message": {"content": json.dumps(recommendation)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 100},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return adapter_cls(client=client, **kwargs)

    monkeypatch.setattr(
        advise, "AnthropicAdvisorAdapter", lambda **kw: build(AnthropicAdvisorAdapter, **kw)
    )
    monkeypatch.setattr(
        advise, "OpenAIAdvisorAdapter", lambda **kw: build(OpenAIAdvisorAdapter, **kw)
    )
    wiring = _wiring(storage)
    label = []
    adapter = advise._build_advisor(_config(), label, cloud_wiring=wiring)
    try:
        await _seed_prices(storage)
        assert await advise._run_cycle(
            adapter,
            SummaryBuilder(storage),
            storage,
            symbol=BTC_USD,
            metrics_lookback=timedelta(hours=24),
            news_lookback=None,
            news_limit=5,
            news_match_coin=True,
            current_grid=_default_grid(),
            model_name=label[0],
        )
        suggestion = (await storage.get_advisor_suggestions())[0]
        rec = suggestion.recommendation
        assert rec.role == "aggregated"
        assert rec.news_materially_drove
        assert [op.role for op in rec.expert_opinions] == ["quant", "risk", "news"]
        assert len(rec.expert_opinions[-1].llm_attempts) == 2
        assert [(a.role, a.provider, a.error_kind) for a in rec.llm_attempts] == [
            ("news", "anthropic", "insufficient_credit"),
            ("news", "openai", None),
            ("arbitrator", "anthropic", "insufficient_credit"),
            ("arbitrator", "openai", None),
        ]
        assert "news=openai/gpt-5-mini" in suggestion.model_name
        assert "arbitrator=openai/gpt-5-mini" in suggestion.model_name
        assert "secret sentinel" not in suggestion.model_dump_json()
        calls = await storage.get_llm_calls()
        assert len(calls) == 6
        assert len({call.trace_id for call in calls}) == 1 and calls[0].trace_id
        assert wiring.session_tracker.total == sum(call.cost_usd for call in calls)
        assert wiring.session_tracker.total > 0
        assert len([r for r in requests if r[1] == "api.anthropic.com"]) == 2
        arb_requests = [body for role, _, body in requests if role == "arbitrator"]
        assert len(arb_requests) == 2
        # Both provider requests contain the same experts' opinion context.
        assert (
            arb_requests[0]["messages"][-1]["content"] == arb_requests[1]["messages"][-1]["content"]
        )
    finally:
        await adapter.aclose()
        for client in clients:
            await client.aclose()


async def test_backup_checks_the_same_shared_budget_after_primary_failure(storage, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "test-only")
    wiring = _wiring(storage, cost=LLMCostConfig(max_spend_per_session_usd=Decimal("0.5")))
    backup_http = AsyncMock()

    def primary_http(request):
        wiring.session_tracker.add(Decimal("0.5"))  # Another role consumed the remaining budget.
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API.",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(primary_http)) as primary_client:
        async with httpx.AsyncClient(transport=httpx.MockTransport(backup_http)) as backup_client:
            monkeypatch.setattr(
                advise,
                "AnthropicAdvisorAdapter",
                lambda **kw: AnthropicAdvisorAdapter(client=primary_client, **kw),
            )
            monkeypatch.setattr(
                advise,
                "OpenAIAdvisorAdapter",
                lambda **kw: OpenAIAdvisorAdapter(client=backup_client, **kw),
            )
            entry = advise._build_expert_entry(_config().experts[-1], wiring)
            from tests.adapters.test_fallback_advisor import _summary

            with pytest.raises(LLMCostCapExceeded):
                await entry.advisor.get_recommendation(_summary())
            backup_http.assert_not_awaited()
            calls = await storage.get_llm_calls()
            assert len(calls) == 1 and calls[0].error_kind == "insufficient_credit"


async def test_missing_backup_key_fails_before_any_client_is_created(storage, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    constructor = AsyncMock()
    monkeypatch.setattr(advise, "AnthropicAdvisorAdapter", constructor)
    with pytest.raises(ValueError, match="OPENAI_API_KEY missing"):
        advise._build_expert_entry(_config().experts[-1], _wiring(storage))
    constructor.assert_not_called()

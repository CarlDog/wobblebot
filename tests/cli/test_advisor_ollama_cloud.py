"""Real CLI composition across cloud providers; all HTTP is mocked."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tests.adapters.test_fallback_advisor import _summary
from tests.adapters.test_ollama_cloud import response_body
from tests.cli.test_advisor_ollama_fallbacks import _success
from wobblebot.adapters.cascading_advisor import CascadingAdvisorAdapter
from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.adapters.ollama_cloud import OllamaCloudAdvisorAdapter
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
from wobblebot.config.heuristic import load_heuristic_spec
from wobblebot.config.llm import LLMConfig
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_retry import LLMRetryConfig

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
PARAMS = InferenceParams(max_tokens=4000, timeout_seconds=10)


@pytest_asyncio.fixture
async def cloud_routes(monkeypatch):
    for key in ("OLLAMA_API_KEY", "OPENAI_API_KEY", "ATLASCLOUD_API_KEY"):
        monkeypatch.setenv(key, "test-only")
    storage = SQLiteStorageAdapter(":memory:")
    await storage.connect()
    wiring = advise._CloudWiring(
        storage=storage,
        session_tracker=SessionCostTracker(),
        llm_config=LLMConfig(
            retry=LLMRetryConfig(max_retries=1, initial_backoff_seconds=0.001),
            cost=LLMCostConfig(max_spend_per_session_usd=Decimal("1")),
        ),
    )
    state = {"winner": "ollama_cloud", "invalid": False, "cap": False}
    requests, clients = [], []

    def build(cls, **kwargs):
        provider = (
            "ollama_cloud"
            if cls is OllamaCloudAdvisorAdapter
            else ("atlas" if kwargs["model"] == "xai/grok-4.5" else "openai")
        )

        def handler(request):
            requests.append((provider, json.loads(request.content)))
            if state["cap"]:
                wiring.session_tracker.add(Decimal("1"))
            if provider == "ollama_cloud" and state["invalid"]:
                return httpx.Response(200, json=response_body() | {"message": {"content": "bad"}})
            if provider == state["winner"]:
                return (
                    httpx.Response(200, json=response_body())
                    if provider == "ollama_cloud"
                    else _success("openai", kwargs["role"])
                )
            return httpx.Response(503, json={"error": "unavailable"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return cls(client=client, **kwargs)

    monkeypatch.setattr(
        advise, "OpenAIAdvisorAdapter", lambda **kw: build(OpenAIAdvisorAdapter, **kw)
    )
    monkeypatch.setattr(
        advise, "OllamaCloudAdvisorAdapter", lambda **kw: build(OllamaCloudAdvisorAdapter, **kw)
    )

    def forbid_local(**_):
        raise AssertionError("unexpected local provider")

    monkeypatch.setattr(advise, "OllamaAdapter", forbid_local)
    yield state, requests, wiring
    for client in clients:
        await client.aclose()
    await storage.close()


def route(wiring, role="news", primary_cloud=False):
    targets = [
        ("openai", "gpt-5-mini"),
        ("ollama_cloud", "gpt-oss:120b"),
        ("atlas", "xai/grok-4.5"),
    ]
    if primary_cloud:
        targets[0], targets[1] = targets[1], targets[0]
    return advise._build_advisor_route(
        provider=targets[0][0],
        model=targets[0][1],
        prompt_file=f"config/prompts/{role}.md",
        role=role,
        inference_params=PARAMS,
        cloud_wiring=wiring,
        fallbacks=[
            FallbackTarget(provider=p, model=m, inference_params=PARAMS) for p, m in targets[1:]
        ],
    )


@pytest.mark.parametrize("role", ["quant", "risk", "news", "arbitrator"])
@pytest.mark.parametrize(
    "winner,expected",
    [
        ("openai", ["openai"]),
        ("ollama_cloud", ["openai", "openai", "ollama_cloud"]),
        ("atlas", ["openai", "openai", "ollama_cloud", "ollama_cloud", "atlas"]),
    ],
)
async def test_cloud_fallback_order_context_roles_and_billing(cloud_routes, role, winner, expected):
    state, requests, wiring = cloud_routes
    state["winner"] = winner
    adapter = route(wiring, role)
    try:
        rec = await adapter.get_recommendation(_summary(), extra_context="expert opinions")
        assert rec.role == role
        assert [p for p, _ in requests] == expected
        assert rec.llm_attempts[-1].provider == winner
        assert all(attempt.role == role for attempt in rec.llm_attempts)
        prompts = [body["messages"][-1]["content"] for _, body in requests]
        assert len(set(prompts)) == 1 and "expert opinions" in prompts[0]
        rows = await wiring.storage.get_llm_calls()
        assert all(row.role == role for row in rows)
        assert wiring.session_tracker.total == sum(row.cost_usd for row in rows) > 0
        if winner != "openai":
            assert len(await wiring.storage.get_llm_calls(provider="ollama_cloud")) == 1
    finally:
        await adapter.aclose()


async def test_ollama_cloud_primary_exhaustion_heuristic_and_next_cycle_recovery(cloud_routes):
    state, requests, wiring = cloud_routes
    state["winner"] = None
    adapter = CascadingAdvisorAdapter(
        heuristic=HeuristicAdvisorAdapter(
            spec=load_heuristic_spec(Path("config/heuristic/quant.yml"))
        ),
        llm=route(wiring, primary_cloud=True),
    )
    try:
        assert (await adapter.get_recommendation(_summary())).role == "heuristic"
        assert [p for p, _ in requests] == ["ollama_cloud"] * 2 + ["openai"] * 2 + ["atlas"] * 2
        requests.clear()
        state["winner"] = "ollama_cloud"
        assert (await adapter.get_recommendation(_summary())).role == "news"
        assert [p for p, _ in requests] == ["ollama_cloud"]
    finally:
        await adapter.aclose()


async def test_invalid_cloud_output_does_not_substitute_another_model(cloud_routes):
    state, requests, wiring = cloud_routes
    state["invalid"] = True
    adapter = route(wiring)
    try:
        with pytest.raises(AdvisorError):
            await adapter.get_recommendation(_summary())
        assert all(p != "atlas" for p, _ in requests)
    finally:
        await adapter.aclose()


async def test_ollama_cloud_cannot_bypass_shared_cap(cloud_routes):
    state, requests, wiring = cloud_routes
    state["cap"] = True
    adapter = route(wiring)
    try:
        with pytest.raises(LLMCostCapExceeded):
            await adapter.get_recommendation(_summary())
        assert all(p == "openai" for p, _ in requests)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("missing", ["key", "wiring"])
async def test_missing_cloud_preconditions_fail_before_construction(
    cloud_routes, monkeypatch, missing
):
    _, requests, wiring = cloud_routes
    if missing == "key":
        monkeypatch.delenv("OLLAMA_API_KEY")
    with pytest.raises(advise.OperatorConfigError, match="OLLAMA_API_KEY|llm:"):
        route(None if missing == "wiring" else wiring)
    assert requests == []


@pytest.mark.parametrize("kind", ["single", "moe"])
async def test_primary_provider_through_complete_advisor_builder(cloud_routes, kind):
    _, requests, wiring = cloud_routes
    common = dict(provider="ollama_cloud", model="gpt-oss:120b", inference_params=PARAMS)
    if kind == "single":
        config = AdvisorConfig(type="single", prompt_file="config/prompts/quant.md", **common)
        expected = {"single"}
    else:
        config = AdvisorConfig(
            type="moe",
            aggregator="arbitrator",
            experts=[
                ExpertConfig(name=r, role=r, prompt_file=f"config/prompts/{r}.md", **common)
                for r in ("quant", "risk", "news")
            ],
            arbitrator=ArbitratorConfig(prompt_file="config/prompts/arbitrator.md", **common),
        )
        expected = {"quant", "risk", "news", "arbitrator"}
    adapter = advise._build_advisor(config, [], cloud_wiring=wiring)
    try:
        await adapter.get_recommendation(_summary())
        rows = await wiring.storage.get_llm_calls()
        assert {r.role for r in rows} == expected
        assert len(requests) == len(expected)
        assert all(r.provider == "ollama_cloud" for r in rows)
    finally:
        await adapter.aclose()

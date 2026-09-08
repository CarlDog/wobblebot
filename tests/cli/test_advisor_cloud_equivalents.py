"""Equivalent cloud route -> previous cloud backup -> heuristic -> primary recovery."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tests.adapters.test_fallback_advisor import _summary
from tests.cli.test_advisor_ollama_fallbacks import _success
from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter
from wobblebot.adapters.cascading_advisor import CascadingAdvisorAdapter
from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.adapters.openai import OpenAIAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import advise
from wobblebot.config.advisor import FallbackTarget, InferenceParams
from wobblebot.config.heuristic import load_heuristic_spec
from wobblebot.config.llm import LLMConfig
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_retry import LLMRetryConfig

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
MIRROR = "anthropic/claude-haiku-4.5-20251001"
PARAMS = InferenceParams(temperature=Decimal("0.6"), max_tokens=4000, timeout_seconds=10)


@pytest_asyncio.fixture
async def cloud_lab(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ATLASCLOUD_API_KEY", "OPENAI_API_KEY"):
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
    state = {"success_slot": 0, "cap_after_mirror": False}
    requests, clients = [], []

    def build(adapter_cls, **kwargs):
        model, role = kwargs["model"], kwargs["role"]
        slot = 0 if adapter_cls is AnthropicAdvisorAdapter else 1 if model == MIRROR else 2

        def handler(request):
            requests.append((slot, request, json.loads(request.content)))
            if slot == 1 and state["cap_after_mirror"]:
                wiring.session_tracker.add(Decimal("1"))
            if slot == state["success_slot"]:
                return _success("anthropic" if slot == 0 else "openai", role)
            if slot == 0:
                return httpx.Response(403, json={"error": {"type": "permission_error"}})
            return httpx.Response(503, json={"error": {"type": "server_error"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return adapter_cls(client=client, **kwargs)

    monkeypatch.setattr(
        advise, "AnthropicAdvisorAdapter", lambda **kw: build(AnthropicAdvisorAdapter, **kw)
    )
    monkeypatch.setattr(
        advise, "OpenAIAdvisorAdapter", lambda **kw: build(OpenAIAdvisorAdapter, **kw)
    )

    def forbid_local(**kwargs):
        raise AssertionError("cloud outage chain must not construct local Ollama")

    monkeypatch.setattr(advise, "OllamaAdapter", forbid_local)
    try:
        yield state, requests, storage, wiring
    finally:
        for client in clients:
            await client.aclose()
        await storage.close()


def route(role, wiring):
    final_provider, final_model = (
        ("atlas", "xai/grok-4.5") if role == "news" else ("openai", "gpt-5-mini")
    )
    return advise._build_advisor_route(
        provider="anthropic",
        model="claude-haiku-4-5",
        prompt_file=f"config/prompts/{role}.md",
        role=role,
        inference_params=PARAMS,
        cloud_wiring=wiring,
        fallbacks=[
            FallbackTarget(provider="atlas", model=MIRROR, inference_params=PARAMS),
            FallbackTarget(provider=final_provider, model=final_model, inference_params=PARAMS),
        ],
    )


@pytest.mark.parametrize("role", ["news", "arbitrator"])
@pytest.mark.parametrize("success_slot", [0, 1, 2])
async def test_equivalent_precedes_original_backup_and_stops_on_success(
    cloud_lab, role, success_slot
):
    state, requests, storage, wiring = cloud_lab
    state["success_slot"] = success_slot
    adapter = route(role, wiring)
    context = "synthetic expert opinions" if role == "arbitrator" else None
    try:
        rec = await adapter.get_recommendation(_summary(), extra_context=context)
        assert rec.role == role
        assert [slot for slot, _, _ in requests] == [0, 1, 1, 2][
            : 1 if success_slot == 0 else 2 if success_slot == 1 else 4
        ]
        assert len(rec.llm_attempts) == success_slot + 1
        if success_slot >= 1:
            assert rec.llm_attempts[1].provider == "atlas"
            assert rec.llm_attempts[1].model == MIRROR
            assert requests[1][1].url.host == "api.atlascloud.ai"
            assert requests[1][2]["model"] == MIRROR
        user_prompts = [body["messages"][-1]["content"] for _, _, body in requests]
        assert len(set(user_prompts)) == 1
        if context:
            assert context in user_prompts[0]
        calls = await storage.get_llm_calls()
        assert len(calls) == success_slot + 1
        assert wiring.session_tracker.total == sum(call.cost_usd for call in calls)
        assert wiring.session_tracker.total > 0
        if success_slot == 1:
            # 100 uncached input + 100 output at catalog $1/$5 per million.
            assert wiring.session_tracker.total == Decimal("0.0006")
    finally:
        await adapter.aclose()


async def test_all_cloud_failures_use_heuristic_then_next_evaluation_recovers_primary(cloud_lab):
    state, requests, storage, wiring = cloud_lab
    state["success_slot"] = None
    heuristic = HeuristicAdvisorAdapter(
        spec=load_heuristic_spec(Path("config/heuristic/quant.yml"))
    )
    adapter = CascadingAdvisorAdapter(heuristic=heuristic, llm=route("news", wiring))
    try:
        summary = _summary()
        assert not heuristic.evaluate(summary).clear_match
        result = await adapter.get_recommendation(summary)
        assert result.role == "heuristic"
        assert [slot for slot, _, _ in requests] == [0, 1, 1, 2, 2]
        assert len(await storage.get_llm_calls()) == 3
        # No extra attempts after exhaustion; the next ordinary call probes primary.
        requests.clear()
        state["success_slot"] = 0
        recovered = await adapter.get_recommendation(summary)
        assert recovered.role == "news"
        assert [slot for slot, _, _ in requests] == [0]
    finally:
        await adapter.aclose()


async def test_shared_cap_after_mirror_blocks_final_cloud_candidate(cloud_lab):
    state, requests, _, wiring = cloud_lab
    state.update(success_slot=2, cap_after_mirror=True)
    adapter = route("arbitrator", wiring)
    try:
        with pytest.raises(LLMCostCapExceeded):
            await adapter.get_recommendation(_summary(), extra_context="opinions")
        assert all(slot != 2 for slot, _, _ in requests)
    finally:
        await adapter.aclose()

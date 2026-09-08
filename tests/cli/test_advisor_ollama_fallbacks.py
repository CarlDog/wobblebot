"""Three real provider adapters through CLI composition; all HTTP is synthetic."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from tests.adapters.test_fallback_advisor import _summary
from tests.cli.test_advise import BTC_USD, _default_grid, _seed_prices
from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter
from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.openai import OpenAIAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import advise
from wobblebot.config.advisor import AdvisorConfig, AutoApplyConfig
from wobblebot.config.grid import GridLevels
from wobblebot.config.llm import LLMConfig
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.auto_apply import evaluate_auto_apply
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_retry import LLMRetryConfig
from wobblebot.services.summary_builder import SummaryBuilder

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# These exercise routing, not the models' judgment or fitness for activation.
_ROUTES = {
    "quant": [
        ("atlas", "xai/grok-4.5"),
        ("atlas", "deepseek-ai/deepseek-v4-pro"),
        ("ollama", "llama3.1:8b-instruct-q4_K_M"),
    ],
    "risk": [
        ("openai", "gpt-5-mini"),
        ("atlas", "deepseek-ai/deepseek-v4-pro"),
        ("ollama", "qwen2.5:7b-instruct-q4_K_M"),
    ],
    "news": [
        ("anthropic", "claude-haiku-4-5"),
        ("atlas", "xai/grok-4.5"),
        ("ollama", "gemma4:e4b-it-q8_0"),
    ],
    "arbitrator": [
        ("anthropic", "claude-haiku-4-5"),
        ("openai", "gpt-5-mini"),
        ("ollama", "gemma4:e4b-it-q8_0"),
    ],
}
_PARAMS = [
    {"temperature": 1.0, "max_tokens": 4000, "timeout_seconds": 11},
    {"temperature": 0.6, "max_tokens": 3000, "timeout_seconds": 13},
    {"temperature": 0.4, "max_tokens": 2048, "timeout_seconds": 17},
]


def _target(role):
    primary, *backups = [
        {"provider": provider, "model": model, "inference_params": _PARAMS[slot]}
        for slot, (provider, model) in enumerate(_ROUTES[role])
    ]
    return {**primary, "prompt_file": f"config/prompts/{role}.md", "fallbacks": backups}


def _config(*, moe=False, engine="llm"):
    common = {"engine": engine, "heuristic_file": "config/heuristic/quant.yml"}
    if not moe:
        return AdvisorConfig.model_validate({**common, "type": "single", **_target("quant")})
    return AdvisorConfig.model_validate(
        {
            **common,
            "type": "moe",
            "aggregator": "arbitrator",
            "experts": [
                {"name": role, "role": role, **_target(role)} for role in ("quant", "risk", "news")
            ],
            "arbitrator": _target("arbitrator"),
        }
    )


def _success(provider, role):
    inner = {
        "role": "quant",  # Attempt to impersonate quant, including from news.
        "rationale": "synthetic opinion",
        "confidence": "high",
        "recommendations": {"spacing_percentage": 2.5 if role in ("news", "arbitrator") else 1.0},
        "llm_attempts": [{"role": "forged", "provider": "forged", "model": "forged"}],
    }
    if provider == "ollama":
        body = {"response": json.dumps(inner), "done": True}
    elif provider == "anthropic":
        body = {
            "content": [{"type": "text", "text": json.dumps(inner)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 100},
        }
    else:
        body = {
            "choices": [{"message": {"content": json.dumps(inner)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        }
    return httpx.Response(200, json=body)


def _local_failure(kind, request):
    if kind == "timeout":
        raise httpx.ReadTimeout("synthetic timeout", request=request)
    if kind == "outage":
        return httpx.Response(503, json={"error": "synthetic outage"})
    if kind == "missing_model":
        return httpx.Response(404, json={"error": "model 'synthetic' not found"})
    if kind == "bad_envelope_json":
        return httpx.Response(200, text="synthetic non-JSON envelope")
    if kind == "bad_envelope_shape":
        return httpx.Response(200, json=[])
    if kind == "bad_answer_shape":
        return httpx.Response(200, json={"response": "[]"})
    return httpx.Response(200, json={"response": '{"recommendations": {}}'})


@pytest_asyncio.fixture
async def lab(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ATLASCLOUD_API_KEY"):
        monkeypatch.setenv(key, "test-only")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    storage = SQLiteStorageAdapter(":memory:")
    await storage.connect()
    wiring = advise._CloudWiring(
        storage=storage,
        session_tracker=SessionCostTracker(),
        llm_config=LLMConfig(
            cost=LLMCostConfig(max_spend_per_session_usd=Decimal("0.5")),
            retry=LLMRetryConfig(max_retries=1, initial_backoff_seconds=0.001),
        ),
    )
    state = {
        "success_slot": 2,
        "local_failure": None,
        "local_failure_roles": list(_ROUTES) + ["single"],
        "cap_after_primary": False,
    }
    clients, requests = [], []

    def build(adapter_cls, provider, **kwargs):
        role = kwargs["role"]
        slot = _ROUTES["quant" if role == "single" else role].index((provider, kwargs["model"]))

        def handler(request):
            requests.append((role, slot, request))
            if slot == 0 and state["cap_after_primary"]:
                wiring.session_tracker.add(Decimal("0.5"))
            if slot < state["success_slot"]:
                # Primary auth denial is not retried; backup outage is retried once.
                return httpx.Response(401 if slot == 0 else 503, json={"error": {}})
            if slot == 2 and state["local_failure"] and role in state["local_failure_roles"]:
                return _local_failure(state["local_failure"], request)
            return _success(provider, role)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=kwargs["timeout_seconds"]
        )
        clients.append(client)
        return adapter_cls(client=client, **kwargs)

    monkeypatch.setattr(
        advise,
        "AnthropicAdvisorAdapter",
        lambda **kw: build(AnthropicAdvisorAdapter, "anthropic", **kw),
    )
    monkeypatch.setattr(
        advise,
        "OpenAIAdvisorAdapter",
        lambda **kw: build(OpenAIAdvisorAdapter, "atlas" if "base_url" in kw else "openai", **kw),
    )
    monkeypatch.setattr(advise, "OllamaAdapter", lambda **kw: build(OllamaAdapter, "ollama", **kw))
    try:
        yield SimpleNamespace(storage=storage, wiring=wiring, requests=requests, state=state)
    finally:
        for client in clients:
            await client.aclose()
        await storage.close()


@pytest.mark.parametrize("success_slot", [0, 1, 2], ids=["primary", "cloud", "ollama"])
async def test_moe_stops_at_first_success_preserves_context_and_persists_actual_routes(
    lab, success_slot
):
    lab.state["success_slot"] = success_slot
    label = []
    adapter = advise._build_advisor(_config(moe=True), label, cloud_wiring=lab.wiring)
    try:
        await _seed_prices(lab.storage)
        assert await advise._run_cycle(
            adapter,
            SummaryBuilder(lab.storage),
            lab.storage,
            symbol=BTC_USD,
            metrics_lookback=timedelta(hours=24),
            news_lookback=None,
            news_limit=5,
            news_match_coin=True,
            current_grid=_default_grid(),
            model_name=label[0],
        )
        suggestion = (await lab.storage.get_advisor_suggestions())[0]
        rec = suggestion.recommendation
        assert rec.role == "aggregated" and rec.news_materially_drove
        assert [op.role for op in rec.expert_opinions] == ["quant", "risk", "news"]
        gate = evaluate_auto_apply(
            suggestion,
            GridLevels.model_validate(_default_grid().model_dump()),
            AutoApplyConfig(enabled=True),
            symbol="BTC",
        )
        assert not gate.role_eligible and not gate.applied_keys
        assert "forged" not in suggestion.model_dump_json()
        for role, route in _ROUTES.items():
            attempts = [a for a in rec.llm_attempts if a.role == role]
            assert [(a.provider, a.model) for a in attempts] == route[: success_slot + 1]
            assert [a.error_kind for a in attempts] == [
                *["authentication_error", "server_error"][:success_slot],
                None,
            ]
            assert (
                f"{role}={route[success_slot][0]}/{route[success_slot][1]}" in suggestion.model_name
            )
            sent = [(slot, request) for r, slot, request in lab.requests if r == role]
            assert [slot for slot, _ in sent] == [[0], [0, 1], [0, 1, 1, 2]][success_slot]
            cloud_user = json.loads(sent[0][1].content)["messages"][-1]["content"]
            for slot, request in sent[1:]:
                body = json.loads(request.content)
                if slot != 2:
                    assert body["messages"][-1]["content"] == cloud_user
                    continue
                assert request.url == "http://ollama.test:11434/api/generate"
                prompt = load_prompt(Path(f"config/prompts/{role}.md"))
                assert body["prompt"] == prompt.body + "\n\n" + cloud_user
                assert body["model"] == route[2][1] and body["stream"] is False
                assert body["options"] == {"temperature": 0.4, "num_predict": 2048}
                assert "confidence" in body["format"]["required"]
                assert request.extensions["timeout"]["read"] == 17
        calls = await lab.storage.get_llm_calls()
        assert len(calls) == 4 * min(success_slot + 1, 2)  # One ledger row per logical cloud call.
        assert all(call.provider != "ollama" for call in calls)
        assert len({call.trace_id for call in calls}) == 1 and calls[0].trace_id
        assert lab.wiring.session_tracker.total == sum(call.cost_usd for call in calls)
        assert sum(call.success for call in calls) == (0 if success_slot == 2 else 4)
        # A fresh evaluation restarts at the primary; no sticky fallback/cooldown.
        lab.requests.clear()
        lab.state["success_slot"] = 0
        recovered = await adapter.get_recommendation(_summary())
        assert len(recovered.llm_attempts) == 4
        assert all(slot == 0 for _, slot, _ in lab.requests)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("engine", ["llm", "cascade"])
@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "outage",
        "missing_model",
        "bad_envelope_json",
        "bad_envelope_shape",
        "bad_answer_shape",
        "bad_schema",
    ],
)
async def test_failed_ollama_ends_chain_and_cascade_retains_heuristic(lab, engine, failure):
    lab.state["local_failure"] = failure
    adapter = advise._build_advisor(_config(engine=engine), [], cloud_wiring=lab.wiring)
    try:
        if engine == "llm":
            with pytest.raises(AdvisorError):
                await adapter.get_recommendation(_summary())
        else:
            result = await adapter.get_recommendation(_summary())
            assert result.role == "heuristic" and result.recommendations == {}
        assert [slot for _, slot, _ in lab.requests] == [0, 1, 1, 2]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "cap_after_primary", [False, True], ids=["before-primary", "before-backup"]
)
async def test_bot_spend_cap_cannot_be_bypassed_by_free_ollama(lab, cap_after_primary):
    lab.state["cap_after_primary"] = cap_after_primary
    if not cap_after_primary:
        lab.wiring.session_tracker.add(Decimal("0.5"))
    adapter = advise._build_advisor(_config(), [], cloud_wiring=lab.wiring)
    try:
        with pytest.raises(LLMCostCapExceeded):
            await adapter.get_recommendation(_summary())
        assert [slot for _, slot, _ in lab.requests] == ([0] if cap_after_primary else [])
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("failed_roles", [["news"], ["arbitrator"], ["quant", "risk", "news"]])
async def test_moe_partial_failure_and_arbitrator_failure_have_distinct_outcomes(lab, failed_roles):
    lab.state.update(local_failure="outage", local_failure_roles=failed_roles)
    adapter = advise._build_advisor(
        _config(moe=True, engine="cascade"), [], cloud_wiring=lab.wiring
    )
    try:
        result = await adapter.get_recommendation(_summary())
        if failed_roles == ["news"]:
            assert result.role == "aggregated"
            assert [op.role for op in result.expert_opinions] == ["quant", "risk"]
        else:
            assert result.role == "heuristic" and result.recommendations == {}
        if len(failed_roles) == 3:
            assert not any(role == "arbitrator" for role, _, _ in lab.requests)
    finally:
        await adapter.aclose()

"""Native Cloud protocol, billing, safe failures and resource ownership (offline)."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tests.adapters.test_fallback_advisor import _summary
from wobblebot.adapters.ollama_cloud import OllamaCloudAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_failures import classify_error
from wobblebot.services.llm_retry import LLMRetryConfig
from wobblebot.services.llm_trace import llm_trace

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def response_body():
    return {
        "model": "gpt-oss:120b",
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 1000,
        "prompt_eval_cached_count": 200,
        "eval_count": 500,
        "message": {
            "role": "assistant",
            "thinking": '{"confidence":"bad"}',
            "content": json.dumps(
                {"confidence": "high", "rationale": "Hold", "recommendations": {}}
            ),
        },
    }


@pytest_asyncio.fixture
async def lab():
    storage = SQLiteStorageAdapter(":memory:")
    await storage.connect()
    clients = []
    tracker = SessionCostTracker()

    def build(handler, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        clients.append(client)
        defaults = dict(
            model="gpt-oss:120b",
            prompt=load_prompt(Path("config/prompts/quant.md")),
            role="quant",
            api_key="test-only",
            storage=storage,
            session_tracker=tracker,
            cost_config=LLMCostConfig(),
            retry_config=LLMRetryConfig(max_retries=1, initial_backoff_seconds=0.001),
            client=client,
        )
        return OllamaCloudAdvisorAdapter(**(defaults | kwargs))

    yield build, storage, tracker, clients
    for client in clients:
        await client.aclose()
    await storage.close()


async def test_native_wire_cache_billing_and_trace(lab, monkeypatch):
    build, storage, tracker, clients = lab
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://local.invalid:11434")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response_body())

    adapter = build(handler, temperature=0.4, max_tokens=4321)
    with llm_trace("ollama-test"):
        result = await adapter.get_recommendation(_summary(), extra_context="expert context")
    assert result.role == "quant"
    assert result.confidence == "high"
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://ollama.com/api/chat"
    assert request.headers["authorization"] == "Bearer test-only"
    body = json.loads(request.content)
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.4, "num_predict": 4321}
    assert "format" not in body and "keep_alive" not in body
    assert "expert context" in body["messages"][1]["content"]
    (row,) = await storage.get_llm_calls(provider="ollama_cloud")
    assert (row.tokens_in, row.tokens_cache_read, row.tokens_out) == (800, 200, 500)
    assert row.tokens_reasoning is None  # eval_count includes thinking, no double charge
    assert row.provider == "ollama_cloud" and row.trace_id == "ollama-test"
    assert row.cost_usd == tracker.total == Decimal("0.000423")
    await adapter.aclose()
    assert not clients[0].is_closed  # injected clients remain caller-owned


@pytest.mark.parametrize(
    "status,kind,attempts",
    [
        (401, "authentication_error", 1),
        (402, "billing_error", 1),
        (403, "permission_denied", 1),
        (429, "rate_limited", 2),
        (503, "server_error", 2),
        (400, "http_400", 1),
    ],
)
async def test_safe_failures_and_bounded_retries(lab, status, kind, attempts):
    build, storage, _, _ = lab
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "sensitive provider body"})

    with pytest.raises(AdvisorError) as caught:
        await build(handler).get_recommendation(_summary())
    assert "sensitive" not in str(caught.value)
    assert classify_error(caught.value) == kind
    assert len(calls) == attempts
    (row,) = await storage.get_llm_calls()
    assert row.error_kind == kind and not row.success


async def test_redirect_does_not_forward_credential(lab):
    build, _, _, _ = lab
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://untrusted.invalid/api/chat"})

    with pytest.raises(AdvisorError):
        await build(handler).get_recommendation(_summary())
    assert hosts == ["ollama.com"]


@pytest.mark.parametrize("mode", ["unpriced", "daily", "session"])
async def test_gate_or_unpriced_model_prevents_http(lab, mode):
    build, _, tracker, _ = lab
    kwargs = {}
    if mode == "unpriced":
        kwargs["model"] = "unknown-cloud-model"
    elif mode == "session":
        tracker.add(Decimal("100"))
    else:
        kwargs["cost_config"] = LLMCostConfig(max_spend_per_day_usd=Decimal("0.000001"))

    def forbid(_):
        raise AssertionError("denied call reached network")

    with pytest.raises(AdvisorError if mode == "unpriced" else LLMCostCapExceeded):
        await build(forbid, **kwargs).get_recommendation(_summary())


@pytest.mark.parametrize(
    "change",
    [
        {"done": False},
        {"done_reason": "length"},
        {"message": []},
        {"message": {"content": "not JSON"}},
        {"message": {"content": '{"confidence":"invalid"}'}},
    ],
)
async def test_invalid_output_remains_billed(lab, change):
    build, storage, tracker, _ = lab
    adapter = build(lambda _: httpx.Response(200, json=response_body() | change))
    with pytest.raises(AdvisorError):
        await adapter.get_recommendation(_summary())
    (row,) = await storage.get_llm_calls()
    assert (
        row.success
    )  # shared ledger measures successful provider call, not recommendation validity
    assert tracker.total == row.cost_usd > 0


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"error": "private"},
        {},
        response_body() | {"eval_count": None},
        response_body() | {"eval_count": True},
        response_body() | {"prompt_eval_cached_count": 1001},
    ],
)
async def test_invalid_envelopes_and_usage_are_clean_failures(lab, body):
    build, storage, _, _ = lab
    with pytest.raises(AdvisorError):
        await build(lambda _: httpx.Response(200, json=body)).get_recommendation(_summary())
    (row,) = await storage.get_llm_calls()
    assert not row.success


async def test_non_json_envelope(lab):
    build, storage, _, _ = lab
    with pytest.raises(AdvisorError, match="non-JSON"):
        await build(lambda _: httpx.Response(200, text="bad gateway page")).get_recommendation(
            _summary()
        )
    assert not (await storage.get_llm_calls())[0].success


async def test_cancellation_propagates(lab):
    build, storage, _, _ = lab

    def cancel(_):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await build(cancel).get_recommendation(_summary())
    assert await storage.get_llm_calls() == []

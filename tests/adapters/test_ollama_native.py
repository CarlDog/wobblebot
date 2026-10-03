"""Upstream-shaped boundaries: no prompt before locality, no partial answers admitted."""

import json

import httpx
import pytest

from tests.adapters.test_ollama import _make_prompt, _make_summary
from tests.adapters.test_ollama_assistant import _context
from tests.adapters.test_ollama_assistant import _operator_prompt as assistant_prompt
from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.ollama_assistant import OllamaAssistantAdapter
from wobblebot.adapters.ollama_native import model_identity, response_metrics
from wobblebot.ports.exceptions import AdvisorError, AssistantError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "model,url",
    [
        ("x:cloud", "http://localhost:11434"),
        ("x:latest", "https://ollama.com"),
        ("x:latest", "https://api.ollama.com"),
        ("x:latest", "http://user:password@localhost"),
    ],
)
def test_explicit_remote_target_refused_before_client(model, url):
    with pytest.raises(AdvisorError):
        OllamaAdapter(model=model, prompt=_make_prompt(), base_url=url)
    with pytest.raises(AssistantError):
        OllamaAssistantAdapter(model=model, prompt=assistant_prompt(), base_url=url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"remote_model": "paid"},
        {"remote_host": "https://provider"},
        {"capabilities": ["embedding"]},
        {},
    ],
)
async def test_metadata_refusal_sends_no_financial_or_operator_context(metadata):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=metadata)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OllamaAdapter(model="fixture", prompt=_make_prompt(), client=client)
        with pytest.raises(AdvisorError):
            await adapter.get_recommendation(_make_summary())
    assert len(calls) == 1 and calls[0].url.path == "/api/show"
    assert json.loads(calls[0].content) == {"model": "fixture"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {"error": "private payload"},
        {"done": False},
        {"done": True, "done_reason": "length", "message": {"content": "valid looking answer"}},
        {"done": True, "remote_host": "remote"},
    ],
)
async def test_assistant_parse_and_summary_reject_bad_envelope_with_port_error(body):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OllamaAssistantAdapter(model="fixture", prompt=assistant_prompt(), client=client)
        with pytest.raises(AssistantError):
            await adapter.parse_intent(_context())
        with pytest.raises(AssistantError):
            await adapter.summarize("system", "private content")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["json", "timeout", "queue"])
async def test_summary_errors_never_leak_retry_marker_or_raw_content(failure):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        if failure == "timeout":
            raise httpx.ReadTimeout("private content", request=request)
        return httpx.Response(503 if failure == "queue" else 200, text="private content")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OllamaAssistantAdapter(model="fixture", prompt=assistant_prompt(), client=client)
        with pytest.raises(AssistantError) as caught:
            await adapter.summarize("system", "private content")
        assert "private content" not in str(caught.value)


@pytest.mark.asyncio
async def test_exact_digest_and_version_measurement_without_inference():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        values = {
            "/api/show": {"capabilities": ["completion"]},
            "/api/version": {"version": "0.12.3"},
            "/api/tags": {"models": [{"name": "fixture:latest", "digest": "a" * 64}]},
        }
        return httpx.Response(200, json=values[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await model_identity(client, "http://localhost", "fixture", AdvisorError)
    assert identity == {"model_digest": "a" * 64, "ollama_version": "0.12.3"}
    assert paths == ["/api/show", "/api/version", "/api/tags"]


def test_metrics_are_allowlisted_and_unknown_is_not_zero():
    metrics = response_metrics(
        {"prompt_eval_count": 123, "eval_count": True, "thinking": "private", "done_reason": []}
    )
    assert metrics["prompt_eval_count"] == 123
    assert metrics["eval_count"] is None and metrics["load_duration"] is None
    assert "private" not in str(metrics)

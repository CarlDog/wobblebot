"""Provider failures must be actionable without echoing untrusted response bodies."""

from unittest.mock import AsyncMock

import httpx
import pytest

from wobblebot.domain.exceptions import LLMRetryExhausted
from wobblebot.ports.exceptions import AdvisorError, AssistantError
from wobblebot.services.llm_cloud_call import classify_error, wrap_provider_errors
from wobblebot.services.llm_failures import FAILOVER_ERROR_KINDS, failure_detail
from wobblebot.services.llm_retry import LLMRetryConfig, retry_with_backoff

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "host,path,status,message,expected",
    [
        ("ollama.com", "/api/chat", 404, "model 'gpt-oss:120b' not found", "model_unavailable"),
        ("ollama.com", "/api/chat", 404, "route missing", "http_404"),
        ("ollama.com", "/wrong", 404, "model 'gpt-oss:120b' not found", "http_404"),
        ("other.invalid", "/api/chat", 404, "model 'gpt-oss:120b' not found", "http_404"),
        ("ollama.com", "/api/chat", 400, "model 'gpt-oss:120b' not found", "http_400"),
    ],
)
def test_ollama_model_unavailability_requires_known_host_endpoint_status(
    host, path, status, message, expected
):
    request = httpx.Request("POST", f"https://{host}{path}")
    response = httpx.Response(status, request=request, json={"error": message})
    exc = httpx.HTTPStatusError("private", request=request, response=response)
    assert classify_error(exc) == expected
    assert "gpt-oss" not in failure_detail(exc)


def test_anthropic_credit_failure_has_actionable_classification() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API.",
            }
        },
    )
    error = httpx.HTTPStatusError("HTTP 400", request=request, response=response)
    assert classify_error(error) == "insufficient_credit"


def _http_error(
    status: int, body: object, host: str = "api.anthropic.com"
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"https://{host}/v1/messages")
    return httpx.HTTPStatusError(
        "unsafe sentinel",
        request=request,
        response=httpx.Response(status, json=body, request=request),
    )


@pytest.mark.parametrize(
    "status,body,kind",
    [
        (429, {"error": {"code": "insufficient_quota"}}, "quota_exceeded"),
        (400, {"error": {"code": "billing_hard_limit_reached"}}, "quota_exceeded"),
        (402, {"error": {"type": "billing_error"}}, "billing_error"),
        (401, {}, "authentication_error"),
        (403, {}, "permission_denied"),
        (429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, "rate_limited"),
        (404, {"error": {"code": "model_not_found"}}, "model_unavailable"),
        (529, {}, "server_error"),
        (504, {}, "server_error"),
    ],
)
def test_known_provider_interruptions(status: int, body: object, kind: str) -> None:
    assert classify_error(_http_error(status, body)) == kind
    assert kind in FAILOVER_ERROR_KINDS


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "unsafe sentinel",
        {"error": []},
        {"error": {"message": "unsafe sentinel", "code": []}},
        {"error": {"type": "invalid_request_error", "message": "max_tokens is invalid"}},
        {"error": {"message": "unsafe sentinel" * 20_000}},
    ],
)
def test_unrecognized_bodies_remain_bad_requests_and_never_echo(body: object) -> None:
    exc = _http_error(400, body)
    assert classify_error(exc) == "http_400"
    assert "unsafe sentinel" not in failure_detail(exc)
    assert classify_error(exc) not in FAILOVER_ERROR_KINDS


def test_anthropic_message_match_is_host_specific() -> None:
    exc = _http_error(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API.",
            }
        },
        host="example.invalid",
    )
    assert classify_error(exc) == "http_400"


def test_unread_or_non_json_response_keeps_status() -> None:
    exc = _http_error(400, {})
    exc.response = httpx.Response(400, content=b"<html>unsafe sentinel</html>", request=exc.request)
    assert classify_error(exc) == "http_400"
    exc.response = httpx.Response(
        400, stream=httpx.ByteStream(b"unsafe sentinel"), request=exc.request
    )
    assert classify_error(exc) == "http_400"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_cls", [AdvisorError, AssistantError])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_wrapping_is_actionable_and_does_not_echo(
    error_cls: type[Exception], exhausted: bool
) -> None:
    cause = _http_error(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API. unsafe sentinel",
            }
        },
    )
    error = LLMRetryExhausted(4, cause) if exhausted else cause
    with pytest.raises(error_cls, match="insufficient_credit") as caught:
        async with wrap_provider_errors("Anthropic", error_cls):
            raise error
    assert "check the provider billing balance" in str(caught.value)
    assert "unsafe sentinel" not in str(caught.value)
    assert classify_error(caught.value) == "insufficient_credit"


def test_cause_cycle_is_bounded() -> None:
    exc = AdvisorError("sentinel")
    exc.__cause__ = exc
    assert classify_error(exc) == "AdvisorError"


@pytest.mark.parametrize("status", [400, 403, 429])
def test_explicit_provider_refusal_never_permits_failover(status: int) -> None:
    exc = _http_error(
        status, {"error": {"code": "content_policy_violation", "message": "sentinel"}}
    )
    assert classify_error(exc) == "content_refused"
    assert classify_error(exc) not in FAILOVER_ERROR_KINDS


def test_deeply_nested_response_does_not_escape_classification() -> None:
    exc = _http_error(400, {})
    exc.response = httpx.Response(400, request=exc.request, content=b"[" * 2000 + b"]" * 2000)
    assert classify_error(exc) == "http_400"


@pytest.mark.asyncio
async def test_quota_denial_does_not_retry_but_rate_limit_does() -> None:
    denied = AsyncMock(side_effect=_http_error(429, {"error": {"code": "insufficient_quota"}}))
    sleep = AsyncMock()
    with pytest.raises(httpx.HTTPStatusError):
        await retry_with_backoff(denied, LLMRetryConfig(max_retries=3), sleep_fn=sleep)
    assert denied.await_count == 1
    sleep.assert_not_awaited()
    limited = AsyncMock(side_effect=_http_error(429, {"error": {"type": "rate_limit_error"}}))
    with pytest.raises(LLMRetryExhausted) as caught:
        await retry_with_backoff(limited, LLMRetryConfig(max_retries=3), sleep_fn=sleep)
    assert limited.await_count == 4
    assert classify_error(caught.value) == "rate_limited"

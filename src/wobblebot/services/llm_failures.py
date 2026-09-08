"""Safe provider failure labels shared by retries, reporting and advisor failover.

Only fixed classifications and operator hints leave this module. Provider bodies
may contain credentials, echoed prompts or arbitrary text; never persist them.
"""

from __future__ import annotations

import json
import re

import httpx

from wobblebot.domain.exceptions import LLMRetryExhausted

# Provider/account interruptions can use an explicitly configured alternative.
# Bad requests, refusals, output-validation errors, local budget denials and
# programming/storage errors must not be hidden by model substitution.
FAILOVER_ERROR_KINDS = frozenset(
    {
        "insufficient_credit",
        "quota_exceeded",
        "billing_error",
        "authentication_error",
        "permission_denied",
        "rate_limited",
        "server_error",
        "connect_error",
        "timeout",
        "transport_error",
        "model_unavailable",
    }
)

_HINTS = {
    "insufficient_credit": (
        "Provider API credit is insufficient; " "check the provider billing balance."
    ),
    "quota_exceeded": "Provider quota or spend limit reached; check usage and billing limits.",
    "billing_error": "Provider rejected billing; check payment and account status.",
    "authentication_error": "Provider authentication failed; check the configured API key.",
    "permission_denied": "Provider access denied; check key permissions and model access.",
    "rate_limited": "Provider rate limit reached; retry after backoff.",
    "server_error": "Provider is unavailable or overloaded; retry after backoff.",
    "connect_error": (
        "Cannot connect to the provider; check connectivity and service availability."
    ),
    "timeout": "Provider request timed out; check service availability and the request timeout.",
    "transport_error": "Provider connection was interrupted; retry after backoff.",
    "model_unavailable": "Configured model is unavailable; check model availability and access.",
}


_PROVIDER_CODES = {
    "content_policy_violation": "content_refused",
    "content_filter": "content_refused",
    "refusal": "content_refused",
    "insufficient_quota": "quota_exceeded",
    "billing_hard_limit_reached": "quota_exceeded",
    "usage_limit_reached": "quota_exceeded",
    "billing_error": "billing_error",
    "model_not_found": "model_unavailable",
}
_HTTP_KINDS = {
    401: "authentication_error",
    402: "billing_error",
    403: "permission_denied",
    429: "rate_limited",
}


def failure_hint(kind: str) -> str | None:
    """Return a fixed, safe operator action; unknown labels retain their old display."""
    return _HINTS.get(kind)


def failure_cause(exc: Exception) -> Exception:
    """Unwrap port/retry errors with a finite bound, including malformed cause cycles."""
    current = exc
    seen: set[int] = set()
    for _ in range(12):
        if id(current) in seen or isinstance(current, httpx.HTTPError):
            break
        seen.add(id(current))
        cause = current.last_error if isinstance(current, LLMRetryExhausted) else current.__cause__
        if not isinstance(cause, Exception):
            break
        current = cause
    return current


def _body_error_kind(exc: httpx.HTTPStatusError) -> str | None:
    """Recognize allowlisted provider codes, plus the observed Anthropic credit error.

    Parsing is bounded; non-JSON, streamed/unread and unexpected response shapes
    simply fall back to status classification. No response text is returned.
    """
    try:
        body = exc.response.content
        if len(body) > 16_384:
            return None
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError, httpx.ResponseNotRead):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    ollama_model_missing = (
        (exc.request.url.host, exc.request.url.path, exc.request.method, exc.response.status_code)
        == ("ollama.com", "/api/chat", "POST", 404)
        and isinstance(error, str)
        and re.fullmatch(r"model ['\"][^'\"\n]+['\"] not found", error)
    )
    if not isinstance(error, dict):
        return "model_unavailable" if ollama_model_missing else None
    codes = {value for key in ("code", "type") if isinstance(value := error.get(key), str)}
    for code, kind in _PROVIDER_CODES.items():
        if code in codes:
            return kind
    # Anthropic exposes this account condition as generic invalid_request_error.
    # Narrow to its actual host/type/status so arbitrary invalid payloads never
    # become permission to substitute another model.
    message = error.get("message")
    if (
        exc.request.url.host == "api.anthropic.com"
        and exc.response.status_code == 400
        and "invalid_request_error" in codes
        and isinstance(message, str)
        and message.lower().startswith("your credit balance is too low to access the anthropic api")
    ):
        return "insufficient_credit"
    return None


def classify_error(exc: Exception) -> str:
    """Stable ledger label for the actionable cause, rather than its retry wrapper."""
    cause = failure_cause(exc)
    if isinstance(cause, httpx.HTTPStatusError):
        status = cause.response.status_code
        specific = _body_error_kind(cause) if 400 <= status < 500 else None
        generic = (
            "server_error" if 500 <= status < 600 else _HTTP_KINDS.get(status, f"http_{status}")
        )
        return specific or generic
    if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connect_error"
    if isinstance(cause, httpx.TimeoutException):
        return "timeout"
    if isinstance(cause, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "transport_error"
    return type(cause).__name__


def failure_detail(exc: Exception) -> str:
    """Describe the failure without echoing exception messages or provider payloads."""
    cause = failure_cause(exc)
    kind = classify_error(exc)
    status = (
        f"HTTP {cause.response.status_code}; " if isinstance(cause, httpx.HTTPStatusError) else ""
    )
    hint = failure_hint(kind)
    return f"{status}{kind}" + (f": {hint}" if hint else "")

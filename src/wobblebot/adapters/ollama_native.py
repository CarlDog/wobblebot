"""Local native Ollama boundary: preflight, envelopes and safe observations."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from wobblebot.ports.exceptions import AdvisorError, AssistantError

PortError = type[AdvisorError] | type[AssistantError]


def require_local_target(
    model: str, base_url: str, error: PortError
) -> None:  # pylint: disable=too-many-boolean-expressions
    """Reject explicit cloud routing before any prompt or metadata request."""
    try:
        target = urlsplit(base_url)
        host = (target.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise error("Invalid local Ollama URL") from exc
    invalid_url = any(
        (
            target.scheme not in {"http", "https"},
            not host,
            target.username is not None,
            target.password is not None,
            bool(target.query),
            bool(target.fragment),
        )
    )
    remote = host == "ollama.com" or host.endswith(".ollama.com")
    if not model.strip() or "cloud" in model.lower().split(":")[-1] or invalid_url or remote:
        raise error("Local Ollama target rejected; use an explicit costed cloud provider")


def read_envelope(
    response: httpx.Response, error: PortError, *, complete: bool = True
) -> dict[str, Any]:
    """Normalize malformed, provider-error, remote and incomplete responses."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise error(f"Ollama HTTP {response.status_code}") from exc
    try:
        envelope = response.json()
    except (ValueError, UnicodeError) as exc:
        raise error("Ollama response envelope is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise error("Ollama response envelope must be a JSON object")
    if envelope.get("remote_host") or envelope.get("remote_model"):
        raise error("Local Ollama returned remote provenance; disable cloud on the server")
    if "error" in envelope:
        raise error("Ollama returned a provider error envelope")
    if complete and (envelope.get("done") is not True or envelope.get("done_reason") == "length"):
        raise error("Ollama response incomplete or truncated; check output-token cap")
    return envelope


async def local_preflight(
    client: httpx.AsyncClient, base_url: str, model: str, error: PortError
) -> dict[str, Any]:
    """Inspect model metadata without transmitting operator/financial context.

    A hostile or reconfigured server cannot be attested by its own response.
    OLLAMA_NO_CLOUD=1 and network policy on the serving host remain required.
    """
    require_local_target(model, base_url, error)
    try:
        response = await client.post(
            f"{base_url}/api/show", json={"model": model}, follow_redirects=False, timeout=10
        )
    except httpx.HTTPError as exc:
        raise error(f"Ollama preflight failed: {type(exc).__name__}") from exc
    envelope = read_envelope(response, error, complete=False)
    capabilities = envelope.get("capabilities")
    if not isinstance(capabilities, list) or "completion" not in capabilities:
        raise error("Ollama model lacks verified completion capability")
    return {"capabilities": ["completion"], "local_provenance": "observed"}


def response_metrics(envelope: dict[str, Any]) -> dict[str, int | str | None]:
    """Allowlist native metrics only; absent/invalid fields remain unknown."""
    result: dict[str, int | str | None] = {}
    for name in (
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
        "prompt_eval_cached_count",
    ):
        value = envelope.get(name)
        result[name] = (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )
    reason = envelope.get("done_reason")
    result["done_reason"] = (
        reason
        if isinstance(reason, str) and reason in {"stop", "length", "load", "unload"}
        else None
    )
    return result


async def model_identity(
    client: httpx.AsyncClient, base_url: str, model: str, error: PortError
) -> dict[str, Any]:
    """Read immutable installed-model digest/version for explicit probe receipts."""
    await local_preflight(client, base_url, model, error)
    try:
        version_response = await client.get(
            f"{base_url}/api/version", follow_redirects=False, timeout=10
        )
        tags_response = await client.get(f"{base_url}/api/tags", follow_redirects=False, timeout=10)
    except httpx.HTTPError as exc:
        raise error(f"Ollama identity failed: {type(exc).__name__}") from exc
    version = read_envelope(version_response, error, complete=False).get("version")
    models = read_envelope(tags_response, error, complete=False).get("models")
    digest = None
    canonical = model if ":" in model else f"{model}:latest"
    if isinstance(models, list):
        for entry in models:
            if isinstance(entry, dict) and entry.get("name") in {model, canonical}:
                candidate = entry.get("digest")
                if isinstance(candidate, str) and re.fullmatch(
                    r"(?:sha256:)?[0-9a-f]{64}", candidate
                ):
                    digest = candidate
    return {
        "model_digest": digest,
        "ollama_version": (
            version
            if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+_-]{1,80}", version)
            else None
        ),
    }


def request_failure(exc: httpx.HTTPError) -> str:
    """Safe transport class/status without provider payloads or request URLs."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__

"""Retry-with-backoff helper for cloud-LLM calls (Phase 6 / ADR-015).

A small pure-async utility: takes a coroutine-returning callable plus
a config, runs it, classifies any raised exception via a pluggable
classifier, and either retries (with exponential backoff) on
``transient`` or re-raises on ``permanent``. After the configured
retry budget is exhausted, raises ``LLMRetryExhausted`` chaining the
final cause.

Per ADR-015:
- Default policy is **fail loudly + retry on transient errors only**.
- Transient = HTTP 429 / 5xx + httpx connection / timeout exceptions.
- Permanent = HTTP 4xx (non-429) + every other exception class.
- Retries cap at ``max_retries`` (default 3); total attempts = 1 + retries.
- Backoff is fixed exponential: ``initial * multiplier ** attempt_index``
  for attempts 0..max_retries-1. With defaults that's 1s, 2s, 4s.
- No cross-provider failover. No silent Ollama fallback. The caller
  decides what to do with the eventual ``LLMRetryExhausted``.

The classifier is a hook so Stages 6.2-6.4 (per-provider adapters) can
override for provider-specific bodies (e.g. Anthropic's
``overloaded_error`` JSON body distinguishing rate-limit from outage).
The default classifier handles the generic-httpx shape Stage 6.1 needs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, Field

from wobblebot.domain.exceptions import LLMRetryExhausted

T = TypeVar("T")

RetryClass = Literal["transient", "permanent"]

_TRANSIENT_HTTPX_TYPES: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class LLMRetryConfig(BaseModel):
    """Backoff/retry knobs (ADR-015 decision 7)."""

    max_retries: int = Field(default=3, ge=0, le=10)
    initial_backoff_seconds: float = Field(default=1.0, gt=0.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)

    class Config:
        frozen = True


def default_classifier(exc: Exception) -> RetryClass:
    """Classify an exception per ADR-015 default policy.

    - ``httpx`` connection / timeout / protocol exceptions → transient.
    - ``httpx.HTTPStatusError``: status 429 or 5xx → transient; other
      4xx → permanent.
    - Anything else (including ValueError, KeyError, programming bugs,
      provider SDK errors that aren't httpx) → permanent. Don't retry
      bugs.
    """
    if isinstance(exc, _TRANSIENT_HTTPX_TYPES):
        return "transient"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or 500 <= status < 600:
            return "transient"
        return "permanent"
    return "permanent"


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    config: LLMRetryConfig,
    *,
    classifier: Callable[[Exception], RetryClass] = default_classifier,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``fn`` with retry-on-transient + exponential backoff.

    Total attempts = ``1 + config.max_retries``. Each retry waits
    ``config.initial_backoff_seconds * config.backoff_multiplier ** i``
    seconds (i = retry index, starting at 0).

    Args:
        fn: Zero-arg async callable producing the result. Closure
            over the actual request args; this helper is intentionally
            ignorant of what's being called.
        config: Retry knobs.
        classifier: Maps an exception to ``"transient"`` (retry) or
            ``"permanent"`` (re-raise immediately). Defaults to
            ``default_classifier``.
        sleep_fn: Async sleep injection point for tests. Production
            uses ``asyncio.sleep``.

    Returns:
        The return value of ``fn`` on the first successful attempt.

    Raises:
        Whatever ``fn`` raises if the classifier returns ``permanent``,
        with its original traceback intact.
        ``LLMRetryExhausted`` if all attempts return ``transient``.
    """
    last_error: Exception | None = None
    total_attempts = 1 + config.max_retries
    for attempt in range(total_attempts):
        try:
            return await fn()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            verdict = classifier(exc)
            if verdict == "permanent":
                raise
            last_error = exc
            if attempt == total_attempts - 1:
                # Final attempt exhausted; fall through to raise below.
                break
            backoff = config.initial_backoff_seconds * (config.backoff_multiplier**attempt)
            await sleep_fn(backoff)
    # Mypy: ``last_error`` is set because we only reach this branch
    # through the except block above.
    assert last_error is not None
    raise LLMRetryExhausted(attempts=total_attempts, last_error=last_error) from last_error

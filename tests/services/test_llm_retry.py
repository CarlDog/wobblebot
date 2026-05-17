"""Tests for ``services/llm_retry.py`` (Stage 6.1.C / ADR-015)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from wobblebot.domain.exceptions import LLMRetryExhausted
from wobblebot.services.llm_retry import (
    LLMRetryConfig,
    RetryClass,
    default_classifier,
    retry_with_backoff,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- #
# Fixtures / helpers                                                    #
# --------------------------------------------------------------------- #


class _SleepRecorder:
    """Injectable async sleep that records each delay; never actually sleeps.

    Tests assert backoff timing by reading ``self.sleeps`` rather than
    measuring wall-clock time — the production helper uses
    ``asyncio.sleep`` but the value is fully observable here.
    """

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.sleeps.append(delay)


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    """Build a synthetic ``HTTPStatusError`` for classifier tests."""
    request = httpx.Request("POST", "https://example.invalid/v1/test")
    response = httpx.Response(status_code=code, request=request)
    return httpx.HTTPStatusError(message=f"HTTP {code}", request=request, response=response)


def _make_flaky(failures: list[Exception], final: object) -> Callable[[], Awaitable[object]]:
    """Build an async callable that raises each exception in ``failures``
    on successive calls, then returns ``final`` once the list is empty."""
    remaining = list(failures)
    call_count = [0]

    async def fn() -> object:
        call_count[0] += 1
        if remaining:
            raise remaining.pop(0)
        return final

    fn.call_count = call_count  # type: ignore[attr-defined]
    return fn


# --------------------------------------------------------------------- #
# default_classifier                                                    #
# --------------------------------------------------------------------- #


class TestDefaultClassifier:
    @pytest.mark.parametrize("code", [500, 502, 503, 504, 599])
    def test_5xx_is_transient(self, code: int) -> None:
        verdict: RetryClass = default_classifier(_http_status_error(code))
        assert verdict == "transient"

    def test_429_is_transient(self) -> None:
        assert default_classifier(_http_status_error(429)) == "transient"

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 413, 422])
    def test_other_4xx_is_permanent(self, code: int) -> None:
        assert default_classifier(_http_status_error(code)) == "permanent"

    def test_connect_error_is_transient(self) -> None:
        assert default_classifier(httpx.ConnectError("dns lookup failed")) == "transient"

    def test_connect_timeout_is_transient(self) -> None:
        assert default_classifier(httpx.ConnectTimeout("timeout")) == "transient"

    def test_read_timeout_is_transient(self) -> None:
        assert default_classifier(httpx.ReadTimeout("read timed out")) == "transient"

    def test_pool_timeout_is_transient(self) -> None:
        assert default_classifier(httpx.PoolTimeout("pool drained")) == "transient"

    def test_remote_protocol_error_is_transient(self) -> None:
        # E.g. server hung up mid-response.
        assert default_classifier(httpx.RemoteProtocolError("server hung up")) == "transient"

    def test_value_error_is_permanent(self) -> None:
        assert default_classifier(ValueError("bad shape")) == "permanent"

    def test_unrelated_exception_is_permanent(self) -> None:
        class _CustomError(Exception):
            pass

        assert default_classifier(_CustomError("nope")) == "permanent"


# --------------------------------------------------------------------- #
# retry_with_backoff — success paths                                    #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestSuccessPaths:
    async def test_first_attempt_succeeds_no_retry(self) -> None:
        sleep = _SleepRecorder()
        config = LLMRetryConfig()

        async def fn() -> str:
            return "ok"

        result = await retry_with_backoff(fn, config, sleep_fn=sleep)
        assert result == "ok"
        assert sleep.sleeps == []  # no retries → no sleeps

    async def test_succeeds_after_transient_failures(self) -> None:
        sleep = _SleepRecorder()
        flaky = _make_flaky(
            failures=[
                _http_status_error(429),
                _http_status_error(503),
            ],
            final="success",
        )
        config = LLMRetryConfig(max_retries=3, initial_backoff_seconds=0.01, backoff_multiplier=2.0)
        result = await retry_with_backoff(flaky, config, sleep_fn=sleep)
        assert result == "success"
        assert flaky.call_count[0] == 3  # type: ignore[attr-defined]
        # Two failed attempts → two backoff sleeps.
        assert len(sleep.sleeps) == 2

    async def test_returns_generic_type(self) -> None:
        sleep = _SleepRecorder()

        async def fn() -> dict[str, int]:
            return {"x": 1}

        result = await retry_with_backoff(fn, LLMRetryConfig(), sleep_fn=sleep)
        assert result == {"x": 1}


# --------------------------------------------------------------------- #
# retry_with_backoff — permanent failures                               #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestPermanentFailures:
    async def test_permanent_error_raises_immediately(self) -> None:
        sleep = _SleepRecorder()
        permanent = _http_status_error(401)  # auth = permanent
        flaky = _make_flaky([permanent], final="never")

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await retry_with_backoff(flaky, LLMRetryConfig(), sleep_fn=sleep)

        assert exc_info.value.response.status_code == 401
        assert flaky.call_count[0] == 1  # type: ignore[attr-defined]
        assert sleep.sleeps == []

    async def test_value_error_treated_permanent(self) -> None:
        sleep = _SleepRecorder()
        flaky = _make_flaky([ValueError("bad json")], final="never")

        with pytest.raises(ValueError):
            await retry_with_backoff(flaky, LLMRetryConfig(), sleep_fn=sleep)

        assert flaky.call_count[0] == 1  # type: ignore[attr-defined]


# --------------------------------------------------------------------- #
# retry_with_backoff — exhaustion                                       #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestExhaustion:
    async def test_raises_llm_retry_exhausted_after_budget(self) -> None:
        sleep = _SleepRecorder()
        # 4 transient failures with max_retries=3 → 4 total attempts → exhausted.
        failures = [_http_status_error(503) for _ in range(4)]
        flaky = _make_flaky(failures, final="never")
        config = LLMRetryConfig(max_retries=3, initial_backoff_seconds=0.01, backoff_multiplier=2.0)

        with pytest.raises(LLMRetryExhausted) as exc_info:
            await retry_with_backoff(flaky, config, sleep_fn=sleep)

        assert exc_info.value.attempts == 4
        assert isinstance(exc_info.value.last_error, httpx.HTTPStatusError)
        # Original error preserved via __cause__.
        assert exc_info.value.__cause__ is exc_info.value.last_error
        # 4 attempts → 3 sleeps between them.
        assert len(sleep.sleeps) == 3

    async def test_zero_retries_means_single_attempt(self) -> None:
        sleep = _SleepRecorder()
        flaky = _make_flaky([_http_status_error(503)], final="never")
        config = LLMRetryConfig(max_retries=0, initial_backoff_seconds=0.01)

        with pytest.raises(LLMRetryExhausted) as exc_info:
            await retry_with_backoff(flaky, config, sleep_fn=sleep)

        assert exc_info.value.attempts == 1
        assert sleep.sleeps == []  # no sleeps when no retries


# --------------------------------------------------------------------- #
# Backoff timing                                                        #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestBackoffTiming:
    async def test_backoff_follows_formula(self) -> None:
        sleep = _SleepRecorder()
        # 3 transient failures + max_retries=3 means 4 attempts total
        # with 3 sleeps between them.
        failures = [_http_status_error(503) for _ in range(3)]
        flaky = _make_flaky(failures, final="success")
        config = LLMRetryConfig(max_retries=3, initial_backoff_seconds=1.0, backoff_multiplier=2.0)
        result = await retry_with_backoff(flaky, config, sleep_fn=sleep)
        assert result == "success"
        # Formula: initial * multiplier ** attempt for attempt 0..2 →
        # 1.0, 2.0, 4.0.
        assert sleep.sleeps == [1.0, 2.0, 4.0]

    async def test_custom_initial_and_multiplier(self) -> None:
        sleep = _SleepRecorder()
        failures = [_http_status_error(503) for _ in range(3)]
        flaky = _make_flaky(failures, final="success")
        config = LLMRetryConfig(max_retries=3, initial_backoff_seconds=0.5, backoff_multiplier=3.0)
        await retry_with_backoff(flaky, config, sleep_fn=sleep)
        # 0.5 * 3**0=0.5, 0.5 * 3**1=1.5, 0.5 * 3**2=4.5
        assert sleep.sleeps == [0.5, 1.5, 4.5]


# --------------------------------------------------------------------- #
# Custom classifier                                                     #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestCustomClassifier:
    async def test_classifier_can_widen_transient_set(self) -> None:
        """Provider-adapter style: classify a custom exception as transient."""
        sleep = _SleepRecorder()

        class _Overloaded(Exception):
            pass

        def widened(exc: Exception) -> RetryClass:
            if isinstance(exc, _Overloaded):
                return "transient"
            return default_classifier(exc)

        flaky = _make_flaky([_Overloaded("provider says wait")], final="ok")
        result = await retry_with_backoff(
            flaky,
            LLMRetryConfig(max_retries=2, initial_backoff_seconds=0.01),
            classifier=widened,
            sleep_fn=sleep,
        )
        assert result == "ok"
        assert flaky.call_count[0] == 2  # type: ignore[attr-defined]

    async def test_classifier_can_narrow_transient_set(self) -> None:
        """Stricter classifier marks 5xx permanent → no retries."""
        sleep = _SleepRecorder()

        def stricter(_exc: Exception) -> RetryClass:
            return "permanent"

        flaky = _make_flaky([_http_status_error(503)], final="never")
        with pytest.raises(httpx.HTTPStatusError):
            await retry_with_backoff(
                flaky,
                LLMRetryConfig(),
                classifier=stricter,
                sleep_fn=sleep,
            )
        assert flaky.call_count[0] == 1  # type: ignore[attr-defined]
        assert sleep.sleeps == []


# --------------------------------------------------------------------- #
# LLMRetryConfig                                                        #
# --------------------------------------------------------------------- #


class TestConfigValidation:
    def test_defaults(self) -> None:
        cfg = LLMRetryConfig()
        assert cfg.max_retries == 3
        assert cfg.initial_backoff_seconds == 1.0
        assert cfg.backoff_multiplier == 2.0

    def test_frozen(self) -> None:
        cfg = LLMRetryConfig()
        with pytest.raises(Exception):
            cfg.max_retries = 99  # type: ignore[misc]

    def test_negative_max_retries_rejected(self) -> None:
        with pytest.raises(Exception):
            LLMRetryConfig(max_retries=-1)

    def test_zero_backoff_rejected(self) -> None:
        with pytest.raises(Exception):
            LLMRetryConfig(initial_backoff_seconds=0.0)

    def test_multiplier_below_one_rejected(self) -> None:
        with pytest.raises(Exception):
            LLMRetryConfig(backoff_multiplier=0.5)

    def test_max_retries_upper_bound(self) -> None:
        # Sanity ceiling to prevent operator typos like 1000.
        with pytest.raises(Exception):
            LLMRetryConfig(max_retries=11)

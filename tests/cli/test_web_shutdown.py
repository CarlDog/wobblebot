"""Tests for ``cli/web._serve_async``'s shutdown path (Layer 6, 2026-09-05).

``cli/web`` already cancels-and-awaits its release-check task before
``safe_shutdown`` — but the suppression around that ``await`` used to be
narrowed to ``asyncio.CancelledError``. Awaiting an ALREADY-DEAD task
re-raises the exception it STORED, which is not ``CancelledError``, so
it propagated out of the ``finally`` **before** ``safe_shutdown`` ran.
The consequence there is worse than at ``cli/operator``'s equivalent
site: ``_close_storages`` and ``kraken_http.aclose`` are skipped
entirely, leaking the sqlite handles and the httpx connection pool.
(Latent, not live — ``check_for_update`` catches
``(httpx.HTTPError, ValueError)`` today, so nothing reachable raises.)

The seam: ``_serve_async`` is driven directly with ``_bootstrap_app``,
``_release_check_loop`` and the module's ``uvicorn`` reference replaced.
No FastAPI app and no real uvicorn server are built. The stub
``serve()`` does NOT simply return — it spins until the release-check
task is genuinely ``done()`` carrying its exception, because a
``serve()`` that returns early would leave that task PENDING, send the
``finally`` down the ordinary ``CancelledError`` branch, and let the
test pass while exercising nothing. Each test asserts the
stored-exception WARNING, the completed cleanup, and the exit code
together for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.cli import web as cli_web
from wobblebot.config.cli import WebConfig
from wobblebot.config.loader import WobbleBotConfig

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_MAX_SPINS = 400
_SPIN_SECONDS = 0.005


async def _spin_until(predicate: Callable[[], bool], *, what: str) -> None:
    """Yield to the loop until ``predicate`` holds, bounded."""
    for _ in range(_MAX_SPINS):
        if predicate():
            return
        await asyncio.sleep(_SPIN_SECONDS)
    raise AssertionError(f"timed out waiting for {what}")


def _config() -> WobbleBotConfig:
    # bind_host stays at the loopback default on purpose: a non-loopback
    # host makes _serve_async emit its own WARNING, which would pollute
    # the "exactly one warning" assertions below.
    return WobbleBotConfig(
        grid=_grid_config(),
        safety=_safety_config(),
        web=WebConfig(release_check_enabled=True, release_check_interval_hours=1.0),
    )


class _StubAdapter:
    """The one ``SQLiteStorageAdapter`` method ``_close_storages`` calls."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _StubHttpClient:
    """The one ``httpx.AsyncClient`` method the shutdown phase calls."""

    def __init__(self) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _ReleaseLoopSpy:
    """Stands in for ``_release_check_loop``.

    Records that it actually ran (an async test whose task never gets
    scheduled passes for the wrong reason) and either dies with a stored
    exception or runs until cancelled.
    """

    def __init__(self, *, exc: BaseException | None) -> None:
        self.started = False
        self.task: asyncio.Task[Any] | None = None
        self.cancelled = False
        self.cycles = 0
        self._exc = exc

    async def __call__(self, app: Any, interval_hours: float, stop_event: asyncio.Event) -> None:
        self.started = True
        self.task = asyncio.current_task()
        if self._exc is not None:
            await asyncio.sleep(0)
            raise self._exc
        try:
            while True:
                self.cycles += 1
                await asyncio.sleep(_SPIN_SECONDS)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _stub_uvicorn(serve_body: Callable[[], Any]) -> SimpleNamespace:
    """A ``uvicorn`` stand-in exposing only ``Config`` and ``Server``.

    Replaces ``cli_web.uvicorn`` (the module's own global) rather than
    patching attributes on the real uvicorn module, so nothing outside
    this test can see it.
    """

    class _Config:
        def __init__(self, app: Any, **kwargs: Any) -> None:
            self.app = app
            self.kwargs = kwargs

    class _Server:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def serve(self) -> None:
            await serve_body()

    return SimpleNamespace(Config=_Config, Server=_Server)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spy: _ReleaseLoopSpy,
    serve_body: Callable[[], Any],
) -> tuple[list[_StubAdapter], _StubHttpClient]:
    adapters = [_StubAdapter(), _StubAdapter()]
    kraken_http = _StubHttpClient()

    async def _fake_bootstrap(_cfg: WobbleBotConfig) -> Any:
        return (object(), adapters, kraken_http)

    monkeypatch.setattr(cli_web, "_bootstrap_app", _fake_bootstrap)
    monkeypatch.setattr(cli_web, "_release_check_loop", spy)
    monkeypatch.setattr(cli_web, "uvicorn", _stub_uvicorn(serve_body))
    return adapters, kraken_http


class TestDeadReleaseCheckTaskDoesNotSkipShutdown:
    async def test_stored_exception_does_not_prevent_safe_shutdown(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The regression. With the suppression narrowed to
        ``CancelledError``, the stored exception escapes the ``finally``
        before line 432 and the storages + connection pool are never
        closed."""
        spy = _ReleaseLoopSpy(exc=RuntimeError("release check exploded"))

        async def _serve() -> None:
            # Do not return until the task is genuinely dead: a pending
            # task would send the finally down the CancelledError branch
            # and this test would prove nothing.
            await _spin_until(
                lambda: spy.task is not None and spy.task.done(),
                what="the release-check task to die with a stored exception",
            )

        adapters, kraken_http = _install(monkeypatch, spy=spy, serve_body=_serve)

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.web"):
            exit_code = await cli_web._serve_async(_config())

        assert spy.started, "the release-check loop never ran"
        # All three together, or the scheduling race above is invisible.
        assert exit_code == 0
        assert [adapter.close_calls for adapter in adapters] == [1, 1], (
            "safe_shutdown never ran: the stored exception escaped the finally "
            "before the cleanup phases"
        )
        assert kraken_http.aclose_calls == 1
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1, "expected exactly one warning — the stored exception"
        record = warnings[0]
        assert getattr(record, "task", None) == "release-check"
        assert getattr(record, "error", None) == "release check exploded"
        rendered = record.getMessage()
        assert "RuntimeError" in rendered
        assert "release check exploded" in rendered

    async def test_a_dead_task_does_not_change_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deliberately NO exit-on-task-death here, unlike cli/operator:
        the release check only feeds the footer's "update available"
        indicator, so a transient GitHub failure must never bounce the
        operator's dashboard."""
        spy = _ReleaseLoopSpy(exc=ValueError("bad payload"))

        async def _serve() -> None:
            await _spin_until(
                lambda: spy.task is not None and spy.task.done(),
                what="the release-check task to die",
            )

        _install(monkeypatch, spy=spy, serve_body=_serve)

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.web"):
            exit_code = await cli_web._serve_async(_config())

        assert exit_code == 0
        assert any(getattr(r, "task", None) == "release-check" for r in caplog.records)


class TestLiveReleaseCheckTaskStillShutsDownCleanly:
    async def test_running_task_is_cancelled_and_shutdown_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ordinary path must stay quiet: cancellation is the EXPECTED
        outcome, and a warning on every Ctrl-C would bury the real one."""
        spy = _ReleaseLoopSpy(exc=None)

        async def _serve() -> None:
            # Prove the poller really is running before shutting down, so
            # "it was cancelled" is a fact about a live task.
            await _spin_until(lambda: spy.cycles > 0, what="the release-check loop to run a cycle")

        adapters, kraken_http = _install(monkeypatch, spy=spy, serve_body=_serve)

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.web"):
            exit_code = await cli_web._serve_async(_config())

        assert exit_code == 0
        assert spy.cancelled, "the still-running release-check task was never cancelled"
        assert spy.task is not None and spy.task.done()
        assert [adapter.close_calls for adapter in adapters] == [1, 1]
        assert kraken_http.aclose_calls == 1
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    async def test_disabled_release_check_still_shuts_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no task at all the ``is not None`` guard must not skip the
        cleanup — the phase list is the point, not the poller."""
        spy = _ReleaseLoopSpy(exc=None)

        async def _serve() -> None:
            await asyncio.sleep(0)

        adapters, kraken_http = _install(monkeypatch, spy=spy, serve_body=_serve)
        config = WobbleBotConfig(
            grid=_grid_config(),
            safety=_safety_config(),
            web=WebConfig(release_check_enabled=False),
        )

        exit_code = await cli_web._serve_async(config)

        assert exit_code == 0
        assert not spy.started, "release_check_enabled=False must not start the poller"
        assert [adapter.close_calls for adapter in adapters] == [1, 1]
        assert kraken_http.aclose_calls == 1

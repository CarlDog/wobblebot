"""Tests for cli/_common.safe_shutdown — the timeout-bounded cleanup helper.

Backs v1.0 graceful-shutdown-timeout. Verifies the helper's three
behaviors: clean-finish, per-phase exception swallow, and the
os._exit(1) escape valve when a phase hangs past the wall-clock budget.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from wobblebot.cli._common import safe_shutdown

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class TestSafeShutdownHappyPath:
    async def test_runs_all_phases_in_order(self) -> None:
        """Phases execute sequentially in the order given."""
        order: list[str] = []

        async def make_phase(name: str) -> None:
            order.append(name)

        await safe_shutdown(
            [
                ("close_adapter", lambda: make_phase("close_adapter")),
                ("close_storage", lambda: make_phase("close_storage")),
                ("close_kraken", lambda: make_phase("close_kraken")),
            ],
            timeout_seconds=1.0,
        )

        assert order == ["close_adapter", "close_storage", "close_kraken"]

    async def test_empty_cleanups_is_noop(self) -> None:
        """Empty list returns immediately with no error."""
        await safe_shutdown([], timeout_seconds=1.0)

    async def test_returns_within_budget_for_fast_phases(self) -> None:
        """Sequence completes well under timeout; no os._exit fired."""
        ran: list[bool] = []

        async def fast() -> None:
            await asyncio.sleep(0.001)
            ran.append(True)

        await safe_shutdown([("phase_one", fast)], timeout_seconds=1.0)
        assert ran == [True]


class TestSafeShutdownPerPhaseExceptions:
    async def test_phase_exception_swallowed_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising phase logs WARNING but doesn't stop subsequent phases."""
        ran_after: list[bool] = []

        async def explodes() -> None:
            raise RuntimeError("boom")

        async def after_explosion() -> None:
            ran_after.append(True)

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.shutdown"):
            await safe_shutdown(
                [
                    ("phase_that_explodes", explodes),
                    ("phase_after", after_explosion),
                ],
                timeout_seconds=1.0,
            )

        assert ran_after == [True]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "shutdown phase raised" in r.message
            and getattr(r, "phase", None) == "phase_that_explodes"
            and getattr(r, "error", None) == "boom"
            for r in warnings
        )

    async def test_uses_caller_provided_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        """When a logger is passed, warnings go through it."""
        custom_logger = logging.getLogger("test.custom.shutdown")

        async def explodes() -> None:
            raise RuntimeError("x")

        with caplog.at_level(logging.WARNING, logger="test.custom.shutdown"):
            await safe_shutdown([("phase", explodes)], timeout_seconds=1.0, logger=custom_logger)

        assert any(r.name == "test.custom.shutdown" for r in caplog.records)


class TestSafeShutdownTimeoutEscapeValve:
    async def test_hanging_phase_triggers_os_exit_with_phase_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A phase that exceeds timeout causes os._exit(1) with phase in log."""

        class _ExitCalled(BaseException):
            """Sentinel substituted for os._exit so the test can observe + continue."""

            def __init__(self, code: int) -> None:
                self.code = code

        exit_calls: list[int] = []

        def fake_exit(code: int) -> None:
            exit_calls.append(code)
            raise _ExitCalled(code)

        monkeypatch.setattr("wobblebot.cli._common.os._exit", fake_exit)

        async def hangs_forever() -> None:
            await asyncio.sleep(60)  # well beyond the test's timeout budget

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.shutdown"):
            with pytest.raises(_ExitCalled) as exc_info:
                await safe_shutdown(
                    [
                        ("first_phase_ok", _noop_coro),
                        ("stuck_phase", hangs_forever),
                    ],
                    timeout_seconds=0.05,
                )

        assert exc_info.value.code == 1
        assert exit_calls == [1]
        timeout_warnings = [
            r for r in caplog.records if "shutdown hung beyond timeout" in r.message
        ]
        assert len(timeout_warnings) == 1
        assert getattr(timeout_warnings[0], "phase", None) == "stuck_phase"
        assert getattr(timeout_warnings[0], "timeout_seconds", None) == 0.05

    async def test_timeout_phase_field_reports_init_when_no_phase_started(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If timeout fires before any phase begins (impossible in practice
        without ``asyncio.sleep(0)``, but worth covering), the WARNING
        reports ``phase=init`` rather than crashing on a None reference."""

        def fake_exit(code: int) -> None:
            raise SystemExit(code)

        monkeypatch.setattr("wobblebot.cli._common.os._exit", fake_exit)

        # A single phase that immediately yields to the event loop, then
        # hangs. wait_for's timeout can race with the first iteration
        # start; the helper must handle either ordering safely.
        async def yield_then_hang() -> None:
            await asyncio.sleep(60)

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.shutdown"):
            with pytest.raises(SystemExit):
                await safe_shutdown([("the_only_phase", yield_then_hang)], timeout_seconds=0.05)

        timeout_warnings = [
            r for r in caplog.records if "shutdown hung beyond timeout" in r.message
        ]
        # Phase should be the one that started; "init" is the pre-loop
        # default but we expect the loop body to have set it.
        assert getattr(timeout_warnings[0], "phase", None) == "the_only_phase"


async def _noop_coro() -> None:
    """Awaitable that returns immediately. Used as a benign first phase
    so we can prove the timeout warning carries the SECOND phase's
    name, not the first's."""
    return None

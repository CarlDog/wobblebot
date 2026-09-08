"""Tests for cli/operator's background-task supervision (2026-09-05 incident).

A NAS upstream-DNS outage killed ``cli/operator``'s notification
forwarder at 11:57:20 UTC on 2026-09-05. The daemon stayed up for a
further 10h11m with the forwarder dead, because:

- ``_forwarder_loop``'s bare ``finally`` logged the SAME routine INFO
  line for a fatal death that it logs for a clean shutdown, so the
  fatal event rendered as a normal one; and
- ``_main_async`` did nothing but ``await stop_event.wait()``, so a task
  that died was never observed at all — its exception sat unretrieved
  while the process stayed alive and `restart: unless-stopped` (which
  acts on process exit, not on the healthcheck) had no reason to fire.

These tests pin the three module-level helpers that fix it. The companion
``test_operator_task_wiring.py`` now exercises the real ``_main_async``
construction with offline transport/loop seams and the real supervisor,
including the required versus one-shot task roles and process exit.

That leaves exactly one link the helpers cannot cover — that a returned
dead task becomes a non-zero process exit — so
``test_returned_task_is_wired_to_a_non_zero_exit`` pins it structurally
against ``_main_async``'s own source rather than asserting it in prose.
Every test here was mutation-verified (11 mutants, all caught).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from wobblebot.adapters.discord_transport import DiscordTransport
from wobblebot.cli import operator
from wobblebot.cli.operator import (
    _cancel_background_tasks,
    _forwarder_loop,
    _supervise_background_tasks,
)
from wobblebot.ports.notifier import PersistedNotification

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Hard cap on every "wait until the loop has actually run" spin, so a
# regression fails the test instead of hanging the suite (the project's
# bounded-watcher rule).
_MAX_SPINS = 400
_SPIN_SECONDS = 0.005


class _StubStorage:
    """The two StoragePort methods ``_forwarder_loop``'s cycle touches.

    ``get_notifications`` counts its calls so a test can prove at least
    one full cycle ran before asserting — an async test that never lets
    its loop schedule passes for the wrong reason.
    """

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.heartbeats = 0
        self.get_calls = 0
        self._fail_on_call = fail_on_call

    async def upsert_daemon_heartbeat(self, daemon_name: str, when: datetime) -> None:
        self.heartbeats += 1

    async def get_notifications(
        self, forwarded: bool | None = None, limit: int | None = None
    ) -> list[PersistedNotification]:
        self.get_calls += 1
        if self._fail_on_call is not None and self.get_calls >= self._fail_on_call:
            # Deliberately NOT a StorageError: the incident's exception
            # was a raw aiohttp error that no handler in this module
            # claims, which is the whole point.
            raise RuntimeError("synthetic upstream failure")
        return []


def _saw_one_shot(caplog: pytest.LogCaptureFixture, name: str) -> bool:
    """True once the supervisor has logged that it observed ``name`` finish.

    This is the "prove a cycle actually ran" hook for the two clean-path
    supervisor tests. Both of them assert that the supervisor did NOT
    return, which a supervisor whose loop body never got scheduled also
    satisfies; requiring this log line first means the supervisor
    demonstrably woke, inspected the finished task, and chose to keep
    waiting.
    """
    return any(
        "completed (one-shot)" in record.getMessage() and name in record.getMessage()
        for record in caplog.records
    )


async def _spin_until(predicate: Any, *, what: str) -> None:
    """Yield to the loop until ``predicate()`` is true, or fail loudly."""
    for _ in range(_MAX_SPINS):
        if predicate():
            return
        await asyncio.sleep(_SPIN_SECONDS)
    raise AssertionError(f"timed out waiting for {what}")


async def _supervise_bounded(
    *,
    must_run: tuple[asyncio.Task[Any], ...],
    one_shot: tuple[asyncio.Task[Any], ...],
    stop_event: asyncio.Event,
) -> asyncio.Task[Any] | None:
    """``_supervise_background_tasks`` with a hard deadline.

    Every caller below expects a failure to be detected in milliseconds
    and never sets ``stop_event``, so a supervisor that does not detect
    it would block forever. The deadline turns that into a red test
    instead of a hung suite — which is what the pre-fix
    ``await stop_event.wait()`` does.
    """
    return await asyncio.wait_for(
        _supervise_background_tasks(must_run=must_run, one_shot=one_shot, stop_event=stop_event),
        timeout=5.0,
    )


async def _forever(stop: asyncio.Event) -> None:
    """Stand-in for a poll loop / the Discord gateway: outlives everything."""
    await stop.wait()


async def _completes(delay: float = 0.01) -> None:
    """Stand-in for the one-shot history backfill."""
    await asyncio.sleep(delay)


async def _raises(delay: float = 0.01) -> None:
    async def _inner() -> None:
        await asyncio.sleep(delay)
        raise RuntimeError("synthetic task death")

    await _inner()


def _dead_task() -> asyncio.Task[None]:
    """A task that is already done carrying a stored exception."""

    async def _boom() -> None:
        raise RuntimeError("already dead")

    return asyncio.create_task(_boom(), name="operator-forwarder")


# --------------------------------------------------------------------- #
# Layer 3 — a loop that DIES must not log like a loop that STOPS        #
# --------------------------------------------------------------------- #


class TestForwarderLoopDeathIsLoud:
    async def test_death_logs_error_and_propagates(self, caplog: pytest.LogCaptureFixture) -> None:
        """The 2026-09-05 shape: the loop dies and must say so at ERROR.

        The old ``finally`` emitted "notification forwarder stopped" at
        INFO on this path — indistinguishable from a clean Ctrl-C, which
        is why 10h of downtime read as routine in the container log.
        """
        storage = _StubStorage(fail_on_call=2)
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.operator"):
            with pytest.raises(RuntimeError, match="synthetic upstream failure"):
                await _forwarder_loop(
                    storage=storage,  # type: ignore[arg-type]
                    transport=MagicMock(spec=DiscordTransport),
                    channel_id="C-1",
                    poll_seconds=0.005,
                    stop_event=stop,
                )
        # Proves the loop really cycled rather than dying on entry.
        assert storage.get_calls == 2
        assert storage.heartbeats == 2

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "the forwarder died and nothing was logged at ERROR"
        message = errors[0].getMessage()
        assert "DIED" in message
        assert "RuntimeError" in message
        assert "synthetic upstream failure" in message
        # And it must NOT also claim the routine clean-stop outcome.
        assert "notification forwarder stopped" not in [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        ]

    async def test_clean_stop_logs_info_and_no_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other half of the gate: a clean stop stays INFO.

        Without this, "log ERROR when it ends" would pass the test above
        while screaming on every shutdown.
        """
        storage = _StubStorage()
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.operator"):
            task = asyncio.create_task(
                _forwarder_loop(
                    storage=storage,  # type: ignore[arg-type]
                    transport=MagicMock(spec=DiscordTransport),
                    channel_id="C-1",
                    poll_seconds=0.005,
                    stop_event=stop,
                )
            )
            await _spin_until(lambda: storage.get_calls >= 1, what="the first forwarder cycle")
            stop.set()
            await task

        assert storage.get_calls >= 1
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert "notification forwarder stopped" in [r.getMessage() for r in caplog.records]


# --------------------------------------------------------------------- #
# Layer 4 — supervision, with per-task gates                            #
# --------------------------------------------------------------------- #


class TestSupervisorCleanShutdown:
    async def test_returns_none_when_stop_event_set(self) -> None:
        stop = asyncio.Event()
        loops = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(3)]
        supervisor = asyncio.create_task(
            _supervise_background_tasks(must_run=tuple(loops), one_shot=(), stop_event=stop)
        )
        # Prove the supervisor is genuinely parked, not already returned.
        await asyncio.sleep(0.02)
        assert not supervisor.done()
        stop.set()
        assert await asyncio.wait_for(supervisor, timeout=5.0) is None
        await _cancel_background_tasks(tuple(loops))

    async def test_pending_tasks_are_never_inspected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression guard: ``Task.exception()`` on a PENDING task raises
        ``InvalidStateError``. Iterating the full input set instead of the
        finished ones would blow up here on the very first wakeup."""
        stop = asyncio.Event()
        loops = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(4)]
        one_shot = asyncio.create_task(_completes(0.01), name="operator-history-backfill")
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.operator"):
            supervisor = asyncio.create_task(
                _supervise_background_tasks(
                    must_run=tuple(loops), one_shot=(one_shot,), stop_event=stop
                )
            )
            # The one-shot completing forces a wakeup with four still pending.
            await _spin_until(lambda: one_shot.done(), what="the one-shot to finish")
            # Proves the supervisor really woke on that completion and
            # inspected it — without this the test would also pass against
            # a supervisor whose loop body the event loop never scheduled.
            await _spin_until(
                lambda: _saw_one_shot(caplog, "operator-history-backfill"),
                what="the supervisor to observe the one-shot",
            )
            assert not supervisor.done()
            stop.set()
            assert await asyncio.wait_for(supervisor, timeout=5.0) is None
        await _cancel_background_tasks((*loops, one_shot))


class TestSupervisorPerTaskGates:
    async def test_one_shot_clean_completion_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The backfill task finishes NORMALLY seconds after every boot.

        A naive FIRST_COMPLETED supervisor treats that as a death and
        exits the daemon on every single startup — the single most likely
        way to ship this fix as a boot crash.
        """
        stop = asyncio.Event()
        loops = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(3)]
        gateway = asyncio.create_task(_forever(stop), name="operator-gateway")
        backfill = asyncio.create_task(_completes(0.01), name="operator-history-backfill")
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.operator"):
            supervisor = asyncio.create_task(
                _supervise_background_tasks(
                    must_run=(*loops, gateway), one_shot=(backfill,), stop_event=stop
                )
            )
            await _spin_until(lambda: backfill.done(), what="the backfill to finish")
            assert backfill.exception() is None
            # "It ignored the completion" is only meaningful if it SAW the
            # completion. A supervisor that merely parked on stop_event
            # would otherwise satisfy every other assertion here.
            await _spin_until(
                lambda: _saw_one_shot(caplog, "operator-history-backfill"),
                what="the supervisor to observe the backfill",
            )
            await asyncio.sleep(0.05)
            assert not supervisor.done(), "supervisor woke on the backfill's CLEAN completion"
            assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

            stop.set()
            assert await asyncio.wait_for(supervisor, timeout=5.0) is None
        await _cancel_background_tasks((*loops, gateway, backfill))

    async def test_gateway_clean_completion_is_a_failure(self) -> None:
        """Opposite gate: ``transport.start()`` returning at all means the
        Discord Gateway connection is gone, even without an exception."""
        stop = asyncio.Event()
        loops = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(3)]
        gateway = asyncio.create_task(_completes(0.01), name="operator-gateway")
        failed = await _supervise_bounded(must_run=(*loops, gateway), one_shot=(), stop_event=stop)
        assert failed is gateway
        assert not stop.is_set(), "the failure path must not be a disguised clean shutdown"
        await _cancel_background_tasks((*loops, gateway))

    async def test_one_shot_exceptional_completion_is_a_failure(self) -> None:
        """Clean completion is ignored; a raised exception still is not."""
        stop = asyncio.Event()
        loops = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(3)]
        backfill = asyncio.create_task(_raises(0.01), name="operator-history-backfill")
        failed = await _supervise_bounded(
            must_run=tuple(loops), one_shot=(backfill,), stop_event=stop
        )
        assert failed is backfill
        await _cancel_background_tasks((*loops, backfill))

    async def test_dead_loop_is_reported_and_named(self, caplog: pytest.LogCaptureFixture) -> None:
        """The incident itself: a poll loop raises and the daemon must
        learn about it. The returned task is what drives ``exit_code = 1``
        in ``_main_async``, so a non-``None`` return IS the non-zero exit."""
        stop = asyncio.Event()
        healthy = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(2)]
        gateway = asyncio.create_task(_forever(stop), name="operator-gateway")
        dying = asyncio.create_task(_raises(0.02), name="operator-forwarder")
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.operator"):
            failed = await _supervise_bounded(
                must_run=(*healthy, gateway, dying), one_shot=(), stop_event=stop
            )
        assert failed is dying
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "a supervised task died and nothing was logged at ERROR"
        assert "operator-forwarder" in errors[0].getMessage()
        assert "synthetic task death" in errors[0].getMessage()
        # The healthy tasks must still be alive at the moment of detection —
        # the supervisor observes, it does not tear down.
        assert all(not t.done() for t in (*healthy, gateway))
        await _cancel_background_tasks((*healthy, gateway, dying))

    async def test_returned_task_is_wired_to_a_non_zero_exit(self) -> None:
        """The half of "a death exits non-zero" that lives in ``_main_async``.

        (``async`` only because this module's ``pytestmark`` applies the
        asyncio marker to every test; it awaits nothing.)

        The test above proves the supervisor RETURNS the dead task; on its
        own that is only a return value. Turning it into a process exit is
        ``_main_async``'s job, and nothing in the suite can drive
        ``_main_async`` (it needs a config with an ``operator:`` section, a
        Discord transport and up to seven storages). Rather than assert the
        link in a docstring and leave it unpinned, this reads the real
        function's own source: the supervisor's result must be tested and
        must set ``exit_code = 1``. Structural, not substring — and rooted
        in the function OBJECT, so a rename breaks the import instead of
        quietly scanning nothing.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(operator._main_async))).body[0]
        assert isinstance(tree, ast.AsyncFunctionDef)

        result_names = {
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and isinstance(node.value.value.func, ast.Name)
            and node.value.value.func.id == "_supervise_background_tasks"
        }
        assert result_names, "_main_async never awaits _supervise_background_tasks"

        wired = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and result_names & {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            and any(
                isinstance(inner, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "exit_code" for t in inner.targets)
                and isinstance(inner.value, ast.Constant)
                # `type(...) is int` so a stray `exit_code = True` (which
                # `== 1` would happily accept) cannot satisfy this.
                and type(inner.value.value) is int  # pylint: disable=unidiomatic-typecheck
                and inner.value.value == 1
                for stmt in node.body
                for inner in ast.walk(stmt)
            )
        ]
        assert wired, (
            "the supervisor's returned task is not converted into exit_code = 1; "
            "a dead background task would leave the daemon exiting 0 and the "
            "container's restart policy would never fire"
        )


# --------------------------------------------------------------------- #
# Layer 5 — the cancel loop must survive an already-dead task           #
# --------------------------------------------------------------------- #


class TestCancelBackgroundTasks:
    async def test_dead_task_does_not_block_the_others(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The pre-fix loop caught only ``CancelledError``, so awaiting an
        already-dead task re-raised its STORED exception, escaped the loop,
        and left every task after it running. The dead one is placed FIRST
        here so that regression is caught deterministically."""
        stop = asyncio.Event()
        dead = _dead_task()
        await _spin_until(lambda: dead.done(), what="the dead task to store its exception")
        others = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(3)]
        gateway = asyncio.create_task(_forever(stop), name="operator-gateway")

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.operator"):
            # Must NOT raise.
            await _cancel_background_tasks((dead, *others, gateway))

        for task in (*others, gateway):
            assert task.cancelled(), f"{task.get_name()} was never cancelled"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "the stored exception was swallowed without a word"
        assert "operator-forwarder" in warnings[0].getMessage()
        assert "already dead" in warnings[0].getMessage()

    async def test_calling_twice_is_safe(self) -> None:
        """``_close_transport_with_cap`` already cancels+awaits the gateway
        on the clean path, and the daemon's final phase list covers all
        five — so the gateway is reached twice by design."""
        stop = asyncio.Event()
        tasks = tuple(asyncio.create_task(_forever(stop), name=f"task-{i}") for i in range(5))
        await _cancel_background_tasks(tasks)
        assert all(t.cancelled() for t in tasks)
        # Second pass must be a no-op, not a crash.
        await _cancel_background_tasks(tasks)
        assert all(t.cancelled() for t in tasks)

    async def test_clean_tasks_produce_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Cancellation is the EXPECTED outcome and must stay silent, or
        every Ctrl-C prints five warnings and the real one gets lost."""
        stop = asyncio.Event()
        running = [asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(3)]
        finished = asyncio.create_task(_completes(0.01), name="operator-history-backfill")
        await _spin_until(lambda: finished.done(), what="the one-shot to finish")
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.operator"):
            await _cancel_background_tasks((*running, finished))
        assert not caplog.records


# --------------------------------------------------------------------- #
# The supervise + cancel sequence must leave nothing running            #
# --------------------------------------------------------------------- #


class TestNoTaskLeak:
    async def test_supervise_then_cancel_leaves_no_task_behind(self) -> None:
        """Measured, not inferred from the absence of a log line.

        ``_supervise_background_tasks`` wraps ``stop_event.wait()`` in a
        Task of its own and cancels it in a ``finally`` WITHOUT awaiting
        it, so on the failure path that task is merely *cancel-requested*
        when the helper returns. Probed on this interpreter: it is left in
        ``cancelling`` state for one tick and then reaped — so this
        asserts the end state of the real daemon sequence (supervise, then
        cancel everything) rather than the instant the helper returns.

        Grepping stderr for "Task was destroyed but it is pending!" is
        NOT a substitute: that message is emitted from ``Task.__del__``
        via the asyncio exception handler, so it is GC-timing dependent
        and its absence proves nothing.
        """
        stop = asyncio.Event()
        loops = tuple(asyncio.create_task(_forever(stop), name=f"loop-{i}") for i in range(3))
        dying = asyncio.create_task(_raises(0.01), name="operator-forwarder")
        mine = {*loops, dying}
        before = asyncio.all_tasks()

        failed = await _supervise_bounded(must_run=(*loops, dying), one_shot=(), stop_event=stop)
        assert failed is dying, "the supervisor must have actually run and detected the death"

        await _cancel_background_tasks(tuple(mine))
        await asyncio.sleep(0)

        current = asyncio.current_task()
        strays = {
            task
            for task in asyncio.all_tasks()
            if task not in before and task is not current and task not in mine
        }
        assert not strays, (
            "the supervise+cancel sequence leaked " f"{[(t.get_name(), t.done()) for t in strays]}"
        )
        assert all(task.done() for task in mine)

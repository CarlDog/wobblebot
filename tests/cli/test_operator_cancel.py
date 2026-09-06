"""Tests for ``cli/operator._cancel_background_tasks`` (Layer 5, 2026-09-05).

The shutdown path's only job is to leave nothing running. Before
2026-09-05 it could not do that: the cancel loop lived inline in
``_main_async``'s ``finally`` as

    for task in (...):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

and ``await`` on a task that is ALREADY DONE re-raises the exception it
STORED — which is not ``CancelledError``. That exception escaped the
loop, so every task positioned after the dead one was never cancelled at
all. The 2026-09-05 NAS DNS outage killed the notification forwarder
that way; Layer 4's supervisor now makes "a supervised task is already
dead when the cancel loop runs" the ROUTINE shutdown path, which turns
that latent bug into a live one.

These tests therefore put the dead task FIRST and assert the SURVIVORS
were actually cancelled — not merely that nothing propagated. A test
that only asserted "did not raise" passes against the pre-fix loop for
the first task and says nothing about the other four.

The stored exception used here is a real
``aiohttp.ClientConnectorDNSError``: the incident's own exception type,
and an ``OSError`` subclass, so it also pins that the helper's catch is
wide enough for the error that actually escaped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey

from wobblebot.cli.operator import _cancel_background_tasks

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Hard cap on every "wait until it actually happened" spin, so a
# regression fails the test instead of hanging the suite (the project's
# bounded-watcher rule).
_MAX_SPINS = 400
_SPIN_SECONDS = 0.005


async def _spin_until(predicate: Callable[[], bool], *, what: str) -> None:
    """Yield to the loop until ``predicate`` holds, bounded.

    An async test that asserts without ever letting its tasks be
    scheduled passes for the wrong reason, so every assertion below is
    gated on a real observation first.
    """
    for _ in range(_MAX_SPINS):
        if predicate():
            return
        await asyncio.sleep(_SPIN_SECONDS)
    raise AssertionError(f"timed out waiting for {what}")


def _dns_error(host: str = "discord.com") -> aiohttp.ClientConnectorDNSError:
    """Build the incident's real exception.

    ``ConnectionKey`` needs every field populated — passing ``None`` for
    the key raises ``AttributeError`` from ``str(exc)`` instead, which
    would make the test assert on the wrong error entirely.
    """
    key = ConnectionKey(
        host=host,
        port=443,
        is_ssl=True,
        ssl=None,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
    )
    return aiohttp.ClientConnectorDNSError(key, OSError("Name or service not known"))


class _RunCounter:
    """Counts a task's completed cycles so a test can prove it ran."""

    def __init__(self) -> None:
        self.ticks = 0


async def _forever(counter: _RunCounter) -> None:
    """A poll-loop stand-in: runs until cancelled, recording each cycle."""
    while True:
        counter.ticks += 1
        await asyncio.sleep(_SPIN_SECONDS)


async def _dies(exc: BaseException) -> None:
    """Start, yield to the loop, then die — a genuinely-run dead task."""
    await asyncio.sleep(0)
    raise exc


async def _make_dead_task(name: str, exc: BaseException) -> asyncio.Task[Any]:
    """Create a task that finishes with ``exc`` stored on it, and wait for
    that state to be real before handing it over."""
    task: asyncio.Task[Any] = asyncio.create_task(_dies(exc), name=name)
    await _spin_until(task.done, what=f"{name} to store its exception")
    assert not task.cancelled()
    return task


async def _make_live_tasks(names: list[str]) -> tuple[list[asyncio.Task[Any]], list[_RunCounter]]:
    """Create running tasks and prove each has completed a cycle."""
    counters = [_RunCounter() for _ in names]
    tasks = [
        asyncio.create_task(_forever(counter), name=name)
        for name, counter in zip(names, counters, strict=True)
    ]
    await _spin_until(
        lambda: all(counter.ticks > 0 for counter in counters),
        what="every survivor task to run at least one cycle",
    )
    return tasks, counters


class TestDeadTaskDoesNotStrandTheOthers:
    async def test_survivors_are_cancelled_when_the_first_task_is_dead(self) -> None:
        """The discriminating case. Dead task FIRST, four live tasks after
        it — exactly the daemon's five-task shutdown list with the
        forwarder (the task the incident killed) at the front."""
        dead = await _make_dead_task("operator-forwarder", _dns_error())
        survivors, counters = await _make_live_tasks(
            [
                "operator-ttl-expirer",
                "operator-heartbeat-alerts",
                "operator-history-backfill",
                "operator-gateway",
            ]
        )
        ticks_before_cancel = [counter.ticks for counter in counters]

        # Must not raise: the stored exception is the helper's problem.
        await _cancel_background_tasks((dead, *survivors))

        for task, ticks in zip(survivors, ticks_before_cancel, strict=True):
            assert ticks > 0, f"{task.get_name()} never ran, so cancelling it proves nothing"
            assert task.cancelled(), (
                f"{task.get_name()} was left RUNNING after the cancel helper returned — "
                "the dead task's stored exception escaped the loop"
            )
        # Nothing pending: the helper awaited everything it cancelled.
        assert all(task.done() for task in (dead, *survivors))
        # The dead task's own outcome is untouched (not turned into a
        # cancellation), and its exception was retrieved — an unretrieved
        # one is exactly the zombie the incident was.
        assert not dead.cancelled()
        assert isinstance(dead.exception(), aiohttp.ClientConnectorDNSError)

    async def test_every_survivor_is_cancelled_when_two_tasks_are_dead(self) -> None:
        """ "Cancels ALL" is the literal requirement: a helper that stopped
        at the FIRST stored exception would strand tasks 3-5 here."""
        dead_first = await _make_dead_task("operator-forwarder", _dns_error())
        dead_second = await _make_dead_task("operator-ttl-expirer", RuntimeError("boom"))
        survivors, counters = await _make_live_tasks(
            ["operator-heartbeat-alerts", "operator-history-backfill", "operator-gateway"]
        )
        assert all(counter.ticks > 0 for counter in counters)

        await _cancel_background_tasks((dead_first, dead_second, *survivors))

        for task in survivors:
            assert task.cancelled(), f"{task.get_name()} was never cancelled"
        assert all(task.done() for task in (dead_first, dead_second, *survivors))

    async def test_survivors_are_cancelled_when_the_dead_task_is_last(self) -> None:
        """Position-independence: the helper must not depend on the dead
        task being reached first or last."""
        survivors, _ = await _make_live_tasks(["operator-forwarder", "operator-ttl-expirer"])
        dead = await _make_dead_task("operator-gateway", _dns_error())

        await _cancel_background_tasks((*survivors, dead))

        assert all(task.cancelled() for task in survivors)
        assert dead.done() and not dead.cancelled()


class TestStoredExceptionIsReported:
    async def test_stored_exception_is_logged_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silently swallowing the stored exception would replace one
        zombie with another: the daemon would exit cleanly and the reason
        it died would never reach the log."""
        exc = _dns_error()
        dead = await _make_dead_task("operator-forwarder", exc)
        survivors, _ = await _make_live_tasks(["operator-gateway"])

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.operator"):
            await _cancel_background_tasks((dead, *survivors))

        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1, "expected exactly one warning — one per dead task"
        record = warnings[0]
        # Structured fields first: these are what a JSON log consumer
        # reads, and they survive a wording change to the message.
        assert getattr(record, "task", None) == "operator-forwarder"
        assert getattr(record, "error_type", None) == "ClientConnectorDNSError"
        assert getattr(record, "error", None) == str(exc)
        # The rendered line must identify the task and the failure type;
        # both come from the helper's own formatting, not from anything
        # the test supplied to pytest.
        rendered = record.getMessage()
        assert "operator-forwarder" in rendered
        assert "ClientConnectorDNSError" in rendered
        assert str(exc) in rendered
        assert all(task.cancelled() for task in survivors)

    async def test_each_dead_task_gets_its_own_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dead_first = await _make_dead_task("operator-forwarder", _dns_error())
        dead_second = await _make_dead_task("operator-heartbeat-alerts", RuntimeError("boom"))
        survivors, _ = await _make_live_tasks(["operator-gateway"])

        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.operator"):
            await _cancel_background_tasks((dead_first, dead_second, *survivors))

        named = {
            getattr(record, "task", None)
            for record in caplog.records
            if record.levelno == logging.WARNING
        }
        assert named == {"operator-forwarder", "operator-heartbeat-alerts"}
        assert all(task.cancelled() for task in survivors)

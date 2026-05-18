"""Tests for cli/_common.run_poll_loop — the shared daemon loop helper (Stage 8.0.C)."""

from __future__ import annotations

import asyncio

import pytest

from wobblebot.cli._common import run_poll_loop

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class TestRunPollLoop:
    async def test_runs_do_one_cycle_until_stop_event_set(self) -> None:
        """Loop exits when stop_event is set between cycles."""
        stop = asyncio.Event()
        cycles = 0

        async def do_one() -> None:
            nonlocal cycles
            cycles += 1
            if cycles >= 3:
                stop.set()

        await run_poll_loop(do_one, interval_seconds=0.001, stop_event=stop)
        assert cycles == 3

    async def test_does_nothing_if_stop_already_set(self) -> None:
        """Pre-set stop_event keeps do_one_cycle from running at all."""
        stop = asyncio.Event()
        stop.set()
        cycles = 0

        async def do_one() -> None:
            nonlocal cycles
            cycles += 1

        await run_poll_loop(do_one, interval_seconds=0.001, stop_event=stop)
        assert cycles == 0

    async def test_propagates_cycle_exception(self) -> None:
        """do_one_cycle exceptions are NOT caught; caller handles them."""
        stop = asyncio.Event()

        async def explodes() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await run_poll_loop(explodes, interval_seconds=0.001, stop_event=stop)

    async def test_stop_event_interrupts_sleep_promptly(self) -> None:
        """Setting stop_event mid-interval causes the loop to exit
        immediately rather than waiting out the timeout."""
        stop = asyncio.Event()
        cycles = 0

        async def do_one() -> None:
            nonlocal cycles
            cycles += 1

        async def signal_stop_after(delay: float) -> None:
            await asyncio.sleep(delay)
            stop.set()

        # interval=10s but we set stop after 50ms. Loop should exit
        # within ~100ms total, not wait 10s.
        await asyncio.gather(
            run_poll_loop(do_one, interval_seconds=10.0, stop_event=stop),
            signal_stop_after(0.05),
        )
        # One cycle ran (before the stop arrived during sleep), then exit.
        assert cycles == 1

    async def test_consecutive_cycles_respect_interval(self) -> None:
        """At least the configured interval elapses between cycle starts."""
        stop = asyncio.Event()
        cycles = 0

        async def do_one() -> None:
            nonlocal cycles
            cycles += 1
            if cycles >= 3:
                stop.set()

        loop = asyncio.get_running_loop()
        start = loop.time()
        await run_poll_loop(do_one, interval_seconds=0.02, stop_event=stop)
        elapsed = loop.time() - start
        # 3 cycles at 20ms interval = >= 40ms total (2 sleeps between
        # cycles + small overhead). Generous upper bound to avoid
        # CI flake.
        assert 0.035 < elapsed < 1.0, elapsed

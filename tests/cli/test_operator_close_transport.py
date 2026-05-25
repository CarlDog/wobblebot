"""Tests for cli/operator._close_transport_with_cap.

Regression coverage for the 2026-05-25 shutdown audit: cli/operator's
``await transport.close()`` was outside ``safe_shutdown``'s timeout
protection, leading to 20-30s perceived hangs on Ctrl-C while
discord.py drained its Gateway websocket. The helper now wraps the
close in ``asyncio.wait_for`` with a 10s cap and cancels the gateway
task on overrun.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from wobblebot.cli.operator import _close_transport_with_cap

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeTransport:
    """Minimal DiscordTransport surface: only the methods the helper uses."""

    def __init__(self, *, close_seconds: float = 0.0) -> None:
        self._close_seconds = close_seconds
        self.close_called = False
        self.close_completed = False

    async def close(self) -> None:
        self.close_called = True
        if self._close_seconds > 0:
            await asyncio.sleep(self._close_seconds)
        self.close_completed = True


async def _make_gateway_task(*, runs_forever: bool = True) -> asyncio.Task[Any]:
    """Create a fake gateway task. Default keeps running until cancelled,
    mirroring discord.py's transport.start() shape."""

    async def _gateway() -> None:
        if runs_forever:
            try:
                while True:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                raise
        # else: completes immediately

    return asyncio.create_task(_gateway())


class TestCloseTransportHappyPath:
    async def test_close_returns_before_timeout(self) -> None:
        transport = _FakeTransport(close_seconds=0.0)
        gateway_task = await _make_gateway_task(runs_forever=False)
        await _close_transport_with_cap(transport, gateway_task, timeout_seconds=1.0)  # type: ignore[arg-type]
        assert transport.close_called
        assert transport.close_completed
        assert gateway_task.done()

    async def test_gateway_task_cancelled_if_still_running(self) -> None:
        """If transport.close() finishes but gateway_task is still alive,
        the helper cancels it explicitly so the orphan doesn't dangle."""
        transport = _FakeTransport(close_seconds=0.0)
        gateway_task = await _make_gateway_task(runs_forever=True)
        await _close_transport_with_cap(transport, gateway_task, timeout_seconds=1.0)  # type: ignore[arg-type]
        assert gateway_task.cancelled() or gateway_task.done()


class TestCloseTransportTimeout:
    async def test_timeout_warning_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """When discord.py hangs past the budget, the helper logs a
        warning and proceeds. The 2026-05-25 incident showed operators
        need an explicit signal that close took too long, not a silent
        cancellation."""
        transport = _FakeTransport(close_seconds=5.0)  # well above timeout
        gateway_task = await _make_gateway_task(runs_forever=True)
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.operator"):
            await _close_transport_with_cap(
                transport, gateway_task, timeout_seconds=0.05  # type: ignore[arg-type]
            )
        assert any("exceeded" in r.getMessage() for r in caplog.records)
        assert any("0.05" in r.getMessage() for r in caplog.records)

    async def test_timeout_cancels_gateway_task(self) -> None:
        """The whole point: even when close() is wedged, gateway_task
        gets cancelled so the daemon doesn't hold the event loop open
        waiting on an orphan coroutine."""
        transport = _FakeTransport(close_seconds=5.0)
        gateway_task = await _make_gateway_task(runs_forever=True)
        await _close_transport_with_cap(
            transport, gateway_task, timeout_seconds=0.05  # type: ignore[arg-type]
        )
        assert gateway_task.done()

    async def test_timeout_does_not_raise(self) -> None:
        """The helper must NEVER let TimeoutError escape — the daemon's
        shutdown path depends on this returning cleanly so the
        downstream safe_shutdown phase list still runs."""
        transport = _FakeTransport(close_seconds=5.0)
        gateway_task = await _make_gateway_task(runs_forever=True)
        # No pytest.raises — must return normally.
        await _close_transport_with_cap(
            transport, gateway_task, timeout_seconds=0.05  # type: ignore[arg-type]
        )


class TestCloseTransportGatewayException:
    async def test_swallows_discord_transport_error_during_cancel(self) -> None:
        """Some discord.py teardown paths raise DiscordTransportError
        during the cancel-and-await dance. The helper swallows it so
        downstream cleanup phases still get to run."""
        from wobblebot.adapters.discord_transport import DiscordTransportError

        async def _gateway_raises() -> None:
            raise DiscordTransportError("synthetic teardown error")

        transport = _FakeTransport(close_seconds=0.0)
        gateway_task = asyncio.create_task(_gateway_raises())
        # No pytest.raises — must swallow.
        await _close_transport_with_cap(transport, gateway_task, timeout_seconds=1.0)  # type: ignore[arg-type]

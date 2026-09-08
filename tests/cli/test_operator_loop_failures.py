"""Required operator loops distinguish death from clean shutdown."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from wobblebot.cli import operator as operator_cli
from wobblebot.services.daemon_health import DaemonHealthThresholds

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _run(kind: str, stop: asyncio.Event) -> None:
    if kind == "ttl":
        await operator_cli._ttl_expirer_loop(
            storage=AsyncMock(), poll_seconds=3600, stop_event=stop
        )
    else:
        await operator_cli._heartbeat_alert_loop(
            notifier=AsyncMock(),
            observe_db=None,
            advise_db=None,
            operator_db=None,
            thresholds=DaemonHealthThresholds(),
            stop_event=stop,
            check_seconds=3600,
        )


@pytest.mark.parametrize(
    "kind,label", [("ttl", "ttl expirer"), ("heartbeat", "heartbeat alert monitor")]
)
@pytest.mark.parametrize("exit_kind", ["failure", "cancel", "stop"])
async def test_required_loop_exit_contract(
    kind: str,
    label: str,
    exit_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    entered = asyncio.Event()
    failure = RuntimeError("unexpected cycle failure")

    async def cycle(*_args: object, **_kwargs: object) -> list[object]:
        entered.set()
        if exit_kind == "failure":
            raise failure
        if exit_kind == "cancel":
            await asyncio.Event().wait()
        stop.set()
        return []

    seam = "_expire_stale_pending_commands" if kind == "ttl" else "fetch_daemon_freshness"
    monkeypatch.setattr(operator_cli, seam, cycle)
    caplog.set_level(logging.INFO, logger=operator_cli._LOGGER.name)
    task = asyncio.create_task(_run(kind, stop))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if exit_kind == "failure":
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(task, timeout=5)
            assert caught.value is failure
        elif exit_kind == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        else:
            await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    records = [r for r in caplog.records if r.name == operator_cli._LOGGER.name]
    terminal = [r for r in records if "stopped" in r.message or "DIED" in r.message]
    assert len(terminal) == 1
    record = terminal[0]
    if exit_kind == "failure":
        assert record.levelno == logging.ERROR
        assert record.message == f"{label} DIED (RuntimeError): unexpected cycle failure"
        assert record.exc_info is not None and record.exc_info[1] is failure
        assert record.error_type == "RuntimeError"
    else:
        assert record.levelno == logging.INFO
        suffix = " (cancelled)" if exit_kind == "cancel" else ""
        assert record.message == f"{label} stopped{suffix}"

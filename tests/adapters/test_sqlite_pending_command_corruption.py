"""Corrupt persisted commands fail through StoragePort without hiding approvals."""

from __future__ import annotations

import asyncio
import logging
import traceback
from unittest.mock import AsyncMock

import pytest

from tests.adapters.test_sqlite_pending_commands import _pending
from tests.adapters.test_sqlite_pending_commands import storage as storage
from tests.cli.test_live_operator_poll import BTC_USD, _operator_service
from wobblebot.cli.live import _process_pending_commands, _run_loop
from wobblebot.config.cli import LiveConfig
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("reader", ["single", "batch"])
@pytest.mark.parametrize(
    "column,value",
    [
        ("command_json", "{do-not-log-fixture-value"),
        ("command_json", '{"kind":"pause","symbol":"do-not-log-fixture-value"}'),
        ("result_json", '{"success": "do-not-log-fixture-value"}'),
        ("ttl_expires_at", "do-not-log-fixture-value"),
        ("confirmed_at", b"do-not-log-fixture-value"),
        ("status", "invalid-status"),
    ],
)
async def test_decoder_failures_use_storage_error(storage, reader, column, value):
    pending = _pending(status="approved")
    await storage.save_pending_command(pending)
    conn = storage._require_conn()
    await conn.execute("PRAGMA ignore_check_constraints=ON")
    await conn.execute(
        f"UPDATE pending_commands SET {column}=? WHERE id=?", (value, str(pending.id))
    )
    await conn.commit()
    with pytest.raises(StorageError, match="decode pending command") as error:
        if reader == "single":
            await storage.get_pending_command(pending.id)
        else:
            await storage.get_pending_commands()
    assert str(pending.id) in str(error.value)
    assert "inspect the persisted row" in str(error.value)
    assert "do-not-log-fixture-value" not in str(error.value)
    assert "do-not-log-fixture-value" not in "".join(traceback.format_exception(error.value))


async def test_real_live_loop_contains_corrupt_batch_without_dispatch(storage, monkeypatch, caplog):
    broken = _pending(status="approved")
    valid = _pending(status="approved")
    await storage.save_pending_command(broken)
    await storage.save_pending_command(valid)
    await storage._require_conn().execute(
        "UPDATE pending_commands SET command_json=? WHERE id=?", ("{broken", str(broken.id))
    )
    await storage._require_conn().commit()
    service, engine = _operator_service(storage)
    dispatch = AsyncMock(wraps=service.dispatch_command)
    monkeypatch.setattr(service, "dispatch_command", dispatch)
    stop = asyncio.Event()
    real_step = engine.step
    ticks = 0

    async def step_and_stop(symbol, **kwargs):
        nonlocal ticks
        result = await real_step(symbol, **kwargs)
        ticks += 1
        if ticks == 2:
            stop.set()
        return result

    monkeypatch.setattr(engine, "step", step_and_stop)
    with caplog.at_level(logging.WARNING):
        code = await asyncio.wait_for(
            _run_loop(
                engine._exchange,
                engine,
                LiveConfig(symbols=[BTC_USD], tick_seconds=0.001),
                storage,
                stop,
                operator_service=service,
                operator_storage=storage,
            ),
            timeout=5,
        )
    assert code == 0 and ticks == 2
    dispatch.assert_not_awaited()
    failures = [r for r in caplog.records if "pending_commands poll failed" in r.getMessage()]
    assert len(failures) == 2
    assert all(r.error_type == "StorageError" for r in failures)
    assert all("inspect the persisted row" in r.getMessage() for r in failures)
    rows = await storage._require_conn().execute_fetchall("SELECT status FROM pending_commands")
    assert [r[0] for r in rows] == ["approved", "approved"]
    # An explicit offline fixture repair restores the valid batch; the reader
    # never pretends that an unreadable batch is empty or drops just one row.
    await storage.save_pending_command(broken)
    assert await _process_pending_commands(service, storage, None) == 2
    assert dispatch.await_count == 2

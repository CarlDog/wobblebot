"""Failed-open shutdown must finish before a caller can close its event loop."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.daemon_health import _heartbeats_or_empty, _latest_iso_timestamp
from wobblebot.services.llm_call_streak import fetch_llm_call_streaks
from wobblebot.sqlite_connection import open_connection

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "reader",
    ["storage-reader", "storage-writer", "llm-streak", "daemon-latest", "daemon-heartbeat"],
)
def test_failed_open_drains_worker_before_loop_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    """Hold the real worker's stop operation until join or caller teardown.

    The join hook releases a scheduling barrier, not a fake connection. Without
    adapter cleanup, run_until_complete returns with the worker held; closing
    the loop before releasing it reproduces the late callback from C5-R1.
    No wall-clock sleep, retry, or global warning-filter change is involved.
    """
    missing = reader in ("storage-reader", "llm-streak")
    db_path = tmp_path / "missing.db" if missing else tmp_path

    async def read_unavailable() -> None:
        if reader.startswith("storage-"):
            adapter = SQLiteStorageAdapter(db_path, read_only=reader == "storage-reader")
            with pytest.raises(StorageError) as failure:
                await adapter.connect()
            assert isinstance(failure.value.__cause__, aiosqlite.OperationalError)
        elif reader == "llm-streak":
            result = await fetch_llm_call_streaks(operator_db=db_path, roles=("news",))
            assert result[0].unavailable_reason == "OperationalError"
        elif reader == "daemon-latest":
            with pytest.raises(aiosqlite.OperationalError):
                await _latest_iso_timestamp(db_path, "news_items", "timestamp")
        else:
            assert await _heartbeats_or_empty(db_path) is None

    loop = asyncio.new_event_loop()
    release = threading.Event()
    workers: list[threading.Thread] = []
    errors: list[BaseException] = []
    real_connect = aiosqlite.connect
    real_excepthook = threading.excepthook

    def capture_worker_error(args: threading.ExceptHookArgs) -> None:
        if args.thread in workers:
            errors.append(args.exc_value)
        else:
            real_excepthook(args)

    def held_connect(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        connection = real_connect(*args, **kwargs)
        worker = connection if isinstance(connection, threading.Thread) else connection._thread
        workers.append(worker)
        real_join = worker.join
        stop_name = "stop" if hasattr(connection, "stop") else "_stop_running"
        real_stop = getattr(connection, stop_name)

        def wait_before_stop() -> None:
            if not release.wait(timeout=10):
                raise AssertionError("test did not release failed-open worker")

        def held_stop() -> Any:
            # Before 0.22.1 queues require a future even for this barrier.
            future = None if stop_name == "stop" else loop.create_future()
            connection._tx.put_nowait((future, wait_before_stop))
            stopped = real_stop()
            if stopped is None:
                return None

            class AwaitedStop:
                def __await__(self) -> Any:
                    # 0.22.0 already awaits its stop future on failed open.
                    # Let that valid cleanup route release the barrier too.
                    release.set()
                    return stopped.__await__()

            return AwaitedStop()

        def release_and_join(timeout: float | None = None) -> None:
            release.set()
            real_join(timeout)

        monkeypatch.setattr(connection, stop_name, held_stop)
        monkeypatch.setattr(worker, "join", release_and_join)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", held_connect)
    monkeypatch.setattr(threading, "excepthook", capture_worker_error)
    try:
        loop.run_until_complete(asyncio.wait_for(read_unavailable(), timeout=15))
        assert len(workers) == 1, "the real connector must have been exercised"
        still_running = workers[0].is_alive()
        loop.close()
    finally:
        if not loop.is_closed():
            loop.close()
        release.set()
        for worker in workers:
            # Test-owned cleanup is deliberately AFTER loop close. It cannot
            # hide missing adapter cleanup or repair the observed ordering.
            threading.Thread.join(worker, timeout=10)
    assert not still_running, f"failed connect returned before worker termination: {errors!r}"
    assert not errors, f"worker tried to use the closed loop: {errors!r}"
    if missing:
        assert not db_path.exists()


@pytest.mark.parametrize("worker_state", ["running", "unstarted", "unknown"])
def test_failed_open_cleanup_errors_are_explicit(
    monkeypatch: pytest.MonkeyPatch, worker_state: str
) -> None:
    """A bounded join that has not terminated the worker is not a clean failure."""
    release = threading.Event()
    worker = threading.Thread(target=release.wait, kwargs={"timeout": 10})
    failure = aiosqlite.OperationalError("cannot open database")
    timeouts: list[float | None] = []

    class FailedConnection:
        _thread = worker if worker_state != "unknown" else object()

        def __await__(self) -> Any:
            async def fail() -> None:
                raise failure

            return fail().__await__()

    def join_without_finishing(timeout: float | None = None) -> None:
        timeouts.append(timeout)
        threading.Thread.join(worker, timeout=0)

    monkeypatch.setattr(aiosqlite, "connect", lambda *args, **kwargs: FailedConnection())
    monkeypatch.setattr(worker, "join", join_without_finishing)
    if worker_state == "running":
        worker.start()
    try:
        if worker_state == "unstarted":
            with pytest.raises(aiosqlite.OperationalError) as raised:
                asyncio.run(open_connection("unused"))
            assert raised.value is failure
            assert timeouts == []
        else:
            message = "Timed out waiting" if worker_state == "running" else "Cannot verify"
            with pytest.raises(StorageError, match=message) as raised:
                asyncio.run(open_connection("unused"))
            assert raised.value.__cause__ is failure
            if worker_state == "running":
                assert len(timeouts) == 1 and timeouts[0] == 5.0
                assert worker.is_alive()
    finally:
        release.set()
        if worker.ident is not None:
            threading.Thread.join(worker, timeout=10)

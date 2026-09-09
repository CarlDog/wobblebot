"""Shared SQLite I/O leaf owning failed-open workers for adapters and readers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Thread

import aiosqlite

from wobblebot.ports.exceptions import StorageError

_FAILED_OPEN_JOIN_TIMEOUT_SECONDS = 5.0


async def open_connection(database: str, *, uri: bool = False) -> aiosqlite.Connection:
    """Retain a failed connection until its already-queued shutdown finishes.

    aiosqlite 0.22.1 queues stop on a failed open without awaiting its future;
    close() then does nothing because no SQLite handle was established. A caller
    closing its loop immediately can strand the worker's final callback (C5-R1).
    Older supported releases expose the worker as Connection itself.

    Join only after an ordinary failed open: the worker schedules callbacks but
    does not wait for their execution, so this bounded synchronous wait cannot
    deadlock on the event loop. It also introduces no cancellation point during
    cleanup. Cancellation during the initial open retains upstream behavior;
    0.20 does not queue shutdown for CancelledError, so do not catch BaseException.
    """
    connection = aiosqlite.connect(database, uri=uri)
    try:
        return await connection
    except Exception as exc:
        worker = (
            connection if isinstance(connection, Thread) else getattr(connection, "_thread", None)
        )
        if not isinstance(worker, Thread):
            raise StorageError("Cannot verify failed SQLite connection worker shutdown") from exc
        if worker.is_alive():
            worker.join(timeout=_FAILED_OPEN_JOIN_TIMEOUT_SECONDS)
            if worker.is_alive():
                raise StorageError(
                    "Timed out waiting for failed SQLite connection worker shutdown"
                ) from exc
        raise


@asynccontextmanager
async def managed_connection(
    database: str, *, uri: bool = False
) -> AsyncIterator[aiosqlite.Connection]:
    """Pair guarded startup with normal close without awaiting a connection twice."""
    connection = await open_connection(database, uri=uri)
    try:
        yield connection
    finally:
        await connection.close()

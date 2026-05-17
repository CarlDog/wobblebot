"""SqliteNotifierAdapter — Stage 5.5 ``NotifierPort`` backed by a ``StoragePort``.

Thin adapter that converts every ``NotifierPort.send_notification`` call
into a ``StoragePort.save_notification`` row. ``cli/live`` and
``cli/harvest`` consume this adapter through the ``NotifierPort``
abstraction — neither imports anything Discord-specific. Per ADR-013
decision 9 the engine layer stays Discord-ignorant; the only code that
ever sees a ``discord.py`` import is ``cli/operator`` (Stage 5.6),
which reads ``notifications`` rows back out and posts them to Discord.

The class is named ``Sqlite*`` to match the roadmap's naming, but it
depends only on the ``StoragePort`` abstraction. Any concrete
``StoragePort`` (current ``SQLiteStorageAdapter``, future Postgres /
Redis / etc.) satisfies the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import NotifierError, StorageError
from wobblebot.ports.notifier import Notification, NotifierPort
from wobblebot.ports.storage import StoragePort


class SqliteNotifierAdapter(NotifierPort):
    """Persist outbound notifications via a ``StoragePort``.

    Args:
        storage: ``StoragePort`` instance opened against the
            operator interaction DB (typically ``operator.db``). The
            adapter never owns the connection — ``cli/live`` /
            ``cli/harvest`` retain lifetime control.
    """

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    async def send_notification(self, notification: Notification) -> None:
        """Persist a ``Notification`` row; ``cli/operator`` forwards it later.

        Raises:
            NotifierError: If the underlying ``StoragePort`` fails.
        """
        try:
            await self._storage.save_notification(notification)
        except StorageError as exc:
            raise NotifierError(f"Failed to persist notification: {exc}") from exc

    async def send_error_alert(self, error: Exception, context: dict[str, Any]) -> None:
        """Synthesize a ``critical`` ``Notification`` from ``error`` and persist it.

        Convenience method that wraps an exception into a structured
        notification so callers (e.g. cli/live's top-level error
        handler) don't have to build the payload themselves.

        Raises:
            NotifierError: If the underlying ``StoragePort`` fails.
        """
        notification = Notification(
            level="critical",
            title=f"Unhandled {type(error).__name__}",
            message=str(error) or repr(error),
            timestamp=Timestamp(dt=datetime.now(UTC)),
            context=context,
        )
        await self.send_notification(notification)

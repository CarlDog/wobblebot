"""NotifierPort - Abstract interface for alerts and notifications.

This port defines the contract for sending notifications (email, Slack, etc.).
Implementations are added as needed (Phase 5+).
"""

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp


class Notification(BaseModel):
    """A notification message."""

    level: Literal["info", "warning", "error", "critical"]
    title: str = Field(..., min_length=1, max_length=200)
    message: str
    timestamp: Timestamp = Field(..., description="When the notification was raised")
    context: dict[str, Any] = Field(default_factory=dict, description="Additional context")


class PersistedNotification(BaseModel):
    """A ``Notification`` augmented with persistence + forwarding fields.

    Stage 5.5: the SQLite ``notifications`` table stores one of these
    per outbound event. ``cli/live`` / ``cli/harvest`` write rows via
    ``SqliteNotifierAdapter``; ``cli/operator`` (Stage 5.6) polls rows
    with ``forwarded=False`` and posts each to Discord, then marks
    ``forwarded=True`` via ``StoragePort.mark_notification_forwarded``.

    Attributes:
        id: Storage-assigned row id (``None`` before insert).
        notification: The original event payload.
        forwarded: Whether ``cli/operator`` has posted this to Discord.
        forwarded_at: Timestamp of the Discord post; ``None`` until
            forwarded.
        created_at: When the row was inserted into storage. May differ
            from ``notification.timestamp`` if the writer queued the
            event slightly before persisting.
    """

    id: int | None = None
    notification: Notification
    forwarded: bool = False
    forwarded_at: Timestamp | None = None
    created_at: Timestamp

    class Config:
        frozen = True


class NotifierPort(ABC):
    """Abstract interface for notifications.

    Future feature - sends alerts via various channels.

    Implementations (TBD):
    - Email notifier
    - Slack notifier
    - Discord notifier
    - SMS notifier (Twilio)

    Error convention:
    - Protocol/transport failure raises ``NotifierError`` (channel
      webhook returns non-2xx, gateway times out, etc.). Notifier
      failures are typically non-fatal to the caller; the caller
      decides whether to retry, fall back, or swallow.
    """

    @abstractmethod
    async def send_notification(self, notification: Notification) -> None:
        """Send a notification.

        Args:
            notification: Notification to send

        Raises:
            NotifierError: If notification cannot be sent
        """
        pass

    @abstractmethod
    async def send_error_alert(self, error: Exception, context: dict[str, Any]) -> None:
        """Send an error alert with context.

        Args:
            error: Exception that occurred
            context: Additional context (module, operation, etc.)

        Raises:
            NotifierError: If alert cannot be sent
        """
        pass

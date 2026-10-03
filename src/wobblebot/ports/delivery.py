"""Outbound-only embed contract; no conversation or financial capabilities."""

from collections.abc import Sequence
from typing import Protocol

from wobblebot.ports.exceptions import WobbleBotPortError


class DeliveryError(WobbleBotPortError):
    """Only a known pre-effect failure permits an automatic retry."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        permanent: bool = False,
        retry_after_seconds: float = 0,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.permanent = permanent
        self.retry_after_seconds = retry_after_seconds


class DeliveryTransport(Protocol):
    """Return a remote receipt; unknown outcomes must never masquerade as success."""

    async def send_embed(  # pylint: disable=too-many-arguments
        # Matches the existing embed transport contract.
        self,
        channel_id: str,
        *,
        title: str,
        description: str,
        color: int = 0,
        fields: Sequence[tuple[str, str] | tuple[str, str, bool]] | None = None,
        footer: str | None = None,
    ) -> str:
        """Send one bounded embed and return its message identifier."""

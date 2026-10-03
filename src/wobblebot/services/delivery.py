"""Bounded notification draining shared by operator and independent delivery."""

import asyncio
import logging
from typing import Literal

from wobblebot.ports.delivery import DeliveryError, DeliveryTransport
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.notification_embed_render import render_notification_embed

_LOGGER = logging.getLogger(__name__)


async def forward_notifications(
    storage: StoragePort,
    transport: DeliveryTransport,
    channel_id: str,
) -> int:
    """Persist claims before sends, then receipts; uncertain sends are not retried."""
    try:
        rows = await storage.get_delivery_notifications(limit=100)
    except StorageError:
        _LOGGER.warning("Cannot read notification outbox")
        return 0
    forwarded = 0
    for row in rows:
        if row.id is None:
            continue
        try:
            attempt = await storage.claim_notification_delivery(row.id)
            if attempt is None:
                continue
            try:
                async with asyncio.timeout(30):
                    message_id = await transport.send_embed(
                        channel_id, **render_notification_embed(row.notification, row.id)
                    )
            except DeliveryError as exc:
                outcome: Literal["retry", "failed", "uncertain"] = (
                    "retry" if exc.retryable else "failed" if exc.permanent else "uncertain"
                )
                await storage.finish_notification_delivery(
                    row.id,
                    attempt,
                    outcome,
                    error_type=type(exc).__name__,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                continue
            except TimeoutError:
                await storage.finish_notification_delivery(
                    row.id, attempt, "uncertain", error_type="SendTimeout"
                )
                continue
            await storage.finish_notification_delivery(
                row.id, attempt, "sent", message_id=message_id
            )
            forwarded += 1
        except StorageError:
            # Includes a lost post-send receipt: keep claim, never blindly resend.
            _LOGGER.warning("Notification delivery persistence unavailable (id=%s)", row.id)
    return forwarded

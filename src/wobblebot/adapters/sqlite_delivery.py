"""Transactional notification outbox; callers own an isolated connection."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import aiosqlite

DeliveryOutcome = Literal["sent", "retry", "failed", "uncertain"]


async def claim(conn: aiosqlite.Connection, notification_id: int) -> int | None:
    """Claim once; a dead sender's lease becomes uncertain, never auto-replayed."""
    now = datetime.now(UTC)
    await conn.execute(
        """INSERT OR IGNORE INTO notification_delivery (notification_id)
           SELECT id FROM notifications WHERE id=? AND forwarded=0""",
        (notification_id,),
    )
    await conn.execute(
        """UPDATE notification_delivery SET state='uncertain', error_type='SenderLeaseExpired'
           WHERE notification_id=? AND state='sending' AND julianday(lease_until)<=julianday(?)""",
        (notification_id, now.isoformat()),
    )
    async with conn.execute(
        """UPDATE notification_delivery
           SET state='sending', attempts=attempts+1, lease_until=?
           WHERE notification_id=? AND attempts<5 AND state IN ('queued','retry')
             AND (next_attempt IS NULL OR julianday(next_attempt)<=julianday(?))
             AND NOT EXISTS (SELECT 1 FROM delivery_backoff
                             WHERE julianday(not_before)>julianday(?))
             AND EXISTS (SELECT 1 FROM notifications
                         WHERE id=notification_id AND forwarded=0)
           RETURNING attempts""",
        (
            (now + timedelta(seconds=60)).isoformat(),
            notification_id,
            now.isoformat(),
            now.isoformat(),
        ),
    ) as cursor:
        row = await cursor.fetchone()
    await conn.commit()
    return int(row[0]) if row else None


async def finish(  # pylint: disable=too-many-arguments
    # Explicit receipt fields form one atomic persistence operation.
    conn: aiosqlite.Connection,
    notification_id: int,
    attempt: int,
    outcome: DeliveryOutcome,
    *,
    message_id: str | None,
    error_type: str | None,
    retry_after_seconds: float = 0,
) -> None:
    """Persist the receipt and forwarded flag together; failed sends stay visible."""
    now = datetime.now(UTC)
    state = "failed" if outcome == "retry" and attempt >= 5 else outcome
    cursor = await conn.execute(
        """UPDATE notification_delivery
           SET state=?, message_id=?, error_type=?, next_attempt=?, lease_until=NULL
           WHERE notification_id=? AND attempts=? AND state='sending'""",
        (
            state,
            message_id,
            error_type,
            (now + timedelta(seconds=max(30 * 2**attempt, retry_after_seconds))).isoformat(),
            notification_id,
            attempt,
        ),
    )
    if cursor.rowcount != 1:
        raise aiosqlite.OperationalError("Delivery claim no longer belongs to this sender")
    if retry_after_seconds > 0:
        await conn.execute(
            """INSERT INTO delivery_backoff(singleton,not_before) VALUES(1,?)
               ON CONFLICT(singleton) DO UPDATE
               SET not_before=MAX(not_before,excluded.not_before)""",
            ((now + timedelta(seconds=retry_after_seconds)).isoformat(),),
        )
    if state == "sent":
        await conn.execute(
            "UPDATE notifications SET forwarded=1, forwarded_at=? WHERE id=?",
            (now.isoformat(), notification_id),
        )
    await conn.commit()

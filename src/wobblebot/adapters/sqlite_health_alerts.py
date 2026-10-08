"""Persist a health transition and its notification in one transaction."""

import json
from datetime import UTC, datetime

import aiosqlite

from wobblebot.ports.notifier import Notification


async def record(
    conn: aiosqlite.Connection, daemon: str, status: str, notification: Notification
) -> bool:
    """Deduplicate across processes/restarts; never lose the enqueue after state save."""
    try:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT status FROM health_alert_state WHERE daemon=?", (daemon,)
        ) as cursor:
            previous = await cursor.fetchone()
        if previous is not None and previous[0] == status:
            await conn.commit()
            return False
        now = datetime.now(UTC).isoformat()
        await conn.execute(
            """INSERT INTO health_alert_state(daemon,status,updated_at) VALUES(?,?,?)
               ON CONFLICT(daemon) DO UPDATE SET
               status=excluded.status,updated_at=excluded.updated_at""",
            (daemon, status, now),
        )
        queued = previous is not None or status != "fresh"
        if queued:
            await conn.execute(
                """INSERT INTO notifications
                   (level,title,message,timestamp,context_json,forwarded,created_at)
                   VALUES(?,?,?,?,?,0,?)""",
                (
                    notification.level,
                    notification.title,
                    notification.message,
                    notification.timestamp.dt.isoformat(),
                    json.dumps(notification.context),
                    now,
                ),
            )
        await conn.commit()
        return queued
    except BaseException:
        await conn.rollback()
        raise

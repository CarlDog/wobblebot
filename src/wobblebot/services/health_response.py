"""Independent page-only health response; no restart or Docker authority."""

from datetime import UTC, datetime
from pathlib import Path

from wobblebot.config.loader import WobbleBotConfig
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.notifier import Notification
from wobblebot.ports.storage import StoragePort
from wobblebot.services.daemon_health import derive_thresholds_from_config, fetch_daemon_freshness


class HealthObserver:
    """Observe from the delivery process, so operator death cannot stop its own alert."""

    def __init__(
        self, config: WobbleBotConfig, storage: StoragePort, *, started_at: datetime | None = None
    ):
        self._config = config
        self._storage = storage
        self._started = started_at or datetime.now(UTC)

    async def poll(self, *, now: datetime | None = None) -> int:
        """Persist changed findings after startup grace; never restart any daemon."""
        current = now or datetime.now(UTC)
        delivery = self._config.delivery
        assert delivery is not None
        rows = await fetch_daemon_freshness(
            observe_db=Path(delivery.observe_db) if delivery.observe_db else None,
            advise_db=Path(delivery.advise_db) if delivery.advise_db else None,
            operator_db=Path(delivery.operator_db),
            thresholds=derive_thresholds_from_config(self._config),
            now=current,
        )
        queued = 0
        for row in rows:
            role = row.name.removeprefix("cli/")
            if getattr(self._config, role, None) is None:
                continue
            if (
                row.last_seen is None
                and (current - self._started).total_seconds() < row.threshold_seconds
            ):
                continue
            recovery = row.status == "fresh"
            last_seen = row.last_seen.isoformat() if row.last_seen else "never observed"
            notification = Notification(
                level="info" if recovery else "critical",
                title=f"Daemon health: {row.name} {row.status}",
                message=f"Independent observer: {row.status}; last evidence {last_seen}. "
                "Inspect the daemon; no automatic restart or repair was attempted.",
                timestamp=Timestamp(dt=current),
                context={"daemon": row.name, "status": row.status.value},
            )
            queued += int(
                await self._storage.record_health_transition(
                    row.name, row.status.value, notification
                )
            )
        return queued

"""Read-only diagnostics with stable finding identifiers; no external probes."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from wobblebot.config.loader import WobbleBotConfig
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.daemon_health import derive_thresholds_from_config, fetch_daemon_freshness
from wobblebot.services.provider_health import PersistedProviderHealth


@dataclass(frozen=True)
class Finding:
    """Machine-stable code plus safe observed evidence, never a repair action."""

    code: str
    status: Literal["ok", "warning", "unknown"]
    summary: str
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """JSON-compatible diagnostic record."""
        return asdict(self)


async def diagnose(config: WobbleBotConfig, storage: StoragePort | None) -> list[Finding]:
    """Read configured freshness and durable uncertain work without mutation."""
    health = await fetch_daemon_freshness(
        observe_db=Path(config.observe.db) if config.observe else None,
        advise_db=Path(config.advise.db) if config.advise else None,
        operator_db=Path(config.operator.operator_db) if config.operator else None,
        thresholds=derive_thresholds_from_config(config),
    )
    findings = [
        Finding(
            code=f"daemon:{item.name}",
            status=(
                "ok"
                if item.status == "fresh"
                else "unknown" if item.status == "unknown" else "warning"
            ),
            summary=f"{item.name}: {item.status}",
            evidence={
                "last_seen": item.last_seen.isoformat() if item.last_seen else None,
                "threshold_seconds": item.threshold_seconds,
            },
        )
        for item in health
    ]
    if storage is None:
        findings.append(Finding("operator-storage", "unknown", "Operator storage unavailable", {}))
        return findings
    try:
        commands = await storage.get_pending_commands(status="claimed", limit=100)
        findings.append(
            Finding(
                "command-claims",
                "warning" if commands else "ok",
                (
                    "Reconcile unresolved command effects"
                    if commands
                    else "No unresolved command claims"
                ),
                {"ids": [str(row.id) for row in commands], "limit": 100},
            )
        )
        failures = await storage.get_unresolved_deliveries(limit=100)
        findings.append(
            Finding(
                "delivery",
                "warning" if failures else "ok",
                (
                    "Delivery outcomes need inspection"
                    if failures
                    else "No unresolved delivery outcomes"
                ),
                {
                    "rows": [
                        {
                            "id": row.id,
                            "state": row.delivery_state,
                            "attempts": row.delivery_attempts,
                        }
                        for row in failures
                    ],
                    "limit": 100,
                },
            )
        )
    except StorageError:
        findings.append(
            Finding("lifecycle-storage", "unknown", "Lifecycle evidence unavailable", {})
        )
    observations = await PersistedProviderHealth(storage).get()
    for index, item in enumerate(observations):
        findings.append(
            Finding(
                f"provider:{index}",
                "unknown" if item.ok is None else "ok" if item.ok else "warning",
                f"{item.name}: {item.detail}",
                {"checked_at": item.checked_at.isoformat() if item.checked_at else None},
            )
        )
    return findings

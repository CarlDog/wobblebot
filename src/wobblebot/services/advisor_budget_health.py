"""Read-only budget-denial evidence for the advisor's functional health."""

from datetime import datetime
from pathlib import Path

from wobblebot.config.advisor import AdvisorConfig, ArbitratorConfig, ExpertConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.services.llm_call_streak import fetch_llm_call_streaks

# Operator conversations have an independent purpose and must not clear or trip
# the advisor's signal. Gremlin successes likewise cannot clear another seat.
_ADVISOR_ROLES = ("single", "quant", "risk", "news", "arbitrator", "gremlin", "unknown")


def advisor_uses_cloud(config: WobbleBotConfig) -> bool:
    """Whether an active advisor route (including explicit fallbacks) is metered."""
    advisor = config.advisor
    if advisor is None:
        return False
    providers: list[str | None] = []
    if advisor.engine != "heuristic":
        targets: list[AdvisorConfig | ExpertConfig | ArbitratorConfig] = (
            [advisor] if advisor.type == "single" else list(advisor.experts)
        )
        if (
            advisor.type == "moe"
            and advisor.aggregator == "arbitrator"
            and advisor.arbitrator is not None
        ):
            targets.append(advisor.arbitrator)
        for target in targets:
            providers.append(target.provider)
            providers.extend(fallback.provider for fallback in target.fallbacks)
    if advisor.gremlin.enabled:
        providers.append(advisor.gremlin.provider)
    return any(provider not in (None, "ollama") for provider in providers)


async def advisor_budget_detail(
    operator_db: Path | None, *, now: datetime | None = None
) -> tuple[bool, str | None]:
    """Return evidence availability and a repeated-denial description, if any.

    Zero calls is valid for local and clear-guard heuristic ticks. Unreadable
    evidence is separate from an empty ledger so configured cloud probes can
    fail closed without claiming that no calls proves a working LLM.
    """
    streaks = await fetch_llm_call_streaks(operator_db=operator_db, roles=_ADVISOR_ROLES, now=now)
    unavailable = next((s.unavailable_reason for s in streaks if s.unavailable_reason), None)
    if unavailable:
        return False, f"LLM budget evidence unavailable ({unavailable})"
    blocked = [s for s in streaks if s.consecutive_cost_denials >= s.threshold]
    if not blocked:
        return True, None
    details = "; ".join(
        f"{s.role}: {s.consecutive_cost_denials} consecutive cost-cap denials ({s.last_error_kind})"
        for s in blocked
    )
    return True, f"LLM recommendations degraded — {details}"

"""Cloud-LLM cost gate (Phase 6 / ADR-014).

A pure-domain service that decides whether one more cloud-LLM call can
go out without exceeding the operator's configured daily / session
USD caps. Called by the cli/* layer BEFORE each outbound API request;
returns ``GateAllow`` or ``GateDeny`` so the caller decides whether to
abort, fall back to local Ollama, or surface an operator error.

Ollama (local, free) calls bypass this module entirely — no need to
instrument anything when there is no money at risk.

Per ADR-014:
- Daily cap is a **sliding 24-hour window** (not midnight-reset), to
  avoid the burst-around-midnight failure mode.
- Session total is **tracked in-memory by the caller** and threaded
  through this function. Restart-reset is the expected semantic.
- Enforcement is **hard-stop on trip**: callers usually convert
  ``GateDeny`` into ``LLMCostCapExceeded`` and propagate. The two-step
  shape lets callers attach extra logging / context first.
- ``enforce=False`` is the **dry-run posture** — gate records calls
  (caller still persists ``LLMCallRecord`` rows) but never denies.
  Used for the first week of cloud usage to observe real costs
  before flipping enforcement on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from wobblebot.domain.llm_cost import LLMRole
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.storage import StoragePort

_DAILY_WINDOW = timedelta(hours=24)


class LLMCostConfig(BaseModel):
    """Operator-tunable cloud-LLM spend caps (ADR-014 decision 1)."""

    max_spend_per_day_usd: Decimal = Field(
        default=Decimal("1.00"),
        gt=Decimal("0"),
        description="Sliding-24h-window USD cap across all roles.",
    )
    max_spend_per_session_usd: Decimal = Field(
        default=Decimal("0.50"),
        gt=Decimal("0"),
        description="Single-CLI-invocation USD cap.",
    )
    enforce: bool = Field(
        default=True,
        description=(
            "When False, gate records but never denies (dry-run posture per "
            "ADR-014 decision 8). Operators set this to False during the "
            "first week of cloud usage to observe real costs before "
            "flipping enforcement on."
        ),
    )

    class Config:
        frozen = True


class SessionCostTracker:
    """Mutable in-memory running total of cloud-LLM spend for one CLI session.

    A "session" is one CLI process lifetime (one ``cli/advise`` tick,
    one ``cli/operator`` daemon run, etc.). The CLI mints one tracker
    at startup and passes it to every cloud adapter it constructs;
    each adapter adds its real cost to the tracker after a successful
    call. The cost gate reads ``.total`` on the next ``check_budget``
    call to enforce ADR-014's per-session cap.

    Restart-reset is the expected semantic (ADR-014 design decision 3
    in the Stage 6.1 design doc) — the table still has all rows for
    cross-session forensic queries.

    Deliberately a tiny mutable class rather than a Pydantic model;
    Pydantic's frozen=True would block ``add()``.
    """

    def __init__(self, initial: Decimal | None = None) -> None:
        self._total: Decimal = initial if initial is not None else Decimal("0")

    @property
    def total(self) -> Decimal:
        return self._total

    def add(self, amount: Decimal) -> None:
        """Increment the running total. Negative amounts rejected."""
        if amount < 0:
            raise ValueError(f"SessionCostTracker.add(): negative amount {amount}")
        self._total += amount


@dataclass(frozen=True)
class GateAllow:
    """The call is within budget; the caller may proceed."""

    kind: Literal["allow"] = "allow"


@dataclass(frozen=True)
class GateDeny:
    """The call would exceed a cap; the caller MUST NOT proceed."""

    reason: str
    cap_kind: Literal["daily", "session"]
    cap_value_usd: Decimal
    daily_spent_usd: Decimal
    session_spent_usd: Decimal
    kind: Literal["deny"] = "deny"


GateDecision = GateAllow | GateDeny


async def check_budget(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    storage: StoragePort,
    role: LLMRole,
    estimated_cost_usd: Decimal,
    session_spent_usd: Decimal,
    config: LLMCostConfig,
    *,
    now: datetime | None = None,
) -> GateDecision:
    """Decide whether one more call fits within both caps.

    Args:
        storage: Source of recent ``LLMCallRecord`` rows for the daily
            window. Typically the same ``SQLiteStorageAdapter`` the
            caller uses to persist the record after the call.
        role: Which role drives the call. Informational — does NOT
            filter the daily total (v1 is single-pool across roles
            per ADR-014 decision 2). Appears in the deny reason for
            operator readability.
        estimated_cost_usd: Worst-case cost of the call about to be
            made. Per ADR-014 decision 4 of stage 6.1 design doc, the
            caller estimates this conservatively from prompt length
            and ``max_tokens`` so the gate refuses calls that could
            tip the budget even if the actual response is cheaper.
        session_spent_usd: Running total of confirmed-cost calls made
            during the current CLI invocation. Caller maintains this
            in-memory and adds the actual ``cost_usd`` from each
            successful call's persisted record.
        config: Cap values + the enforce kill switch.
        now: Optional injected wall-clock time for testing the sliding
            window. Defaults to ``datetime.now(UTC)``.

    Returns:
        ``GateAllow`` when both caps would hold after the call;
        ``GateDeny`` when either cap would be exceeded. ``enforce=False``
        always returns ``GateAllow`` (per ADR-014 decision 8).

    Raises:
        StorageError: Propagated from ``storage.get_llm_calls``. The
            caller decides whether to fail-loud or fail-open in that
            case; the gate makes no policy.
    """
    if not config.enforce:
        return GateAllow()
    # Check session cap first — it's an in-memory comparison with no
    # DB round-trip. Daily cap requires a query, so save it for the
    # case where session is fine.
    session_projected = session_spent_usd + estimated_cost_usd
    if session_projected > config.max_spend_per_session_usd:
        return GateDeny(
            reason=(
                f"session cap ${config.max_spend_per_session_usd} would be exceeded "
                f"by the {role} call (projected ${session_projected})"
            ),
            cap_kind="session",
            cap_value_usd=config.max_spend_per_session_usd,
            daily_spent_usd=Decimal("0"),  # not queried; not relevant
            session_spent_usd=session_spent_usd,
        )
    wall = now or datetime.now(UTC)
    cutoff = Timestamp(dt=wall - _DAILY_WINDOW)
    recent = await storage.get_llm_calls(since=cutoff)
    daily_spent = sum((r.cost_usd for r in recent), Decimal("0"))
    daily_projected = daily_spent + estimated_cost_usd
    if daily_projected > config.max_spend_per_day_usd:
        return GateDeny(
            reason=(
                f"daily cap ${config.max_spend_per_day_usd} would be exceeded "
                f"by the {role} call (projected ${daily_projected})"
            ),
            cap_kind="daily",
            cap_value_usd=config.max_spend_per_day_usd,
            daily_spent_usd=daily_spent,
            session_spent_usd=session_spent_usd,
        )
    return GateAllow()

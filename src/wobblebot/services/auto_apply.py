"""Auto-apply gate for Stage 3.4b bounded auto-tuning.

Pure service: takes an ``AdvisorSuggestion``, the operator's current
``GridLevels`` for the suggestion's coin, and the ``AutoApplyConfig``
bounds, and returns an ``AutoApplyResult`` that says which proposed
keys can land on the running config and which must stay advisory.
No I/O, no file writes — that's the cli/apply tool's job in Slice B/C.

Gate rules (per ADR-007 + the AutoApplyConfig docstring):

1. **``enabled=False``** → blanket reject. Every proposed key gets
   logged with reason ``"auto-apply disabled"``. The whole point of
   the flag is that the operator opts in.
2. **``recommendation.role == "news"``** → blanket reject. News-derived
   recommendations are advisory-only regardless of bounds. ``aggregated``
   suggestions that *included* a news opinion in ``expert_opinions``
   still auto-apply because the aggregated role is metrics-driven
   (news is one of several inputs; the aggregation IS the
   metrics-driven synthesis).
3. **Whitelist** of mutable keys: ``spacing_percentage`` and
   ``order_size_usd``. Both have ``max_*_change_percentage`` caps in
   ``AutoApplyConfig`` and a clear current-value baseline in
   ``GridLevels``. Level keys (``levels_above`` / ``levels_below``)
   are *not* whitelisted in v1 — they need their own cap and the
   operator hasn't expressed one. Reject with
   ``"no magnitude cap configured for level keys"``.
4. **Magnitude cap** per key: ``|proposed - current| / current``
   must be ≤ ``max_<key>_change_percentage / 100``. Numeric coercion
   tolerates int/Decimal/float on the proposal side.

Anything not in the whitelist is rejected with ``"key not whitelisted"``.

The result also carries a ``proposed_grid`` — the current ``GridLevels``
with applied keys merged in. cli/apply uses this to render the unified
diff against settings.yml.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from wobblebot.config.advisor import AutoApplyConfig
from wobblebot.config.grid import GridLevels
from wobblebot.ports.advisor import AdvisorSuggestion

# Keys with a configured magnitude cap in v1. Extend when the operator
# adds `max_levels_change` or similar to AutoApplyConfig.
_WHITELISTED_NUMERIC_KEYS = ("spacing_percentage", "order_size_usd")
_LEVEL_KEYS = ("levels_above", "levels_below")

# Roles whose recommendations never auto-apply, per ADR-007.
# ``"aggregated"`` is intentionally NOT here — the MoE's aggregated
# output is metrics-driven even when a news expert contributed.
_BLOCKED_ROLES: frozenset[str] = frozenset({"news"})


class AppliedKey(BaseModel):
    """One key that cleared the gate."""

    key: str = Field(min_length=1)
    before: float
    after: float
    delta_pct: float  # signed; +5.0 means proposed is 5% above current

    class Config:
        frozen = True


class RejectedKey(BaseModel):
    """One key that did not clear the gate, with the operator-facing reason."""

    key: str = Field(min_length=1)
    proposed: Any
    reason: str = Field(min_length=1)

    class Config:
        frozen = True


class AutoApplyResult(BaseModel):
    """Outcome of evaluating a suggestion through the gate.

    Attributes:
        enabled: ``AutoApplyConfig.enabled`` value at the time of
            evaluation. Operator-facing.
        role_eligible: Whether the suggestion's role permits auto-apply
            at all. False for news-role; the suggestion can still be
            valuable advisory output, just not auto-applied.
        symbol: Coin we evaluated against (``BTC``, ``ETH``, ...).
        applied_keys: Keys that cleared every gate rule.
        rejected_keys: Keys that failed at least one rule. Reasons are
            human-readable.
        proposed_grid: ``current_grid`` merged with ``applied_keys``.
            If nothing applied, this equals ``current_grid``.
    """

    enabled: bool
    role_eligible: bool
    symbol: str = Field(min_length=1)
    applied_keys: list[AppliedKey] = Field(default_factory=list)
    rejected_keys: list[RejectedKey] = Field(default_factory=list)
    proposed_grid: GridLevels

    class Config:
        frozen = True

    def is_clean_apply(self) -> bool:
        """True iff at least one key applied and none were rejected.

        cli/apply --commit can use this as a "no caveats" green light;
        a partial apply (some accepted + some rejected) is still
        legitimate but the operator should see the rejection reasons
        before proceeding."""
        return bool(self.applied_keys) and not self.rejected_keys


def evaluate_auto_apply(
    suggestion: AdvisorSuggestion,
    current_grid: GridLevels,
    auto_apply_config: AutoApplyConfig,
    *,
    symbol: str,
) -> AutoApplyResult:
    """Run the suggestion through the gate.

    Args:
        suggestion: Persisted advisor suggestion to evaluate.
        current_grid: Operator's currently-running grid for the symbol
            (the result of ``GridConfig.for_coin(symbol)``).
        auto_apply_config: ``AdvisorConfig.auto_apply`` bounds.
        symbol: The coin this grid belongs to (e.g. ``"BTC"``). Carried
            into the result so the audit row + cli/apply printout know
            which coin's section to rewrite.

    Returns:
        ``AutoApplyResult`` describing per-key outcomes.

    The function never raises on bad input — every failure mode shows
    up as a ``RejectedKey`` so the operator's audit trail is complete.
    """
    recommendations = suggestion.recommendation.recommendations
    role = suggestion.recommendation.role

    if not auto_apply_config.enabled:
        return AutoApplyResult(
            enabled=False,
            role_eligible=True,  # role check is moot when the gate is off
            symbol=symbol,
            rejected_keys=[
                RejectedKey(key=k, proposed=v, reason="auto-apply disabled")
                for k, v in recommendations.items()
            ],
            proposed_grid=current_grid,
        )

    if role in _BLOCKED_ROLES:
        return AutoApplyResult(
            enabled=True,
            role_eligible=False,
            symbol=symbol,
            rejected_keys=[
                RejectedKey(
                    key=k,
                    proposed=v,
                    reason=(
                        f"role={role!r} blocked by ADR-007 "
                        "(news-derived recommendations never auto-apply)"
                    ),
                )
                for k, v in recommendations.items()
            ],
            proposed_grid=current_grid,
        )

    applied: list[AppliedKey] = []
    rejected: list[RejectedKey] = []
    grid_overrides: dict[str, Decimal | int] = {}

    for key, proposed_raw in recommendations.items():
        if key in _LEVEL_KEYS:
            rejected.append(
                RejectedKey(
                    key=key,
                    proposed=proposed_raw,
                    reason="no magnitude cap configured for level keys",
                )
            )
            continue
        if key not in _WHITELISTED_NUMERIC_KEYS:
            rejected.append(
                RejectedKey(
                    key=key,
                    proposed=proposed_raw,
                    reason="key not whitelisted for auto-apply",
                )
            )
            continue

        current_value = _current_value(current_grid, key)
        coerced = _coerce_numeric(proposed_raw)
        if coerced is None:
            rejected.append(
                RejectedKey(
                    key=key,
                    proposed=proposed_raw,
                    reason=f"proposed value {proposed_raw!r} is not numeric",
                )
            )
            continue
        if coerced <= 0:
            rejected.append(
                RejectedKey(
                    key=key,
                    proposed=proposed_raw,
                    reason=f"proposed value {coerced} must be > 0",
                )
            )
            continue

        cap_pct = _cap_for_key(auto_apply_config, key)
        delta_pct = (float(coerced) - float(current_value)) / float(current_value) * 100.0
        if abs(delta_pct) > float(cap_pct):
            rejected.append(
                RejectedKey(
                    key=key,
                    proposed=proposed_raw,
                    reason=(
                        f"delta {delta_pct:+.2f}% exceeds "
                        f"max_{key.replace('_usd','')}_change_percentage={cap_pct}%"
                    ),
                )
            )
            continue

        applied.append(
            AppliedKey(
                key=key,
                before=float(current_value),
                after=float(coerced),
                delta_pct=round(delta_pct, 4),
            )
        )
        grid_overrides[key] = coerced

    proposed_grid = (
        current_grid.model_copy(update=grid_overrides) if grid_overrides else current_grid
    )
    return AutoApplyResult(
        enabled=True,
        role_eligible=True,
        symbol=symbol,
        applied_keys=applied,
        rejected_keys=rejected,
        proposed_grid=proposed_grid,
    )


def _current_value(grid: GridLevels, key: str) -> Decimal:
    """Return the grid's current value for one of the whitelisted keys."""
    value = getattr(grid, key)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _coerce_numeric(value: Any) -> Decimal | None:
    """Coerce LLM-emitted numerics (float/int/str) into ``Decimal`` for
    bounded comparison. Returns None on non-numerics or NaN."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ArithmeticError, ValueError):
            return None
    return None


def _cap_for_key(config: AutoApplyConfig, key: str) -> Decimal:
    """Return the configured percentage cap for a whitelisted key."""
    if key == "spacing_percentage":
        return config.max_spacing_change_percentage
    if key == "order_size_usd":
        return config.max_order_size_change_percentage
    raise AssertionError(f"unreachable: key {key!r} not in whitelist")

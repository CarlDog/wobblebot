"""Recalibration service — scale operator-tunable USD caps with balance (Stage 7.6.A).

The operator's settings.yml encodes a policy that's calibrated for a
particular starting balance. When the balance moves (drawdown, top-up,
intentional scale-down for a small-balance experiment), every
USD-denominated knob in the config should move proportionally to keep
the same risk posture.

This module exposes one pure function — :func:`recalibrate` — that
takes the current balance, target balance, and the operator's current
:class:`WobbleBotConfig`, computes the scale factor
(``target / current``), and returns a :class:`RecalibrationProposal`
enumerating every proposed change.

**What scales** (10 sections + per-coin grid overrides):

- ``grid.default.order_size_usd``
- ``grid.coins.<COIN>.order_size_usd`` for every per-coin override
- ``safety.max_total_exposure_usd``
- ``safety.max_daily_spend_usd``
- ``safety.max_per_coin_exposure_usd``
- ``safety.emergency_stop.min_exchange_balance_usd`` (skipped when 0
  — multiplying zero by any ratio is still zero; no point listing
  a no-op change in the proposal)
- ``live.max_session_loss_usd``
- ``harvester.min_exchange_liquidity_usd``
- ``harvester.topup_threshold_usd``
- ``harvester.surplus_threshold_usd``
- ``harvester.max_withdrawal_per_day_usd``

**What doesn't scale** (intentionally — these are policy invariants,
not USD amounts):

- ``grid.default.spacing_percentage`` and per-coin overrides (already
  a percentage of price)
- ``grid.default.levels_above`` / ``levels_below`` and per-coin
  overrides (counts)
- ``safety.max_orders_per_coin`` (count)
- ``safety.emergency_stop.max_loss_percentage`` (already a percentage)
- ``live.max_runtime_minutes`` (time, not money)
- ``shadow.*`` (synthetic balances; operator manages independently)

The proposal is pure data — no I/O. ``cli/recalibrate`` (Stage 7.6.B)
threads it through ``services/settings_rewriter`` for ``--commit``.

Per ADR-012's auto-tuning gate posture: this is **operator-initiated**
(not LLM-initiated), so the auto-apply bounds don't apply. The
operator drove the recalibration via an explicit CLI invocation; the
gate exists to defend against LLM proposals slipping through, not
against the operator's own intent.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from pydantic import BaseModel, Field

from wobblebot.config.loader import WobbleBotConfig

# USD precision: 2 decimals (cents). Smaller-than-a-cent values
# would round to $0.00 — which is itself a useful signal to the
# operator that the target balance is below the point where the
# math breaks down (an "order size of $0.00" is obviously wrong;
# the proposal still surfaces it so the operator notices).
_QUANTIZE = Decimal("0.01")


class RecalibrationChange(BaseModel):
    """One proposed knob change in a :class:`RecalibrationProposal`.

    Attributes:
        yaml_path: Dotted path inside settings.yml, e.g.
            ``"safety.max_total_exposure_usd"`` or
            ``"grid.coins.DOGE.order_size_usd"``. The
            ``services/settings_rewriter`` consumes this directly.
        current_value: Value currently in the operator's config.
        proposed_value: Value the calibrator computed for the target
            balance. Quantized to 6 decimal places.
    """

    yaml_path: str = Field(min_length=1)
    current_value: Decimal
    proposed_value: Decimal

    class Config:
        frozen = True


class RecalibrationProposal(BaseModel):
    """Output of :func:`recalibrate`.

    Pure data; no I/O. Stage 7.6.B's CLI feeds ``changes`` into
    ``services/settings_rewriter`` for ``--commit``.

    Attributes:
        current_balance: Operator's current USD balance (what they
            calibrated the existing config for).
        target_balance: USD balance the proposal is computed for.
        scale_factor: ``target_balance / current_balance``. Reported
            separately so the operator can sanity-check the magnitude
            at a glance.
        changes: Per-knob proposed changes. Includes only knobs whose
            value actually changes — a knob currently at $0 won't
            appear, and a knob whose current and proposed values are
            equal won't appear.
    """

    current_balance: Decimal = Field(gt=Decimal("0"))
    target_balance: Decimal = Field(gt=Decimal("0"))
    scale_factor: Decimal = Field(gt=Decimal("0"))
    changes: tuple[RecalibrationChange, ...] = ()

    class Config:
        frozen = True


def _scaled(value: Decimal, ratio: Decimal) -> Decimal:
    """Scale a USD value by the ratio and quantize to 6 decimals."""
    return (value * ratio).quantize(_QUANTIZE, rounding=ROUND_HALF_UP)


def _change_if_different(
    *,
    yaml_path: str,
    current: Decimal,
    proposed: Decimal,
) -> RecalibrationChange | None:
    """Build a change only when proposed differs from current.

    Avoids cluttering the proposal with no-op rows (e.g. a knob
    already at zero stays at zero). Comparison is on quantized values
    so the ``$10.00 → $10.000001`` floating-point pseudo-change is
    treated as a no-op.
    """
    if proposed == current:
        return None
    return RecalibrationChange(
        yaml_path=yaml_path,
        current_value=current,
        proposed_value=proposed,
    )


def recalibrate(
    *,
    current_balance: Decimal,
    target_balance: Decimal,
    current_config: WobbleBotConfig,
) -> RecalibrationProposal:
    """Compute a recalibration proposal at the target balance.

    Args:
        current_balance: Operator's current USD balance. Must be > 0.
            Typically pulled live from Kraken via the read-only key,
            but a CLI flag can override (useful for what-if analysis
            without hitting the API).
        target_balance: The balance the new config should be tuned
            for. Must be > 0. Can be larger OR smaller than
            ``current_balance``.
        current_config: Fully loaded ``WobbleBotConfig`` (the
            ``load_resolved_config(...)`` output).

    Returns:
        Frozen :class:`RecalibrationProposal` enumerating every
        proposed change. Empty ``changes`` tuple means the proposal
        is a no-op (current == target, or all scaled values land
        identical after rounding).

    Raises:
        ValueError: If either balance is non-positive.
    """
    if current_balance <= Decimal("0"):
        raise ValueError(f"current_balance must be positive; got {current_balance}")
    if target_balance <= Decimal("0"):
        raise ValueError(f"target_balance must be positive; got {target_balance}")

    ratio = target_balance / current_balance

    changes: list[RecalibrationChange] = []
    changes.extend(_grid_changes(current_config, ratio))
    changes.extend(_safety_changes(current_config, ratio))
    changes.extend(_live_changes(current_config, ratio))
    changes.extend(_harvester_changes(current_config, ratio))

    return RecalibrationProposal(
        current_balance=current_balance,
        target_balance=target_balance,
        scale_factor=ratio,
        changes=tuple(changes),
    )


# --------------------------------------------------------------------- #
# Per-section change builders                                           #
# --------------------------------------------------------------------- #


def _grid_changes(config: WobbleBotConfig, ratio: Decimal) -> Iterable[RecalibrationChange]:
    """Scale ``grid.default.order_size_usd`` + every per-coin override."""
    default = config.grid.default
    proposed = _scaled(default.order_size_usd, ratio)
    change = _change_if_different(
        yaml_path="grid.default.order_size_usd",
        current=default.order_size_usd,
        proposed=proposed,
    )
    if change is not None:
        yield change

    for coin_symbol, coin_cfg in config.grid.coins.items():
        proposed = _scaled(coin_cfg.order_size_usd, ratio)
        change = _change_if_different(
            yaml_path=f"grid.coins.{coin_symbol}.order_size_usd",
            current=coin_cfg.order_size_usd,
            proposed=proposed,
        )
        if change is not None:
            yield change


def _safety_changes(config: WobbleBotConfig, ratio: Decimal) -> Iterable[RecalibrationChange]:
    """Scale every USD field in the safety block."""
    safety = config.safety
    for attr, path in (
        ("max_total_exposure_usd", "safety.max_total_exposure_usd"),
        ("max_daily_spend_usd", "safety.max_daily_spend_usd"),
        ("max_per_coin_exposure_usd", "safety.max_per_coin_exposure_usd"),
    ):
        current = getattr(safety, attr)
        proposed = _scaled(current, ratio)
        change = _change_if_different(yaml_path=path, current=current, proposed=proposed)
        if change is not None:
            yield change

    # emergency_stop.min_exchange_balance_usd: scale only if non-zero.
    # Zero is "no minimum balance enforced"; scaling stays zero.
    floor = safety.emergency_stop.min_exchange_balance_usd
    if floor > Decimal("0"):
        proposed = _scaled(floor, ratio)
        change = _change_if_different(
            yaml_path="safety.emergency_stop.min_exchange_balance_usd",
            current=floor,
            proposed=proposed,
        )
        if change is not None:
            yield change


def _live_changes(config: WobbleBotConfig, ratio: Decimal) -> Iterable[RecalibrationChange]:
    """Scale ``live.max_session_loss_usd``."""
    if config.live is None:
        return
    current = config.live.max_session_loss_usd
    proposed = _scaled(current, ratio)
    change = _change_if_different(
        yaml_path="live.max_session_loss_usd",
        current=current,
        proposed=proposed,
    )
    if change is not None:
        yield change


def _harvester_changes(config: WobbleBotConfig, ratio: Decimal) -> Iterable[RecalibrationChange]:
    """Scale every USD threshold in the harvester block.

    The ordering invariant (``min < topup < surplus``) is preserved
    automatically by scaling all three with the same positive ratio.
    """
    if config.harvester is None:
        return
    h = config.harvester
    for attr, path in (
        ("min_exchange_liquidity_usd", "harvester.min_exchange_liquidity_usd"),
        ("topup_threshold_usd", "harvester.topup_threshold_usd"),
        ("surplus_threshold_usd", "harvester.surplus_threshold_usd"),
        ("max_withdrawal_per_day_usd", "harvester.max_withdrawal_per_day_usd"),
    ):
        current = getattr(h, attr)
        proposed = _scaled(current, ratio)
        change = _change_if_different(yaml_path=path, current=current, proposed=proposed)
        if change is not None:
            yield change


__all__ = (
    "RecalibrationChange",
    "RecalibrationProposal",
    "recalibrate",
)

"""SellGuard — the cost-basis sell guard service (ADR-032).

Wraps ``domain/cost_basis.py``'s pure replay/assessment with the storage
read, a per-symbol basis cache, and transition + heartbeat logging
(mirroring ``GridEngine``'s offside pattern, ADR-006) so a symbol stuck
deferring SELLs doesn't flood the log every tick.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from wobblebot.domain.cost_basis import CostBasis, SellAssessment, assess_sell, replay_average_cost
from wobblebot.domain.value_objects import Symbol, fmt_decimal
from wobblebot.ports.storage import StoragePort

_LOGGER = logging.getLogger("wobblebot.services.cost_basis")

# Loss percentages come out of a Decimal division carrying 28
# significant digits. Two decimal places is the precision an operator
# acts on; the tail is division noise, so quantize for DISPLAY before
# formatting (fmt_decimal deliberately does not impose a scale — see
# its docstring).
_PCT_DISPLAY = Decimal("0.01")


def _fmt_money(value: Decimal | None) -> str:
    """Display a possibly-absent Decimal; never prints a bare "None".

    SellAssessment.average_cost is optional (no basis recorded yet),
    and the previous message interpolated it raw — so an unknown basis
    rendered as the literal None, which reads like a bug rather
    than a state.
    """
    # max_significant: average cost is a Decimal division, so it has
    # no trailing zeros to strip — capping significant digits is the
    # only thing that makes it readable.
    return "unknown" if value is None else fmt_decimal(value, max_significant=10)


def _fmt_pct(value: Decimal | None) -> str:
    """Display a percentage at 2dp — the precision an operator acts on.

    A Decimal division carries 28 significant digits; the tail is noise.
    Quantize BEFORE formatting because fmt_decimal deliberately imposes
    no scale of its own.
    """
    if value is None:
        return "unknown"
    return fmt_decimal(value.quantize(_PCT_DISPLAY, rounding=ROUND_HALF_UP))


# Effectively "all history" for one symbol's trade ledger — matches the
# established convention for a full-history read (web/routes/status.py's
# _TRADE_FETCH_LIMIT); a local SQLite read, not a rate-limited exchange call.
_BASIS_TRADE_FETCH_LIMIT = 10_000

# How often (in consecutive deferrals) to emit an INFO "still deferring"
# summary while a symbol's SELL side stays guarded — never a WARNING
# every tick (ADR-006's offside logging lesson, same shape here).
_DEFER_SUMMARY_EVERY_TICKS = 240


class SellGuard:
    """Per-symbol cost-basis SELL guard.

    Caches each symbol's replayed basis; callers must call
    :meth:`invalidate` after new trades are recorded for that symbol so a
    stale basis doesn't outlive the fills that changed it.
    """

    def __init__(
        self,
        storage: StoragePort,
        *,
        max_loss_percentage: Decimal,
        maker_fee_rate: Decimal,
    ) -> None:
        self._storage = storage
        self._max_loss_percentage = max_loss_percentage
        self._maker_fee_rate = maker_fee_rate
        self._basis_cache: dict[Symbol, CostBasis] = {}
        self._warned_unknown_basis: set[Symbol] = set()
        self._defer_ticks: dict[Symbol, int] = {}

    def invalidate(self, symbol: Symbol) -> None:
        """Drop ``symbol``'s cached basis — call after new trades save."""
        self._basis_cache.pop(symbol, None)

    async def _basis_for(self, symbol: Symbol) -> CostBasis:
        cached = self._basis_cache.get(symbol)
        if cached is not None:
            return cached
        trades = await self._storage.get_trades(symbol=symbol, limit=_BASIS_TRADE_FETCH_LIMIT)
        basis = replay_average_cost(trades)
        self._basis_cache[symbol] = basis
        return basis

    async def assess(self, symbol: Symbol, proposed_price: Decimal) -> SellAssessment:
        """Assess a proposed SELL at ``proposed_price`` for ``symbol``.

        Logs a once-per-symbol notice when the basis is unknown (allowed
        through unguarded, per ADR-032 decision 3) and transition +
        heartbeat logging while SELLs are actively deferred.
        """
        basis = await self._basis_for(symbol)
        assessment = assess_sell(
            proposed_price,
            basis,
            max_loss_percentage=self._max_loss_percentage,
            maker_fee_rate=self._maker_fee_rate,
        )

        if assessment.reason == "unknown_basis":
            if symbol not in self._warned_unknown_basis:
                self._warned_unknown_basis.add(symbol)
                _LOGGER.warning(
                    "sell guard: no tracked cost basis for symbol; allowing SELL unguarded "
                    "(symbol=%s)",
                    symbol,
                    extra={"symbol": str(symbol)},
                )
        else:
            self._warned_unknown_basis.discard(symbol)

        if not assessment.allowed:
            consecutive = self._defer_ticks.get(symbol, 0) + 1
            self._defer_ticks[symbol] = consecutive
            if consecutive == 1:
                _LOGGER.warning(
                    "sell guard: deferring %s SELL @ %s below avg cost %s (%s%% loss)",
                    symbol,
                    fmt_decimal(proposed_price),
                    _fmt_money(assessment.average_cost),
                    _fmt_pct(assessment.loss_percentage),
                    extra={
                        "symbol": str(symbol),
                        "proposed_price": str(proposed_price),
                        "average_cost": str(assessment.average_cost),
                        "loss_percentage": str(assessment.loss_percentage),
                    },
                )
            elif consecutive % _DEFER_SUMMARY_EVERY_TICKS == 0:
                _LOGGER.info(
                    "sell guard: still deferring %s SELLs below avg cost %s (%d consecutive)",
                    symbol,
                    _fmt_money(assessment.average_cost),
                    consecutive,
                    extra={
                        "symbol": str(symbol),
                        "consecutive_defers": consecutive,
                        "average_cost": str(assessment.average_cost),
                    },
                )
        elif self._defer_ticks.pop(symbol, 0):
            _LOGGER.info(
                "sell guard: %s SELL recovered above cost basis",
                symbol,
                extra={"symbol": str(symbol)},
            )

        return assessment


__all__ = ["SellGuard"]

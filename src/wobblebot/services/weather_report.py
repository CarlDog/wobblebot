"""Weather-report aggregation — the deterministic half of P4.5.

The ``weather_report`` operator query is ``status_report``'s
external-market sibling: what is the MARKET doing, per symbol, over a
multi-day window. This module computes every fact the narrative is
allowed to cite (the llm-app rule: precompute authoritative numbers in
code; the model narrates and cites, it never counts or does arithmetic
over raw arrays). The LLM call and the Discord surface live in
``services/operator_service.py`` / ``discord_embed_render.py``.

Null discipline: a symbol with no usable bar history reports ``None``
fields, never fabricated zeros — null means unknown (the
``PerformanceSummary`` contract).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.ports.advisor import AdvisorSuggestion
from wobblebot.ports.operator_results import DirectionalCall, SymbolTrend
from wobblebot.services.ta_metrics import compute_adx, compute_rsi

# The report surfaces at most this many Gremlin calls (newest first);
# at the default 4h emission cooldown a 7-day window can hold ~42 per
# symbol, which would drown the embed.
MAX_DIRECTIONAL_CALLS = 6


def _pct_change(reference: float, latest: float) -> float | None:
    if reference == 0:
        return None
    return (latest / reference - 1.0) * 100.0


def compute_symbol_trend(symbol: Symbol, bars: list[OHLCBar], *, now: datetime) -> SymbolTrend:
    """One symbol's precomputed market read from its window of hourly bars.

    ``bars`` is the (oldest-first) hourly history fetched for the
    report window. Change percentages compare last close against the
    window's first close and against the last close at-or-before 24h
    ago; both are ``None`` when the window doesn't reach back far
    enough. RSI/ADX come from ``services/ta_metrics`` and are ``None``
    on insufficient history, per that module's contract.
    """
    if not bars:
        return SymbolTrend(symbol=str(symbol))

    latest = float(bars[-1].close)
    change_window = _pct_change(float(bars[0].close), latest)

    day_ago = now - timedelta(hours=24)
    day_reference = [b for b in bars if b.opened_at <= day_ago]
    change_24h = _pct_change(float(day_reference[-1].close), latest) if day_reference else None

    window_low = min(float(b.low) for b in bars)
    window_high = max(float(b.high) for b in bars)
    span = window_high - window_low
    range_position = ((latest - window_low) / span) * 100.0 if span > 0 else None

    return SymbolTrend(
        symbol=str(symbol),
        latest_price=latest,
        change_24h_pct=change_24h,
        change_window_pct=change_window,
        range_position_pct=range_position,
        rsi_14=compute_rsi(bars, 14),
        adx_14=compute_adx(bars, 14),
    )


def extract_directional_calls(
    suggestions: list[AdvisorSuggestion], *, now: datetime
) -> list[DirectionalCall]:
    """The Gremlin's gradeable calls from a window of suggestions.

    Filters to ``role == "gremlin"`` rows carrying the P4.4b call shape
    (a valid ``direction`` string + positive numeric ``horizon_hours``);
    malformed emissions are simply omitted — the outcome ledger records
    them as unscoreable, the weather report just doesn't parrot them.
    Newest first, capped at :data:`MAX_DIRECTIONAL_CALLS`.
    """
    calls: list[DirectionalCall] = []
    ordered = sorted(suggestions, key=lambda s: s.created_at.dt, reverse=True)
    for suggestion in ordered:
        if suggestion.recommendation.role != "gremlin":
            continue
        recs = suggestion.recommendation.recommendations
        direction = recs.get("direction")
        horizon = recs.get("horizon_hours")
        if not isinstance(direction, str) or not direction:
            continue
        if isinstance(horizon, bool) or not isinstance(horizon, (int, float)) or horizon <= 0:
            continue
        symbol = suggestion.input_summary.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        age_hours = max(0.0, (now - suggestion.created_at.dt).total_seconds() / 3600.0)
        calls.append(
            DirectionalCall(
                symbol=symbol,
                direction=direction,
                horizon_hours=float(horizon),
                confidence=suggestion.recommendation.confidence,
                age_hours=age_hours,
            )
        )
        if len(calls) >= MAX_DIRECTIONAL_CALLS:
            break
    return calls

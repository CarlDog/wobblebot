"""Shared test helper builders for config-heavy unit + integration tests.

Eight test modules were hand-rolling ``_grid_config()`` / ``_safety_config()``
builders with subtly different signatures (default vs explicit ``enabled=True``,
different cap magnitudes, different emergency-stop knobs). They've been
consolidated into the two public builders below.

These are plain functions, not pytest fixtures — call them directly in test
bodies. The signatures are the union of every variant that existed; every
prior call site can be expressed by overriding kwargs against the defaults.

Permissive defaults make the "I just want a valid config object" case a
single zero-arg call:

    grid = grid_config()
    safety = safety_config()

Tighter caps for cap-trip tests override the relevant kwargs:

    safety = safety_config(max_total="100", max_orders=10, max_loss_pct="5")
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from wobblebot.config.grid import CoinGridConfig, GridConfig, GridLevels
from wobblebot.config.safety import SafetyConfig, SellGuardConfig
from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.ports.exceptions import ExchangeError

_BARS_DEFAULT_SYMBOL = Symbol(base="BTC", quote="USD")
_BARS_DEFAULT_START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)


def grid_config(
    *,
    spacing_pct: str = "1.0",
    above: int = 3,
    below: int = 3,
    order_size: str = "10",
    counter_target_mode: Literal["spacing_up", "top_sell"] = "spacing_up",
    coins: dict[str, CoinGridConfig] | None = None,
) -> GridConfig:
    """Build a ``GridConfig`` with default-only or with explicit per-coin overrides."""
    return GridConfig(
        default=GridLevels(
            spacing_percentage=Decimal(spacing_pct),
            levels_above=above,
            levels_below=below,
            order_size_usd=Decimal(order_size),
            counter_target_mode=counter_target_mode,
        ),
        coins=coins or {},
    )


def safety_config(
    *,
    max_total: str = "100000",
    max_daily: str = "100000",
    max_per_coin: str = "100000",
    max_orders: int = 100,
    sell_guard_enabled: bool = True,
    max_loss_pct: str = "1.0",
    max_coin_inventory: str = "100000",
    max_total_inventory: str = "100000",
) -> SafetyConfig:
    """Permissive default — individual tests tighten one cap to test it.
    The ADR-039 inventory caps are permissive here too (their schema
    defaults, 40/300, would trip unrelated tests placing $40+ books)."""
    return SafetyConfig(
        max_total_exposure_usd=Decimal(max_total),
        max_daily_spend_usd=Decimal(max_daily),
        max_per_coin_exposure_usd=Decimal(max_per_coin),
        max_orders_per_coin=max_orders,
        max_per_coin_inventory_usd=Decimal(max_coin_inventory),
        max_total_inventory_usd=Decimal(max_total_inventory),
        sell_guard=SellGuardConfig(
            enabled=sell_guard_enabled,
            max_loss_percentage=Decimal(max_loss_pct),
        ),
    )


def bars_from_closes(
    closes: list[float],
    *,
    symbol: Symbol = _BARS_DEFAULT_SYMBOL,
    start: datetime = _BARS_DEFAULT_START,
    interval_minutes: int = 60,
    spread: float = 1.0,
) -> list[OHLCBar]:
    """Bars from a close series; open = previous close.

    The subtle correctness rule this consolidates (it was hand-rolled in
    three test modules): ``high``/``low`` bracket both open and close by
    ``spread`` so every bar satisfies the P2 ``low <= open/close <= high``
    validator on ``OHLCBar``. A drifting copy of this bracketing produces
    ``ValidationError`` in an unrelated test.
    """
    bars = []
    prev_close = closes[0]
    for i, close in enumerate(closes):
        high = max(prev_close, close) + spread
        low = min(prev_close, close) - spread
        bars.append(
            OHLCBar(
                symbol=symbol,
                interval_minutes=interval_minutes,
                opened_at=start + timedelta(minutes=interval_minutes * i),
                open=Decimal(str(prev_close)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                vwap=Decimal("0"),
                volume=Decimal("1"),
                count=1,
            )
        )
        prev_close = close
    return bars


class StubOHLCAdapter:
    """Captures ``get_ohlc`` calls; returns one canned page then empty.

    Shared by the observe bar-topup and auto-gap-fill CLI tests (each
    previously hand-rolled a near-identical stub). ``raise_error``
    simulates Kraken unreachable.
    """

    def __init__(self, bars: list[OHLCBar] | None = None, *, raise_error: bool = False) -> None:
        self._bars = list(bars or [])
        self._raise = raise_error
        self.calls: list[tuple[Symbol, int, datetime | None]] = []

    async def get_ohlc(
        self, symbol: Symbol, interval_minutes: int = 1, since: datetime | None = None
    ) -> list[OHLCBar]:
        self.calls.append((symbol, interval_minutes, since))
        if self._raise:
            raise ExchangeError("kraken unreachable")
        page, self._bars = self._bars, []
        return page
